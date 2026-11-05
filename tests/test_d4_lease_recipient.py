"""D4 gap 5: node-level lease recipient / broadcast rejection.

Uses the same proven ManualPeer harness as test_node's recipient test, but
sends LEASE_* messages: a lease message addressed to another node is
UNAUTHORIZED; a directed lease message with recipient=null is INVALID_BODY.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from idle_compute.config import from_dict
from idle_compute.identity import NodeIdentity
from idle_compute.node import Node
from idle_compute.protocol import ErrorCode, MessageType, build_envelope, encode_envelope

try:
    from test_node import ManualPeer
except ImportError:  # pragma: no cover
    from tests.test_node import ManualPeer


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"coordinator never listened on {port}")


class NodeLeaseRecipientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-recipient-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.peer_identity = NodeIdentity.load_or_create(self.root / "p" / "identity.key", node_name="p")
        self.other = NodeIdentity.load_or_create(self.root / "o" / "identity.key", node_name="o")
        self.port = _free_port()
        self.node = Node(
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": self.port,
                    "data_dir": str(self.root / "coord"),
                    "peers": [self.peer_identity.node_id, self.other.node_id],
                    "poll_interval_seconds": 1,
                    "log_level": "ERROR",
                }
            )
        )
        self.thread = threading.Thread(target=self.node.run, name="d4-recipient-coord", daemon=True)
        self.thread.start()
        _wait_port(self.port)
        self.peer = ManualPeer(self.peer_identity)
        self.addCleanup(self.peer.stop)
        self.peer.connect(self.node.identity.node_id, f"127.0.0.1:{self.port}")
        self.peer.hello()
        self.assertIsNotNone(self.peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 10.0))
        self.addCleanup(self.node.shutdown)

    def _send_null_recipient(self, type_: MessageType, body: dict) -> None:
        self.peer._seq += 1
        frame = encode_envelope(
            build_envelope(
                type_,
                self.peer_identity.node_id,
                self.peer_identity.sign,
                recipient=None,
                seq=self.peer._seq,
                body=body,
            )
        )
        self.peer.send(frame)

    def test_lease_accept_addressed_to_another_node_is_unauthorized(self) -> None:
        self.peer.send_craft(
            MessageType.LEASE_ACCEPT,
            {"lease_id": "lea_x", "terms_hash": "a" * 64, "acceptance": {}},
            recipient=self.other.node_id,
        )
        self.assertIsNotNone(self.peer.expect_error(ErrorCode.UNAUTHORIZED, 10.0))

    def test_lease_accept_without_recipient_is_invalid(self) -> None:
        self._send_null_recipient(
            MessageType.LEASE_ACCEPT,
            {"lease_id": "lea_x", "terms_hash": "a" * 64, "acceptance": {}},
        )
        self.assertIsNotNone(self.peer.expect_error(ErrorCode.INVALID_BODY, 10.0))

    def test_broadcast_lease_propose_is_invalid(self) -> None:
        self._send_null_recipient(
            MessageType.LEASE_PROPOSE,
            {
                "lease_id": "lea_x",
                "task_id": "tsk_x",
                "terms": {},
                "task": {"task_type": "numeric_batch_v1", "payload": {}},
            },
        )
        self.assertIsNotNone(self.peer.expect_error(ErrorCode.INVALID_BODY, 10.0))


if __name__ == "__main__":
    unittest.main()
