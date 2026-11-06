"""Documentation checks for the remote-partner onboarding path."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RemotePartnerGuideTests(unittest.TestCase):
    def test_guide_has_commands_limits_and_disclaimer(self) -> None:
        text = (ROOT / "REMOTE-PARTNER-GUIDE.md").read_text(encoding="utf-8")
        required = [
            "REMOTE PARTNER GUIDE",
            "真实跨公网双设备验证尚未完成",
            "openssl req",
            "identity --config",
            "node run",
            "node create-task",
            "credit --db",
            "allow_public_listen",
            "server_hostname",
            "NAT 穿透",
            "对称 NAT",
            "INSUFFICIENT_CREDIT",
            "17700",
        ]
        for needle in required:
            self.assertIn(needle, text)

    def test_example_configs_are_valid_json_with_tls_and_placeholders(self) -> None:
        coordinator = json.loads(
            (ROOT / "examples" / "public-coordinator" / "node.json").read_text(encoding="utf-8")
        )
        worker = json.loads(
            (ROOT / "examples" / "public-worker" / "node.json").read_text(encoding="utf-8")
        )
        client = json.loads(
            (ROOT / "examples" / "public-client" / "node.json").read_text(encoding="utf-8")
        )

        self.assertEqual(coordinator["role"], "coordinator")
        self.assertEqual(coordinator["listen_host"], "0.0.0.0")
        self.assertTrue(coordinator["allow_public_listen"])
        self.assertIn("cert_file", coordinator["tls"])
        self.assertIn("key_file", coordinator["tls"])
        self.assertIn("<WORKER_NODE_ID>", coordinator["peers"])

        self.assertEqual(worker["role"], "worker")
        self.assertIn("ca_file", worker["tls"])
        self.assertIn("<PUBLIC_HOST>", worker["tls"]["server_hostname"])
        self.assertIn("numeric_batch_v1", worker["capability"]["supported_task_types"])
        self.assertTrue(worker["capability"]["accepting_tasks"])

        self.assertEqual(client["role"], "client")
        self.assertIn("ca_file", client["tls"])
        self.assertEqual(client["coordinator_addr"], "<PUBLIC_HOST>:17700")
        self.assertIn("<COORDINATOR_NODE_ID>", client["peers"])

        self.assertTrue((ROOT / "examples" / "README.md").is_file())
        self.assertIn("公网必须", (ROOT / "examples" / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
