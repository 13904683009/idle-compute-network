"""D5 integration: registry-first matching + node registry synchronization.

Covers the requested wiring:
- Matcher prefers a FINAL LocalRegistryAdapter record; a missing record falls
  back to the in-memory announcement cache; revoked/expired/pending/mismatched
  registry records are authoritative denials.
- Node registers its own manifest on publish (and at startup), registers peer
  manifests on accepted announcements, revokes in both cache and registry, and
  exposes registry-first matching over its discovery cache.
- Version updates move the registry forward.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path

from idle_compute.capability import CapabilityRevocation
from idle_compute.config import from_dict
from idle_compute.discovery import build_announcement, build_manifest
from idle_compute.identity import NodeIdentity
from idle_compute.matcher import (
    REGISTRY_EXPIRED,
    REGISTRY_MISMATCH,
    REGISTRY_NOT_FINAL,
    REGISTRY_REVOKED,
    Candidate,
    match,
)
from idle_compute.node import Node
from idle_compute.protocol import MessageType
from idle_compute.registry import LocalRegistryAdapter
from idle_compute.requirement import TaskRequirement

NOW = int(time.time())
TEMPLATE = {
    "capability_version": 1,
    "ttl_seconds": 3600,
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


def requirement(**overrides) -> TaskRequirement:
    data = {
        "task_id": "task-1",
        "task_type": "numeric_batch_v1",
        "task_version": 1,
        "input_manifest_hash": "",
        "required_runtime": "python_sandbox_v1",
        "minimum_resources": {"cpu_cores": 1, "ram_mb": 512, "gpu_memory_mb": 0},
        "preferred_resources": {},
        "constraints": {
            "max_execution_seconds": 60,
            "max_input_bytes": 1024,
            "max_output_bytes": 1024,
        },
        "data_policy": {"requires_private_data": False, "allow_relay": True},
        "verification_policy": {"mode": "RECOMPUTE", "required": False},
        "deadline": NOW + 3600,
    }
    data.update(overrides)
    return TaskRequirement(**data)


class RegistryMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d5-reg-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.identity = NodeIdentity.load_or_create(self.root / "n" / "identity.key", node_name="n")
        self.registry = LocalRegistryAdapter(self.root / "registry.db", clock=lambda: NOW)
        self.addCleanup(self.registry.close)
        self.manifest = build_manifest(TEMPLATE, self.identity, now=NOW)
        self.announcement = build_announcement(
            self.manifest, TEMPLATE, self.identity, now=NOW, sequence=1, ttl_seconds=600
        )
        self.candidate = Candidate(
            node_id=self.identity.node_id,
            manifest=self.manifest,
            announcement=self.announcement,
            reachable=True,
        )

    def _match(self):
        return match(requirement(), [self.candidate], now=NOW, registry=self.registry)

    def test_final_registry_record_is_preferred(self) -> None:
        self.registry.register(self.manifest, now=NOW)
        result = self._match()
        self.assertEqual(len(result.ranking), 1)
        self.assertTrue(result.ranking[0].registry_verified)
        self.assertTrue(result.as_dict()["ranking"][0]["registry_verified"])

    def test_unregistered_candidate_falls_back_to_cache(self) -> None:
        result = self._match()
        self.assertEqual(len(result.ranking), 1)
        self.assertFalse(result.ranking[0].registry_verified, "cache fallback is not registry-verified")

    def test_revoked_registry_record_denies_even_if_cache_valid(self) -> None:
        self.registry.register(self.manifest, now=NOW)
        revocation = CapabilityRevocation(
            node_id=self.identity.node_id,
            capability_version=self.manifest.capability_version,
            issued_at=NOW,
            reason="test revoke",
        ).sign(self.identity)
        self.registry.revoke(revocation, now=NOW)
        result = self._match()
        self.assertEqual(result.ranking, ())
        self.assertIn(REGISTRY_REVOKED, result.rejected[0].rejection_codes)

    def test_expired_registry_record_is_denied(self) -> None:
        short = dict(TEMPLATE, ttl_seconds=10)
        manifest = build_manifest(short, self.identity, now=NOW)
        self.registry.register(manifest, now=NOW)
        announcement = build_announcement(
            manifest, short, self.identity, now=NOW, sequence=2, ttl_seconds=600
        )
        candidate = Candidate(
            node_id=self.identity.node_id,
            manifest=manifest,
            announcement=announcement,
            reachable=True,
        )
        result = match(requirement(), [candidate], now=NOW + 11, registry=self.registry)
        self.assertIn(REGISTRY_EXPIRED, result.rejected[0].rejection_codes)

    def test_pending_registry_record_is_not_final(self) -> None:
        registry = LocalRegistryAdapter(
            self.root / "pending.db", finality_delay_seconds=60, clock=lambda: NOW
        )
        self.addCleanup(registry.close)
        registry.register(self.manifest, now=NOW)
        result = match(requirement(), [self.candidate], now=NOW, registry=registry)
        self.assertIn(REGISTRY_NOT_FINAL, result.rejected[0].rejection_codes)

    def test_registry_mismatch_is_denied(self) -> None:
        other = dict(TEMPLATE, hardware={"cpu_arch": "x86_64", "cpu_cores": 32})
        other_manifest = build_manifest(other, self.identity, now=NOW)
        self.registry.register(other_manifest, now=NOW)
        result = match(requirement(), [self.candidate], now=NOW, registry=self.registry)
        self.assertIn(REGISTRY_MISMATCH, result.rejected[0].rejection_codes)


class NodeRegistryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d5-node-reg-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.peer = NodeIdentity.load_or_create(self.root / "p" / "identity.key", node_name="p")
        self.node = Node(
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(self.root / "coord"),
                    "peers": [self.peer.node_id],
                    "capability": TEMPLATE,
                    "log_level": "ERROR",
                }
            )
        )
        self.addCleanup(self.node.shutdown)

    def test_publish_registers_own_manifest(self) -> None:
        self.node.publish_announcement()
        record = self.node._registry_adapter().get(self.node.identity.node_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "FINAL")
        self.assertEqual(record.capability_version, 1)

    def test_peer_announcement_is_registered_on_ingest(self) -> None:
        manifest = build_manifest(TEMPLATE, self.peer, now=int(time.time()))
        announcement = build_announcement(
            manifest, TEMPLATE, self.peer, now=int(time.time()), sequence=1, ttl_seconds=600
        )
        env = SimpleNamespace(
            type=MessageType.CAPABILITY_ANNOUNCE,
            sender=self.peer.node_id,
            seq=1,
            body={"manifest": manifest.to_dict(), "announcement": announcement.to_dict()},
        )
        self.assertTrue(self.node._handle_discovery(env, lambda _frame: None))
        record = self.node._registry_adapter().get(self.peer.node_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "FINAL")
        self.assertEqual(record.manifest_hash, manifest.hash())

    def test_match_candidates_uses_registry_then_cache(self) -> None:
        manifest = build_manifest(TEMPLATE, self.peer, now=NOW)
        announcement = build_announcement(
            manifest, TEMPLATE, self.peer, now=NOW, sequence=1, ttl_seconds=600
        )
        cache = self.node._discovery_cache()
        result = cache.ingest(manifest, announcement, now=NOW, sender_node_id=self.peer.node_id)
        self.assertEqual(result.code, "ACCEPTED")
        first = self.node.match_candidates(requirement(), now=NOW)
        self.assertEqual(len(first.ranking), 1)
        self.assertFalse(first.ranking[0].registry_verified, "not registered yet -> cache fallback")
        self.node._register_manifest(manifest)
        second = self.node.match_candidates(requirement(), now=NOW)
        self.assertEqual(len(second.ranking), 1)
        self.assertTrue(second.ranking[0].registry_verified)

    def test_revoke_syncs_cache_and_registry(self) -> None:
        self.node.publish_announcement()
        self.node.revoke_capability(1, "test revoke")
        record = self.node._registry_adapter().get(self.node.identity.node_id)
        self.assertEqual(record.status, "REVOKED")
        with self.assertRaises(Exception):
            self.node._registry_adapter().verify(
                build_manifest(TEMPLATE, self.node.identity, now=int(time.time()))
            )

    def test_version_update_moves_registry_forward(self) -> None:
        self.node.publish_announcement()
        updated = dict(TEMPLATE, capability_version=2)
        manifest_v2 = build_manifest(updated, self.node.identity, now=int(time.time()))
        self.node._register_manifest(manifest_v2)
        record = self.node._registry_adapter().get(self.node.identity.node_id)
        self.assertEqual(record.capability_version, 2)
        self.assertEqual(record.manifest_hash, manifest_v2.hash())


if __name__ == "__main__":
    unittest.main()
