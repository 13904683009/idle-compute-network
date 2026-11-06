"""Tests for scripts/setup_public_certs.py (cert generation only, no network)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography import x509

ROOT = Path(__file__).resolve().parent.parent


class SetupPublicCertsTests(unittest.TestCase):
    def test_generates_cert_with_dns_and_ip_sans(self) -> None:
        with tempfile.TemporaryDirectory(prefix="certs-") as tmp:
            out = Path(tmp) / "certs"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/setup_public_certs.py",
                    "--host",
                    "coordinator.example.com",
                    "--ip",
                    "203.0.113.10",
                    "--ip",
                    "2001:db8::1",
                    "--out-dir",
                    str(out),
                    "--json",
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            report = json.loads(result.stdout)
            cert_path = Path(report["cert_file"])
            key_path = Path(report["key_file"])
            self.assertTrue(cert_path.is_file())
            self.assertTrue(key_path.is_file())
            self.assertEqual(report["server_hostname"], "coordinator.example.com")
            self.assertEqual(report["client_tls"]["ca_file"], str(cert_path))
            self.assertEqual(
                report["coordinator_tls"],
                {"cert_file": str(cert_path), "key_file": str(key_path)},
            )
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            self.assertIn("coordinator.example.com", san.get_values_for_type(x509.DNSName))
            self.assertIn(
                "203.0.113.10",
                [str(value) for value in san.get_values_for_type(x509.IPAddress)],
            )
            self.assertIn(
                "2001:db8::1",
                [str(value) for value in san.get_values_for_type(x509.IPAddress)],
            )

    def test_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory(prefix="certs-") as tmp:
            out = Path(tmp) / "certs"
            args = [
                sys.executable,
                "scripts/setup_public_certs.py",
                "--host",
                "203.0.113.20",
                "--out-dir",
                str(out),
            ]
            first = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
            self.assertEqual(first.returncode, 0, first.stderr[-2000:])
            second = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("refusing to overwrite", (second.stderr + second.stdout))
            forced = subprocess.run(
                args + ["--force"], cwd=str(ROOT), capture_output=True, text=True, timeout=120
            )
            self.assertEqual(forced.returncode, 0, forced.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
