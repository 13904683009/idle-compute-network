"""Per-peer ingress rate limiting (Phase 2, F-1).

Bounded and deterministic:

- token bucket: ``RATE_PER_SECOND`` (20) sustained, ``BURST`` (40) peak;
- a *trip* is one contiguous episode of dropped frames: the first drop after
  admitted traffic starts a trip, and an admitted frame ends it — so a single
  burst costs one trip, not one trip per frame;
- ``2`` trips within ``TRIP_WINDOW_SECONDS`` (60) close the stream and
  quarantine the peer for ``COOLDOWN_SECONDS`` (30); new streams from that peer
  are refused until the cooldown expires;
- state is bounded: at most ``MAX_PEERS`` peers are tracked, the least
  recently active is evicted first.

The clock is injectable so the bucket math and the trip/cooldown rules are
tested deterministically instead of with sleeps.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

RATE_PER_SECOND = 20.0
BURST = 40
TRIP_WINDOW_SECONDS = 60.0
COOLDOWN_SECONDS = 30.0
MAX_PEERS = 1024


class RateDecision(NamedTuple):
    allow: bool
    close_stream: bool
    reason: str  # "ok" | "cooldown" | "rate"


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/second, capped at ``burst``."""

    def __init__(
        self,
        rate: float = RATE_PER_SECOND,
        burst: int = BURST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self._rate = float(rate)
        self._burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()

    @property
    def burst(self) -> float:
        return self._burst

    @property
    def rate(self) -> float:
        return self._rate

    def tokens(self) -> float:
        """Current tokens (refilled for the elapsed time)."""
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last = now
        return self._tokens

    def take(self, cost: float = 1.0) -> bool:
        if self.tokens() >= cost:
            self._tokens -= cost
            return True
        return False


@dataclass
class _PeerState:
    bucket: TokenBucket
    trips: list[float] = field(default_factory=list)
    in_trip: bool = False
    cooldown_until: float = 0.0
    last_seen: float = 0.0


class PeerRateLimiter:
    """Per-peer token buckets plus the trip/cooldown escalation rule."""

    def __init__(
        self,
        rate: float = RATE_PER_SECOND,
        burst: int = BURST,
        trip_window: float = TRIP_WINDOW_SECONDS,
        cooldown: float = COOLDOWN_SECONDS,
        max_peers: int = MAX_PEERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._trip_window = trip_window
        self._cooldown = cooldown
        self._max_peers = max_peers
        self._clock = clock
        self._peers: dict[str, _PeerState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ API

    def check(self, peer: str) -> RateDecision:
        """Account one inbound frame from ``peer`` and decide what to do."""
        now = self._clock()
        with self._lock:
            state = self._peers.get(peer)
            if state is None:
                state = _PeerState(bucket=TokenBucket(self._rate, self._burst, self._clock))
                self._peers[peer] = state
                self._evict_locked(now)
            state.last_seen = now

            if state.cooldown_until > now:
                return RateDecision(False, True, "cooldown")

            if state.bucket.take():
                state.in_trip = False
                return RateDecision(True, False, "ok")

            if state.in_trip:
                return RateDecision(False, False, "rate")
            # First drop of a new episode.
            state.in_trip = True
            state.trips = [t for t in state.trips if now - t <= self._trip_window]
            state.trips.append(now)
            if len(state.trips) >= 2:
                state.trips.clear()
                state.in_trip = False
                state.cooldown_until = now + self._cooldown
                return RateDecision(False, True, "rate")
            return RateDecision(False, False, "rate")

    def blocked(self, peer: str) -> bool:
        """True while ``peer`` is quarantined (new streams must be refused)."""
        now = self._clock()
        with self._lock:
            state = self._peers.get(peer)
            return bool(state is not None and state.cooldown_until > now)

    def tracked_peers(self) -> int:
        with self._lock:
            return len(self._peers)

    # -------------------------------------------------------------- private

    def _evict_locked(self, now: float) -> None:
        if len(self._peers) <= self._max_peers:
            return
        # Drop the least recently active peer; never evict one in cooldown
        # unless it is the oldest by far (an evicted quarantine only weakens a
        # mitigation, it cannot corrupt state).
        victim = min(self._peers, key=lambda key: self._peers[key].last_seen)
        self._peers.pop(victim, None)
