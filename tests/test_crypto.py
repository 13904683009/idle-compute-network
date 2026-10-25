from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from idle_compute.crypto import (
    GENESIS_HASH,
    KEY_BYTES,
    LocalSigner,
    canonical_json,
    content_hash,
    credit_body,
    node_msg_payload,
    result_payload,
    result_signing_payload,
    sha256_hex,
)


class PayloadBuilderTests(unittest.TestCase):
    """B-2: v2 payload builders have stable golden hashes; v1 is unchanged."""

    NODE_ID = "12D3KooW" + "A" * 44

    def test_result_payload_golden_hash(self) -> None:
        payload = result_payload("tsk_golden", "lea_golden", self.NODE_ID, "a" * 64)
        self.assertIn("idle-compute-result-v2", payload)
        self.assertNotIn("idle-compute-result-v1", payload)
        self.assertEqual(
            sha256_hex(payload),
            "b11749218b96324d48fc3f0bb1c83ae87fe2ebacb6aca21fe57fd14b1c53a90e",
        )

    def test_node_msg_payload_golden_hash(self) -> None:
        payload = node_msg_payload(
            2, "HELLO", "12D3KooWA", "12D3KooWB", 7, "2026-10-26T00:00:00+00:00", None, {"proto": 2}
        )
        self.assertIn("idle-compute-node-msg-v2", payload)
        self.assertEqual(
            sha256_hex(payload),
            "e52e14de818c267b2cf54ae41df82d77c4d39e759bd3e0f12304116dc61ffb73",
        )

    def test_credit_body_golden_hash(self) -> None:
        body = credit_body(
            "evt_golden",
            "CREDIT",
            "12D3KooWC",
            "tsk_golden",
            1,
            "credit:tsk_golden:12D3KooWC",
            "2026-10-26T00:00:00+00:00",
            GENESIS_HASH,
            {"result_id": "res_golden"},
        )
        self.assertIn("idle-compute-credit-v2", body["domain"])
        self.assertEqual(
            content_hash(body),
            "91b00d323b049c6830605d046b4d6963eca674363fc0e00a29b2067cfeecc829",
        )

    def test_v2_domains_never_collide_with_v1(self) -> None:
        v1 = result_signing_payload("t", "l", "p", "h")
        v2 = result_payload("t", "l", "p", "h")
        self.assertIn("idle-compute-result-v1", v1)
        self.assertIn("idle-compute-result-v2", v2)
        self.assertNotEqual(sha256_hex(v1), sha256_hex(v2))

    def test_canonical_helpers_unchanged(self) -> None:
        # Phase 1 helpers keep their exact behavior (regression pin).
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')
        self.assertEqual(content_hash({"b": 1, "a": 2}), sha256_hex('{"a":2,"b":1}'))
        self.assertEqual(len(GENESIS_HASH), 64)


class KeyManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key = Path(self._tmp.name) / "ledger.key"

    def test_load_or_create_is_idempotent_and_stable(self) -> None:
        first = LocalSigner.load_or_create(self.key)
        second = LocalSigner.load_or_create(self.key)
        payload = "idle-compute-key-stability"
        self.assertEqual(len(self.key.read_bytes()), KEY_BYTES)
        self.assertEqual(first.sign(payload), second.sign(payload))

    def test_partial_key_file_fails_loudly(self) -> None:
        self.key.write_bytes(b"x" * 10)
        with mock.patch("idle_compute.crypto.time.sleep"):  # skip retry delays
            with self.assertRaises(ValueError) as ctx:
                LocalSigner.load_or_create(self.key)
        self.assertIn("too short", str(ctx.exception))

    def test_windows_creation_warns_about_acl_limitation(self) -> None:
        with mock.patch("idle_compute.crypto._POSIX_PERMISSIONS_UNSUPPORTED", True):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                LocalSigner.load_or_create(self.key)
        self.assertTrue(any("directory ACLs" in str(w.message) for w in caught))

    def test_loser_of_creation_race_waits_for_full_key(self) -> None:
        # The file exists (winner of the O_EXCL race) but only a partial key is
        # visible yet; the retry loop must wait until the full 32 bytes appear.
        self.key.write_bytes(b"z" * 4)

        def complete_write(*_args, **_kwargs):
            self.key.write_bytes(b"y" * KEY_BYTES)

        with mock.patch("idle_compute.crypto.time.sleep", side_effect=complete_write):
            signer = LocalSigner.load_or_create(self.key)
        self.assertEqual(len(self.key.read_bytes()), KEY_BYTES)
        self.assertEqual(len(signer.sign("race-test")), 64)


if __name__ == "__main__":
    unittest.main()
