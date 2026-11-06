"""Smoke test for the three-process network loopback demo (not D6)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NetworkDemoSmokeTests(unittest.TestCase):
    def test_demo_document_mentions_network_loopback(self) -> None:
        text = (ROOT / "DEMO.md").read_text(encoding="utf-8")
        self.assertIn("scripts/demo_network_loopback.py", text)
        self.assertIn("非跨设备", text)
        self.assertIn("非 D6", text)
        self.assertIn("credits.task_cost", text)

    def test_network_demo_runs_full_lease_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="net-demo-") as tmp:
            root = Path(tmp) / "demo"
            result = subprocess.run(
                [sys.executable, "scripts/demo_network_loopback.py", "--dir", str(root), "--reset"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=240,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        out = result.stdout
        self.assertIn("NETWORK DEMO", out)
        self.assertIn("NOT cross-device", out)
        self.assertIn("TASK_CREATE", out)
        self.assertIn("final negotiation state=COMPLETED", out)
        self.assertIn('"held": 1', out)
        self.assertIn('"spent": 1', out)
        self.assertIn("INSUFFICIENT_CREDIT", out)


if __name__ == "__main__":
    unittest.main()
