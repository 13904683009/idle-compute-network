from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from idle_compute.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "idle.db")
        self.key = str(Path(self._tmp.name) / "ledger.key")

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*args, "--db", self.db, "--key", self.key])
        return code, out.getvalue(), err.getvalue()

    def _json(self, text: str):
        return json.loads(text)

    def test_end_to_end_cli(self) -> None:
        code, out, _ = self._run("init")
        self.assertEqual(code, 0)
        self.assertTrue(self._json(out)["initialized"])

        code, out, _ = self._run("create-task", "--numbers", "1,2,3", "--max-attempts", "2", "--lease-seconds", "30")
        self.assertEqual(code, 0)
        task = self._json(out)
        task_id = task["task_id"]
        self.assertEqual(task["status"], "QUEUED")

        code, out, _ = self._run("run", "--peer", "peer-a", "--task-id", task_id)
        self.assertEqual(code, 0)
        run = self._json(out)
        self.assertEqual(run["status"], "valid")
        self.assertEqual(run["task"]["status"], "COMPLETED")

        code, out, _ = self._run("status", "--task-id", task_id)
        self.assertEqual(code, 0)
        status = self._json(out)
        self.assertEqual(status["task"]["status"], "COMPLETED")
        self.assertEqual(len(status["credits"]), 1)

        code, out, _ = self._run("events", "--peer", "peer-a")
        self.assertEqual(code, 0)
        events = self._json(out)["events"]
        self.assertEqual(len(events), 1)

        code, out, _ = self._run("verify-ledger")
        self.assertEqual(code, 0)
        self.assertTrue(self._json(out)["valid"])

        code, out, _ = self._run("rebuild-ledger")
        self.assertEqual(code, 0)
        self.assertEqual(self._json(out)["balances"], {"peer-a": 1})

        # Cancelling a terminal task must be a JSON error, not a traceback.
        code, _, err = self._run("cancel", "--task-id", task_id)
        self.assertEqual(code, 2)
        self.assertIn("InvalidTransition", err)

    def test_cancel_and_bad_input(self) -> None:
        self._run("init")
        code, out, _ = self._run("create-task", "--numbers", "5")
        self.assertEqual(code, 0)
        task_id = self._json(out)["task_id"]
        code, out, _ = self._run("cancel", "--task-id", task_id, "--reason", "cli-test")
        self.assertEqual(code, 0)
        self.assertEqual(self._json(out)["status"], "CANCELLED")

        code, _, err = self._run("create-task", "--numbers", "1,true")
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_corrupt_db_file_yields_structured_error(self) -> None:
        corrupt = Path(self._tmp.name) / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database" * 100)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["status", "--db", str(corrupt), "--key", self.key])
        self.assertEqual(code, 2)
        stderr = err.getvalue()
        self.assertIn("error", stderr)
        self.assertIn("DatabaseError", stderr)
        # The failed Store init must have closed the DB handle: the corrupt
        # file must be removable immediately (no Windows GC-dependent lock).
        corrupt.unlink()

    def test_tampered_ledger_yields_structured_errors_from_cli(self) -> None:
        self._run("init")
        code, out, _ = self._run("create-task", "--numbers", "1,2,3")
        task_id = self._json(out)["task_id"]
        self._run("run", "--peer", "peer-a", "--task-id", task_id)

        import sqlite3

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE credit_events SET event_type='evil' WHERE seq=1")
        conn.commit()
        conn.close()

        code, out, _ = self._run("verify-ledger")
        self.assertEqual(code, 0)
        verdict = self._json(out)
        self.assertFalse(verdict["valid"])
        self.assertTrue(any("event_type" in error for error in verdict["errors"]))

        code, out, _ = self._run("status", "--task-id", task_id)
        self.assertEqual(code, 0)
        self.assertTrue(self._json(out)["credits_errors"])

        code, out, _ = self._run("events")
        self.assertEqual(code, 0)
        self.assertTrue(self._json(out)["errors"])

    def test_main_module_import_has_no_side_effect(self) -> None:
        import importlib

        module = importlib.import_module("idle_compute.__main__")
        self.assertTrue(callable(module.main))


