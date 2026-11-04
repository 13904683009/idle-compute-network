from __future__ import annotations

import re
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

    def test_libp2p_imports_are_confined_to_two_modules(self) -> None:
        # F-3: all reverse-engineered libp2p API usage stays in the two pinned
        # modules; every other file only mentions the name in docstrings.
        allowed = {"identity.py", "net_transport.py"}
        findings = []
        for path in PACKAGE.glob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if re.match(r"^(from|import)\s+libp2p\b", stripped) and path.name not in allowed:
                    findings.append(f"{path.name}:{lineno}: {stripped}")
        self.assertEqual(findings, [], findings)

    def test_no_asyncio_anywhere_in_the_package(self) -> None:
        # The eval proved the asyncio bridge crashes; the runtime is trio-native.
        findings = [
            path.name
            for path in PACKAGE.glob("*.py")
            if re.search(r"^\s*(import|from)\s+asyncio\b", path.read_text(encoding="utf-8"), re.M)
        ]
        self.assertEqual(findings, [], findings)

    def test_task_dispatch_goes_only_through_the_registry(self) -> None:
        # The registry is defined once; no other module touches it.
        mentions = {
            path.name for path in PACKAGE.glob("*.py") if "TASK_TYPES" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(mentions, {"tasks.py"}, "TASK_TYPES must live in tasks.py only")

        # The Phase-2 runtime must resolve executors through get_task_type();
        # only the Phase-1 local orchestrator (coordinator.py) and the store's
        # validation helper are allowed to name the concrete task class.
        for name in ("worker.py", "node.py", "net_transport.py", "transport_tcp.py"):
            text = (PACKAGE / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "NumericBatchTask", text, f"{name} must resolve tasks through the registry"
            )
        for name in ("db.py", "worker.py"):
            self.assertIn(
                "get_task_type",
                (PACKAGE / name).read_text(encoding="utf-8"),
                f"{name} must resolve the executor through the registry",
            )


if __name__ == "__main__":
    unittest.main()
