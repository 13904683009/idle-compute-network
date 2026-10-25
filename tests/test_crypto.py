from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from idle_compute.crypto import KEY_BYTES, LocalSigner


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
