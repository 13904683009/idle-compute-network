"""F-1: per-peer token bucket + trip/cooldown escalation.

Two layers:
1. deterministic unit tests with an injected clock (bucket math, trip window,
   episode semantics, cooldown);
2. wire tests against a REAL coordinator over real TCP sockets: a flooding
   peer gets throttled and then quarantined while an allowlisted peer keeps
   working; a frame replayed after a coordinator restart is rejected by the
   PERSISTED seq window.

The remaining F-1 acceptance items already have dedicated coverage elsewhere
and are referenced rather than duplicated:
- seq window edges / ts tolerance / verify-before-parse order:
  tests/test_protocol.py (46 tests);
- sender != transport peer -> BAD_SENDER: tests/test_node.py
  (test_hello_identity_mismatch_rejected, test_unauthorized_hello_rejected);
- old RESULT_ACK replay does not double-credit: tests/test_node.py
  (test_duplicate_submission_no_double_credit) + ledger idempotency tests.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from idle_compute.config import from_dict
from idle_compute.identity import NodeIdentity
from idle_compute.node import Node
from idle_compute.protocol import (
    ErrorCode,
    MessageType,
    ProtocolError,
    SeqWindow,
    build_envelope,
    encode_envelope,
    parse_message,
    read_frame,
)
from idle_compute.ratelimit import (
    BURST,
    COOLDOWN_SECONDS,
    RATE_PER_SECOND,
    PeerRateLimiter,
    TokenBucket,
)
from idle_compute.transport_tcp import TcpTransport


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TokenBucketTests(unittest.TestCase):
    def test_burst_then_refill_then_cap(self) -> None:
        clock = FakeClock()
        bucket = TokenBucket(rate=20.0, burst=40, clock=clock)

        self.assertEqual(bucket.tokens(), 40.0)
        for i in range(40):
            self.assertTrue(bucket.take(), f"take {i + 1}/40 must succeed")
        self.assertFalse(bucket.take(), "the 41st immediate take must fail")

        clock.advance(0.5)  # 20/s * 0.5s = 10 tokens
        self.assertAlmostEqual(bucket.tokens(), 10.0, places=6)
        for i in range(10):
            self.assertTrue(bucket.take(), f"refilled take {i + 1}/10 must succeed")
        self.assertFalse(bucket.take())

        clock.advance(100.0)  # refill is capped at burst
        self.assertEqual(bucket.tokens(), 40.0)

    def test_defaults_match_the_documented_limits(self) -> None:
        self.assertEqual((RATE_PER_SECOND, BURST, COOLDOWN_SECONDS), (20.0, 40, 30.0))
        self.assertEqual(TokenBucket().burst, 40.0)


class PeerRateLimiterTests(unittest.TestCase):
    def test_one_episode_is_one_trip_and_two_trips_quarantine(self) -> None:
        clock = FakeClock()
        limiter = PeerRateLimiter(clock=clock)

        allowed = sum(1 for _ in range(60) if limiter.check("peer-a").allow)
        self.assertEqual(allowed, 40, "the burst is exactly 40 frames")
        # All remaining frames belong to the SAME episode: one trip, no close.
        decisions = [limiter.check("peer-a") for _ in range(5)]
        self.assertTrue(all(not d.allow and not d.close_stream for d in decisions))
        self.assertFalse(limiter.blocked("peer-a"))

        clock.advance(0.2)  # 4 tokens refill -> the episode ends on the first admit
        self.assertTrue(limiter.check("peer-a").allow)
        trip = [limiter.check("peer-a") for _ in range(10)]
        self.assertTrue(any(d.close_stream for d in trip), "second trip must close the stream")
        self.assertTrue(limiter.blocked("peer-a"), "two trips within the window quarantine the peer")

    def test_cooldown_expires(self) -> None:
        clock = FakeClock()
        limiter = PeerRateLimiter(clock=clock)
        for _ in range(40):
            limiter.check("peer-a")
        limiter.check("peer-a")  # trip 1
        clock.advance(0.2)  # 4 tokens refill
        self.assertEqual(
            [limiter.check("peer-a").allow for _ in range(4)], [True] * 4, "refilled tokens are served"
        )
        decision = limiter.check("peer-a")  # trip 2 -> quarantine
        self.assertTrue(decision.close_stream)
        self.assertTrue(limiter.blocked("peer-a"))

        clock.advance(COOLDOWN_SECONDS + 1)
        self.assertFalse(limiter.blocked("peer-a"))
        self.assertTrue(limiter.check("peer-a").allow, "a recovered peer is served again")

    def test_trips_outside_the_window_do_not_accumulate(self) -> None:
        clock = FakeClock()
        limiter = PeerRateLimiter(trip_window=60.0, clock=clock)
        for _ in range(41):
            limiter.check("peer-a")  # trip 1
        clock.advance(61.0)  # first trip falls out of the window
        for _ in range(41):
            limiter.check("peer-a")  # would be trip 2, but trip 1 expired
        self.assertFalse(limiter.blocked("peer-a"))

    def test_peers_are_isolated(self) -> None:
        clock = FakeClock()
        limiter = PeerRateLimiter(clock=clock)
        for _ in range(60):
            limiter.check("peer-a")
        self.assertTrue(limiter.check("peer-b").allow, "one peer's flood must not throttle another")


class RawPeer:
    """Bare TCP peer: craft/verify frames by hand, record replies and closure."""

    def __init__(self, identity: NodeIdentity, coord_id: str) -> None:
        self.identity = identity
        self.coord_id = coord_id
        self.frames: list = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = threading.Event()
        self._seq = 0
        self._window = SeqWindow()
        self.transport = TcpTransport(identity.node_id, "127.0.0.1", 0)
        self.transport.set_handler(self._record)
        self.transport.start()

    def _record(self, peer, read, write) -> None:
        try:
            while True:
                try:
                    frame = read_frame(read)
                except ProtocolError:
                    return
                if frame is None:
                    return
                try:
                    env = parse_message(
                        frame,
                        transport_peer_id=None,
                        verifier=NodeIdentity.verify_signature,
                        seq_window=self._window,
                    )
                except ProtocolError as exc:
                    with self._lock:
                        self.errors.append(exc.code.value)
                        self._cond.notify_all()
                    continue
                with self._lock:
                    self.frames.append(env)
                    self._cond.notify_all()
        finally:
            self._closed.set()

    def dial(self, addr: str) -> None:
        self.transport.dial(self.coord_id, [addr])

    def craft(self, type_: MessageType, body: dict, ts: str | None = None) -> bytes:
        self._seq += 1
        return encode_envelope(
            build_envelope(
                type_,
                self.identity.node_id,
                self.identity.sign,
                recipient=self.coord_id,
                seq=self._seq,
                body=body,
                ts=ts,
            )
        )

    def send(self, frame: bytes) -> None:
        self.transport.send(self.coord_id, frame)

    def send_craft(self, type_: MessageType, body: dict) -> bytes:
        frame = self.craft(type_, body)
        self.send(frame)
        return frame

    def hello(self) -> bytes:
        return self.send_craft(
            MessageType.HELLO,
            {"node_id": self.identity.node_id, "role": "worker", "listen_addrs": ["127.0.0.1:1"]},
        )

    def wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                for index, env in enumerate(self.frames):
                    if predicate(env):
                        return self.frames.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def wait_closed(self, timeout: float) -> bool:
        return self._closed.wait(timeout)

    def stop(self) -> None:
        self.transport.stop()


class WireRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="ratelimit-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord_dir = self.root / "coord"
        self.coord_dir.mkdir(parents=True, exist_ok=True)
        self.coord_identity = NodeIdentity.load_or_create(
            self.coord_dir / "identity.key", node_name="coordinator"
        )
        self.flood_id = NodeIdentity.load_or_create(
            self.root / "flood" / "identity.key", node_name="flood"
        )
        self.good_id = NodeIdentity.load_or_create(
            self.root / "good" / "identity.key", node_name="good"
        )
        self.start_coordinator()

    def start_coordinator(self) -> None:
        config = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.coord_dir),
                "peers": [self.flood_id.node_id, self.good_id.node_id],
                "poll_interval_seconds": 1,
                "log_level": "WARNING",
                "log_file": str(self.coord_dir / "coordinator.log"),
            }
        )
        self.coordinator = Node(config)
        self.addCleanup(self.coordinator.shutdown)
        self._thread = threading.Thread(
            target=self.coordinator.run, name="ratelimit-coordinator", daemon=True
        )
        self._thread.start()
        self.addCleanup(self._thread.join, 10.0)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.coordinator.transport.advertised_addrs():
            time.sleep(0.05)
        self.addr = self.coordinator.transport.advertised_addrs()[0]

    def _hello(self, peer: RawPeer) -> None:
        peer.dial(self.addr)
        peer.hello()
        ack = peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 15.0)
        self.assertIsNotNone(ack, "handshake must complete before the flood")

    def test_flood_is_throttled_and_quarantined_while_other_peer_works(self) -> None:
        flood = RawPeer(self.flood_id, self.coord_identity.node_id)
        self.addCleanup(flood.stop)
        self._hello(flood)

        # 60 frames: 40 admitted (burst), the rest one episode -> no close yet.
        for _ in range(60):
            try:
                flood.send_craft(MessageType.PING, {"nonce": "n"})
            except Exception:  # socket backpressure after a close
                break
        time.sleep(0.25)  # let a few tokens refill -> the episode ends
        for _ in range(120):
            try:
                flood.send_craft(MessageType.PING, {"nonce": "n"})
            except Exception:
                break
        time.sleep(0.5)

        # Second trip within the window -> quarantined: new streams are refused.
        self.assertTrue(
            flood.wait_closed(10.0) or self.coordinator._ratelimit.blocked(self.flood_id.node_id),
            "the flooding peer must be closed or quarantined",
        )
        late = RawPeer(self.flood_id, self.coord_identity.node_id)
        self.addCleanup(late.stop)
        late.dial(self.addr)
        late.hello()
        self.assertIsNone(
            late.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 3.0),
            "a quarantined peer must not complete a new handshake",
        )

        # Isolation: an allowlisted, well-behaved peer still works.
        good = RawPeer(self.good_id, self.coord_identity.node_id)
        self.addCleanup(good.stop)
        self._hello(good)
        for _ in range(20):
            good.send_craft(MessageType.PING, {"nonce": "g"})
        pongs = 0
        deadline = time.monotonic() + 10
        while pongs < 20 and time.monotonic() < deadline:
            if good.wait_for(lambda e: e.type is MessageType.PONG, 2.0) is not None:
                pongs += 1
        self.assertEqual(pongs, 20, "the other peer must not be throttled by the flood")

    def test_replayed_frame_is_rejected_after_coordinator_restart(self) -> None:
        peer = RawPeer(self.flood_id, self.coord_identity.node_id)
        self.addCleanup(peer.stop)
        peer.dial(self.addr)
        hello_frame = peer.hello()  # seq=1, accepted before the restart
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 15.0))
        peer.send_craft(MessageType.PING, {"nonce": "first"})  # seq=2
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.PONG, 10.0))

        # Restart the coordinator on the same data dir (node_state persists
        # last_seq_seen=2). Replaying seq=1 must now be rejected; with an empty
        # window it would have been accepted.
        self.coordinator.shutdown()
        self._thread.join(10.0)
        self.start_coordinator()

        replay = RawPeer(self.flood_id, self.coord_identity.node_id)
        self.addCleanup(replay.stop)
        replay.dial(self.addr)
        replay.send(hello_frame)
        error = replay.wait_for(
            lambda e: e.type is MessageType.ERROR and e.body["code"] == ErrorCode.SEQ_OUT_OF_WINDOW.value,
            10.0,
        )
        self.assertIsNotNone(
            error,
            f"a frame replayed after restart must be rejected by the persisted window; errors={replay.errors}",
        )
        self.assertIsNone(
            replay.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 1.0),
            "the replayed HELLO must not bind the stream",
        )
