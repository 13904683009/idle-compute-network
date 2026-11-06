"""D6 preparation checks: preflight script + design/evidence document.

These tests do NOT execute D6 and do not connect to another device.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class D6PreparationTests(unittest.TestCase):
    def test_design_document_is_preparation_only(self) -> None:
        text = (ROOT / "D6-DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("D6 仍为 NOT TESTED", text)
        self.assertIn("仅设计，未执行", text)
        self.assertIn("scripts/d6_preflight.py", text)
        self.assertIn("## 10. 单机预检", text)
        self.assertIn("## 11. D6 通过所需的最小证据集", text)
        self.assertIn("E1", text)
        self.assertIn("lease_negotiations", text)
        self.assertIn("credit_events", text)
        self.assertIn("时钟偏差", text)
        self.assertIn("负向 fail-closed", text)

    def test_preflight_passes_and_marks_d6_not_tested(self) -> None:
        with tempfile.TemporaryDirectory(prefix="d6-test-") as tmp:
            out = Path(tmp) / "preflight.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/d6_preflight.py",
                    "--json",
                    "--allow-dirty",
                    "--out",
                    str(out),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["d6_status"], "NOT TESTED")
        self.assertTrue(report["preflight_only"])
        ids = {check["id"] for check in report["checks"]}
        for required in (
            "python_version",
            "git_revision",
            "source_doc_gates",
            "critical_regressions",
            "db_schema",
            "config_examples",
            "public_listen_guard",
            "capability_signing",
            "port_bind",
            "clock_snapshot",
            "cross_device_clock_skew",
        ):
            self.assertIn(required, ids)
        self.assertGreaterEqual(report["summary"]["total"], 14)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertGreaterEqual(report["summary"]["warn"], 2)


if __name__ == "__main__":
    unittest.main()
