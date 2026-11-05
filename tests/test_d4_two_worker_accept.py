"""D4 gap 1: two workers concurrently ACCEPT the same lease.

Protocol semantics (D4): a lease proposal is addressed to exactly ONE worker;
``accept_negotiation`` rejects any other sender, and the store serializes
concurrent writers with BEGIN IMMEDIATE. This test uses two real Store
connections and a barrier, so the assertion does not depend on timing.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from idle_compute.db import InvalidNegotiationState, Store
from idle_compute.identity import NodeIdentity
from idle_compute.lease import TaskLeaseTerms, terms_hash

NOW = 1_800_000_000


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
        expires_at=NOW + 60,
        execution_deadline=NOW + 60,
        verification_policy={"mode": "RECOMPUTE", "required": False},
        reward_policy={"amount": 1},
    ).sign_proposal(requester)


class TwoWorkerAcceptRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-race-accept-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.worker_a = NodeIdentity.load_or_create(self.root / "a" / "identity.key", node_name="a")
        self.worker_b = NodeIdentity.load_or_create(self.root / "b" / "identity.key", node_name="b")
        self.db = self.root / "coordinator.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)
        self.task = self.store.create_task("numeric_batch_v1", {"numbers": [1]})
        self.lease = self.store.claim_task(self.worker_a.node_id, self.task.task_id)
        assert self.lease is not None
        self.terms = _terms(self.coord, self.worker_a, lease_id=self.lease.lease_id, task_id=self.task.task_id)
        self.store.create_negotiation_for_lease(
            lease_id=self.lease.lease_id,
            task_id=self.task.task_id,
            requester_id=self.coord.node_id,
            worker_id=self.worker_a.node_id,
            authority_id=self.coord.node_id,
            terms_json="{}",
            terms_hash=terms_hash(self.terms),
            requester_signature=self.terms.requester_signature,
            expires_at="2099-01-01T00:00:00Z",
        )

    def test_two_workers_accepting_the_same_lease_only_addressed_worker_wins(self) -> None:
        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}
        lock = threading.Lock()

        def accept(name: str, worker, signature: str) -> None:
            store = Store(self.db, self.key)
            try:
                barrier.wait(timeout=10)
                try:
                    row = store.accept_negotiation(
                        self.lease.lease_id, worker.node_id, signature, "{}", now=NOW
                    )
                    with lock:
                        outcomes[name] = row["state"]
                except InvalidNegotiationState as exc:
                    with lock:
                        outcomes[name] = exc
            finally:
                store.close()

        threads = [
            threading.Thread(target=accept, args=("A", self.worker_a, "a" * 128)),
            threading.Thread(target=accept, args=("B", self.worker_b, "b" * 128)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(outcomes.get("A"), "ACCEPTED")
        self.assertIsInstance(outcomes.get("B"), InvalidNegotiationState)
        row = self.store.get_negotiation(self.lease.lease_id)
        self.assertEqual(row["state"], "ACCEPTED")
        self.assertEqual(row["worker_signature"], "a" * 128, "the other worker's signature must not be stored")

    def test_second_worker_cannot_accept_even_sequentially(self) -> None:
        self.store.accept_negotiation(self.lease.lease_id, self.worker_a.node_id, "a" * 128, "{}", now=NOW)
        with self.assertRaises(InvalidNegotiationState):
            self.store.accept_negotiation(
                self.lease.lease_id, self.worker_b.node_id, "b" * 128, "{}", now=NOW
            )
        self.assertEqual(self.store.get_negotiation(self.lease.lease_id)["worker_signature"], "a" * 128)


if __name__ == "__main__":
    unittest.main()
