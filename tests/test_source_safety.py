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

# Narrow, deliberate exemption (Phase C, C-4): transport_tcp.py is the one
# sanctioned raw-socket module — the stdlib TCP fallback transport. It is
# the ONLY file allowed to contain "socket."; every other token above stays
# forbidden in every file, including transport_tcp.py. The second test below
# pins that boundary so the exemption cannot silently spread.
SOCKET_ALLOWED_FILES = {"transport_tcp.py"}


class SourceSafetyTests(unittest.TestCase):
    def test_no_arbitrary_execution_or_network_tokens(self) -> None:
        findings = []
        for path in PACKAGE.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN:
                if token in text and not (token == "socket." and path.name in SOCKET_ALLOWED_FILES):
                    findings.append(f"{path.name}: {token}")
        self.assertEqual(findings, [], findings)

    def test_socket_exemption_stays_confined(self) -> None:
        # transport_tcp.py: sockets allowed, everything else still banned.
        tcp_path = PACKAGE / "transport_tcp.py"
        if not tcp_path.exists():
            self.skipTest("transport_tcp.py not yet created (C-4 pending)")
        tcp_text = tcp_path.read_text(encoding="utf-8")
        findings = [f"{tcp_path.name}: {t}" for t in FORBIDDEN if t != "socket." and t in tcp_text]
        # Every OTHER module keeps the full ban, including "socket.".
        for path in PACKAGE.glob("*.py"):
            if path.name in SOCKET_ALLOWED_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            if "socket." in text:
                findings.append(f"{path.name}: socket.")
        self.assertEqual(findings, [], findings)


if __name__ == "__main__":
    unittest.main()
