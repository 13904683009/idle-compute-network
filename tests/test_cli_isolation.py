"""A6: the local Phase-1 CLI execution path is not reachable from the network.

Two independent proofs:
1. Static: ``node.py`` never imports/constructs the local ``Coordinator`` and
   never calls its ``run_one``; ``coordinator.py`` does not import the wire
   protocol at all, so no inbound message can dispatch to it.
2. Semantic: a real coordinator's TASK_CREATE handler only queues a task
   (QUEUED, no result, no CREDIT); execution happens solely through the
   dual-signed worker path.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from idle_compute.config import from_dict
from idle_compute.db import Store
from idle_compute.identity import NodeIdentity
from idle_compute.node import Node
from idle_compute.protocol import MessageType

ROOT = Path(__file__).resolve().parent.parent


class LocalCliIsolationTests(unittest.TestCase):
    def test_node_and_coordinator_sources_are_not_linked(self) -> None:
        node_source = (ROOT / "idle_compute" / "node.py").read_text(encoding="utf-8")
        coord_source = (ROOT / "idle_compute" / "coordinator.py").read_text(encoding="utf-8")
        self.assertNotIn("from .coordinator import", node_source)
        self.assertNotIn("Coordinator(", node_source)
        self.assertNotIn("run_one", node_source)
        self.assertNotIn("MessageType", coord_source, "local CLI must not handle wire messages")
        self.assertNotIn("import protocol", coord_source)
        self.assertNotIn("from .protocol", coord_source)

    def test_network_task_create_only_queues_never_executes(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="a6-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        coord = NodeIdentity.load_or_create(root / "c" / "identity.key", node_name="c")
        client = NodeIdentity.load_or_create(root / "cl" / "identity.key", node_name="cl")
        node = Node(
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(root / "coord"),
                    "peers": [client.node_id],
                    "log_level": "ERROR",
                }
            )
        )
        self.addCleanup(node.shutdown)
        store = Store(root / "coord" / "coordinator.db", root / "coord" / "ledger.key")
        self.addCleanup(store.close)
        node._store = store
        env = SimpleNamespace(
            type=MessageType.TASK_CREATE,
            sender=client.node_id,
            seq=1,
            body={
                "client_ref": "a6-ref",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [1]},
                "max_attempts": 2,
                "lease_seconds": 30,
            },
        )
        frames: list[bytes] = []
        node._handle_task_create(env, frames.append)
        self.assertEqual(len(frames), 1)
        ack = json.loads(frames[0][4:].decode("utf-8"))
        self.assertEqual(ack["type"], "TASK_CREATE_ACK")
        task = store.get_task(ack["body"]["task_id"])
        self.assertEqual(task.status.value, "QUEUED")
        self.assertEqual(int(store.conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]), 0)
        self.assertEqual(int(store.conn.execute("SELECT COUNT(*) FROM credit_events").fetchone()[0]), 0)


if __name__ == "__main__":
    unittest.main()
