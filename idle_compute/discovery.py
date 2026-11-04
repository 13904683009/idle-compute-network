"""Capability discovery: signed announcement cache + persisted anti-rollback state.

D2 scope (see DISCOVERY-PROTOCOL.md):

- verify a ``CapabilityManifest`` / ``CapabilityAnnouncement`` pair (D1 rules:
  canonical JSON, Ed25519, domains, expiry, monotonic sequence);
- keep a BOUNDED cache (entry count, per-entry bytes) with a deterministic
  eviction rule;
- persist the highest accepted sequence / manifest version so a restart cannot
  replay an old announcement (``<data_dir>/discovery.db``);
- expose ``lookup`` for later matching (D3) and ``sweep`` for expiry/offline.

No P2P here: this module never imports libp2p / asyncio / socket and never
touches a transport. ``node.py`` owns the wire; this module owns the data.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .capability import (
    CapabilityAnnouncement,
    CapabilityError,
    CapabilityManifest,
    CapabilityRevocation,
    check_manifest_update,
)

DEFAULT_MAX_ENTRIES = 1024
DEFAULT_MAX_ENTRY_BYTES = 64 * 1024
DEFAULT_MAX_TTL_SECONDS = 120
DEFAULT_OFFLINE_AFTER_SECONDS = 90
MAX_QUERY_RESULTS = 32

# Ingest codes (stable strings for tests/logs; never raise for remote input).
ACCEPTED = "ACCEPTED"
DUPLICATE = "DUPLICATE"
SCHEMA = "SCHEMA"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
SIGNATURE = "SIGNATURE"
EXPIRED = "EXPIRED"
SEQ_ROLLBACK = "SEQ_ROLLBACK"
VERSION_ROLLBACK = "VERSION_ROLLBACK"
VERSION_CONFLICT = "VERSION_CONFLICT"
REVOKED = "REVOKED"
ENTRY_TOO_LARGE = "ENTRY_TOO_LARGE"
CACHE_FULL = "CACHE_FULL"
PERSIST_FAILED = "PERSIST_FAILED"


@dataclass
class IngestResult:
    code: str
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.code == ACCEPTED


@dataclass
class CachedPeer:
    node_id: str
    manifest: CapabilityManifest
    announcement: CapabilityAnnouncement
    received_at: float
    expires_at: float
    reachable: bool = True
    last_alive_at: float = 0.0


class DiscoveryStateStore:
    """Persisted per-node discovery state (own SQLite file, not the ledger).

    Opened with ``check_same_thread=False`` because the cache is used from the
    node's loop thread (sweep/publish) AND from stream handler threads
    (ingest); every statement is serialized by an internal lock.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS peer_announcement_state (
                    node_id TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    capability_version INTEGER NOT NULL DEFAULT 0,
                    manifest_hash TEXT NOT NULL DEFAULT '',
                    revoked_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def last_sequence(self, node_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_sequence FROM peer_announcement_state WHERE node_id=?", (node_id,)
            ).fetchone()
        return 0 if row is None else int(row["last_sequence"])

    def state(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peer_announcement_state WHERE node_id=?", (node_id,)
            ).fetchone()
        return {} if row is None else dict(row)

    def record(
        self,
        node_id: str,
        sequence: int,
        capability_version: int,
        manifest_hash: str,
        now: str,
    ) -> None:
        """Persist the accepted sequence/version; raises sqlite3.Error on failure.

        The caller must treat a failure as "announcement was NOT accepted":
        the cache is never updated before this write succeeds.
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO peer_announcement_state
                    (node_id, last_sequence, capability_version, manifest_hash, revoked_version, updated_at)
                VALUES (?,?,?,?,0,?)
                ON CONFLICT(node_id) DO UPDATE SET
                    last_sequence=excluded.last_sequence,
                    capability_version=excluded.capability_version,
                    manifest_hash=excluded.manifest_hash,
                    updated_at=excluded.updated_at
                """,
                (node_id, int(sequence), int(capability_version), manifest_hash, now),
            )
            self._conn.commit()

    def record_revoked(self, node_id: str, capability_version: int, now: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO peer_announcement_state
                    (node_id, last_sequence, capability_version, manifest_hash, revoked_version, updated_at)
                VALUES (?,0,?, '', ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    revoked_version=MAX(revoked_version, excluded.revoked_version),
                    updated_at=excluded.updated_at
                """,
                (node_id, int(capability_version), int(capability_version), now),
            )
            self._conn.commit()

    def is_revoked(self, node_id: str, capability_version: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT revoked_version FROM peer_announcement_state WHERE node_id=?", (node_id,)
            ).fetchone()
        return bool(row is not None and int(row["revoked_version"]) >= int(capability_version))


def _entry_bytes(manifest: CapabilityManifest, announcement: CapabilityAnnouncement) -> int:
    payload = {"manifest": manifest.to_dict(), "announcement": announcement.to_dict()}
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


class AnnouncementCache:
    """Bounded, verified cache of peer capability announcements."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS,
        offline_after_seconds: int = DEFAULT_OFFLINE_AFTER_SECONDS,
        state_store: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = int(max_entries)
        self._max_entry_bytes = int(max_entry_bytes)
        self._max_ttl = int(max_ttl_seconds)
        self._offline_after = int(offline_after_seconds)
        self._store = state_store
        self._clock = clock
        self._entries: dict[str, CachedPeer] = {}
        self._last_sequences: dict[str, int] = {}

    # ------------------------------------------------------------- ingest

    def ingest(
        self,
        manifest: CapabilityManifest | dict[str, Any],
        announcement: CapabilityAnnouncement | dict[str, Any],
        *,
        now: float | None = None,
        sender_node_id: str | None = None,
    ) -> IngestResult:
        """Verify a manifest+announcement pair and cache it.

        Order: schema -> identity binding -> signatures/expiry -> sequence ->
        version -> size -> capacity -> PERSIST -> memory. The perspective is
        the receiver's clock; a remote ``expires_at`` is clamped to
        ``now + max_announcement_ttl`` so a lying clock cannot pin an entry.
        """
        now = self._clock() if now is None else float(now)
        try:
            manifest = (
                manifest
                if isinstance(manifest, CapabilityManifest)
                else CapabilityManifest.from_dict(manifest)
            )
            announcement = (
                announcement
                if isinstance(announcement, CapabilityAnnouncement)
                else CapabilityAnnouncement.from_dict(announcement)
            )
        except CapabilityError as exc:
            return IngestResult(SCHEMA, exc.message)

        if sender_node_id is not None and manifest.node_id != sender_node_id:
            return IngestResult(
                IDENTITY_MISMATCH,
                "manifest node_id does not match the signed envelope sender",
            )
        if announcement.node_id != manifest.node_id:
            return IngestResult(IDENTITY_MISMATCH, "announcement/manifest node_id differ")

        try:
            manifest.verify(int(now))
        except CapabilityError as exc:
            return IngestResult(exc.code, exc.message)

        cached = self._entries.get(manifest.node_id)
        if cached is not None:
            decision = check_manifest_update(
                {
                    "capability_version": cached.manifest.capability_version,
                    "manifest_hash": cached.manifest.hash(),
                    "revoked": False,
                },
                manifest,
                int(now),
            )
            if decision == "ROLLBACK":
                return IngestResult(VERSION_ROLLBACK, "manifest version went backwards")
            if decision == "CONFLICT":
                return IngestResult(VERSION_CONFLICT, "same version, different manifest")
            if decision == "REVOKED":
                return IngestResult(REVOKED, "manifest version is revoked")

        last_sequence = max(
            self._last_sequences.get(manifest.node_id, 0),
            int(self._store.last_sequence(manifest.node_id)) if self._store else 0,
        )
        try:
            # Signature/time/binding first; the sequence rule is applied below
            # so an exact re-delivery can be reported as DUPLICATE rather than
            # as a rollback, while still being rejected by the cache.
            announcement.verify(manifest, int(now))
        except CapabilityError as exc:
            return IngestResult(exc.code, exc.message)

        if (
            cached is not None
            and announcement.sequence == cached.announcement.sequence
            and manifest.hash() == cached.manifest.hash()
        ):
            self.mark_alive(manifest.node_id, now)
            return IngestResult(DUPLICATE, "identical announcement already cached")

        if announcement.sequence <= last_sequence:
            return IngestResult(
                SEQ_ROLLBACK,
                f"announcement sequence {announcement.sequence} <= last accepted {last_sequence}",
            )

        if self._store is not None and self._store.is_revoked(
            manifest.node_id, manifest.capability_version
        ):
            return IngestResult(REVOKED, "manifest version is revoked")

        size = _entry_bytes(manifest, announcement)
        if size > self._max_entry_bytes:
            return IngestResult(ENTRY_TOO_LARGE, f"entry is {size} bytes")

        victim: CachedPeer | None = None
        if cached is None and len(self._entries) >= self._max_entries:
            victim = self._pick_victim(now)
            if victim is None:
                return IngestResult(CACHE_FULL, "cache is full and nothing is evictable")

        if self._store is not None:
            try:
                self._store.record(
                    manifest.node_id,
                    announcement.sequence,
                    manifest.capability_version,
                    manifest.hash(),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                )
            except sqlite3.Error as exc:  # persistence failed -> do not pretend
                return IngestResult(PERSIST_FAILED, f"could not persist discovery state: {exc}")

        if victim is not None:
            self._entries.pop(victim.node_id, None)
        effective_expiry = min(float(announcement.expires_at), now + self._max_ttl)
        self._entries[manifest.node_id] = CachedPeer(
            node_id=manifest.node_id,
            manifest=manifest,
            announcement=announcement,
            received_at=now,
            expires_at=effective_expiry,
            reachable=True,
            last_alive_at=now,
        )
        self._last_sequences[manifest.node_id] = max(last_sequence, announcement.sequence)
        return IngestResult(ACCEPTED, "")

    # -------------------------------------------------------------- query

    def lookup(
        self,
        *,
        task_type: str | None = None,
        runtime: str | None = None,
        include_unreachable: bool = False,
        limit: int = MAX_QUERY_RESULTS,
        now: float | None = None,
    ) -> list[CachedPeer]:
        """Return cached, unexpired, non-revoked peers (deterministic order)."""
        now = self._clock() if now is None else float(now)
        limit = max(0, min(int(limit), MAX_QUERY_RESULTS))
        results: list[CachedPeer] = []
        for node_id in sorted(self._entries):
            entry = self._entries[node_id]
            if entry.expires_at <= now:
                continue
            if entry.manifest.expires_at <= now:
                continue
            if self._store is not None and self._store.is_revoked(
                entry.manifest.node_id, entry.manifest.capability_version
            ):
                continue
            if not include_unreachable and not entry.reachable:
                continue
            if task_type is not None and task_type not in entry.manifest.supported_task_types:
                continue
            if runtime is not None and runtime not in entry.manifest.supported_runtimes:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    # --------------------------------------------------------- liveness

    def mark_alive(self, node_id: str, now: float | None = None) -> None:
        now = self._clock() if now is None else float(now)
        entry = self._entries.get(node_id)
        if entry is not None:
            entry.reachable = True
            entry.last_alive_at = now

    def sweep(self, now: float | None = None) -> dict[str, int]:
        """Drop expired entries; mark quiet peers unreachable (two states)."""
        now = self._clock() if now is None else float(now)
        removed = 0
        unreachable = 0
        for node_id in list(self._entries):
            entry = self._entries[node_id]
            if entry.expires_at <= now:
                del self._entries[node_id]
                removed += 1
                continue
            if entry.reachable and (now - entry.last_alive_at) > self._offline_after:
                entry.reachable = False
                unreachable += 1
        return {"removed": removed, "unreachable": unreachable}

    # ------------------------------------------------------ revocation

    def revoke(self, revocation: CapabilityRevocation | dict[str, Any], now: float | None = None) -> IngestResult:
        now = self._clock() if now is None else float(now)
        try:
            revocation = (
                revocation
                if isinstance(revocation, CapabilityRevocation)
                else CapabilityRevocation.from_dict(revocation)
            )
            revocation.verify(int(now))
        except CapabilityError as exc:
            return IngestResult(exc.code, exc.message)
        if self._store is not None:
            try:
                self._store.record_revoked(
                    revocation.node_id,
                    revocation.capability_version,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                )
            except sqlite3.Error as exc:
                return IngestResult(PERSIST_FAILED, f"could not persist revocation: {exc}")
        entry = self._entries.get(revocation.node_id)
        if entry is not None and entry.manifest.capability_version == revocation.capability_version:
            del self._entries[revocation.node_id]
        return IngestResult(ACCEPTED, "")

    # ----------------------------------------------------------- private

    def _pick_victim(self, now: float) -> CachedPeer | None:
        """Deterministic eviction candidate: expired first, then oldest."""
        if not self._entries:
            return None
        expired = [e for e in self._entries.values() if e.expires_at <= now]
        if expired:
            return min(expired, key=lambda e: (e.expires_at, e.node_id))
        return min(self._entries.values(), key=lambda e: (e.received_at, e.node_id))

    # ------------------------------------------------------------- stats

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "reachable": sum(1 for e in self._entries.values() if e.reachable),
        }


# ---------------------------------------------------------------------------
# Builders: config template -> signed manifest/announcement (used by node.py)


def build_manifest(template: dict[str, Any], identity: Any, *, now: int) -> CapabilityManifest:
    """Build+sign this node's manifest from the ``capability`` config section."""
    required = ("supported_task_types",)
    for key in required:
        if key not in template:
            raise CapabilityError(CapabilityError.SCHEMA, f"capability.{key} is required")
    manifest = CapabilityManifest(
        node_id=identity.node_id,
        capability_version=int(template.get("capability_version", 1)),
        issued_at=int(now),
        expires_at=int(now) + int(template.get("ttl_seconds", 120)),
        supported_task_types=tuple(template["supported_task_types"]),
        supported_runtimes=tuple(template.get("supported_runtimes", ())),
        hardware=dict(template.get("hardware", {"cpu_arch": "other"})),
        resource_limits=dict(
            template.get(
                "resource_limits",
                {
                    "max_cpu_cores": 1,
                    "max_ram_mb": 512,
                    "max_gpu_memory_mb": 0,
                    "max_task_duration_seconds": 600,
                },
            )
        ),
        network=dict(
            template.get("network", {"supported_transports": ["tcp"], "supports_relay": False})
        ),
        verification=dict(
            template.get("verification", {"level": "SELF_DECLARED", "benchmark_profile": None})
        ),
    )
    return manifest.sign(identity)


def build_announcement(
    manifest: CapabilityManifest,
    template: dict[str, Any],
    identity: Any,
    *,
    now: int,
    sequence: int,
    ttl_seconds: int = 60,
) -> CapabilityAnnouncement:
    """Build+sign this node's announcement for the current manifest."""
    resources = template.get("available_resources", {"cpu_cores": 0, "ram_mb": 0, "gpu_memory_mb": 0})
    announcement = CapabilityAnnouncement(
        node_id=identity.node_id,
        capability_version=manifest.capability_version,
        sequence=int(sequence),
        observed_at=int(now),
        expires_at=int(now) + int(ttl_seconds),
        available_resources=dict(resources),
        queue={"running_tasks": 0, "queued_tasks": 0},
        network_status={
            "connectivity": "DIRECT",
            "supported_transports": list(manifest.network.get("supported_transports", ["tcp"])),
        },
        accepting_tasks=bool(template.get("accepting_tasks", False)),
    )
    return announcement.sign(identity)
