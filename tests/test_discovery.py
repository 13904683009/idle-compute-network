"""D2-2: capability discovery cache (unit level).

Covers the acceptance list items 1-12: signature/identity rejection, duplicate
and rollback handling, restart anti-rollback, revocation, expiry, bounded
cache, offline vs valid, and the receive-time TTL clamp.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from idle_compute.capability import CapabilityAnnouncement, CapabilityManifest, CapabilityRevocation
from idle_compute.discovery import (
    ACCEPTED,
    CACHE_FULL,
    DUPLICATE,
    ENTRY_TOO_LARGE,
    IDENTITY_MISMATCH,
    PERSIST_FAILED,
    REVOKED,
    SCHEMA,
    SEQ_ROLLBACK,
    SIGNATURE,
    VERSION_CONFLICT,
    VERSION_ROLLBACK,
    AnnouncementCache,
    DiscoveryStateStore,
    build_announcement,
    build_manifest,
)
from idle_compute.identity import NodeIdentity

TEMPLATE = {
    "capability_version": 1,
    "ttl_seconds": 120,
    "supported_task_types": ["numeric_batch_v1"],
    "supported_runtimes": ["python_sandbox_v1"],
    "hardware": {"cpu_arch": "x86_64", "cpu_cores": 8, "ram_mb": 16384},
    "resource_limits": {
        "max_cpu_cores": 4,
        "max_ram_mb": 8192,
        "max_gpu_memory_mb": 0,
        "max_task_duration_seconds": 600,
    },
    "network": {"supported_transports": ["tcp"], "supports_relay": False},
    "verification": {"level": "SELF_DECLARED", "benchmark_profile": None},
    "available_resources": {"cpu_cores": 4, "ram_mb": 8192, "gpu_memory_mb": 0},
    "accepting_tasks": True,
}


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FailingStore:
    """State store double whose record() always fails."""

    def last_sequence(self, node_id: str) -> int:
        return 0

    def is_revoked(self, node_id: str, version: int) -> bool:
        return False

    def record(self, *args, **kwargs) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def record_revoked(self, *args, **kwargs) -> None:
        raise sqlite3.OperationalError("disk I/O error")


class DiscoveryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="discovery-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.peer_a = NodeIdentity.load_or_create(self.root / "a" / "identity.key", node_name="a")
        self.peer_b = NodeIdentity.load_or_create(self.root / "b" / "identity.key", node_name="b")
        self.clock = FakeClock()
        self.store = DiscoveryStateStore(self.root / "discovery.db")
        self.addCleanup(self.store.close)
        self.cache = AnnouncementCache(state_store=self.store, clock=self.clock)

    def signed(self, identity=None, *, version=1, now=None, seq=1, ttl=60):
        identity = identity or self.peer_a
        now = int(self.clock.now if now is None else now)
        template = dict(TEMPLATE, capability_version=version)
        manifest = build_manifest(template, identity, now=now)
        announcement = build_announcement(manifest, template, identity, now=now, sequence=seq, ttl_seconds=ttl)
        return manifest, announcement

    def ingest(self, manifest, announcement, **kwargs):
        return self.cache.ingest(manifest, announcement, now=self.clock.now, **kwargs)


class IngestTests(DiscoveryTestBase):
    def test_valid_announcement_is_cached(self) -> None:
        manifest, announcement = self.signed()
        result = self.ingest(manifest, announcement, sender_node_id=self.peer_a.node_id)
        self.assertEqual(result.code, ACCEPTED, result.detail)
        peers = self.cache.lookup(now=self.clock.now)
        self.assertEqual([peer.node_id for peer in peers], [self.peer_a.node_id])
        self.assertEqual(peers[0].manifest.supported_task_types, ("numeric_batch_v1",))

    def test_bad_signature_is_rejected(self) -> None:
        manifest, announcement = self.signed()
        tampered = CapabilityAnnouncement.from_dict(
            {**announcement.to_dict(), "accepting_tasks": False}
        )
        result = self.ingest(manifest, tampered)
        self.assertEqual(result.code, SIGNATURE)
        self.assertEqual(self.cache.lookup(now=self.clock.now), [])

    def test_manifest_signer_must_match_sender(self) -> None:
        manifest, announcement = self.signed(self.peer_a)
        result = self.ingest(manifest, announcement, sender_node_id=self.peer_b.node_id)
        self.assertEqual(result.code, IDENTITY_MISMATCH)

    def test_announcement_must_match_manifest_identity(self) -> None:
        manifest, _ = self.signed(self.peer_a)
        other_manifest, other_announcement = self.signed(self.peer_b)
        result = self.ingest(manifest, other_announcement)
        self.assertEqual(result.code, IDENTITY_MISMATCH)
        self.assertEqual(self.cache.lookup(now=self.clock.now), [])

    def test_duplicate_is_not_recorded_twice(self) -> None:
        manifest, announcement = self.signed()
        self.assertEqual(self.ingest(manifest, announcement).code, ACCEPTED)
        result = self.ingest(manifest, announcement)
        self.assertEqual(result.code, DUPLICATE)
        self.assertEqual(self.cache.stats()["entries"], 1)

    def test_old_sequence_is_rejected(self) -> None:
        manifest, announcement = self.signed(seq=5)
        self.assertEqual(self.ingest(manifest, announcement).code, ACCEPTED)
        old = build_announcement(manifest, dict(TEMPLATE), self.peer_a, now=int(self.clock.now), sequence=4)
        self.assertEqual(self.ingest(manifest, old).code, SEQ_ROLLBACK)

    def test_restart_rejects_old_sequence(self) -> None:
        manifest, announcement = self.signed(seq=7)
        self.assertEqual(self.ingest(manifest, announcement).code, ACCEPTED)

        # New process: fresh cache, same persisted state file.
        self.store.close()
        reopened = DiscoveryStateStore(self.root / "discovery.db")
        self.addCleanup(reopened.close)
        restarted = AnnouncementCache(state_store=reopened, clock=self.clock)
        replay = build_announcement(manifest, dict(TEMPLATE), self.peer_a, now=int(self.clock.now), sequence=7)
        result = restarted.ingest(manifest, replay, now=self.clock.now)
        self.assertEqual(result.code, SEQ_ROLLBACK)
        self.assertEqual(restarted.lookup(now=self.clock.now), [])

    def test_manifest_version_rollback_and_conflict(self) -> None:
        manifest_v2, announcement_v2 = self.signed(version=2, seq=1)
        self.assertEqual(self.ingest(manifest_v2, announcement_v2).code, ACCEPTED)

        manifest_v1, announcement_v1 = self.signed(version=1, seq=2)
        self.assertEqual(self.ingest(manifest_v1, announcement_v1).code, VERSION_ROLLBACK)

        # Same version, different MANIFEST content, higher sequence.
        template = dict(TEMPLATE, capability_version=2, hardware={"cpu_arch": "x86_64", "cpu_cores": 16})
        changed = build_manifest(template, self.peer_a, now=int(self.clock.now))
        changed_announcement = build_announcement(changed, template, self.peer_a, now=int(self.clock.now), sequence=3)
        self.assertEqual(self.ingest(changed, changed_announcement).code, VERSION_CONFLICT)

    def test_expired_announcement_is_rejected(self) -> None:
        manifest, announcement = self.signed(ttl=10)
        self.clock.now += 11
        result = self.ingest(manifest, announcement)
        self.assertEqual(result.code, "EXPIRED")

    def test_remote_expiry_is_clamped_by_local_ttl(self) -> None:
        manifest, _ = self.signed()
        long_lived = build_announcement(
            manifest, dict(TEMPLATE), self.peer_a, now=int(self.clock.now), sequence=1, ttl_seconds=86_400
        )
        result = self.ingest(manifest, long_lived)
        self.assertEqual(result.code, ACCEPTED, result.detail)
        entry = self.cache.lookup(now=self.clock.now)[0]
        self.assertEqual(entry.expires_at, self.clock.now + 120, "clamped to max_announcement_ttl")

    def test_oversized_entry_is_rejected(self) -> None:
        cache = AnnouncementCache(state_store=self.store, clock=self.clock, max_entry_bytes=200)
        manifest, announcement = self.signed()
        self.assertEqual(cache.ingest(manifest, announcement, now=self.clock.now).code, ENTRY_TOO_LARGE)

    def test_cache_is_bounded_and_evicts_deterministically(self) -> None:
        cache = AnnouncementCache(state_store=self.store, clock=self.clock, max_entries=1)
        manifest_a, announcement_a = self.signed(self.peer_a)
        manifest_b, announcement_b = self.signed(self.peer_b)
        self.assertEqual(cache.ingest(manifest_a, announcement_a, now=self.clock.now).code, ACCEPTED)
        self.clock.now += 1
        self.assertEqual(cache.ingest(manifest_b, announcement_b, now=self.clock.now).code, ACCEPTED)
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertEqual([peer.node_id for peer in cache.lookup(now=self.clock.now)], [self.peer_b.node_id])

    def test_cache_full_when_nothing_is_evictable(self) -> None:
        cache = AnnouncementCache(state_store=self.store, clock=self.clock, max_entries=1)
        cache._pick_victim = lambda now: None  # type: ignore[assignment]
        manifest_a, announcement_a = self.signed(self.peer_a)
        manifest_b, announcement_b = self.signed(self.peer_b)
        self.assertEqual(cache.ingest(manifest_a, announcement_a, now=self.clock.now).code, ACCEPTED)
        result = cache.ingest(manifest_b, announcement_b, now=self.clock.now)
        self.assertEqual(result.code, CACHE_FULL)
        self.assertEqual(cache.stats()["entries"], 1)

    def test_persistence_failure_does_not_update_the_cache(self) -> None:
        cache = AnnouncementCache(state_store=FailingStore(), clock=self.clock)
        manifest, announcement = self.signed()
        result = cache.ingest(manifest, announcement, now=self.clock.now)
        self.assertEqual(result.code, PERSIST_FAILED)
        self.assertEqual(cache.lookup(now=self.clock.now), [])

    def test_schema_error_is_reported_not_raised(self) -> None:
        result = self.cache.ingest({"node_id": "x"}, {}, now=self.clock.now)
        self.assertEqual(result.code, SCHEMA)


class LookupAndLifecycleTests(DiscoveryTestBase):
    def test_task_type_filter(self) -> None:
        manifest, announcement = self.signed()
        self.assertEqual(self.ingest(manifest, announcement).code, ACCEPTED)
        self.assertEqual(len(self.cache.lookup(task_type="numeric_batch_v1", now=self.clock.now)), 1)
        self.assertEqual(len(self.cache.lookup(task_type="onnx_inference_v1", now=self.clock.now)), 0)

    def test_expired_entries_do_not_participate(self) -> None:
        manifest, announcement = self.signed(ttl=10)
        self.assertEqual(self.ingest(manifest, announcement).code, ACCEPTED)
        self.clock.now += 11
        self.assertEqual(self.cache.lookup(now=self.clock.now), [])
        self.assertEqual(self.cache.sweep(now=self.clock.now)["removed"], 1)

    def test_offline_is_not_online(self) -> None:
        # ttl deliberately longer than offline_after: the entry is still VALID
        # but the peer is no longer reachable — two different states.
        manifest, announcement = self.signed(ttl=120)
        self.assertEqual(self.ingest(manifest, announcement).code, ACCEPTED)
        self.clock.now += 91  # offline_after_seconds default 90
        swept = self.cache.sweep(now=self.clock.now)
        self.assertEqual(swept["unreachable"], 1)
        self.assertEqual(self.cache.lookup(now=self.clock.now), [], "unreachable is not a candidate")
        peers = self.cache.lookup(include_unreachable=True, now=self.clock.now)
        self.assertEqual(len(peers), 1)
        self.assertFalse(peers[0].reachable)

        # A fresh message makes it reachable again without a new announcement.
        self.cache.mark_alive(self.peer_a.node_id, now=self.clock.now)
        self.assertEqual(len(self.cache.lookup(now=self.clock.now)), 1)

    def test_revocation_removes_and_persists(self) -> None:
        manifest, announcement = self.signed()
        self.assertEqual(self.ingest(manifest, announcement).code, ACCEPTED)
        revocation = CapabilityRevocation(
            node_id=self.peer_a.node_id,
            capability_version=1,
            issued_at=int(self.clock.now),
            reason="decommissioned",
        ).sign(self.peer_a)
        self.assertEqual(self.cache.revoke(revocation, now=self.clock.now).code, ACCEPTED)
        self.assertEqual(self.cache.lookup(now=self.clock.now), [])
        self.assertTrue(self.store.is_revoked(self.peer_a.node_id, 1))

        # A later higher version is a different capability_version and may be
        # announced again (the revocation is version-scoped).
        manifest_v2, announcement_v2 = self.signed(version=2, seq=2)
        self.assertEqual(self.ingest(manifest_v2, announcement_v2).code, ACCEPTED)

    def test_bad_revocation_signature_is_rejected(self) -> None:
        revocation = CapabilityRevocation(
            node_id=self.peer_a.node_id, capability_version=1, issued_at=int(self.clock.now), reason="x"
        ).sign(self.peer_a)
        tampered_signature = ("0" if revocation.signature[0] != "0" else "1") + revocation.signature[1:]
        tampered = CapabilityRevocation.from_dict(
            {**revocation.to_dict(), "signature": tampered_signature}
        )
        result = self.cache.revoke(tampered, now=self.clock.now)
        self.assertEqual(result.code, SIGNATURE)
        self.assertFalse(self.store.is_revoked(self.peer_a.node_id, 1))


if __name__ == "__main__":
    unittest.main()
