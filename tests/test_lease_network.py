"""D4: REAL two-process dual-signed lease flow (coordinator + worker over TCP).

Topology:
    worker subprocess --CLAIM_REQUEST--> coordinator subprocess
                      <--LEASE_PROPOSE (signed terms)--
                      --LEASE_ACCEPT (worker signature)-->
                      <--LEASE_COMMIT (coordinator signature)--
                      -> execute ONLY with the verified authorization
                      --SUBMIT_RESULT--> validation + credit

The test also asserts the negotiation reached ACTIVE in the coordinator DB and
that a task whose lease never commits is not executed (worker rejects).
"""

from __future__ import annotations

import json
import os
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

ROOT = Path(__file__).resolve().parent.parent
NODE_RUN = [sys.executable, "-m", "idle_compute", "node", "run", "--config"]


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


class DualSignedLeaseNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="lease-net-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        keep = os.environ.get("LEASE_NET_DIR")
        self.root = Path(keep) if keep else Path(self._tmp.name)
        self.coord_dir = self.root / "coord"
        self.worker_dir = self.root / "worker"
        self.client_dir = self.root / "client"
        for path in (self.coord_dir, self.worker_dir, self.client_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.coord = _identity(self.root, "coord")
        self.worker = _identity(self.root, "worker")
        self.client = _identity(self.root, "client")
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
                        "peers": [self.worker.node_id, self.client.node_id],
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
                        "peers": [self.coord.node_id],
                        "coordinator_node_id": self.coord.node_id,
                        "coordinator_addr": f"127.0.0.1:{self.port}",
                        "poll_interval_seconds": 1,
                        "backoff_min": 1,
                        "backoff_max": 4,
                        "lease": {"proposal_timeout_seconds": 10},
                        "log_level": "DEBUG",
                        "log_file": str(self.worker_dir / "worker.log"),
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
        self.worker_proc = _spawn(self.worker_cfg)
        self.addCleanup(self.worker_proc.wait, 10.0)
        self.addCleanup(self.worker_proc.terminate)

    def _client(self) -> Node:
        config = from_dict(
            {
                "role": "client",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.client_dir),
                "peers": [self.coord.node_id],
                "coordinator_node_id": self.coord.node_id,
                "coordinator_addr": f"127.0.0.1:{self.port}",
                "log_level": "WARNING",
            }
        )
        node = Node(config)
        self.addCleanup(node.shutdown)
        return node

    def test_dual_signed_lease_flow_executes_and_settles(self) -> None:
        client = self._client()
        ack = client.client_create_task({"numbers": [1, 2, 3]}, max_attempts=2, lease_seconds=30)
        task_id = ack["task_id"]

        db_path = self.coord_dir / "coordinator.db"
        deadline = time.monotonic() + 60.0
        status = None
        while time.monotonic() < deadline:
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=10)
                try:
                    row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                finally:
                    conn.close()
                status = None if row is None else row[0]
                if status == "COMPLETED":
                    break
            time.sleep(0.3)
        self.assertEqual(status, "COMPLETED", f"task did not complete (status={status})")

        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            neg = conn.execute(
                "SELECT state FROM lease_negotiations WHERE task_id=?", (task_id,)
            ).fetchone()
            self.assertIsNotNone(neg, "the dual-signed negotiation must be persisted")
            self.assertEqual(neg[0], "ACTIVE")
            active = conn.execute(
                "SELECT COUNT(*) FROM leases WHERE task_id=? AND status='active'", (task_id,)
            ).fetchone()[0]
            self.assertEqual(active, 0, "the lease is completed, no ACTIVE lease may remain")
            credits = conn.execute(
                "SELECT COUNT(*) FROM credit_events WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            self.assertEqual(credits, 1, "exactly one credit event, no double settlement")
            result = conn.execute(
                "SELECT lease_id FROM results WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(result)
        self.assertEqual(self.worker_proc.poll(), None, "the worker process must still be alive")


if __name__ == "__main__":
    unittest.main()
