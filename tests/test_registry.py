"""D5: registry abstraction + local simulated adapter (no chain, no network)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from idle_compute.capability import CapabilityRevocation
from idle_compute.discovery import build_manifest
from idle_compute.identity import NodeIdentity
from idle_compute.registry import (
    STATUS_EXPIRED,
    STATUS_FINAL,
    STATUS_PENDING,
    STATUS_REVOKED,
    LocalRegistryAdapter,
    RegistryError,
)

NOW = 1_800_000_000
TEMPLATE = {
    "capability_version": 1,
    "ttl_seconds": 600,
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
}


class RegistryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="registry-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.identity = NodeIdentity.load_or_create(self.root / "n" / "identity.key", node_name="n")

    def manifest(self, *, version=1, now=NOW, ttl=600, template=None):
        values = dict(TEMPLATE)
        values.update(template or {})
        values["capability_version"] = version
        values["ttl_seconds"] = ttl
        return build_manifest(values, self.identity, now=now)

    def adapter(self, *, delay=0, clock=None):
        path = self.root / "registry.db"
        adapter = LocalRegistryAdapter(
            path, finality_delay_seconds=delay, clock=clock or (lambda: NOW)
        )
        self.addCleanup(adapter.close)
        return adapter

    def revocation(self, version=1, reason="decommissioned"):
        return CapabilityRevocation(
            node_id=self.identity.node_id,
            capability_version=version,
            issued_at=NOW,
            reason=reason,
        ).sign(self.identity)


class RegisterTests(RegistryTestBase):
    def test_register_with_zero_delay_is_final(self) -> None:
        adapter = self.adapter()
        record = adapter.register(self.manifest(), now=NOW)
        self.assertEqual(record.status, STATUS_FINAL)
        self.assertEqual(record.capability_version, 1)
        self.assertEqual(record.manifest_hash, self.manifest().hash())

    def test_register_is_idempotent(self) -> None:
        adapter = self.adapter()
        first = adapter.register(self.manifest(), now=NOW)
        version = adapter.registry_version()
        second = adapter.register(self.manifest(), now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(adapter.registry_version(), version, "idempotent register must not bump the version")

    def test_duplicate_with_different_content_conflicts(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(), now=NOW)
        changed = self.manifest(template={"hardware": {"cpu_arch": "x86_64", "cpu_cores": 16}})
        with self.assertRaises(RegistryError) as ctx:
            adapter.register(changed, now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.VERSION_CONFLICT)

    def test_register_with_future_version_after_existing_is_an_update(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=1), now=NOW)
        record = adapter.register(self.manifest(version=2), now=NOW)
        self.assertEqual(record.capability_version, 2)
        self.assertEqual(record.status, STATUS_FINAL)

    def test_register_with_expired_manifest_is_rejected(self) -> None:
        adapter = self.adapter()
        expired = self.manifest(now=NOW - 700, ttl=600)
        with self.assertRaises(RegistryError) as ctx:
            adapter.register(expired, now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.EXPIRED)


class UpdateTests(RegistryTestBase):
    def test_update_to_higher_version(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=1), now=NOW)
        updated = adapter.update(self.manifest(version=2), now=NOW)
        self.assertEqual(updated.capability_version, 2)
        self.assertEqual(adapter.registry_version(), 2)

    def test_update_rollback_is_rejected(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=2), now=NOW)
        with self.assertRaises(RegistryError) as ctx:
            adapter.update(self.manifest(version=1), now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.VERSION_ROLLBACK)

    def test_update_same_version_same_hash_is_idempotent(self) -> None:
        adapter = self.adapter()
        first = adapter.register(self.manifest(version=2), now=NOW)
        twice = adapter.update(self.manifest(version=2), now=NOW)
        self.assertEqual(first, twice)

    def test_update_same_version_different_hash_conflicts(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=2), now=NOW)
        changed = self.manifest(version=2, template={"hardware": {"cpu_arch": "x86_64", "cpu_cores": 32}})
        with self.assertRaises(RegistryError) as ctx:
            adapter.update(changed, now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.VERSION_CONFLICT)

    def test_update_unknown_node_is_not_found(self) -> None:
        adapter = self.adapter()
        with self.assertRaises(RegistryError) as ctx:
            adapter.update(self.manifest(), now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.NOT_FOUND)


class RevokeTests(RegistryTestBase):
    def test_revoke_current_version(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=1), now=NOW)
        record = adapter.revoke(self.revocation(version=1), now=NOW)
        self.assertEqual(record.status, STATUS_REVOKED)
        with self.assertRaises(RegistryError) as ctx:
            adapter.verify(self.manifest(version=1), now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.REVOKED)

    def test_revoke_is_idempotent(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=1), now=NOW)
        first = adapter.revoke(self.revocation(version=1), now=NOW)
        second = adapter.revoke(self.revocation(version=1), now=NOW)
        self.assertEqual(first.status, second.status)

    def test_revoke_superseded_version_is_rejected(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=2), now=NOW)
        with self.assertRaises(RegistryError) as ctx:
            adapter.revoke(self.revocation(version=1), now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.SCHEMA)

    def test_revoke_with_bad_signature_is_rejected(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(version=1), now=NOW)
        revocation = self.revocation(version=1)
        tampered = CapabilityRevocation.from_dict(
            {**revocation.to_dict(), "signature": ("0" if revocation.signature[0] != "0" else "1") + revocation.signature[1:]}
        )
        with self.assertRaises(RegistryError) as ctx:
            adapter.revoke(tampered, now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.SIGNATURE)


class FinalityAndExpiryTests(RegistryTestBase):
    def test_finality_delay_pending_then_final(self) -> None:
        adapter = self.adapter(delay=60)
        record = adapter.register(self.manifest(), now=NOW)
        self.assertEqual(record.status, STATUS_PENDING)
        with self.assertRaises(RegistryError) as ctx:
            adapter.verify(self.manifest(), now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.NOT_FINAL)
        self.assertEqual(adapter.finalize(now=NOW + 59), 0)
        self.assertEqual(adapter.finalize(now=NOW + 60), 1)
        self.assertEqual(adapter.verify(self.manifest(), now=NOW + 60).status, STATUS_FINAL)

    def test_record_expiry(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(ttl=10), now=NOW)
        record = adapter.get(self.identity.node_id, now=NOW + 11)
        self.assertEqual(record.status, STATUS_EXPIRED)
        with self.assertRaises(RegistryError) as ctx:
            adapter.verify(self.manifest(ttl=10), now=NOW + 11)
        self.assertEqual(ctx.exception.code, RegistryError.EXPIRED)

    def test_invalid_finality_delay_is_rejected(self) -> None:
        for bad in (-1, 3601, True, "5"):
            with self.assertRaises(ValueError):
                LocalRegistryAdapter(self.root / "r.db", finality_delay_seconds=bad)  # type: ignore[arg-type]


class PersistenceTests(RegistryTestBase):
    def test_reopen_keeps_version_and_record(self) -> None:
        path = self.root / "registry.db"
        adapter = LocalRegistryAdapter(path, clock=lambda: NOW)
        adapter.register(self.manifest(version=2), now=NOW)
        version = adapter.registry_version()
        adapter.close()

        reopened = LocalRegistryAdapter(path, clock=lambda: NOW)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.registry_version(), version)
        self.assertEqual(reopened.get(self.identity.node_id).capability_version, 2)

    def test_registry_uses_its_own_file(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(), now=NOW)
        self.assertTrue((self.root / "registry.db").exists())
        self.assertFalse((self.root / "coordinator.db").exists())

    def test_write_failure_does_not_bump_version(self) -> None:
        adapter = self.adapter()
        adapter.register(self.manifest(), now=NOW)
        version = adapter.registry_version()
        adapter._conn.close()  # simulate a lost/broken connection
        with self.assertRaises(RegistryError) as ctx:
            adapter.update(self.manifest(version=2), now=NOW)
        self.assertEqual(ctx.exception.code, RegistryError.PERSIST_FAILED)
        # The writer is broken, so prove the committed version via a fresh open.
        reopened = LocalRegistryAdapter(self.root / "registry.db", clock=lambda: NOW)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.registry_version(), version)


if __name__ == "__main__":
    unittest.main()
