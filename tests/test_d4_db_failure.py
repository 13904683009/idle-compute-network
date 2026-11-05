"""D4 gap 4: DB write-failure injection.

Two levels:
- Store: a broken connection raises (never silently reports success) and the
  persisted state is unchanged when the DB is reopened.
- Node: when acceptance/activation raises, the coordinator replies ERROR and
  NEVER sends LEASE_COMMIT, so the worker cannot obtain an execution
  authorization from a failed write.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from idle_compute.config import from_dict
from idle_compute.db import Store
from idle_compute.identity import NodeIdentity
from idle_compute.lease import TaskLeaseTerms, terms_hash
from idle_compute.node import Node
from idle_compute.protocol import MessageType

NOW = int(time.time())


class FailingStore:
    """Delegates reads; fails the configured write method."""

    def __init__(self, inner: Store, fail_on: str) -> None:
        self.inner = inner
        self.fail_on = fail_on

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def accept_negotiation(self, *args, **kwargs):
        if self.fail_on == "accept_negotiation":
            raise sqlite3.OperationalError("injected write failure")
        return self.inner.accept_negotiation(*args, **kwargs)

    def activate_negotiation(self, *args, **kwargs):
        if self.fail_on == "activate_negotiation":
            raise sqlite3.OperationalError("injected write failure")
        return self.inner.activate_negotiation(*args, **kwargs)


class StoreFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-db-fail-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.worker = NodeIdentity.load_or_create(self.root / "w" / "identity.key", node_name="w")
        self.db = self.root / "coordinator.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1]})
        lease = self.store.claim_task(self.worker.node_id, task.task_id)
        assert lease is not None
        self.lease_id = lease.lease_id
        self.task_id = task.task_id
        self.terms = TaskLeaseTerms(
            lease_id=lease.lease_id,
            task_id=task.task_id,
            requester_node_id=self.coord.node_id,
            worker_node_id=self.worker.node_id,
            task_spec_hash="a" * 64,
            input_manifest_hash="",
            lease_version=1,
            issued_at=NOW,
            expires_at=NOW + 60,
            execution_deadline=NOW + 60,
            verification_policy={"mode": "RECOMPUTE", "required": False},
            reward_policy={"amount": 1},
        ).sign_proposal(self.coord)
        self.store.create_negotiation_for_lease(
            lease_id=self.lease_id,
            task_id=self.task_id,
            requester_id=self.coord.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.coord.node_id,
            terms_json=json.dumps(self.terms.to_dict()),
            terms_hash=terms_hash(self.terms),
            requester_signature=self.terms.requester_signature,
            expires_at="2099-01-01T00:00:00Z",
        )

    def test_broken_store_raises_and_leaves_state_proposed(self) -> None:
        self.store.conn.close()
        with self.assertRaises(sqlite3.Error):
            self.store.accept_negotiation(self.lease_id, self.worker.node_id, "a" * 128, "{}")
        reopened = Store(self.db, self.key)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_negotiation(self.lease_id)["state"], "PROPOSED")


class NodeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-db-node-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.worker = NodeIdentity.load_or_create(self.root / "w" / "identity.key", node_name="w")
        self.db = self.root / "coordinator.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1]})
        lease = self.store.claim_task(self.worker.node_id, task.task_id)
        assert lease is not None
        self.lease_id = lease.lease_id
        self.task_id = task.task_id
        self.terms = TaskLeaseTerms(
            lease_id=lease.lease_id,
            task_id=task.task_id,
            requester_node_id=self.coord.node_id,
            worker_node_id=self.worker.node_id,
            task_spec_hash="a" * 64,
            input_manifest_hash="",
            lease_version=1,
            issued_at=NOW,
            expires_at=NOW + 60,
            execution_deadline=NOW + 60,
            verification_policy={"mode": "RECOMPUTE", "required": False},
            reward_policy={"amount": 1},
        ).sign_proposal(self.coord)
        self.store.create_negotiation_for_lease(
            lease_id=self.lease_id,
            task_id=self.task_id,
            requester_id=self.coord.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.coord.node_id,
            terms_json=json.dumps(self.terms.to_dict()),
            terms_hash=terms_hash(self.terms),
            requester_signature=self.terms.requester_signature,
            expires_at="2099-01-01T00:00:00Z",
        )
        self.addCleanup(self.store.close)
        self.accepted = self.terms.sign_accept(self.worker)
        self.node = Node(
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(self.root / "coord-data"),
                    "peers": [self.worker.node_id],
                    "log_level": "ERROR",
                }
            )
        )
        self.addCleanup(self.node.shutdown)

    def _env(self):
        return SimpleNamespace(
            type=MessageType.LEASE_ACCEPT,
            sender=self.worker.node_id,
            seq=1,
            body={
                "lease_id": self.lease_id,
                "terms_hash": terms_hash(self.terms),
                "acceptance": {
                    **self.accepted.acceptance(),
                    "signature": self.accepted.worker_signature,
                },
            },
        )

    def _run(self, fail_on: str):
        self.node._store = FailingStore(self.store, fail_on)
        frames: list[bytes] = []
        self.node._handle_coordinator(self._env(), frames.append)
        return frames

    def test_accept_write_failure_replies_error_without_commit(self) -> None:
        frames = self._run("accept_negotiation")
        self.assertEqual(len(frames), 1)
        text = frames[0].decode("utf-8", errors="replace")
        self.assertIn("ERROR", text)
        self.assertNotIn("LEASE_COMMIT", text)
        self.assertEqual(self.store.get_negotiation(self.lease_id)["state"], "PROPOSED")

    def test_activation_failure_replies_error_without_commit(self) -> None:
        frames = self._run("activate_negotiation")
        self.assertEqual(len(frames), 1)
        text = frames[0].decode("utf-8", errors="replace")
        self.assertIn("ERROR", text)
        self.assertNotIn("LEASE_COMMIT", text)
        row = self.store.get_negotiation(self.lease_id)
        self.assertEqual(row["state"], "ACCEPTED", "acceptance persisted, but never ACTIVE")


if __name__ == "__main__":
    unittest.main()
