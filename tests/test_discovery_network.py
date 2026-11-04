"""D2-2: REAL two-process capability discovery over real TCP sockets.

Topology (no in-process shortcut for the exchange):

    worker subprocess (has capability config)
        --dial + HELLO over TCP-->  coordinator subprocess
        --CAPABILITY_ANNOUNCE------>

    test process (client Node, role=client)
        --dial + HELLO-->           coordinator
        --CAPABILITY_QUERY-------->  coordinator answers from its cache
        <--CAPABILITY_RESPONSE------  (directed reply, recipient = client)

The coordinator's cache is asserted through the wire answer and through its
persisted ``discovery.db`` state row.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from idle_compute.config import from_dict
from idle_compute.identity import NodeIdentity
from idle_compute.node import Node
from idle_compute.protocol import ErrorCode, MessageType

try:
    from helpers.peers import RawPeer
except ImportError:  # pragma: no cover
    from tests.helpers.peers import RawPeer

ROOT = Path(__file__).resolve().parent.parent
NODE_RUN = [sys.executable, "-m", "idle_compute", "node", "run", "--config"]

CAPABILITY = {
    "capability_version": 1,
    "ttl_seconds": 120,
    "supported_task_types": ["numeric_batch_v1"],
    "supported_runtimes": ["python_sandbox_v1"],
    "hardware": {"cpu_arch": "x86_64", "cpu_cores": 8, "ram_mb": 16384},
    "resource_limits": {
        "max_cpu_cores": 4,
        "max_ram_mb": 8192,
        "max_gpu_memory_mb": 0,
        "max_task_duration_seconds": 600,
    },
    "network": {"supported_transports": ["tcp"], "supports_relay": False},
    "verification": {"level": "SELF_DECLARED", "benchmark_profile": None},
    "available_resources": {"cpu_cores": 4, "ram_mb": 8192, "gpu_memory_mb": 0},
    "accepting_tasks": True,
}


def _identity(root: Path, name: str) -> NodeIdentity:
    return NodeIdentity.load_or_create(root / name / "identity.key", node_name=name)


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
    raise AssertionError(f"node never listened on 127.0.0.1:{port}")


def _spawn(config_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        NODE_RUN + [str(config_path)],
        cwd=str(ROOT),
        stdout=open(config_path.with_name("proc.log"), "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def _wait(predicate, timeout: float, message: str, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


class DiscoveryNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="discovery-net-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord_dir = self.root / "coord"
        self.worker_dir = self.root / "worker"
        self.client_dir = self.root / "client"
        for path in (self.coord_dir, self.worker_dir, self.client_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.coord_id = _identity(self.root, "coord")
        self.worker_id = _identity(self.root, "worker")
        self.client_id = _identity(self.root, "client")
        self.port = _free_port()

        self.coord_cfg = self.coord_dir / "coord.json"
        self.coord_cfg.write_text(
            json.dumps(
                from_dict(
                    {
                        "role": "coordinator",
                        "transport": "tcp",
                        "listen_host": "127.0.0.1",
                        "listen_port": self.port,
                        "data_dir": str(self.coord_dir),
                        "peers": [self.worker_id.node_id, self.client_id.node_id],
                        "poll_interval_seconds": 1,
                        "log_level": "DEBUG",
                        "log_file": str(self.coord_dir / "coordinator.log"),
                    }
                ).to_dict(),
                indent=2,
            ),
            encoding="utf-8",
        )
        self.worker_cfg = self.worker_dir / "worker.json"
        self.worker_cfg.write_text(
            json.dumps(
                from_dict(
                    {
                        "role": "worker",
                        "transport": "tcp",
                        "listen_host": "127.0.0.1",
                        "listen_port": 0,
                        "data_dir": str(self.worker_dir),
                        "peers": [self.coord_id.node_id],
                        "coordinator_node_id": self.coord_id.node_id,
                        "coordinator_addr": f"127.0.0.1:{self.port}",
                        "poll_interval_seconds": 1,
                        "backoff_min": 1,
                        "backoff_max": 4,
                        "log_level": "DEBUG",
                        "log_file": str(self.worker_dir / "worker.log"),
                        "capability": CAPABILITY,
                        "discovery": {"announce_interval_seconds": 5, "offline_after_seconds": 30},
                    }
                ).to_dict(),
                indent=2,
            ),
            encoding="utf-8",
        )

        self.coordinator = _spawn(self.coord_cfg)
        self.addCleanup(self.coordinator.wait, 10.0)
        self.addCleanup(self.coordinator.terminate)
        _wait_port(self.port)
        self.worker = _spawn(self.worker_cfg)
        self.addCleanup(self.worker.wait, 10.0)
        self.addCleanup(self.worker.terminate)

    def _client(self) -> Node:
        config = from_dict(
            {
                "role": "client",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.client_dir),
                "peers": [self.coord_id.node_id],
                "coordinator_node_id": self.coord_id.node_id,
                "coordinator_addr": f"127.0.0.1:{self.port}",
                "log_level": "WARNING",
            }
        )
        client = Node(config)
        self.addCleanup(client.shutdown)
        return client

    def _query(self, client: Node) -> list[dict]:
        try:
            return client.query_capabilities(
                self.coord_id.node_id, task_type="numeric_batch_v1", timeout=10.0
            )
        except Exception:
            return []

    def test_worker_announcement_reaches_the_coordinator_and_is_queryable(self) -> None:
        client = self._client()
        entries: list[dict] = []
        _wait(lambda: bool(self._query(client)), 45.0, "the worker announcement to be cached")
        entries = self._query(client)
        self.assertEqual(len(entries), 1, entries)
        entry = entries[0]
        self.assertEqual(entry["manifest"]["node_id"], self.worker_id.node_id)
        self.assertEqual(entry["manifest"]["capability_version"], 1)
        self.assertIn("numeric_batch_v1", entry["manifest"]["supported_task_types"])
        self.assertTrue(entry["announcement"]["accepting_tasks"])
        # Both objects are the SIGNED forms (128-hex signature present).
        self.assertEqual(len(entry["manifest"]["signature"]), 128)
        self.assertEqual(len(entry["announcement"]["signature"]), 128)
        # A filter that does not match yields nothing (no accidental match-all).
        self.assertEqual(client.query_capabilities(self.coord_id.node_id, task_type="onnx_inference_v1", timeout=10.0), [])

        # The coordinator persisted the anti-rollback state for the worker.
        db = Path(self.coord_dir) / "discovery.db"
        _wait(lambda: db.exists(), 10.0, "coordinator discovery.db")
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            row = conn.execute(
                "SELECT last_sequence, capability_version FROM peer_announcement_state WHERE node_id=?",
                (self.worker_id.node_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "coordinator must persist the accepted announcement state")
        self.assertGreaterEqual(int(row[0]), 1)
        self.assertEqual(int(row[1]), 1)

        # The worker is really a separate process and it is still alive.
        self.assertIsNone(self.worker.poll())

    def test_directed_query_recipient_rules_on_the_wire(self) -> None:
        """A directed query must name a recipient; a misaddressed one is refused."""
        peer = RawPeer(self.client_id, self.coord_id.node_id)
        self.addCleanup(peer.stop)
        peer.dial(f"127.0.0.1:{self.port}")
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 15.0))

        # recipient = another node -> UNAUTHORIZED
        peer.send_craft(
            MessageType.CAPABILITY_QUERY,
            {"task_type": None, "runtime": None},
            recipient=self.worker_id.node_id,
        )
        self.assertIsNotNone(peer.expect_error(ErrorCode.UNAUTHORIZED.value, 10.0))

        # recipient = null for a directed type -> INVALID_BODY
        from idle_compute.protocol import build_envelope, encode_envelope

        peer._seq += 1
        bare = encode_envelope(
            build_envelope(
                MessageType.CAPABILITY_QUERY,
                peer.identity.node_id,
                peer.identity.sign,
                recipient=None,
                seq=peer._seq,
                body={"task_type": None, "runtime": None},
            )
        )
        peer.send(bare)
        self.assertIsNotNone(peer.expect_error(ErrorCode.INVALID_BODY.value, 10.0))


if __name__ == "__main__":
    unittest.main()
