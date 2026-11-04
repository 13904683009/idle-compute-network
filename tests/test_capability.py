"""D1: CapabilityManifest / CapabilityAnnouncement / revocation protocol tests."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from idle_compute.capability import (
    ANNOUNCEMENT_DOMAIN,
    CAPABILITY_DOMAIN,
    REVOCATION_DOMAIN,
    CapabilityAnnouncement,
    CapabilityError,
    CapabilityManifest,
    CapabilityRevocation,
    check_manifest_update,
)
from idle_compute.crypto import canonical_json
from idle_compute.identity import NodeIdentity

NOW = 1_800_000_000


def manifest_for(identity: NodeIdentity, *, version: int = 1, ttl: int = 3600, **overrides):
    base = CapabilityManifest(
        node_id=identity.node_id,
        capability_version=version,
        issued_at=NOW,
        expires_at=NOW + ttl,
        supported_task_types=("numeric_batch_v1",),
        supported_runtimes=("python_sandbox_v1",),
        hardware={"cpu_arch": "x86_64", "cpu_cores": 8, "ram_mb": 16_384},
        resource_limits={
            "max_cpu_cores": 4,
            "max_ram_mb": 8192,
            "max_gpu_memory_mb": 0,
            "max_task_duration_seconds": 600,
        },
        network={"supported_transports": ["tcp"], "supports_relay": False},
        verification={"level": "SELF_DECLARED", "benchmark_profile": None},
    )
    return replace(base, **overrides).sign(identity)


def announcement_for(manifest: CapabilityManifest, identity: NodeIdentity, *, seq: int = 1, ttl: int = 60, **overrides):
    base = CapabilityAnnouncement(
        node_id=identity.node_id,
        capability_version=manifest.capability_version,
        sequence=seq,
        observed_at=NOW,
        expires_at=NOW + ttl,
        available_resources={"cpu_cores": 2, "ram_mb": 4096, "gpu_memory_mb": 0},
        queue={"running_tasks": 0, "queued_tasks": 0},
        network_status={"connectivity": "DIRECT", "supported_transports": ["tcp"]},
        accepting_tasks=True,
    )
    return replace(base, **overrides).sign(identity)


class CapabilityTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="capability-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.identity = NodeIdentity.load_or_create(root / "a" / "identity.key", node_name="a")
        self.other = NodeIdentity.load_or_create(root / "b" / "identity.key", node_name="b")


class ManifestTests(CapabilityTestBase):
    def test_canonical_hash_is_stable_across_roundtrip(self) -> None:
        manifest = manifest_for(self.identity)
        payload = manifest.payload()
        reordered = {key: payload[key] for key in reversed(list(payload))}
        self.assertEqual(
            canonical_json(payload),
            canonical_json(reordered),
            "canonical JSON must not depend on dict insertion order",
        )
        restored = CapabilityManifest.from_dict(manifest.to_dict())
        self.assertEqual(restored.hash(), manifest.hash())

    def test_sign_and_verify(self) -> None:
        manifest = manifest_for(self.identity)
        self.assertEqual(len(manifest.signature), 128)
        manifest.verify(NOW)  # must not raise
        self.assertEqual(manifest.hash(), CapabilityManifest.from_dict(manifest.to_dict()).hash())

    def test_tampered_field_breaks_the_signature(self) -> None:
        manifest = manifest_for(self.identity)
        tampered = replace(manifest, supported_task_types=("numeric_batch_v1", "other_v1"))
        with self.assertRaises(CapabilityError) as ctx:
            tampered.verify(NOW)
        self.assertEqual(ctx.exception.code, CapabilityError.SIGNATURE)

    def test_wrong_signer_is_rejected(self) -> None:
        manifest = manifest_for(self.identity)
        with self.assertRaises(CapabilityError) as ctx:
            manifest.sign(self.other)
        self.assertEqual(ctx.exception.code, CapabilityError.SCHEMA)

    def test_expired_manifest_is_rejected(self) -> None:
        manifest = manifest_for(self.identity, ttl=10)
        with self.assertRaises(CapabilityError) as ctx:
            manifest.verify(NOW + 11)
        self.assertEqual(ctx.exception.code, CapabilityError.EXPIRED)

    def test_future_issued_at_is_rejected(self) -> None:
        manifest = manifest_for(self.identity, issued_at=NOW + 10_000)
        with self.assertRaises(CapabilityError) as ctx:
            manifest.verify(NOW)
        self.assertEqual(ctx.exception.code, CapabilityError.SCHEMA)

    def test_same_version_different_content_is_conflict(self) -> None:
        first = manifest_for(self.identity, version=1)
        changed = manifest_for(self.identity, version=1, hardware={"cpu_arch": "x86_64", "cpu_cores": 4})
        record = {
            "capability_version": 1,
            "manifest_hash": first.hash(),
            "revoked": False,
        }
        self.assertEqual(check_manifest_update(record, first, NOW), "IDEMPOTENT")
        self.assertEqual(check_manifest_update(record, changed, NOW), "CONFLICT")

    def test_lower_version_is_rollback_and_higher_is_accept(self) -> None:
        current = manifest_for(self.identity, version=2)
        record = {"capability_version": 2, "manifest_hash": current.hash(), "revoked": False}
        older = manifest_for(self.identity, version=1)
        newer = manifest_for(self.identity, version=3)
        self.assertEqual(check_manifest_update(record, older, NOW), "ROLLBACK")
        self.assertEqual(check_manifest_update(record, newer, NOW), "ACCEPT")

    def test_revoked_version_stays_revoked(self) -> None:
        manifest = manifest_for(self.identity, version=1)
        record = {"capability_version": 1, "manifest_hash": manifest.hash(), "revoked": True}
        self.assertEqual(check_manifest_update(record, manifest, NOW), "REVOKED")

    def test_schema_is_exact_and_strict(self) -> None:
        manifest = manifest_for(self.identity)
        data = manifest.to_dict()
        data["extra"] = 1
        with self.assertRaises(CapabilityError) as ctx:
            CapabilityManifest.from_dict(data)
        self.assertEqual(ctx.exception.code, CapabilityError.SCHEMA)
        missing = dict(manifest.to_dict())
        missing.pop("hardware")
        with self.assertRaises(CapabilityError):
            CapabilityManifest.from_dict(missing)


class AnnouncementTests(CapabilityTestBase):
    def test_sign_and_verify(self) -> None:
        manifest = manifest_for(self.identity)
        announcement = announcement_for(manifest, self.identity)
        announcement.verify(manifest, NOW)
        self.assertEqual(len(announcement.signature), 128)

    def test_old_sequence_is_rejected(self) -> None:
        manifest = manifest_for(self.identity)
        announcement = announcement_for(manifest, self.identity, seq=5)
        with self.assertRaises(CapabilityError) as ctx:
            announcement.verify(manifest, NOW, last_sequence=5)
        self.assertEqual(ctx.exception.code, CapabilityError.SEQ_ROLLBACK)

    def test_higher_sequence_is_accepted(self) -> None:
        manifest = manifest_for(self.identity)
        announcement = announcement_for(manifest, self.identity, seq=6)
        announcement.verify(manifest, NOW, last_sequence=5)

    def test_expired_announcement_is_rejected(self) -> None:
        manifest = manifest_for(self.identity)
        announcement = announcement_for(manifest, self.identity, ttl=10)
        with self.assertRaises(CapabilityError) as ctx:
            announcement.verify(manifest, NOW + 11)
        self.assertEqual(ctx.exception.code, CapabilityError.EXPIRED)

    def test_wrong_capability_version_is_rejected(self) -> None:
        manifest = manifest_for(self.identity, version=2)
        announcement = announcement_for(manifest, self.identity)
        other_manifest = manifest_for(self.identity, version=3)
        with self.assertRaises(CapabilityError):
            announcement.verify(other_manifest, NOW)

    def test_tampered_announcement_is_rejected(self) -> None:
        manifest = manifest_for(self.identity)
        announcement = announcement_for(manifest, self.identity)
        tampered = replace(announcement, accepting_tasks=False)
        with self.assertRaises(CapabilityError) as ctx:
            tampered.verify(manifest, NOW)
        self.assertEqual(ctx.exception.code, CapabilityError.SIGNATURE)


class RevocationTests(CapabilityTestBase):
    def test_sign_and_verify(self) -> None:
        revocation = CapabilityRevocation(
            node_id=self.identity.node_id,
            capability_version=2,
            issued_at=NOW,
            reason="hardware changed",
        ).sign(self.identity)
        revocation.verify(NOW)
        self.assertEqual(CapabilityRevocation.from_dict(revocation.to_dict()).payload(), revocation.payload())

    def test_tampered_reason_is_rejected(self) -> None:
        revocation = CapabilityRevocation(
            node_id=self.identity.node_id, capability_version=1, issued_at=NOW, reason="x"
        ).sign(self.identity)
        tampered = replace(revocation, reason="y")
        with self.assertRaises(CapabilityError) as ctx:
            tampered.verify(NOW)
        self.assertEqual(ctx.exception.code, CapabilityError.SIGNATURE)

    def test_wrong_signer_is_rejected(self) -> None:
        revocation = CapabilityRevocation(
            node_id=self.identity.node_id, capability_version=1, issued_at=NOW, reason="x"
        )
        with self.assertRaises(CapabilityError):
            revocation.sign(self.other)

    def test_domains_are_distinct(self) -> None:
        self.assertEqual(len({CAPABILITY_DOMAIN, ANNOUNCEMENT_DOMAIN, REVOCATION_DOMAIN}), 3)


if __name__ == "__main__":
    unittest.main()