class IdentityCommandTests(unittest.TestCase):
    """B-4: identity command prints node_id only, is stable, never leaks keys."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = str(Path(self._tmp.name) / "n1" / "node.json")

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*args, "--config", self.config])
        return code, out.getvalue(), err.getvalue()

    def test_identity_prints_node_id_only_and_is_stable(self) -> None:
        code, out, _ = self._run("identity")
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(set(parsed.keys()), {"node_id"})
        node_id = parsed["node_id"]
        self.assertTrue(node_id.startswith("12D3KooW"))
        self.assertEqual(len(node_id), 52)
        # Running twice prints the same node_id (seed file reuse).
        _, out2, _ = self._run("identity")
        self.assertEqual(json.loads(out2)["node_id"], node_id)

    def test_deleting_identity_key_yields_different_node_id(self) -> None:
        _, out1, _ = self._run("identity")
        first = json.loads(out1)["node_id"]
        Path(self.config).parent.joinpath("identity.key").unlink()
        _, out2, _ = self._run("identity")
        second = json.loads(out2)["node_id"]
        self.assertNotEqual(first, second)

    def test_identity_never_prints_key_bytes(self) -> None:
        code, out, err = self._run("identity")
        self.assertEqual(code, 0)
        key_bytes = Path(self.config).parent.joinpath("identity.key").read_bytes()
        self.assertNotIn(key_bytes, out.encode("utf-8"))
        self.assertNotIn(key_bytes, err.encode("utf-8"))

    def test_legacy_init_and_run_never_print_ledger_key_bytes(self) -> None:
        for command in ("init", "run --peer peer-a"):
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(command.split() + ["--db", self.db_path(), "--key", self.key_path()])
            self.assertEqual(code, 0, (command, out.getvalue(), err.getvalue()))
            key_bytes = Path(self.key_path()).read_bytes()
            self.assertNotIn(key_bytes, out.getvalue().encode("utf-8"))
            self.assertNotIn(key_bytes, err.getvalue().encode("utf-8"))

    def db_path(self) -> str:
        return str(Path(self._tmp.name) / "legacy" / "idle.db")

    def key_path(self) -> str:
        return str(Path(self._tmp.name) / "legacy" / "ledger.key")


class PeersCommandTests(unittest.TestCase):
    """B-4: peers add/list manage static config entries; malformed ids rejected."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = str(Path(self._tmp.name) / "coordinator" / "node.json")
        # A real node id from a generated identity.
        identity_path = Path(self._tmp.name) / "worker1" / "identity.key"
        from idle_compute.identity import NodeIdentity

        self.node_id = NodeIdentity.load_or_create(identity_path, node_name="worker1").node_id

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*args, "--config", self.config])
        return code, out.getvalue(), err.getvalue()

    def test_peers_add_is_idempotent_and_list_roundtrips(self) -> None:
        code, out, _ = self._run("peers", "add", self.node_id, "--role", "worker")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["peers"], [self.node_id], "D2: peers is now a node-id list")
        self.assertEqual(payload["directory"][self.node_id]["role"], "worker")

        # Duplicate add is idempotent: same single entry, role updated.
        code, out, _ = self._run("peers", "add", self.node_id, "--role", "coordinator")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["peers"], [self.node_id])
        self.assertEqual(payload["directory"][self.node_id]["role"], "coordinator")

        code, out, _ = self._run("peers", "list")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), payload)

    def test_peers_add_appends_addr_without_duplicates(self) -> None:
        addr = "/ip4/127.0.0.1/tcp/12345"
        for _ in range(2):
            code, out, _ = self._run("peers", "add", self.node_id, "--role", "worker", "--addr", addr)
            self.assertEqual(code, 0)
        directory = json.loads(out)["directory"]
        self.assertEqual(directory[self.node_id]["addrs"], [addr])

    def test_peers_add_output_is_loadable_by_load_config(self) -> None:
        """D2 round-trip: what `peers add` writes must load as a NodeConfig."""
        from idle_compute.config import load_config

        Path(self.config).parent.mkdir(parents=True, exist_ok=True)
        Path(self.config).write_text(
            json.dumps(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "data_dir": "var/n1",
                    "peers": [],
                }
            ),
            encoding="utf-8",
        )
        for role in ("worker", "coordinator"):
            code, out, _ = self._run(
                "peers", "add", self.node_id, "--role", role, "--addr", "/ip4/127.0.0.1/tcp/1"
            )
            self.assertEqual(code, 0)
        config = load_config(self.config)
        self.assertIn(self.node_id, config.peers)
        self.assertEqual(config.directory[self.node_id].role, "coordinator")
        self.assertEqual(config.directory[self.node_id].addrs, ("/ip4/127.0.0.1/tcp/1",))
        self.assertIsNone(config.migration_hint)

    def test_malformed_node_id_is_rejected_with_structured_error(self) -> None:
        code, out, err = self._run("peers", "add", "bogus-id", "--role", "worker")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error", err)
        self.assertIn("malformed node_id", err)

    def test_peers_list_with_missing_config_is_empty(self) -> None:
        code, out, _ = self._run("peers", "list")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"peers": [], "directory": {}})


if __name__ == "__main__":
    unittest.main()
