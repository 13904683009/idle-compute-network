"""D4: two-party lease negotiation, commit and the execution gate.

Unit level (no network): the D1 signature model, the D4 coordinator commit,
the worker execution gate, and the Store negotiation state machine.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from idle_compute.db import (
    InvalidNegotiationState,
    LeaseExpired,
    NegotiationConflict,
    Store,
    TaskUnavailable,
)
from idle_compute.identity import NodeIdentity
from idle_compute.lease import (
    ExecutionAuthorization,
    LeaseCommit,
    LeaseError,
    TaskLeaseTerms,
    authorization_from_commit,
    require_authorization,
    terms_hash,
)
from idle_compute.models import Lease, LeaseStatus, Task, TaskStatus
from idle_compute.signing import Ed25519Policy
from idle_compute.worker import Worker

NOW = 1_800_000_000


def make_terms(requester, worker, *, lease_id="lea_1", task_id="task-1", now=NOW, ttl=60):
    return TaskLeaseTerms(
        lease_id=lease_id,
        task_id=task_id,
        requester_node_id=requester.node_id,
        worker_node_id=worker.node_id,
        task_spec_hash="a" * 64,
        input_manifest_hash="",
        lease_version=1,
        issued_at=now,
        expires_at=now + ttl,
        execution_deadline=now + ttl,
        verification_policy={"mode": "RECOMPUTE", "required": False},
        reward_policy={"amount": 1},
    )


class LeaseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lease-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.requester = NodeIdentity.load_or_create(self.root / "req" / "identity.key", node_name="req")
        self.worker_id = NodeIdentity.load_or_create(self.root / "wrk" / "identity.key", node_name="wrk")
        self.other = NodeIdentity.load_or_create(self.root / "oth" / "identity.key", node_name="oth")

    def signed_terms(self, **kwargs):
        terms = make_terms(self.requester, self.worker_id, **kwargs).sign_proposal(self.requester)
        return terms

    def commit_for(self, terms, *, ttl=60, now=NOW):
        return LeaseCommit(
            lease_id=terms.lease_id,
            task_id=terms.task_id,
            requester_node_id=terms.requester_node_id,
            worker_node_id=terms.worker_node_id,
            terms_hash=terms_hash(terms),
            committed_at=now,
            expires_at=now + ttl,
        ).sign(self.requester)


class SignatureTests(LeaseTestBase):
    def test_full_two_party_flow_verifies(self) -> None:
        terms = self.signed_terms()
        accepted = terms.sign_accept(self.worker_id)
        accepted.verify_accept(NOW)
        accepted.verify_for_worker(self.worker_id.node_id, NOW)
        commit = self.commit_for(accepted)
        commit.verify(NOW)
        self.assertEqual(commit.terms_hash, terms_hash(accepted))

    def test_tampered_terms_break_the_requester_signature(self) -> None:
        terms = self.signed_terms()
        tampered = replace(terms, task_spec_hash="b" * 64)
        with self.assertRaises(LeaseError) as ctx:
            tampered.verify_proposal(NOW)
        self.assertEqual(ctx.exception.code, LeaseError.SIGNATURE)

    def test_accept_cannot_be_replayed_onto_other_terms(self) -> None:
        terms = self.signed_terms()
        accepted = terms.sign_accept(self.worker_id)
        other = replace(accepted, lease_id="lea_2")  # acceptance stays valid for lea_1 only
        with self.assertRaises(LeaseError) as ctx:
            other.verify_accept(NOW)
        self.assertEqual(ctx.exception.code, LeaseError.SIGNATURE)

    def test_accept_replay_onto_another_task(self) -> None:
        terms = self.signed_terms()
        accepted = terms.sign_accept(self.worker_id)
        other = replace(accepted, task_id="task-2")
        with self.assertRaises(LeaseError) as ctx:
            other.verify_accept(NOW)
        self.assertEqual(ctx.exception.code, LeaseError.SIGNATURE)

    def test_wrong_worker_cannot_accept(self) -> None:
        terms = self.signed_terms()
        with self.assertRaises(LeaseError) as ctx:
            terms.sign_accept(self.other)
        self.assertEqual(ctx.exception.code, LeaseError.WRONG_WORKER)

    def test_expired_commit_is_rejected(self) -> None:
        terms = self.signed_terms()
        commit = self.commit_for(terms, ttl=10)
        with self.assertRaises(LeaseError) as ctx:
            commit.verify(NOW + 11)
        self.assertEqual(ctx.exception.code, LeaseError.EXPIRED)

    def test_commit_must_match_terms(self) -> None:
        terms = self.signed_terms()
        commit = self.commit_for(terms)
        other = replace(terms, task_id="task-2")
        with self.assertRaises(LeaseError):
            authorization_from_commit(commit, other, authority_node_id=self.requester.node_id)


class ExecutionGateTests(LeaseTestBase):
    def _authorization(self, terms=None, commit=None):
        terms = terms or self.signed_terms()
        return authorization_from_commit(
            commit or self.commit_for(terms), terms, authority_node_id=self.requester.node_id
        )

    def test_valid_dual_signed_authorization_passes(self) -> None:
        require_authorization(
            self._authorization(),
            task_id="task-1",
            worker_node_id=self.worker_id.node_id,
            now=NOW,
            require_dual_signed=True,
        )

    def test_missing_authorization_is_refused(self) -> None:
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(
                None, task_id="task-1", worker_node_id=self.worker_id.node_id, now=NOW
            )
        self.assertEqual(ctx.exception.code, LeaseError.NOT_AUTHORIZED)

    def test_legacy_authorization_is_refused_in_dual_mode(self) -> None:
        legacy = ExecutionAuthorization(
            lease_id="lea_1",
            task_id="task-1",
            worker_node_id=self.worker_id.node_id,
            requester_node_id=self.requester.node_id,
            authority_node_id=self.requester.node_id,
            terms_hash="a" * 64,
            expires_at=NOW + 60,
            dual_signed=False,
        )
        require_authorization(
            legacy, task_id="task-1", worker_node_id=self.worker_id.node_id, now=NOW
        )
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(
                legacy,
                task_id="task-1",
                worker_node_id=self.worker_id.node_id,
                now=NOW,
                require_dual_signed=True,
            )
        self.assertEqual(ctx.exception.code, LeaseError.NOT_AUTHORIZED)

    def test_authorization_for_another_task_or_worker_is_refused(self) -> None:
        auth = self._authorization()
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(auth, task_id="task-2", worker_node_id=self.worker_id.node_id, now=NOW)
        self.assertEqual(ctx.exception.code, LeaseError.TASK_MISMATCH)
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(auth, task_id="task-1", worker_node_id=self.other.node_id, now=NOW)
        self.assertEqual(ctx.exception.code, LeaseError.WRONG_WORKER)

    def test_expired_authorization_is_refused(self) -> None:
        terms = self.signed_terms()
        auth = authorization_from_commit(
            self.commit_for(terms, ttl=5), terms, authority_node_id=self.requester.node_id
        )
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(auth, task_id="task-1", worker_node_id=self.worker_id.node_id, now=NOW + 6)
        self.assertEqual(ctx.exception.code, LeaseError.EXPIRED)

    def test_tampered_commit_signature_is_refused(self) -> None:
        terms = self.signed_terms()
        commit = self.commit_for(terms)
        tampered = replace(commit, signature=("0" if commit.signature[0] != "0" else "1") + commit.signature[1:])
        auth = ExecutionAuthorization(
            lease_id=terms.lease_id,
            task_id=terms.task_id,
            worker_node_id=terms.worker_node_id,
            requester_node_id=terms.requester_node_id,
            authority_node_id=self.requester.node_id,
            terms_hash=terms_hash(terms),
            expires_at=NOW + 60,
            dual_signed=True,
            commit=tampered,
        )
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(auth, task_id="task-1", worker_node_id=self.worker_id.node_id, now=NOW)
        self.assertEqual(ctx.exception.code, LeaseError.SIGNATURE)

    def test_worker_execute_requires_commit_in_dual_mode(self) -> None:
        terms = self.signed_terms()
        task = Task(task_id=terms.task_id, task_type="numeric_batch_v1", payload={"numbers": [1, 2]})
        lease = Lease(
            lease_id=terms.lease_id,
            task_id=terms.task_id,
            peer_id=self.worker_id.node_id,
            status=LeaseStatus.ACTIVE,
            expires_at="2030-01-01T00:00:00Z",
        )
        worker = Worker(self.worker_id.node_id, Ed25519Policy(self.worker_id))
        with self.assertRaises(LeaseError) as ctx:
            worker.execute(task, lease, require_dual_signed=True, now=NOW)
        self.assertEqual(ctx.exception.code, LeaseError.NOT_AUTHORIZED)

        output, digest, signature = worker.execute(
            task,
            lease,
            authorization=self._authorization(terms),
            require_dual_signed=True,
            now=NOW,
        )
        self.assertEqual(digest, __import__("idle_compute.crypto", fromlist=["content_hash"]).content_hash(output))
        self.assertEqual(len(signature), 128)

    def test_worker_execute_legacy_path_still_binds_lease(self) -> None:
        terms = self.signed_terms()
        worker = Worker(self.worker_id.node_id, Ed25519Policy(self.worker_id))
        wrong = Lease(
            lease_id="lea_x",
            task_id="task-2",
            peer_id=self.worker_id.node_id,
            status=LeaseStatus.ACTIVE,
            expires_at="2030-01-01T00:00:00Z",
        )
        task = Task(task_id="task-1", task_type="numeric_batch_v1", payload={"numbers": [1]})
        with self.assertRaises(LeaseError) as ctx:
            worker.execute(task, wrong, now=NOW)
        self.assertEqual(ctx.exception.code, LeaseError.TASK_MISMATCH)


class StoreNegotiationTests(LeaseTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.root / "coordinator.db", self.root / "ledger.key")
        self.addCleanup(self.store.close)
        self.task = self.store.create_task("numeric_batch_v1", {"numbers": [1, 2]})
        self.terms = self.signed_terms(task_id=self.task.task_id)

    def create(self, *, lease_id=None, worker=None, expires_at=None):
        lease_id = lease_id or self.terms.lease_id
        worker = worker or self.worker_id
        return self.store.create_negotiation(
            lease_id=lease_id,
            task_id=self.task.task_id,
            requester_id=self.requester.node_id,
            worker_id=worker.node_id,
            authority_id=self.requester.node_id,
            terms_json="{}",
            terms_hash=terms_hash(self.terms),
            requester_signature=self.terms.requester_signature,
            expires_at=expires_at or "2030-01-01T00:00:00Z",
        )

    def accept(self, lease_id=None, signature="b" * 128):
        return self.store.accept_negotiation(
            lease_id or self.terms.lease_id, self.worker_id.node_id, signature, "{}"
        )

    def test_schema_is_v3_and_table_exists(self) -> None:
        version = self.store.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(version, "3")
        table = self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lease_negotiations'"
        ).fetchone()
        self.assertIsNotNone(table)

    def test_v2_database_upgrades_to_v3_without_losing_rows(self) -> None:
        # Simulate a D3 database: schema_version=2 and an existing task row.
        legacy = self.root / "legacy.db"
        import sqlite3

        conn = sqlite3.connect(legacy)
        try:
            conn.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO meta(key,value) VALUES ('schema_version','2');"
            )
            conn.commit()
        finally:
            conn.close()
        store = Store(legacy, self.root / "legacy.key")
        self.addCleanup(store.close)
        version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(version, "3")
        self.assertIsNotNone(
            store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='lease_negotiations'"
            ).fetchone()
        )

    def test_happy_path_proposed_accepted_committed(self) -> None:
        self.create()
        self.assertEqual(self.store.get_negotiation(self.terms.lease_id)["state"], "PROPOSED")
        self.accept()
        self.assertEqual(self.store.get_negotiation(self.terms.lease_id)["state"], "ACCEPTED")
        task, lease = self.store.commit_negotiation(self.terms.lease_id)
        self.assertEqual(task.status, TaskStatus.LEASED)
        self.assertEqual(lease.lease_id, self.terms.lease_id)
        self.assertEqual(lease.status, LeaseStatus.ACTIVE)
        self.assertEqual(self.store.get_negotiation(self.terms.lease_id)["state"], "ACTIVE")

    def test_only_one_open_negotiation_per_task(self) -> None:
        self.create()
        with self.assertRaises(NegotiationConflict):
            self.create(lease_id="lea_other")

    def test_accept_is_idempotent_and_guards_other_signatures(self) -> None:
        self.create()
        first = self.accept(signature="b" * 128)
        again = self.accept(signature="b" * 128)
        self.assertEqual(first["state"], again["state"])
        with self.assertRaises(InvalidNegotiationState):
            self.accept(signature="c" * 128)

    def test_reject_and_cancel_are_idempotent(self) -> None:
        self.create()
        self.assertEqual(self.store.reject_negotiation(self.terms.lease_id, "busy")["state"], "REJECTED")
        self.assertEqual(self.store.reject_negotiation(self.terms.lease_id, "busy")["state"], "REJECTED")

        other_task = self.store.create_task("numeric_batch_v1", {"numbers": [3]})
        other_terms = self.signed_terms(task_id=other_task.task_id, lease_id="lea_2")
        self.store.create_negotiation(
            lease_id=other_terms.lease_id,
            task_id=other_task.task_id,
            requester_id=self.requester.node_id,
            worker_id=self.worker_id.node_id,
            authority_id=self.requester.node_id,
            terms_json="{}",
            terms_hash=terms_hash(other_terms),
            requester_signature=other_terms.requester_signature,
            expires_at="2030-01-01T00:00:00Z",
        )
        self.assertEqual(self.store.cancel_negotiation("lea_2", "changed mind")["state"], "CANCELLED")
        self.assertEqual(self.store.cancel_negotiation("lea_2", "changed mind")["state"], "CANCELLED")

    def test_commit_requires_accept(self) -> None:
        self.create()
        with self.assertRaises(InvalidNegotiationState):
            self.store.commit_negotiation(self.terms.lease_id)

    def test_commit_is_idempotent(self) -> None:
        self.create()
        self.accept()
        task, lease = self.store.commit_negotiation(self.terms.lease_id)
        task2, lease2 = self.store.commit_negotiation(self.terms.lease_id)
        self.assertEqual(task.task_id, task2.task_id)
        self.assertEqual(lease.lease_id, lease2.lease_id)

    def test_expired_negotiation_cannot_be_committed(self) -> None:
        self.create(expires_at="2099-01-01T00:00:00Z")
        self.accept()
        # The commit happens after the negotiation expired.
        with self.assertRaises((LeaseExpired, InvalidNegotiationState)):
            self.store.commit_negotiation(self.terms.lease_id, now="2100-01-01T00:00:00Z")
        self.store.expire_negotiations(now="2100-01-01T00:00:00Z")
        self.assertEqual(self.store.get_negotiation(self.terms.lease_id)["state"], "EXPIRED")

    def test_cancelled_or_rejected_cannot_commit(self) -> None:
        self.create()
        self.store.cancel_negotiation(self.terms.lease_id, "stop")
        with self.assertRaises(InvalidNegotiationState):
            self.store.commit_negotiation(self.terms.lease_id)

    def test_unknown_negotiation(self) -> None:
        with self.assertRaises(TaskUnavailable):
            self.store.accept_negotiation("lea_missing", self.worker_id.node_id, "b" * 128, "{}")


if __name__ == "__main__":
    unittest.main()
