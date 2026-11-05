"""D4 gap 2: ACCEPT vs EXPIRE determinism.

The store expires negotiations inside the same transaction that accepts them
(``BEGIN IMMEDIATE`` + ``_expire_locked``), so exactly one of the two outcomes
is possible and the negotiation can never become ACTIVE after expiry.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from idle_compute.db import InvalidNegotiationState, LeaseExpired, Store
from idle_compute.identity import NodeIdentity
from idle_compute.lease import TaskLeaseTerms, terms_hash

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


class AcceptExpireRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-race-expire-")
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

    def _propose(self, *, expires_at: str):
        return self.store.create_negotiation_for_lease(
            lease_id=self.lease.lease_id,
            task_id=self.task.task_id,
            requester_id=self.coord.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.coord.node_id,
            terms_json="{}",
            terms_hash=terms_hash(self.terms),
            requester_signature=self.terms.requester_signature,
            expires_at=expires_at,
        )

    def test_accept_at_exact_expiry_is_rejected(self) -> None:
        self._propose(expires_at=_iso(NOW + 60))
        with self.assertRaises(InvalidNegotiationState):
            self.store.accept_negotiation(
                self.lease.lease_id, self.worker.node_id, "a" * 128, "{}", now=_iso(NOW + 60)
            )
        # Rejection rolls the transaction back; the NEXT transaction must still
        # expire it (lazy expiry), and it can never become ACTIVE.
        self.store.expire_negotiations(now=_iso(NOW + 60))
        self.assertEqual(self.store.get_negotiation(self.lease.lease_id)["state"], "EXPIRED")
        with self.assertRaises(InvalidNegotiationState):
            self.store.activate_negotiation(self.lease.lease_id, now=_iso(NOW + 61))

    def test_accept_then_expire_cannot_activate(self) -> None:
        self._propose(expires_at=_iso(NOW + 60))
        self.store.accept_negotiation(
            self.lease.lease_id, self.worker.node_id, "a" * 128, "{}", now=_iso(NOW)
        )
        expired = self.store.expire_negotiations(now=_iso(NOW + 60))
        self.assertEqual(expired, 1)
        self.assertEqual(self.store.get_negotiation(self.lease.lease_id)["state"], "EXPIRED")
        with self.assertRaises(InvalidNegotiationState):
            self.store.activate_negotiation(self.lease.lease_id, now=_iso(NOW + 61))
        with self.assertRaises(InvalidNegotiationState):
            self.store.commit_negotiation(self.lease.lease_id, now=_iso(NOW + 61))

    def test_concurrent_accept_and_expire_never_produce_active(self) -> None:
        self._propose(expires_at=_iso(NOW + 60))
        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}
        lock = threading.Lock()

        def accept() -> None:
            store = Store(self.db, self.key)
            try:
                barrier.wait(timeout=10)
                try:
                    store.accept_negotiation(
                        self.lease.lease_id, self.worker.node_id, "a" * 128, "{}", now=_iso(NOW)
                    )
                    with lock:
                        outcomes["accept"] = "ACCEPTED"
                except InvalidNegotiationState as exc:
                    with lock:
                        outcomes["accept"] = exc
            finally:
                store.close()

        def expire() -> None:
            store = Store(self.db, self.key)
            try:
                barrier.wait(timeout=10)
                store.expire_negotiations(now=_iso(NOW + 60))
                with lock:
                    outcomes["expire"] = "RAN"
            finally:
                store.close()

        threads = [threading.Thread(target=accept), threading.Thread(target=expire)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        row = self.store.get_negotiation(self.lease.lease_id)
        self.assertEqual(row["state"], "EXPIRED")
        self.assertTrue(outcomes.get("accept") == "ACCEPTED" or isinstance(outcomes.get("accept"), InvalidNegotiationState))
        with self.assertRaises(InvalidNegotiationState):
            self.store.activate_negotiation(self.lease.lease_id, now=_iso(NOW + 61))
        # No ACTIVE negotiation may exist for the task.
        active = self.store.conn.execute(
            "SELECT COUNT(*) FROM lease_negotiations WHERE task_id=? AND state='ACTIVE'",
            (self.task.task_id,),
        ).fetchone()[0]
        self.assertEqual(active, 0)

    def test_expired_lease_also_prevents_accept(self) -> None:
        self._propose(expires_at=_iso(NOW + 3600))
        # The underlying DB lease (30s) is gone by then; accept must fail.
        with self.assertRaises((InvalidNegotiationState, LeaseExpired)):
            self.store.accept_negotiation(
                self.lease.lease_id, self.worker.node_id, "a" * 128, "{}", now=_iso(NOW + 86400)
            )


if __name__ == "__main__":
    unittest.main()
