from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from idle_compute.crypto import (
    GENESIS_HASH,
    LocalSigner,
    content_hash,
    result_payload,
    result_signing_payload,
)
from idle_compute.db import DuplicateSubmission, LeaseExpired, Store, TaskUnavailable
from idle_compute.identity import NodeIdentity
from idle_compute.ledger import append_credit, verify_ledger
from idle_compute.models import LeaseStatus, ResultStatus, TaskStatus
from idle_compute.signing import Ed25519Policy
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


V1_MINIMAL_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta(key,value) VALUES ('schema_version','1');
CREATE TABLE credit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE TABLE results (
    result_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    output_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class SchemaV2MigrationTests(unittest.TestCase):
    """B-3: a v1 database migrates in place; old rows are never re-signed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "v1.db"
        self.key = Path(self._tmp.name) / "ledger.key"

    def _build_v1_db(self) -> None:
        signer = LocalSigner.load_or_create(self.key)
        conn = sqlite3.connect(self.db)
        conn.executescript(V1_MINIMAL_SCHEMA)
        created_at = "2026-01-01T00:00:00+00:00"
        event_id = "evt_v1_1"
        body = {
            "domain": "idle-compute-credit-v1",
            "event_id": event_id,
            "event_type": "CREDIT",
            "peer_id": "peer-v1",
            "task_id": "tsk_v1",
            "amount": 1,
            "idempotency_key": "credit:tsk_v1:peer-v1",
            "created_at": created_at,
            "prev_hash": GENESIS_HASH,
            "metadata": {},
        }
        conn.execute(
            "INSERT INTO credit_events "
            "(event_id,event_type,peer_id,task_id,amount,idempotency_key,created_at,prev_hash,event_hash,signature,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                "CREDIT",
                "peer-v1",
                "tsk_v1",
                1,
                "credit:tsk_v1:peer-v1",
                created_at,
                GENESIS_HASH,
                content_hash(body),
                signer.sign(content_hash(body)),
                "{}",
            ),
        )
        conn.commit()
        conn.close()

    def test_v1_database_migrates_in_place_to_schema_v3(self) -> None:
        self._build_v1_db()
        store = Store(self.db, self.key)
        self.addCleanup(store.close)
        version = store.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, "3")
        for table in ("credit_events", "results"):
            columns = store.conn.execute(f"PRAGMA table_info({table})").fetchall()
            sig_algo = next((c for c in columns if c[1] == "sig_algo"), None)
            self.assertIsNotNone(sig_algo, f"{table} missing sig_algo column")
            self.assertEqual(sig_algo[4], "'hmac-sha256'")  # dflt_value
        # The pre-existing v1 row was NOT re-signed: it keeps the default tag.
        row = store.conn.execute(
            "SELECT sig_algo, signature FROM credit_events WHERE event_id='evt_v1_1'"
        ).fetchone()
        self.assertEqual(row["sig_algo"], "hmac-sha256")
        # verify-ledger still passes on the migrated legacy chain.
        ok, errors = verify_ledger(store.conn, store.signer)
        self.assertTrue(ok, errors)

    def test_network_tables_and_backstop_constraints_exist(self) -> None:
        store = Store(self.db, self.key)
        self.addCleanup(store.close)
        conn = store.conn
        for table, columns in {
            "node_state": ("peer_id", "last_seq_sent", "last_seq_seen"),
            "claims": ("claim_request_id", "lease_id", "task_id", "worker_id", "created_at"),
            "client_refs": ("client_ref", "task_id", "created_at"),
        }.items():
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            self.assertIsNotNone(row, f"table {table} missing")
            for col in columns:
                self.assertIn(col, row["sql"], f"{table} missing column {col}")
        one_active = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='one_active_lease'"
        ).fetchone()
        self.assertIsNotNone(one_active)
        self.assertIn("WHERE status = 'active'", one_active["sql"])
        results_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='results'"
        ).fetchone()["sql"]
        self.assertIn("UNIQUE(task_id, lease_id)", results_sql)
        validations_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='validations'"
        ).fetchone()["sql"]
        self.assertIn("result_id", validations_sql)
        self.assertIn("UNIQUE", validations_sql)
        events_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='credit_events'"
        ).fetchone()["sql"]
        self.assertIn("idempotency_key", events_sql)
        self.assertIn("UNIQUE", events_sql)


class NetworkStoreTests(unittest.TestCase):
    """B-3: network-mode store backed by Ed25519Policy."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.coordinator = NodeIdentity.load_or_create(
            root / "coordinator" / "identity.key", node_name="coordinator"
        )
        self.worker = NodeIdentity.load_or_create(
            root / "worker" / "identity.key", node_name="worker"
        )
        self.db = root / "net.db"
        self.store = Store(self.db, signer=Ed25519Policy.from_identity(self.coordinator))
        self.addCleanup(self._close)

    def _close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def test_network_mode_creates_no_hmac_key_file(self) -> None:
        self.assertIsNone(self.store.key_path)
        self.assertFalse((Path(self._tmp.name) / "ledger.key").exists())

    def test_append_credit_ed25519_and_verify_ledger(self) -> None:
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1, 2]})
        with self.store.transaction():
            event, created = append_credit(
                self.store.conn, self.store.signer, self.worker.node_id, task.task_id, 1, {"result_id": "res_1"}
            )
        self.assertTrue(created)
        row = self.store.conn.execute(
            "SELECT sig_algo FROM credit_events WHERE event_id=?", (event.event_id,)
        ).fetchone()
        self.assertEqual(row["sig_algo"], "ed25519")
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertTrue(ok, errors)

    def _seed_two_events(self):
        events = []
        for i in (1, 2):
            task = self.store.create_task("numeric_batch_v1", {"numbers": [i]})
            with self.store.transaction():
                event, created = append_credit(
                    self.store.conn, self.store.signer, self.worker.node_id, task.task_id
                )
            self.assertTrue(created)
            events.append(event)
        return events

    def test_tampered_ed25519_signature_fails_verify(self) -> None:
        self._seed_two_events()
        self.store.conn.execute(
            "UPDATE credit_events SET signature=? WHERE seq=2", ("00" * 64,)
        )
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("signature invalid" in e for e in errors))

    def test_tampered_prev_hash_fails_verify(self) -> None:
        self._seed_two_events()
        self.store.conn.execute(
            "UPDATE credit_events SET prev_hash=? WHERE seq=2", ("f" * 64,)
        )
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("prev_hash mismatch" in e for e in errors))

    def test_tampered_amount_fails_verify(self) -> None:
        self._seed_two_events()
        self.store.conn.execute("UPDATE credit_events SET amount=-5 WHERE seq=2")
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("non-positive amount" in e for e in errors))
        self.assertTrue(any("event_hash mismatch" in e for e in errors))

    def test_hmac_tagged_row_fails_with_structured_conflict(self) -> None:
        self._seed_two_events()
        self.store.conn.execute(
            "UPDATE credit_events SET sig_algo='hmac-sha256' WHERE seq=2"
        )
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(
            any("CONFLICT" in e and "hmac-sha256" in e for e in errors), errors
        )

    def test_validate_result_accepts_worker_ed25519_signature(self) -> None:
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1, 2, 3]})
        lease = self.store.claim_task(self.worker.node_id, task.task_id)
        self.assertIsNotNone(lease)
        self.store.start_task(lease.lease_id, self.worker.node_id)
        output = NumericBatchTask.execute(task.payload)
        digest = content_hash(output)
        signature = self.worker.sign(
            result_payload(task.task_id, lease.lease_id, self.worker.node_id, digest)
        )
        result = self.store.submit_result(
            lease.lease_id, self.worker.node_id, output, digest, signature
        )
        row = self.store.conn.execute(
            "SELECT sig_algo FROM results WHERE result_id=?", (result.result_id,)
        ).fetchone()
        self.assertEqual(row["sig_algo"], "ed25519")
        validation = self.store.validate_result(result.result_id, "network-validator")
        self.assertEqual(validation.status.value, "VALID")
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertTrue(ok, errors)
        self.assertEqual(self.store.balances(), {self.worker.node_id: 1})

    def test_validate_result_rejects_signature_from_different_node_key(self) -> None:
        impostor = NodeIdentity.load_or_create(
            Path(self._tmp.name) / "impostor" / "identity.key", node_name="impostor"
        )
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1, 2, 3]})
        lease = self.store.claim_task(self.worker.node_id, task.task_id)
        self.assertIsNotNone(lease)
        self.store.start_task(lease.lease_id, self.worker.node_id)
        output = NumericBatchTask.execute(task.payload)
        digest = content_hash(output)
        # Signed with a DIFFERENT node's key than the claiming peer_id.
        signature = impostor.sign(
            result_payload(task.task_id, lease.lease_id, self.worker.node_id, digest)
        )
        result = self.store.submit_result(
            lease.lease_id, self.worker.node_id, output, digest, signature
        )
        validation = self.store.validate_result(result.result_id, "network-validator")
        self.assertEqual(validation.status.value, "INVALID")
        self.assertIn("invalid signature", validation.reason)
        self.assertEqual(self.store.balances(), {})


if __name__ == "__main__":
    unittest.main()
