"""B-1: Ed25519 node identity — seed file lifecycle, node_id derivation,
sign/verify, and cryptography cross-verification."""

from __future__ import annotations

import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from libp2p.peer.id import ID

from idle_compute.identity import (
    SEED_BYTES,
    Ed25519Signer,
    NodeIdentity,
    is_valid_node_id,
)


class IdentityKeyManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key = Path(self._tmp.name) / "identity.key"

    def test_load_or_create_is_idempotent_and_node_id_stable(self) -> None:
        first = NodeIdentity.load_or_create(self.key, node_name="n1")
        second = NodeIdentity.load_or_create(self.key, node_name="n1")
        self.assertEqual(first.node_id, second.node_id)
        payload = "idle-compute-node-identity-stability"
        self.assertEqual(first.sign(payload), second.sign(payload))
        self.assertTrue(first.verify(payload, first.sign(payload)))

    def test_identity_key_is_exactly_32_bytes(self) -> None:
        NodeIdentity.load_or_create(self.key, node_name="n1")
        self.assertEqual(len(self.key.read_bytes()), SEED_BYTES)

    def test_partial_key_file_fails_loudly(self) -> None:
        self.key.write_bytes(b"x" * 10)
        with mock.patch("idle_compute.identity.time.sleep"):  # skip retry delays
            with self.assertRaises(ValueError) as ctx:
                NodeIdentity.load_or_create(self.key)
        self.assertIn("corrupt", str(ctx.exception))

    def test_windows_creation_warns_about_acl_limitation(self) -> None:
        with mock.patch("idle_compute.identity.os.name", "nt"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                NodeIdentity.load_or_create(self.key, node_name="n1")
        self.assertTrue(any("directory ACLs" in str(w.message) for w in caught))

    def test_loser_of_creation_race_waits_for_full_key(self) -> None:
        # The file exists (winner of the O_EXCL race) but only a partial seed is
        # visible yet; the retry loop must wait until the full 32 bytes appear.
        self.key.write_bytes(b"z" * 4)

        def complete_write(*_args, **_kwargs):
            self.key.write_bytes(os.urandom(SEED_BYTES))

        with mock.patch("idle_compute.identity.time.sleep", side_effect=complete_write):
            identity = NodeIdentity.load_or_create(self.key, node_name="n1")
        self.assertEqual(len(self.key.read_bytes()), SEED_BYTES)
        self.assertTrue(identity.verify("race-test", identity.sign("race-test")))


class IdentitySignVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.identity = NodeIdentity.load_or_create(
            Path(self._tmp.name) / "identity.key", node_name="n1"
        )
        self.other = NodeIdentity.load_or_create(
            Path(self._tmp.name) / "other" / "identity.key", node_name="other"
        )
        self.payload = "idle-compute-sign-roundtrip"

    def test_node_id_matches_libp2p_derivation(self) -> None:
        expected = ID.from_pubkey(self.identity.key_pair.public_key).to_string()
        self.assertEqual(self.identity.node_id, expected)
        # And the id roundtrips through the base58 parser.
        self.assertEqual(ID.from_base58(self.identity.node_id), expected)

    def test_node_id_is_validated_by_pattern(self) -> None:
        self.assertTrue(is_valid_node_id(self.identity.node_id))
        self.assertFalse(is_valid_node_id("not-a-node-id"))
        self.assertFalse(is_valid_node_id(""))
        self.assertFalse(is_valid_node_id(None))

    def test_sign_verify_roundtrip(self) -> None:
        signature = self.identity.sign(self.payload)
        self.assertEqual(len(signature), 128)  # 64-byte sig as hex
        self.assertTrue(self.identity.verify(self.payload, signature))

    def test_tampered_signature_fails(self) -> None:
        signature = self.identity.sign(self.payload)
        tampered = "00" + signature[2:]
        self.assertFalse(self.identity.verify(self.payload, tampered))

    def test_tampered_payload_fails(self) -> None:
        signature = self.identity.sign(self.payload)
        self.assertFalse(self.identity.verify(self.payload + "-tampered", signature))

    def test_verify_fails_with_wrong_key(self) -> None:
        signature = self.identity.sign(self.payload)
        self.assertFalse(self.other.verify(self.payload, signature))

    def test_verify_signature_from_node_id_no_key_distribution(self) -> None:
        signature = self.identity.sign(self.payload)
        # Any party can verify with only the sender's node_id string: the
        # Ed25519 pubkey is embedded in the identity multihash.
        self.assertTrue(
            NodeIdentity.verify_signature(self.identity.node_id, self.payload, signature)
        )
        self.assertFalse(
            NodeIdentity.verify_signature(self.other.node_id, self.payload, signature)
        )
        self.assertFalse(
            NodeIdentity.verify_signature("bogus-id", self.payload, signature)
        )
        self.assertFalse(
            NodeIdentity.verify_signature(self.identity.node_id, self.payload, "zz")
        )

    def test_extract_public_key_verify_via_id(self) -> None:
        signature = self.identity.sign(self.payload)
        public_key = ID.from_base58(self.identity.node_id).extract_public_key()
        self.assertIsNotNone(public_key)
        self.assertTrue(public_key.verify(self.payload.encode("utf-8"), bytes.fromhex(signature)))

    def test_cryptography_cross_verifies_libp2p_signature(self) -> None:
        # cryptography==50.0.2 verifies a signature produced by libp2p's Ed25519
        # from the SAME 32-byte seed file.
        seed = self.key_bytes_from_identity()
        crypto_key = Ed25519PrivateKey.from_private_bytes(seed)
        signature = self.identity.sign(self.payload)
        crypto_key.public_key().verify(bytes.fromhex(signature), self.payload.encode("utf-8"))
        # And a cryptography-produced signature verifies through libp2p.
        crypto_sig = crypto_key.sign(self.payload.encode("utf-8"))
        self.assertTrue(self.identity.verify(self.payload, crypto_sig.hex()))

    def test_cryptography_derives_identical_public_key_from_same_seed(self) -> None:
        seed = self.key_bytes_from_identity()
        crypto_pub = Ed25519PrivateKey.from_private_bytes(seed).public_key()
        raw = crypto_pub.public_bytes_raw()
        self.assertEqual(raw, self.identity.public_key_bytes)
        # And the embedded-pubkey roundtrip: the node_id's extracted pubkey
        # matches the cryptography-derived one.
        extracted = ID.from_base58(self.identity.node_id).extract_public_key()
        self.assertEqual(extracted.to_bytes(), raw)

    def key_bytes_from_identity(self) -> bytes:
        seed_path = self.identity.seed_path
        assert seed_path is not None
        return seed_path.read_bytes()

    def test_wrong_key_cryptography_fails(self) -> None:
        seed = self.key_bytes_from_identity()
        crypto_key = Ed25519PrivateKey.from_private_bytes(seed)
        signature = self.identity.sign(self.payload)
        other_crypto = Ed25519PrivateKey.generate()
        with self.assertRaises(InvalidSignature):
            other_crypto.public_key().verify(
                bytes.fromhex(signature), self.payload.encode("utf-8")
            )

    def test_signer_mirrors_local_signer_interface(self) -> None:
        signer: Ed25519Signer = self.identity.signer()
        signature = signer.sign(self.payload)
        self.assertTrue(signer.verify(self.payload, signature))
        self.assertFalse(signer.verify(self.payload + "x", signature))


if __name__ == "__main__":
    unittest.main()
