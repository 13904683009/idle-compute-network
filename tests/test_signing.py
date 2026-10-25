"""B-2: signing-scheme policies (Ed25519 network / HMAC legacy)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from idle_compute.crypto import (
    LocalSigner,
    content_hash,
    result_payload,
    result_signing_payload,
)
from idle_compute.identity import NodeIdentity
from idle_compute.signing import Ed25519Policy, HmacLegacyPolicy


class PolicyInterfaceTests(unittest.TestCase):
    """Both policies expose the same sign/verify surface."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.identity = NodeIdentity.load_or_create(
            Path(self._tmp.name) / "identity.key", node_name="n1"
        )
        self.coordinator = NodeIdentity.load_or_create(
            Path(self._tmp.name) / "coordinator.key", node_name="coordinator"
        )
        self.ed_policy = Ed25519Policy.from_identity(self.identity)
        self.hmac_policy = HmacLegacyPolicy(
            LocalSigner.load_or_create(Path(self._tmp.name) / "ledger.key")
        )

    def test_ed25519_policy_sign_verify_roundtrip(self) -> None:
        payload = "idle-compute-ed25519-roundtrip"
        signature = self.ed_policy.sign(payload)
        self.assertEqual(len(signature), 128)
        self.assertTrue(self.ed_policy.verify(payload, signature))
        self.assertFalse(self.ed_policy.verify(payload + "x", signature))

    def test_hmac_policy_delegates_to_local_signer(self) -> None:
        payload = "idle-compute-hmac-roundtrip"
        signature = self.hmac_policy.sign(payload)
        self.assertEqual(len(signature), 64)
        self.assertTrue(self.hmac_policy.verify(payload, signature))
        self.assertFalse(self.hmac_policy.verify(payload + "x", signature))

    def test_schemes_are_tagged(self) -> None:
        self.assertEqual(self.ed_policy.scheme, "ed25519")
        self.assertEqual(self.hmac_policy.scheme, "hmac-sha256")

    def test_result_payload_builders_match_scheme(self) -> None:
        self.assertEqual(
            self.ed_policy.result_payload("t", "l", "p", "h"),
            result_payload("t", "l", "p", "h"),
        )
        self.assertIn("idle-compute-result-v2", self.ed_policy.result_payload("t", "l", "p", "h"))
        self.assertEqual(
            self.hmac_policy.result_payload("t", "l", "p", "h"),
            result_signing_payload("t", "l", "p", "h"),
        )
        self.assertIn("idle-compute-result-v1", self.hmac_policy.result_payload("t", "l", "p", "h"))

    def test_ed25519_verify_result_uses_peer_id_embedded_pubkey(self) -> None:
        worker = NodeIdentity.load_or_create(
            Path(self._tmp.name) / "worker" / "identity.key", node_name="worker"
        )
        payload = result_payload("tsk_1", "lea_1", worker.node_id, "h" * 64)
        signature = worker.sign(payload)
        self.assertTrue(self.ed_policy.verify_result(worker.node_id, payload, signature))
        # Wrong key (different node) is rejected.
        self.assertFalse(
            self.ed_policy.verify_result(self.identity.node_id, payload, signature)
        )
        self.assertFalse(self.ed_policy.verify_result(worker.node_id, payload + "x", signature))

    def test_ed25519_verify_credit_with_pinned_coordinator(self) -> None:
        pinned = Ed25519Policy.from_identity(
            self.identity, coordinator_node_id=self.coordinator.node_id
        )
        body = {"domain": "idle-compute-credit-v2", "event_id": "evt_1", "amount": 1}
        payload = content_hash(body)
        signature = self.coordinator.sign(payload)
        self.assertTrue(pinned.verify_credit(payload, signature))
        # A policy without a pinned coordinator verifies against its own key.
        unpinned = Ed25519Policy.from_identity(self.coordinator)
        self.assertTrue(unpinned.verify_credit(payload, signature))
        # The worker's own policy (unpinned, different key) rejects it.
        self.assertFalse(self.ed_policy.verify_credit(payload, signature))

    def test_cross_scheme_rows_are_unverifiable(self) -> None:
        payload = "idle-compute-cross-scheme"
        ed_sig = self.ed_policy.sign(payload)
        hmac_sig = self.hmac_policy.sign(payload)
        # Ed25519 policy cannot verify hmac rows.
        self.assertFalse(self.ed_policy.verify_hmac(payload, hmac_sig))
        # Legacy policy has no Ed25519 verifier.
        self.assertFalse(self.hmac_policy.verify_ed25519(payload, ed_sig))
        # Each policy verifies only its own scheme.
        self.assertTrue(self.ed_policy.verify_ed25519(payload, ed_sig))
        self.assertTrue(self.hmac_policy.verify_hmac(payload, hmac_sig))


if __name__ == "__main__":
    unittest.main()
