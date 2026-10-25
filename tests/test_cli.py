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


if __name__ == "__main__":
    unittest.main()
