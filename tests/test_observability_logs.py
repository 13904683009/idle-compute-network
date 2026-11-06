"""Basic observability-log existence checks (no behavior assertions)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from idle_compute.config import from_dict
from idle_compute.db import Store
from idle_compute.identity import NodeIdentity
from idle_compute.net_transport import TransportError
from idle_compute.node import Node
from idle_compute.protocol import MessageType

NODE_ID = "12D3KooW" + "A" * 44


class ObservabilityLogTests(unittest.TestCase):
    def _node(self, root: Path, role: str = "coordinator", **overrides):
        data = {
            "role": role,
            "transport": "tcp",
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "data_dir": str(root / role),
            "peers": [NODE_ID],
            "log_level": "INFO",
        }
        data.update(overrides)
        return Node(from_dict(data))

    def test_startup_log_has_role_listen_and_tls_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="obs-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            node = self._node(root)
            self.addCleanup(node.shutdown)
            with self.assertLogs("idle_compute.node", level="INFO") as captured:
                node._ensure_transport_started()
            output = "\n".join(captured.output)
            self.assertIn("node started", output)
            self.assertIn("role=coordinator", output)
            self.assertIn("listen=127.0.0.1:", output)
            self.assertIn("tls=off", output)
            self.assertIn(node.identity.node_id, output)

    def test_task_created_log(self) -> None:
        with tempfile.TemporaryDirectory(prefix="obs-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            node = self._node(root)
            self.addCleanup(node.shutdown)
            store = Store(root / "coordinator" / "coordinator.db", root / "coordinator" / "ledger.key")
            self.addCleanup(store.close)
            node._store = store
            env = SimpleNamespace(
                type=MessageType.TASK_CREATE,
                sender=NODE_ID,
                seq=1,
                body={
                    "client_ref": "obs-ref",
                    "task_type": "numeric_batch_v1",
                    "payload": {"numbers": [1, 2]},
                    "max_attempts": 2,
                    "lease_seconds": 30,
                },
            )
            frames: list[bytes] = []
            with self.assertLogs("idle_compute.node", level="INFO") as captured:
                node._handle_task_create(env, frames.append)
            output = "\n".join(captured.output)
            self.assertIn("task tsk_", output)
            self.assertIn("CREATED", output)
            self.assertIn("status=QUEUED", output)

    def test_connection_failure_log_has_peer_and_reason(self) -> None:
        with tempfile.TemporaryDirectory(prefix="obs-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            node = self._node(
                root,
                role="worker",
                coordinator_node_id=NODE_ID,
                coordinator_addr="127.0.0.1:1",
            )
            self.addCleanup(node.shutdown)
            node._ensure_transport_started()
            with self.assertLogs("idle_compute.node", level="INFO") as captured:
                with self.assertRaises(TransportError):
                    node._ensure_connected()
            output = "\n".join(captured.output)
            self.assertIn("connection to coordinator", output)
            self.assertIn("127.0.0.1:1", output)


if __name__ == "__main__":
    unittest.main()
