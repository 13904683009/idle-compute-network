"""Smoke checks for the single-machine DEMO flow (no network, not D6)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DemoSmokeTests(unittest.TestCase):
    def test_demo_document_covers_both_paths(self) -> None:
        text = (ROOT / "DEMO.md").read_text(encoding="utf-8")
        self.assertIn("scripts/demo_loopback.py", text)
        self.assertIn("非跨设备", text)
        self.assertIn("credits.task_cost", text)
        self.assertIn("python -m idle_compute credit --db", text)
        self.assertIn("INSUFFICIENT_CREDIT", text)
        self.assertIn("D6", text)

    def test_demo_script_runs_and_shows_credit_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="demo-smoke-") as tmp:
            db = Path(tmp) / "idle.db"
            result = subprocess.run(
                [sys.executable, "scripts/demo_loopback.py", "--db", str(db), "--reset"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        out = result.stdout
        self.assertIn("LOOPBACK DEMO", out)
        self.assertIn("NOT cross-device", out)
        self.assertIn('"held": 1', out)
        self.assertIn('"spent": 1', out)
        self.assertIn('"refunded": 1', out)
        self.assertIn("INSUFFICIENT_CREDIT", out)
        self.assertIn("Demo finished", out)


if __name__ == "__main__":
    unittest.main()
