"""Checks for the signed Base Sepolia deployment helper (no network/key use)."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy_node_registry_signed.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("deploy_signed", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SignedDeployScriptTests(unittest.TestCase):
    def test_help_and_key_sources(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("--private-key-file", "--private-key", "--rpc", "--dry-run"):
            self.assertIn(option, result.stdout)

        module = _load_module()
        with tempfile.TemporaryDirectory(prefix="deploy-key-") as tmp:
            key_file = Path(tmp) / "key"
            key_file.write_text("11" * 32, encoding="utf-8")
            key = module._load_private_key(
                argparse.Namespace(private_key=None, private_key_file=str(key_file))
            )
        self.assertEqual(key, "0x" + "11" * 32)

        with self.assertRaises(SystemExit):
            module._load_private_key(argparse.Namespace(private_key=None, private_key_file=None))

    def test_docs_mark_base_sepolia_status(self) -> None:
        text = (ROOT / "contracts" / "README.md").read_text(encoding="utf-8")
        self.assertIn("Base Sepolia", text)
        self.assertIn("faucet", text)
        self.assertIn("deploy_node_registry_signed.py", text)
        self.assertIn("尚未部署", text)


if __name__ == "__main__":
    unittest.main()
