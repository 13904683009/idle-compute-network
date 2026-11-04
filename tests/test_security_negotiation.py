"""D1: libp2p security is pinned to Noise/TLS and plaintext is refused.

Three assertions:
1. the transport's security registry contains EXACTLY Noise and TLS;
2. a real handshake negotiates one of those two, observed from both sides
   (``negotiated_security_protocol``), never plaintext;
3. a peer that only offers plaintext is refused (no downgrade).
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.custom_types import TProtocol
from libp2p.peer.peerinfo import PeerInfo
from libp2p.security.insecure.transport import PLAINTEXT_PROTOCOL_ID, InsecureTransport
from multiaddr import Multiaddr

from idle_compute.identity import NodeIdentity
from idle_compute.net_transport import SECURITY_PROTOCOLS, Libp2pTransport, TransportError

LOOPBACK = "/ip4/127.0.0.1/tcp/0"


def _make_reader(data: bytes):
    buf = bytearray(data)

    def read(n: int) -> bytes:
        chunk = bytes(buf[:n])
        del buf[:n]
        return chunk

    return read


class SecurityNegotiationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="sec-neg-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.a = NodeIdentity.load_or_create(root / "a" / "identity.key", node_name="a")
        self.b = NodeIdentity.load_or_create(root / "b" / "identity.key", node_name="b")

    def test_security_registry_is_noise_and_tls_only(self) -> None:
        transport = Libp2pTransport(self.a.key_pair, [LOOPBACK])
        try:
            options = transport._security_options()
        finally:
            transport.stop()
        offered = {str(protocol) for protocol in options}
        self.assertEqual(offered, set(SECURITY_PROTOCOLS))
        self.assertNotIn("/plaintext/2.0.0", offered)
        self.assertNotIn("/secio/1.0.0", offered)

    def test_handshake_negotiates_a_pinned_protocol_on_both_sides(self) -> None:
        ta = Libp2pTransport(self.a.key_pair, [LOOPBACK])
        tb = Libp2pTransport(self.b.key_pair, [LOOPBACK])
        self.addCleanup(ta.stop)
        self.addCleanup(tb.stop)
        got = []

        def echo(peer, read, write):
            data = read(4)
            got.append(data)
            write(b"pong")

        tb.set_handler(echo)
        ta.start()
        tb.start()
        ta.dial(self.b.node_id, tb.advertised_addrs())
        ta.send(self.b.node_id, b"ping")
        deadline = time.monotonic() + 15
        while not got and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(got, [b"ping"], "the stream must work over the pinned security protocol")

        for label, transport, peer in (
            ("initiator", ta, self.b.node_id),
            ("responder", tb, self.a.node_id),
        ):
            negotiated = transport.negotiated_security_protocol(peer)
            self.assertIn(
                negotiated,
                SECURITY_PROTOCOLS,
                f"{label} negotiated {negotiated!r}, which is not one of {SECURITY_PROTOCOLS}",
            )

    def test_plaintext_only_peer_is_refused(self) -> None:
        """A pinned dialer must fail instead of falling back to plaintext."""
        plaintext = _PlaintextHost()
        self.addCleanup(plaintext.stop)
        plaintext.start()
        self.assertTrue(plaintext.ready.wait(20), "plaintext host did not start")

        transport = Libp2pTransport(self.a.key_pair, [LOOPBACK])
        self.addCleanup(transport.stop)
        transport.start()
        with self.assertRaises(TransportError):
            transport.dial(plaintext.peer_id, plaintext.addrs)
        self.assertIsNone(
            transport.negotiated_security_protocol(plaintext.peer_id),
            "no connection may be established without a pinned security protocol",
        )


class _PlaintextHost:
    """A libp2p host that offers ONLY /plaintext/2.0.0 (negative test fixture)."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self._stop = threading.Event()
        self.peer_id = ""
        self.addrs: list[str] = []
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="plaintext-host", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        async def main() -> None:
            key_pair = create_new_key_pair()
            host = new_host(
                key_pair=key_pair,
                listen_addrs=[Multiaddr(LOOPBACK)],
                sec_opt={TProtocol(PLAINTEXT_PROTOCOL_ID): InsecureTransport(key_pair)},
            )
            async with host.run([Multiaddr(LOOPBACK)]):
                self.peer_id = host.get_id().to_string()
                self.addrs = [str(addr) for addr in host.get_addrs()]
                self.ready.set()
                while not self._stop.is_set():
                    await trio.sleep(0.1)

        trio.run(main)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
