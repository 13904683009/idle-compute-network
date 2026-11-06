"""Public-deployment preparation: non-loopback bind/dial config and full flow.

This simulates the recommended public deployment on one machine:
coordinator binds 0.0.0.0 with allow_public_listen=true and worker/client dial
a real non-loopback local IPv4. It is NOT a real cross-internet test, does not
exercise NAT traversal/relay, and is not D6.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from idle_compute.config import ValidationError, from_dict

ROOT = Path(__file__).resolve().parent.parent
NODE_ID = "12D3KooW" + "A" * 44


def _lan_ipv4() -> str | None:
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return None
    candidates = [
        ip
        for ip in ips
        if not ip.startswith("127.")
        and not ip.startswith("169.254.")
        and ip != "0.0.0.0"
    ]
    return candidates[0] if candidates else None


class PublicConfigTests(unittest.TestCase):
    def test_coordinator_public_bind_requires_explicit_opt_in(self) -> None:
        base = {
            "role": "coordinator",
            "transport": "tcp",
            "listen_host": "0.0.0.0",
            "listen_port": 17700,
            "data_dir": "var/public/coord",
            "peers": [],
        }
        with self.assertRaises(ValidationError):
            from_dict(base)
        cfg = from_dict({**base, "allow_public_listen": True})
        self.assertEqual(cfg.listen_host, "0.0.0.0")
        self.assertTrue(cfg.allow_public_listen)

    def test_worker_and_client_accept_public_coordinator_address(self) -> None:
        worker = from_dict(
            {
                "role": "worker",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": "var/public/w",
                "peers": [NODE_ID],
                "coordinator_node_id": NODE_ID,
                "coordinator_addr": "203.0.113.10:17700",
            }
        )
        client = from_dict(
            {
                "role": "client",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": "var/public/c",
                "peers": [NODE_ID],
                "coordinator_node_id": NODE_ID,
                "coordinator_addr": "coordinator.example.com:17700",
            }
        )
        self.assertEqual(worker.coordinator_addr, "203.0.113.10:17700")
        self.assertEqual(client.coordinator_addr, "coordinator.example.com:17700")


class PublicStyleDeltaTest(unittest.TestCase):
    def test_public_style_full_flow_over_non_loopback_ip(self) -> None:
        ip = _lan_ipv4()
        if not ip:
            self.skipTest("no non-loopback IPv4 available in this environment")
        with tempfile.TemporaryDirectory(prefix="public-demo-") as tmp:
            root = Path(tmp) / "demo"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/demo_network_loopback.py",
                    "--dir",
                    str(root),
                    "--reset",
                    "--host",
                    ip,
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=300,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        out = result.stdout
        self.assertIn("PUBLIC-STYLE", out)
        self.assertIn(f"dial {ip}", out)
        self.assertIn("TASK_CREATE", out)
        self.assertIn("final negotiation state=COMPLETED", out)
        self.assertIn('"held": 1', out)
        self.assertIn('"spent": 1', out)
        self.assertIn("INSUFFICIENT_CREDIT", out)
        self.assertIn("NOT cross-device", out)


if __name__ == "__main__":
    unittest.main()
