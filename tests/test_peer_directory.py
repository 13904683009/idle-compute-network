"""D2-1: peers list + directory migration (ADR-D0-13).

Covers: legacy object form is read without losing role/addrs, the new shape
roundtrips through the loader, `peers add` writes a loadable config, and every
ambiguous/contradictory file is rejected with an actionable error.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from idle_compute.cli import main
from idle_compute.config import ValidationError, from_dict, load_config
from idle_compute.identity import NodeIdentity


def _node_id(root: Path, name: str) -> str:
    return NodeIdentity.load_or_create(root / name / "identity.key", node_name=name).node_id


class PeerDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="directory-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = _node_id(self.root, "coord")
        self.worker = _node_id(self.root, "worker")

    def _base(self, **overrides) -> dict:
        data = {"role": "coordinator", "transport": "tcp", "data_dir": "var/n1"}
        data.update(overrides)
        return data

    # ------------------------------------------------------------- loading

    def test_legacy_object_form_is_migrated_without_losing_metadata(self) -> None:
        legacy = self._base(
            peers={
                self.worker: {"role": "worker", "addrs": ["/ip4/127.0.0.1/tcp/1"]},
                self.coord: {"role": "coordinator", "addrs": []},
            }
        )
        config = from_dict(legacy)
        self.assertEqual(config.peers, [self.worker, self.coord], "keys become the allowlist")
        self.assertEqual(config.directory[self.worker].role, "worker")
        self.assertEqual(config.directory[self.worker].addrs, ("/ip4/127.0.0.1/tcp/1",))
        self.assertEqual(config.directory[self.coord].role, "coordinator")
        self.assertIsNotNone(config.migration_hint)
        self.assertIn("directory", config.migration_hint)

        # Writing the migrated config produces the new shape, which reloads
        # with identical semantics (and no hint).
        rewritten = config.to_dict()
        self.assertIsInstance(rewritten["peers"], list)
        self.assertIsInstance(rewritten["directory"], dict)
        reloaded = from_dict(rewritten)
        self.assertEqual(reloaded.peers, config.peers)
        self.assertEqual(reloaded.directory, config.directory)
        self.assertIsNone(reloaded.migration_hint)

    def test_new_shape_roundtrips_exactly(self) -> None:
        config = from_dict(
            self._base(
                peers=[self.worker],
                directory={self.worker: {"role": "worker", "addrs": ["/ip4/127.0.0.1/tcp/2"]}},
            )
        )
        self.assertEqual(from_dict(config.to_dict()), config)

    def test_legacy_and_directory_together_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(
                self._base(
                    peers={self.worker: {"role": "worker", "addrs": []}},
                    directory={self.worker: {"role": "worker", "addrs": []}},
                )
            )
        self.assertIn("legacy", str(ctx.exception))

    def test_legacy_entry_without_role_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(self._base(peers={self.worker: {"addrs": []}}))
        self.assertIn("role", str(ctx.exception))

    def test_directory_key_must_be_in_peers(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(
                self._base(
                    peers=[self.worker],
                    directory={self.coord: {"role": "coordinator", "addrs": []}},
                )
            )
        self.assertIn("not listed in peers", str(ctx.exception))

    def test_directory_unknown_key_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(
                self._base(
                    peers=[self.worker],
                    directory={self.worker: {"role": "worker", "addrs": [], "pubkey": "x"}},
                )
            )
        self.assertIn("unknown key", str(ctx.exception))

    def test_directory_addrs_must_be_strings(self) -> None:
        with self.assertRaises(ValidationError):
            from_dict(
                self._base(
                    peers=[self.worker],
                    directory={self.worker: {"role": "worker", "addrs": [123]}},
                )
            )

    def test_duplicate_peer_ids_are_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(self._base(peers=[self.worker, self.worker]))
        self.assertIn("duplicates", str(ctx.exception))

    def test_peers_as_a_string_is_still_rejected(self) -> None:
        # Only the legacy OBJECT form is accepted; a plain string is a typo.
        with self.assertRaises(ValidationError) as ctx:
            from_dict(self._base(peers="peer-string"))
        self.assertIn("peers", str(ctx.exception))

    # ----------------------------------------------------------------- CLI

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_repairs_a_legacy_file_and_preserves_metadata(self) -> None:
        config_path = self.root / "node.json"
        config_path.write_text(
            json.dumps(
                self._base(
                    peers={self.worker: {"role": "worker", "addrs": ["/ip4/127.0.0.1/tcp/9"]}}
                )
            ),
            encoding="utf-8",
        )
        code, out, err = self._run(
            "peers", "add", self.coord, "--role", "coordinator", "--config", str(config_path)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("migration", err)
        payload = json.loads(out)
        self.assertEqual(set(payload["peers"]), {self.worker, self.coord})
        self.assertEqual(payload["directory"][self.worker]["addrs"], ["/ip4/127.0.0.1/tcp/9"])

        # The repaired file loads, keeps the legacy metadata, and has no hint.
        config = load_config(config_path)
        self.assertIn(self.worker, config.peers)
        self.assertEqual(config.directory[self.worker].role, "worker")
        self.assertIsNone(config.migration_hint)


if __name__ == "__main__":
    unittest.main()
