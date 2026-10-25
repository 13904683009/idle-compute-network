from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from idle_compute.crypto import content_hash, result_signing_payload
from idle_compute.db import Store
from idle_compute.ledger import LedgerError, append_credit, iter_events_lenient, rebuild_ledger, verify_ledger
from idle_compute.tasks import NumericBatchTask


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "idle.db", Path(self._tmp.name) / "ledger.key")
        self.addCleanup(self.store.close)

    def _credit_one(self, numbers=None):
        task = self.store.create_task("numeric_batch_v1", {"numbers": numbers or [1, 2, 3]})
        lease = self.store.claim_task("peer-a", task.task_id)
        self.store.start_task(lease.lease_id, "peer-a")
        output = NumericBatchTask.execute(task.payload)
        digest = content_hash(output)
        signature = self.store.signer.sign(
            result_signing_payload(task.task_id, lease.lease_id, "peer-a", digest)
        )
        result = self.store.submit_result(lease.lease_id, "peer-a", output, digest, signature)
        self.store.validate_result(result.result_id, "validator")
        return task

    def test_chain_and_rebuild(self) -> None:
        self._credit_one()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertTrue(ok, errors)
        rebuilt = rebuild_ledger(self.store.conn, self.store.signer)
        self.assertEqual(rebuilt["balances"], {"peer-a": 1})
        self.assertEqual(rebuilt["events"], 1)
        self.assertEqual(self.store.balances(), {"peer-a": 1})

    def test_duplicate_credit_is_idempotent(self) -> None:
        task = self._credit_one()
        with self.store.transaction():
            event, created = append_credit(self.store.conn, self.store.signer, "peer-a", task.task_id)
        self.assertFalse(created)
        self.assertEqual(self.store.balances(), {"peer-a": 1})
        self.assertEqual(len(self.store.list_events()), 1)

    def test_tampered_signature_fails_verification(self) -> None:
        self._credit_one()
        self.store.conn.execute("UPDATE credit_events SET signature=? WHERE seq=1", ("0" * 64,))
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("signature" in error for error in errors))
        with self.assertRaises(LedgerError):
            rebuild_ledger(self.store.conn, self.store.signer)

    def test_negative_credit_rejected(self) -> None:
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1]})
        with self.assertRaises(ValueError):
            with self.store.transaction():
                append_credit(self.store.conn, self.store.signer, "peer-a", task.task_id, amount=0)

    def _three_events(self) -> None:
        self._credit_one([1])
        self._credit_one([2])
        self._credit_one([3])
        self.assertTrue(verify_ledger(self.store.conn, self.store.signer)[0])

    def test_broken_prev_hash_fails_verification(self) -> None:
        self._three_events()
        self.store.conn.execute("UPDATE credit_events SET prev_hash=? WHERE seq=2", ("f" * 64,))
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("prev_hash" in error for error in errors))
        with self.assertRaises(LedgerError):
            rebuild_ledger(self.store.conn, self.store.signer)

    def test_tampered_amount_fails_verification(self) -> None:
        self._three_events()
        self.store.conn.execute("UPDATE credit_events SET amount=10 WHERE seq=1")
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("event_hash" in error for error in errors))

    def test_deleted_middle_event_fails_verification(self) -> None:
        self._three_events()
        self.store.conn.execute("DELETE FROM credit_events WHERE seq=2")
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("prev_hash" in error for error in errors))
        with self.assertRaises(LedgerError):
            rebuild_ledger(self.store.conn, self.store.signer)

    def test_tail_deletion_is_not_detected_without_external_anchor(self) -> None:
        # Documented residual risk: a local administrator who can delete the
        # last event leaves a valid prefix chain. Production needs an external
        # anchor/audit export; this test makes the limitation explicit.
        self._three_events()
        self.store.conn.execute("DELETE FROM credit_events WHERE seq=3")
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertTrue(ok, errors)
        self.assertEqual(self.store.balances(), {"peer-a": 2})

    def test_tampered_event_type_fails_verification_without_crashing(self) -> None:
        self._credit_one()
        self.store.conn.execute("UPDATE credit_events SET event_type='evil' WHERE seq=1")
        self.store.conn.commit()
        ok, errors = verify_ledger(self.store.conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("event_type" in error for error in errors))
        with self.assertRaises(LedgerError):
            rebuild_ledger(self.store.conn, self.store.signer)

    def test_null_tampering_yields_structured_errors_not_crashes(self) -> None:
        # Direct DB-file tampering can produce NULLs despite NOT NULL columns;
        # verification must report structured errors instead of raising.
        import sqlite3

        conn = sqlite3.connect(Path(self._tmp.name) / "tampered.db")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE credit_events ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT, "
            "peer_id TEXT, task_id TEXT, amount INTEGER, idempotency_key TEXT, "
            "created_at TEXT, prev_hash TEXT, event_hash TEXT, signature TEXT, "
            "metadata_json TEXT)"
        )
        conn.execute(
            "INSERT INTO credit_events "
            "(event_id,event_type,peer_id,task_id,amount,idempotency_key,created_at,prev_hash,event_hash,signature,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("evt_1", "CREDIT", "peer-a", "tsk_1", None, "k1", "2026-01-01T00:00:00+00:00", "0" * 64, None, None, "{}"),
        )
        conn.execute(
            "INSERT INTO credit_events "
            "(event_id,event_type,peer_id,task_id,amount,idempotency_key,created_at,prev_hash,event_hash,signature,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("evt_2", "CREDIT", "peer-a", "tsk_2", 1, "k2", "2026-01-01T00:00:00+00:00", "0" * 64, "h" * 64, None, "{}"),
        )
        conn.commit()

        ok, errors = verify_ledger(conn, self.store.signer)
        self.assertFalse(ok)
        self.assertTrue(any("malformed" in error for error in errors))
        self.assertTrue(any("missing event_hash or signature" in error for error in errors))

        events, read_errors = iter_events_lenient(conn)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "evt_2")
        self.assertTrue(any("malformed" in error for error in read_errors))


if __name__ == "__main__":
    unittest.main()
