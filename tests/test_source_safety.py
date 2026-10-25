from __future__ import annotations

import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "idle_compute"
FORBIDDEN = (
    "eval(",
    "exec(",
    "os.system",
    "os.popen",
    "subprocess",
    "socket.",
    "requests.",
    "urllib",
    "pickle",
    "shell=True",
    "__import__",
    "importlib",
    "ctypes",
    "marshal",
    "compile(",
    "--dangerously",
)


class SourceSafetyTests(unittest.TestCase):
    def test_no_arbitrary_execution_or_network_tokens(self) -> None:
        findings = []
        for path in PACKAGE.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN:
                if token in text:
                    findings.append(f"{path.name}: {token}")
        self.assertEqual(findings, [], findings)


if __name__ == "__main__":
    unittest.main()
