"""Optional TLS for the TCP transport: handshake, verification, plaintext.

TLS is opt-in and stdlib-only at runtime (`ssl`). Certificates here are
generated with the project's existing `cryptography` dependency.
"""

from __future__ import annotations

import ipaddress
import queue
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from idle_compute.config import TlsConfig, ValidationError, from_dict
from idle_compute.node import Node
from idle_compute.transport_tcp import TcpTransport, TransportError


def make_cert(root: Path, name: str = "server") -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = root / f"{name}.crt"
    key_path = root / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class TlsTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tls-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cert, self.key = make_cert(self.root)
        self.server: TcpTransport | None = None
        self.client: TcpTransport | None = None

    def tearDown(self) -> None:
        for transport in (self.client, self.server):
            if transport is not None:
                transport.stop()

    def _server(self) -> tuple[TcpTransport, queue.Queue]:
        received: queue.Queue = queue.Queue()

        def handler(_peer_id, read, write):
            data = read(4)
            received.put(data)
            if data:
                write(b"PONG")

        server = TcpTransport(
            "server",
            "127.0.0.1",
            0,
            tls=TlsConfig(cert_file=str(self.cert), key_file=str(self.key)),
        )
        server.set_handler(handler)
        server.start()
        self.server = server
        return server, received

    def _client(self, tls: TlsConfig | None) -> TcpTransport:
        client = TcpTransport("client", "127.0.0.1", 0, tls=tls)

        def handler(_peer_id, read, _write):
            # Block on the stream so the transport keeps the pooled
            # connection alive (a no-op handler closes it immediately).
            try:
                read(4)
            except OSError:
                pass

        client.set_handler(handler)
        client.start()
        self.client = client
        return client

    def test_tls_handshake_and_roundtrip(self) -> None:
        server, received = self._server()
        client = self._client(
            TlsConfig(ca_file=str(self.cert), server_hostname="localhost")
        )
        client.dial("server", server.advertised_addrs())
        client.send("server", b"ping")
        self.assertEqual(received.get(timeout=10), b"ping")

    def test_tls_rejects_untrusted_server_name(self) -> None:
        server, _ = self._server()
        client = self._client(
            TlsConfig(ca_file=str(self.cert), server_hostname="wrong.example")
        )
        with self.assertRaises(TransportError) as ctx:
            client.dial("server", server.advertised_addrs())
        self.assertIn("TLS", str(ctx.exception))

    def test_plaintext_still_works_by_default(self) -> None:
        server, received = self._server_without_tls()
        client = self._client(None)
        client.dial("server", server.advertised_addrs())
        client.send("server", b"ping")
        self.assertEqual(received.get(timeout=10), b"ping")

    def _server_without_tls(self) -> tuple[TcpTransport, queue.Queue]:
        received: queue.Queue = queue.Queue()

        def handler(_peer_id, read, write):
            received.put(read(4))

        server = TcpTransport("server", "127.0.0.1", 0)
        server.set_handler(handler)
        server.start()
        self.server = server
        return server, received


class TlsConfigTests(unittest.TestCase):
    def test_coordinator_tls_roundtrip_and_requirements(self) -> None:
        cfg = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": "var/tls/coord",
                "peers": [],
                "tls": {"cert_file": "server.crt", "key_file": "server.key"},
            }
        )
        self.assertIsNotNone(cfg.tls)
        self.assertEqual(cfg.to_dict()["tls"]["cert_file"], "server.crt")
        with self.assertRaises(ValidationError):
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "data_dir": "var/tls/bad",
                    "peers": [],
                    "tls": {"ca_file": "ca.pem"},
                }
            )

    def test_tls_rejected_for_non_tcp_transport(self) -> None:
        with self.assertRaises(ValidationError):
            from_dict(
                {
                    "role": "worker",
                    "transport": "libp2p",
                    "data_dir": "var/tls/w",
                    "peers": [],
                    "tls": {"ca_file": "ca.pem"},
                }
            )

    def test_node_passes_tls_config_to_tcp_transport(self) -> None:
        captured: dict = {}

        class FakeTransport:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

            def set_handler(self, _handler):
                pass

            def stop(self):
                pass

        cfg = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": "var/tls/node",
                "peers": [],
                "tls": {"cert_file": "server.crt", "key_file": "server.key"},
            }
        )
        with tempfile.TemporaryDirectory(prefix="tls-node-") as tmp:
            cfg = from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(Path(tmp) / "coord"),
                    "peers": [],
                    "tls": {"cert_file": "server.crt", "key_file": "server.key"},
                }
            )
            with patch("idle_compute.node.TcpTransport", FakeTransport):
                node = Node(cfg)
                node.shutdown()
        self.assertEqual(captured["kwargs"]["tls"].cert_file, "server.crt")


if __name__ == "__main__":
    unittest.main()
