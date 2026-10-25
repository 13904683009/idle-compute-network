from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from idle_compute.crypto import content_hash, result_signing_payload
from idle_compute.db import DuplicateSubmission, LeaseExpired, Store, TaskUnavailable
from idle_compute.models import LeaseStatus, ResultStatus, TaskStatus
from idle_compute.tasks import NumericBatchTask


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "idle.db"
        self.key = Path(self._tmp.name) / "ledger.key"
        self.store = Store(self.db, self.key)
        self.addCleanup(self._close_store)

    def _close_store(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def _new_task(self, numbers=None, max_attempts=3, lease_seconds=30):
        return self.store.create_task(
            "numeric_batch_v1",
            {"numbers": numbers or [1, 2, 3]},
            max_attempts=max_attempts,
            lease_seconds=lease_seconds,
        )

    def _claim_start(self, task_id=None, peer="peer-a", now=None):
        lease = self.store.claim_task(peer, task_id, now=now)
        self.assertIsNotNone(lease)
        self.store.start_task(lease.lease_id, peer, now=now)
        return lease

    def _submit(self, lease, peer="peer-a", output=None, signature=None, now=None):
        task = self.store.get_task(lease.task_id)
        self.assertIsNotNone(task)
        value = output if output is not None else NumericBatchTask.execute(task.payload)
        digest = content_hash(value)
        sig = signature or self.store.signer.sign(
            result_signing_payload(task.task_id, lease.lease_id, peer, digest)
        )
        return self.store.submit_result(lease.lease_id, peer, value, digest, sig, now=now)

    def test_happy_path_and_credit_once(self) -> None:
        task = self._new_task()
        lease = self._claim_start(task.task_id)
        result = self._submit(lease)
        validation = self.store.validate_result(result.result_id, "validator")
        self.assertEqual(validation.status.value, "VALID")
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.COMPLETED)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.RELEASED)
        self.assertEqual(self.store.balances(), {"peer-a": 1})
        self.assertEqual(len(self.store.list_events()), 1)

        duplicate = self.store.validate_result(result.result_id, "validator")
        self.assertEqual(duplicate.validation_id, validation.validation_id)
        self.assertEqual(self.store.balances(), {"peer-a": 1})
        self.assertEqual(len(self.store.list_events()), 1)

    def test_duplicate_claim_does_not_create_second_active_lease(self) -> None:
        task = self._new_task()
        lease = self.store.claim_task("peer-a", task.task_id, now="2026-01-01T00:00:00+00:00")
        self.assertIsNotNone(lease)
        self.assertIsNone(self.store.claim_task("peer-b", task.task_id, now="2026-01-01T00:00:00+00:00"))
        active = self.store.conn.execute("SELECT COUNT(*) AS n FROM leases WHERE status='active'").fetchone()["n"]
        self.assertEqual(active, 1)

    def test_second_active_lease_insert_is_rejected_by_index(self) -> None:
        task = self._new_task()
        lease = self._claim_start(task.task_id)
        self.assertIsNotNone(lease)
        # Use an already-ensured peer_id so the partial unique index
        # one_active_lease is the ONLY constraint that can fire; pin the
        # failure source on the UNIQUE message so an index removal cannot
        # masquerade as a foreign-key failure.
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.store.conn.execute(
                "INSERT INTO leases(lease_id,task_id,peer_id,status,expires_at,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "lea_second_active",
                    task.task_id,
                    "peer-a",
                    "active",
                    "2026-01-01T00:01:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        self.assertIn("UNIQUE constraint failed", str(ctx.exception))

    def test_duplicate_submission_rejected(self) -> None:
        task = self._new_task()
        lease = self._claim_start(task.task_id)
        self._submit(lease)
        with self.assertRaises(DuplicateSubmission):
            self._submit(lease)
        count = self.store.conn.execute("SELECT COUNT(*) AS n FROM results").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_lease_expiry_retries_then_expires(self) -> None:
        task = self._new_task(max_attempts=2, lease_seconds=1)
        t0 = "2026-01-01T00:00:00+00:00"
        lease1 = self.store.claim_task("peer-a", task.task_id, now=t0)
        self.assertIsNotNone(lease1)
        self.store.expire_leases("2026-01-01T00:00:02+00:00")
        current = self.store.get_task(task.task_id)
        self.assertEqual(current.status, TaskStatus.QUEUED)
        self.assertEqual(current.attempts, 1)
        self.assertEqual(self.store.get_lease(lease1.lease_id).status, LeaseStatus.EXPIRED)

        lease2 = self.store.claim_task("peer-a", task.task_id, now="2026-01-01T00:00:03+00:00")
        self.assertIsNotNone(lease2)
        self.store.expire_leases("2026-01-01T00:00:05+00:00")
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.EXPIRED)
        self.assertIsNone(self.store.claim_task("peer-a", task.task_id, now="2026-01-01T00:00:06+00:00"))

    def test_expired_lease_rejects_result_without_credit(self) -> None:
        task = self._new_task(lease_seconds=1)
        t0 = "2026-01-01T00:00:00+00:00"
        lease = self._claim_start(task.task_id, now=t0)
        result = self._submit(lease, now=t0)
        validation = self.store.validate_result(result.result_id, "validator", now="2026-01-01T00:00:02+00:00")
        self.assertEqual(validation.status.value, "INVALID")
        self.assertEqual(self.store.get_result(result.result_id).status, ResultStatus.REJECTED)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.EXPIRED)
        self.assertIsNone(self.store.get_task(task.task_id).result_id)
        self.assertEqual(self.store.balances(), {})
        self.assertEqual(self.store.list_events(), [])

    def test_invalid_signature_rejected(self) -> None:
        task = self._new_task()
        lease = self._claim_start(task.task_id)
        result = self._submit(lease, signature="0" * 64)
        validation = self.store.validate_result(result.result_id, "validator")
        self.assertEqual(validation.status.value, "INVALID")
        self.assertIn("signature", validation.reason)
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.RELEASED)
        self.assertEqual(self.store.balances(), {})

    def test_null_signature_and_hash_treated_as_invalid_signature(self) -> None:
        task = self._new_task()
        lease = self._claim_start(task.task_id)
        result = self._submit(lease)
        # Direct DB tampering can yield NULL signature/content_hash despite the
        # NOT NULL columns; patch the loaded row to simulate it (the validator
        # must treat it as an invalid signature, not crash in hmac).
        tampered = replace(result, signature=None, content_hash=None)
        with mock.patch.object(self.store, "get_result", return_value=tampered):
            validation = self.store.validate_result(result.result_id, "validator")
        self.assertEqual(validation.status.value, "INVALID")
        self.assertIn("invalid signature", validation.reason)
        self.assertEqual(validation.actual_hash, "")
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.RELEASED)
        self.assertEqual(self.store.balances(), {})

    def test_wrong_output_rejected(self) -> None:
        task = self._new_task()
        lease = self._claim_start(task.task_id)
        wrong = {"count": 3, "sum": 999, "sum_squares": 999}
        result = self._submit(lease, output=wrong)
        validation = self.store.validate_result(result.result_id, "validator")
        self.assertEqual(validation.status.value, "INVALID")
        self.assertEqual(self.store.balances(), {})

    def test_cancel_prevents_claim_and_credit(self) -> None:
        task = self._new_task()
        cancelled = self.store.cancel_task(task.task_id, "test cancel")
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertIsNone(self.store.claim_task("peer-a", task.task_id))
        self.assertEqual(self.store.balances(), {})

    def test_start_after_expiry_raises(self) -> None:
        task = self._new_task(lease_seconds=1)
        lease = self.store.claim_task("peer-a", task.task_id, now="2026-01-01T00:00:00+00:00")
        with self.assertRaises(LeaseExpired):
            self.store.start_task(lease.lease_id, "peer-a", now="2026-01-01T00:00:03+00:00")

    def test_restart_restores_state_and_key(self) -> None:
        task = self._new_task()
        lease = self.store.claim_task("peer-a", task.task_id, now="2026-01-01T00:00:00+00:00")
        self.assertIsNotNone(lease)
        self.store.close()
        self.store = Store(self.db, self.key)
        restored = self.store.get_task(task.task_id)
        self.assertEqual(restored.status, TaskStatus.LEASED)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.ACTIVE)
        self.store.expire_leases("2026-01-01T00:01:00+00:00")
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.QUEUED)
        self.assertTrue(self.store.verify_ledger()[0])

    def test_repeated_credit_direct_is_idempotent(self) -> None:
        task = self._new_task()
        lease = self._claim_start(task.task_id)
        result = self._submit(lease)
        self.store.validate_result(result.result_id, "validator")
        from idle_compute.ledger import append_credit

        with self.store.transaction():
            event, created = append_credit(self.store.conn, self.store.signer, "peer-a", task.task_id)
        self.assertFalse(created)
        self.assertEqual(self.store.balances(), {"peer-a": 1})

    def test_task_ids_are_unique_and_shard_is_present(self) -> None:
        first = self._new_task([1])
        second = self._new_task([2])
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(len(first.shards), 1)
        self.assertEqual(first.shards[0].shard_count, 1)
        self.assertEqual(first.shards[0].payload, {"numbers": [1]})

    def test_running_task_survives_restart_then_expires(self) -> None:
        task = self._new_task(lease_seconds=1)
        t0 = "2026-01-01T00:00:00+00:00"
        lease = self._claim_start(task.task_id, now=t0)
        self.store.close()
        self.store = Store(self.db, self.key)
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.RUNNING)
        self.store.expire_leases("2026-01-01T00:00:02+00:00")
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.QUEUED)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.EXPIRED)

    def test_recover_requeues_stale_lease(self) -> None:
        task = self._new_task(lease_seconds=1)
        lease = self._claim_start(task.task_id, now="2026-01-01T00:00:00+00:00")
        self.store.close()
        self.store = Store(self.db, self.key)
        self.assertEqual(self.store.recover("2026-01-01T00:00:02+00:00"), 1)
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.QUEUED)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.EXPIRED)

    def test_submit_after_expiry_is_rejected(self) -> None:
        task = self._new_task(lease_seconds=1)
        t0 = "2026-01-01T00:00:00+00:00"
        lease = self._claim_start(task.task_id, now=t0)
        self.store.expire_leases("2026-01-01T00:00:02+00:00")
        with self.assertRaises(LeaseExpired):
            self._submit(lease, now="2026-01-01T00:00:02+00:00")

    def test_submitted_task_with_expired_lease_returns_to_queue(self) -> None:
        # Crash window: a result is submitted but never validated. Lease expiry
        # must reject the stale result and requeue the task instead of leaving
        # it wedged in SUBMITTED, which claim_task never selects.
        task = self._new_task(lease_seconds=1)
        t0 = "2026-01-01T00:00:00+00:00"
        lease = self._claim_start(task.task_id, now=t0)
        result = self._submit(lease, now=t0)
        self.assertEqual(self.store.get_task(task.task_id).status, TaskStatus.SUBMITTED)
        self.store.expire_leases("2026-01-01T00:00:02+00:00")
        current = self.store.get_task(task.task_id)
        self.assertEqual(current.status, TaskStatus.QUEUED)
        self.assertIsNone(current.result_id)
        self.assertEqual(self.store.get_lease(lease.lease_id).status, LeaseStatus.EXPIRED)
        self.assertEqual(self.store.get_result(result.result_id).status, ResultStatus.REJECTED)
        again = self.store.claim_task("peer-a", task.task_id, now="2026-01-01T00:00:03+00:00")
        self.assertIsNotNone(again)

    def test_submitted_task_with_expired_lease_expires_after_max_attempts(self) -> None:
        task = self._new_task(max_attempts=1, lease_seconds=1)
        t0 = "2026-01-01T00:00:00+00:00"
        lease = self._claim_start(task.task_id, now=t0)
        self._submit(lease, now=t0)
        self.store.expire_leases("2026-01-01T00:00:02+00:00")
        current = self.store.get_task(task.task_id)
        self.assertEqual(current.status, TaskStatus.EXPIRED)
        self.assertIsNone(current.result_id)

    def test_peer_id_is_not_sql(self) -> None:
        task = self._new_task()
        weird = "peer' OR 1=1 --"
        lease = self.store.claim_task(weird, task.task_id)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.peer_id, weird)


if __name__ == "__main__":
    unittest.main()
