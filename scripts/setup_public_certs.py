"""Generate a self-signed TLS certificate for public Coordinator deployment.

Runs locally, uses the existing `cryptography` dependency, and writes:
  <out-dir>/server.crt  (server certificate; also usable as client ca_file)
  <out-dir>/server.key  (private key; keep secret)

Examples:
    python scripts/setup_public_certs.py --host coordinator.example.com --ip 203.0.113.10
    python scripts/setup_public_certs.py --host 203.0.113.10 --out-dir certs --force
    python scripts/setup_public_certs.py --interactive

This does NOT deploy anything and does NOT prove cross-internet validation.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _split_args(
    host: str | None, ip: str | list[str] | None
) -> tuple[list[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    dns_names: list[str] = []
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    extra_ips = [ip] if isinstance(ip, str) else (ip or [])
    for value, is_host in [(host, True), *((item, False) for item in extra_ips)]:
        if not value:
            continue
        candidate = value.strip()
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            if not is_host:
                raise SystemExit(f"not a valid IP address: {value!r}")
            if not _valid_dns_name(candidate):
                raise SystemExit(f"not a valid DNS name or IP: {value!r}")
            if candidate not in dns_names:
                dns_names.append(candidate)
        else:
            if address not in ips:
                ips.append(address)
    return dns_names, ips


def _valid_dns_name(value: str) -> bool:
    if len(value) > 253 or value.startswith(".") or value.endswith("."):
        return False
    labels = value.split(".")
    return all(
        0 < len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(char.isalnum() or char == "-" for char in label)
        for label in labels
    )


def generate(
    dns_names: list[str],
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
    *,
    days: int,
) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    common_name = dns_names[0] if dns_names else str(ips[0])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    sans: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
    sans.extend(x509.IPAddress(address) for address in ips)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def main() -> int:
    parser = argparse.ArgumentParser(description="generate a self-signed TLS certificate (public deployment)")
    parser.add_argument("--host", default=None, help="public DNS name or IP (also used as server_hostname)")
    parser.add_argument("--ip", action="append", default=None, help="additional IP SAN (repeatable)")
    parser.add_argument("--out-dir", default="certs", help="output directory (default: certs)")
    parser.add_argument("--days", type=int, default=365, help="validity in days (1..3650)")
    parser.add_argument("--force", action="store_true", help="overwrite existing cert/key")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    parser.add_argument("--interactive", action="store_true", help="prompt for host/IP/out-dir")
    args = parser.parse_args()

    if not 1 <= args.days <= 3650:
        parser.error("--days must be in 1..3650")

    if args.interactive or (not args.host and not args.ip and sys.stdin.isatty()):
        if not args.host:
            args.host = input("Public DNS name or IP: ").strip() or None
        if not args.ip:
            extra = input("Additional IP SAN (optional): ").strip()
            args.ip = [extra] if extra else None
        if args.out_dir == "certs":
            chosen = input("Output directory [certs]: ").strip()
            if chosen:
                args.out_dir = chosen

    dns_names, ips = _split_args(args.host, args.ip)
    if not dns_names and not ips:
        parser.error("provide at least one --host (DNS name or IP) or --ip")

    out_dir = Path(args.out_dir).resolve()
    cert_path = out_dir / "server.crt"
    key_path = out_dir / "server.key"
    if not args.force and (cert_path.exists() or key_path.exists()):
        raise SystemExit(
            f"refusing to overwrite {cert_path} / {key_path}; pass --force to replace"
        )

    cert_pem, key_pem = generate(dns_names, ips, days=args.days)
    out_dir.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass  # Windows: key confidentiality depends on directory ACLs

    server_hostname = dns_names[0] if dns_names else str(ips[0])
    san_text = [*dns_names, *(str(address) for address in ips)]
    report = {
        "cert_file": str(cert_path),
        "key_file": str(key_path),
        "server_hostname": server_hostname,
        "sans": san_text,
        "days": args.days,
        "coordinator_tls": {
            "cert_file": str(cert_path),
            "key_file": str(key_path),
        },
        "client_tls": {
            "ca_file": str(cert_path),
            "server_hostname": server_hostname,
        },
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Generated self-signed TLS certificate")
        print(f"  cert: {cert_path}")
        print(f"  key:  {key_path}  (keep secret)")
        print(f"  SANs: {', '.join(san_text)}")
        print(f"  validity: {args.days} days")
        print()
        print("Coordinator config (copy/paste):")
        print("  " + json.dumps(report["coordinator_tls"], ensure_ascii=False))
        print()
        print("Worker/Client config (copy/paste; server_hostname must match SAN):")
        print("  " + json.dumps(report["client_tls"], ensure_ascii=False))
        print()
        print("Notes:")
        print("- server.crt doubles as ca_file because this is self-signed.")
        print("- add --force to overwrite; add --ip for each extra SAN IP.")
        print("- this only generates files; no network/D6 validation is performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
