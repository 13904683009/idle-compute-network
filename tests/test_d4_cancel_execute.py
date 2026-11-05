"""D4 gap 3: CANCEL vs EXECUTE semantics.

Semantics under test (D4 §8.2):
- a lease cancelled before execution must never start (worker-side gate);
- once ACTIVE, the API refuses to pretend it can interrupt a running task;
- a barrier race between accept and cancel always ends CANCELLED, never ACTIVE.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from idle_compute.config import from_dict
from idle_compute.db import InvalidNegotiationState, Store
from idle_compute.identity import NodeIdentity
from idle_compute.lease import TaskLeaseTerms, terms_hash
from idle_compute.node import Node
from idle_compute.protocol import MessageType

NOW = int(time.time())


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _terms(requester, worker, *, lease_id, task_id):
    return TaskLeaseTerms(
        lease_id=lease_id,
        task_id=task_id,
        requester_node_id=requester.node_id,
        worker_node_id=worker.node_id,
        task_spec_hash="a" * 64,
        input_manifest_hash="",
        lease_version=1,
        issued_at=NOW,
        expires_at=NOW + 3600,
        execution_deadline=NOW + 3600,
        verification_policy={"mode": "RECOMPUTE", "required": False},
        reward_policy={"amount": 1},
    ).sign_proposal(requester)


class WorkerCancelGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-cancel-node-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.worker = NodeIdentity.load_or_create(self.root / "w" / "identity.key", node_name="w")
        self.node = Node(
            from_dict(
                {
                    "role": "worker",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(self.root / "worker-data"),
                    "peers": [self.coord.node_id],
                    "coordinator_node_id": self.coord.node_id,
                    "coordinator_addr": "127.0.0.1:1",
                    "log_level": "ERROR",
                }
            )
        )
        self.addCleanup(self.node.shutdown)

    def _cancel(self, lease_id: str) -> None:
        env = SimpleNamespace(
            type=MessageType.LEASE_CANCEL,
            body={"lease_id": lease_id, "reason": "test"},
            sender=self.coord.node_id,
            seq=1,
        )
        self.assertTrue(self.node._on_envelope(env, lambda _frame: None))

    def test_cancel_before_execution_blocks_the_gate(self) -> None:
        self.assertTrue(self.node._may_execute("lea_before"))
        self._cancel("lea_before")
        self.assertFalse(self.node._may_execute("lea_before"))
        self.assertTrue(self.node._may_execute("lea_other"), "cancel is lease-scoped")

    def test_duplicate_cancel_is_idempotent(self) -> None:
        self._cancel("lea_dup")
        self._cancel("lea_dup")
        self.assertFalse(self.node._may_execute("lea_dup"))

    def test_cancel_does_not_touch_unrelated_leases(self) -> None:
        self._cancel("lea_one")
        self.assertTrue(self.node._may_execute("lea_two"))


class CancelStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-cancel-store-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.worker = NodeIdentity.load_or_create(self.root / "w" / "identity.key", node_name="w")
        self.db = self.root / "coordinator.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)
        self.task = self.store.create_task("numeric_batch_v1", {"numbers": [1]})
        self.lease = self.store.claim_task(self.worker.node_id, self.task.task_id)
        assert self.lease is not None
        self.terms = _terms(
            self.coord, self.worker, lease_id=self.lease.lease_id, task_id=self.task.task_id
        )
        self.store.create_negotiation_for_lease(
            lease_id=self.lease.lease_id,
            task_id=self.task.task_id,
            requester_id=self.coord.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.coord.node_id,
            terms_json="{}",
            terms_hash=terms_hash(self.terms),
            requester_signature=self.terms.requester_signature,
            expires_at=_iso(NOW + 3600),
        )

    def test_cancel_after_active_is_refused(self) -> None:
        self.store.accept_negotiation(
            self.lease.lease_id, self.worker.node_id, "a" * 128, "{}", now=NOW
        )
        self.store.activate_negotiation(self.lease.lease_id, now=_iso(NOW))
        with self.assertRaises(InvalidNegotiationState):
            self.store.cancel_negotiation(self.lease.lease_id, "interrupt", now=_iso(NOW))
        self.assertEqual(self.store.get_negotiation(self.lease.lease_id)["state"], "ACTIVE")

    def test_concurrent_accept_and_cancel_ends_cancelled(self) -> None:
        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}
        lock = threading.Lock()

        def accept() -> None:
            store = Store(self.db, self.key)
            try:
                barrier.wait(timeout=10)
                try:
                    store.accept_negotiation(
                        self.lease.lease_id, self.worker.node_id, "a" * 128, "{}", now=NOW
                    )
                    with lock:
                        outcomes["accept"] = "ACCEPTED"
                except InvalidNegotiationState as exc:
                    with lock:
                        outcomes["accept"] = exc
            finally:
                store.close()

        def cancel() -> None:
            store = Store(self.db, self.key)
            try:
                barrier.wait(timeout=10)
                try:
                    store.cancel_negotiation(self.lease.lease_id, "race", now=_iso(NOW))
                    with lock:
                        outcomes["cancel"] = "CANCELLED"
                except InvalidNegotiationState as exc:
                    with lock:
                        outcomes["cancel"] = exc
            finally:
                store.close()

        threads = [threading.Thread(target=accept), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(self.store.get_negotiation(self.lease.lease_id)["state"], "CANCELLED")
        with self.assertRaises(InvalidNegotiationState):
            self.store.activate_negotiation(self.lease.lease_id, now=_iso(NOW))
        with self.assertRaises(InvalidNegotiationState):
            self.store.commit_negotiation(self.lease.lease_id, now=_iso(NOW))


if __name__ == "__main__":
    unittest.main()
