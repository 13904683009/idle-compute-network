"""D4 final audit regressions: bypass removal, stale authorization, restart,
concurrency, migration failure and result replay.

Deterministic: barriers/transactions only, no sleep-based assertions.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from idle_compute.config import LeaseConfig, NodeConfig, ValidationError, from_dict
from idle_compute.db import (
    DuplicateSubmission,
    InvalidNegotiationState,
    Store,
    TaskUnavailable,
)
from idle_compute.identity import NodeIdentity
from idle_compute.lease import (
    LeaseCommit,
    LeaseError,
    TaskLeaseTerms,
    authorization_from_commit,
    require_authorization,
    terms_hash,
)
from idle_compute.models import LeaseStatus, TaskStatus
from idle_compute.node import Node
from idle_compute.signing import Ed25519Policy
from idle_compute.worker import Worker

NOW = 1_800_000_000


def _terms(requester, worker, *, lease_id="lea_1", task_id="task-1", expires=NOW + 60):
    return TaskLeaseTerms(
        lease_id=lease_id,
        task_id=task_id,
        requester_node_id=requester.node_id,
        worker_node_id=worker.node_id,
        task_spec_hash="a" * 64,
        input_manifest_hash="",
        lease_version=1,
        issued_at=NOW,
        expires_at=expires,
        execution_deadline=expires,
        verification_policy={"mode": "RECOMPUTE", "required": False},
        reward_policy={"amount": 1},
    ).sign_proposal(requester)


def _commit(requester, terms, *, ttl=60):
    return LeaseCommit(
        lease_id=terms.lease_id,
        task_id=terms.task_id,
        requester_node_id=terms.requester_node_id,
        worker_node_id=terms.worker_node_id,
        terms_hash=terms_hash(terms),
        committed_at=NOW,
        expires_at=NOW + ttl,
    ).sign(requester)


class AuditBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-audit-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.requester = NodeIdentity.load_or_create(self.root / "r" / "identity.key", node_name="r")
        self.worker = NodeIdentity.load_or_create(self.root / "w" / "identity.key", node_name="w")
        self.other = NodeIdentity.load_or_create(self.root / "o" / "identity.key", node_name="o")


class LegacyBypassTests(AuditBase):
    def test_config_rejects_legacy_mode(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict({"role": "coordinator", "data_dir": "var/n", "lease": {"mode": "legacy"}})
        self.assertIn("dual_signed", str(ctx.exception))

    def test_config_default_is_dual_signed(self) -> None:
        config = from_dict({"role": "coordinator", "data_dir": "var/n"})
        self.assertEqual(config.lease.mode, "dual_signed")

    def test_node_rejects_hand_built_legacy_config(self) -> None:
        config = NodeConfig(
            role="worker",
            data_dir=str(self.root / "legacy-node"),
            coordinator_node_id=self.requester.node_id,
            peers=[self.requester.node_id],
            lease=LeaseConfig(mode="legacy"),
        )
        with self.assertRaises(ValidationError) as ctx:
            Node(config)
        self.assertIn("dual_signed", str(ctx.exception))

    def test_worker_legacy_api_refuses_dual_signed_requirement(self) -> None:
        terms = _terms(self.requester, self.worker)
        task_type = "numeric_batch_v1"
        from idle_compute.models import Lease, Task

        task = Task(task_id="task-1", task_type=task_type, payload={"numbers": [1]})
        lease = Lease(
            lease_id=terms.lease_id,
            task_id=terms.task_id,
            peer_id=self.worker.node_id,
            status=LeaseStatus.ACTIVE,
            expires_at="2030-01-01T00:00:00Z",
        )
        with self.assertRaises(LeaseError) as ctx:
            Worker(self.worker.node_id, Ed25519Policy(self.worker)).execute(
                task, lease, require_dual_signed=True, now=NOW
            )
        self.assertEqual(ctx.exception.code, LeaseError.NOT_AUTHORIZED)


class StaleAuthorizationTests(AuditBase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.root / "c.db", self.root / "ledger.key")
        self.addCleanup(self.store.close)
        self.task = self.store.create_task("numeric_batch_v1", {"numbers": [1]})
        self.lease = self.store.claim_task(self.worker.node_id, self.task.task_id)
        self.terms = _terms(
            self.requester, self.worker, lease_id=self.lease.lease_id, task_id=self.task.task_id
        )

    def _propose(self):
        return self.store.create_negotiation_for_lease(
            lease_id=self.terms.lease_id,
            task_id=self.task.task_id,
            requester_id=self.requester.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.requester.node_id,
            terms_json="{}",
            terms_hash=terms_hash(self.terms),
            requester_signature=self.terms.requester_signature,
            expires_at="2030-01-01T00:00:00Z",
        )

    def _accept(self):
        self.store.accept_negotiation(
            self.terms.lease_id, self.worker.node_id, "b" * 128, "{}"
        )

    def test_expiring_the_lease_expires_the_active_negotiation(self) -> None:
        self._propose()
        self._accept()
        self.store.activate_negotiation(self.terms.lease_id)
        self.assertEqual(self.store.get_negotiation(self.terms.lease_id)["state"], "ACTIVE")

        self.store.expire_leases(now="2100-01-01T00:00:00Z")
        self.assertEqual(self.store.get_negotiation(self.terms.lease_id)["state"], "EXPIRED")
        with self.assertRaises(InvalidNegotiationState):
            self.store.activate_negotiation(self.terms.lease_id)

    def test_old_authorization_is_rejected_after_expiry(self) -> None:
        self._propose()
        self._accept()
        self.store.activate_negotiation(self.terms.lease_id)
        authorization = authorization_from_commit(
            _commit(self.requester, self.terms, ttl=10), self.terms, authority_node_id=self.requester.node_id
        )
        require_authorization(
            authorization, task_id=self.task.task_id, worker_node_id=self.worker.node_id, now=NOW
        )
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(
                authorization,
                task_id=self.task.task_id,
                worker_node_id=self.worker.node_id,
                now=NOW + 11,
            )
        self.assertEqual(ctx.exception.code, LeaseError.EXPIRED)

    def test_commit_no_longer_matches_a_new_lease(self) -> None:
        commit = _commit(self.requester, self.terms)
        new_terms = _terms(self.requester, self.worker, lease_id="lea_2", task_id=self.task.task_id)
        with self.assertRaises(LeaseError):
            authorization_from_commit(commit, new_terms, authority_node_id=self.requester.node_id)


class RestartRecoveryTests(AuditBase):
    def test_unfinished_negotiation_does_not_become_active_on_reopen(self) -> None:
        db = self.root / "c.db"
        store = Store(db, self.root / "ledger.key")
        task = store.create_task("numeric_batch_v1", {"numbers": [1]})
        lease = store.claim_task(self.worker.node_id, task.task_id)
        terms = _terms(self.requester, self.worker, lease_id=lease.lease_id, task_id=task.task_id)
        store.create_negotiation_for_lease(
            lease_id=terms.lease_id,
            task_id=task.task_id,
            requester_id=self.requester.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.requester.node_id,
            terms_json="{}",
            terms_hash=terms_hash(terms),
            requester_signature=terms.requester_signature,
            expires_at="2030-01-01T00:00:00Z",
        )
        store.accept_negotiation(terms.lease_id, self.worker.node_id, "b" * 128, "{}")
        store.close()

        reopened = Store(db, self.root / "ledger.key")
        self.addCleanup(reopened.close)
        row = reopened.get_negotiation(terms.lease_id)
        self.assertEqual(row["state"], "ACCEPTED", "restart must not auto-upgrade to ACTIVE")
        lease = reopened.get_lease(terms.lease_id)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.status, LeaseStatus.ACTIVE)

    def test_expired_or_cancelled_lease_cannot_be_revived(self) -> None:
        store = Store(self.root / "c.db", self.root / "ledger.key")
        self.addCleanup(store.close)
        task = store.create_task("numeric_batch_v1", {"numbers": [1]})
        lease = store.claim_task(self.worker.node_id, task.task_id)
        terms = _terms(self.requester, self.worker, lease_id=lease.lease_id, task_id=task.task_id)
        store.create_negotiation_for_lease(
            lease_id=terms.lease_id,
            task_id=task.task_id,
            requester_id=self.requester.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.requester.node_id,
            terms_json="{}",
            terms_hash=terms_hash(terms),
            requester_signature=terms.requester_signature,
            expires_at="2030-01-01T00:00:00Z",
        )
        store.accept_negotiation(terms.lease_id, self.worker.node_id, "b" * 128, "{}")
        store.activate_negotiation(terms.lease_id)
        with self.assertRaises(InvalidNegotiationState):
            # ACTIVE means execution may already be running: the API refuses to
            # pretend it can cancel/interrupt it.
            store.cancel_negotiation(terms.lease_id, "operator stop")
        store.expire_leases(now="2100-01-01T00:00:00Z")
        self.assertEqual(store.get_negotiation(terms.lease_id)["state"], "EXPIRED")
        with self.assertRaises(InvalidNegotiationState):
            store.activate_negotiation(terms.lease_id)
        with self.assertRaises(InvalidNegotiationState):
            store.commit_negotiation(terms.lease_id)


class ConcurrencyTests(AuditBase):
    def test_two_threads_claiming_the_same_task_never_create_two_leases(self) -> None:
        db = self.root / "c.db"
        setup = Store(db, self.root / "ledger.key")
        task = setup.create_task("numeric_batch_v1", {"numbers": [1]})
        setup.close()

        barrier = threading.Barrier(2)
        results: list[object] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def claim() -> None:
            store = Store(db, self.root / "ledger.key")
            try:
                barrier.wait(timeout=10)
                lease = store.claim_task("peer-a", task.task_id)
                with lock:
                    results.append(lease)
            except BaseException as exc:  # database-locked under contention is acceptable
                with lock:
                    errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        check = Store(db, self.root / "ledger.key")
        self.addCleanup(check.close)
        active = check.conn.execute(
            "SELECT COUNT(*) FROM leases WHERE task_id=? AND status='active'", (task.task_id,)
        ).fetchone()[0]
        self.assertEqual(active, 1, "exactly one ACTIVE lease must exist")
        self.assertLessEqual(sum(1 for item in results if item is not None), 1)

    def test_concurrent_activate_is_idempotent(self) -> None:
        db = self.root / "c.db"
        store = Store(db, self.root / "ledger.key")
        task = store.create_task("numeric_batch_v1", {"numbers": [1]})
        lease = store.claim_task(self.worker.node_id, task.task_id)
        terms = _terms(self.requester, self.worker, lease_id=lease.lease_id, task_id=task.task_id)
        store.create_negotiation_for_lease(
            lease_id=terms.lease_id,
            task_id=task.task_id,
            requester_id=self.requester.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.requester.node_id,
            terms_json="{}",
            terms_hash=terms_hash(terms),
            requester_signature=terms.requester_signature,
            expires_at="2030-01-01T00:00:00Z",
        )
        store.accept_negotiation(terms.lease_id, self.worker.node_id, "b" * 128, "{}")
        store.close()

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def activate() -> None:
            worker_store = Store(db, self.root / "ledger.key")
            try:
                barrier.wait(timeout=10)
                row = worker_store.activate_negotiation(terms.lease_id)
                with lock:
                    outcomes.append(row["state"])
            finally:
                worker_store.close()

        threads = [threading.Thread(target=activate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        check = Store(db, self.root / "ledger.key")
        self.addCleanup(check.close)
        self.assertEqual(outcomes, ["ACTIVE", "ACTIVE"])
        active = check.conn.execute(
            "SELECT COUNT(*) FROM lease_negotiations WHERE lease_id=? AND state='ACTIVE'",
            (terms.lease_id,),
        ).fetchone()[0]
        self.assertEqual(active, 1)


class MigrationFailureTests(AuditBase):
    def test_failed_migration_keeps_schema_version_2_and_retries(self) -> None:
        db = self.root / "v2.db"
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO meta(key,value) VALUES ('schema_version','2');"
                # A conflicting VIEW makes CREATE TABLE lease_negotiations fail.
                "CREATE VIEW lease_negotiations AS SELECT key AS lease_id, value AS state FROM meta;"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.Error):
            Store(db, self.root / "v2.key")
        conn = sqlite3.connect(db)
        try:
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, "2", "a failed migration must not claim v3")

        # Repair and retry: the migration is idempotent.
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP VIEW lease_negotiations")
            conn.commit()
        finally:
            conn.close()
        store = Store(db, self.root / "v2.key")
        self.addCleanup(store.close)
        version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(version, "3")
        self.assertIsNotNone(
            store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='lease_negotiations'"
            ).fetchone()
        )


class ResultReplayTests(AuditBase):
    def test_duplicate_result_submission_yields_one_credit(self) -> None:
        policy = Ed25519Policy(self.worker)
        store = Store(self.root / "c.db", self.root / "ledger.key", signer=policy)
        self.addCleanup(store.close)
        task = store.create_task("numeric_batch_v1", {"numbers": [1, 2]})
        lease = store.claim_task(self.worker.node_id, task.task_id)
        assert lease is not None
        store.start_task(lease.lease_id, self.worker.node_id)
        worker = Worker(self.worker.node_id, policy)
        output, digest, signature = worker.execute(
            store.get_task(task.task_id), lease, now=NOW
        )
        result = store.submit_result(lease.lease_id, self.worker.node_id, output, digest, signature)
        validation = store.validate_result(result.result_id, self.requester.node_id)
        self.assertEqual(validation.status.value, "VALID")
        credits = store.conn.execute(
            "SELECT COUNT(*) FROM credit_events WHERE task_id=?", (task.task_id,)
        ).fetchone()[0]
        self.assertEqual(credits, 1)

        with self.assertRaises(DuplicateSubmission):
            store.submit_result(lease.lease_id, self.worker.node_id, output, digest, signature)
        credits = store.conn.execute(
            "SELECT COUNT(*) FROM credit_events WHERE task_id=?", (task.task_id,)
        ).fetchone()[0]
        self.assertEqual(credits, 1, "a replayed result must not create a second CREDIT")
        self.assertEqual(store.get_task(task.task_id).status, TaskStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
