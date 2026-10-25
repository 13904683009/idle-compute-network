from __future__ import annotations

import unittest

from idle_compute.p2p import (
    LocalLoopbackTransport,
    LocalPeerDirectory,
    MessageType,
    NetworkTransportNotImplemented,
    PROTOCOL_VERSION,
    ProtocolMessage,
)


class P2PTests(unittest.TestCase):
    def test_loopback_roundtrip(self) -> None:
        transport = LocalLoopbackTransport()
        message = ProtocolMessage(PROTOCOL_VERSION, MessageType.TASK_ANNOUNCE, "peer-a", "task-1")
        transport.send("peer-b", message)
        self.assertEqual(transport.receive("peer-b"), message)
        self.assertIsNone(transport.receive("peer-b"))
        transport.close()

    def test_network_transport_is_explicitly_unimplemented(self) -> None:
        transport = NetworkTransportNotImplemented()
        with self.assertRaises(NotImplementedError):
            transport.send("peer-b", ProtocolMessage(PROTOCOL_VERSION, MessageType.LEASE_OFFER, "peer-a", "t"))
        with self.assertRaises(NotImplementedError):
            transport.receive("peer-b")

    def test_version_mismatch_rejected(self) -> None:
        transport = LocalLoopbackTransport()
        with self.assertRaises(ValueError):
            transport.send("peer-b", ProtocolMessage("9.9", MessageType.TASK_ANNOUNCE, "peer-a", "t"))

    def test_peer_directory(self) -> None:
        self.assertEqual(LocalPeerDirectory(["a", "b"]).list_peers(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
