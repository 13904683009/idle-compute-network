from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from idle_compute.crypto import content_hash, result_signing_payload
from idle_compute.db import Store
from idle_compute.identity import NodeIdentity
from idle_compute.ledger import (
    MAX_SYNC_BATCH,
    LedgerError,
    append_credit,
    ensure_ledger_copy,
    export_since,
    iter_events_lenient,
    rebuild_ledger,
    verify_and_append,
    verify_ledger,
)
from idle_compute.signing import Ed25519Policy
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


class ExportSinceTests(unittest.TestCase):
    """B-3: LEDGER_SYNC batching via export_since."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "idle.db", Path(self._tmp.name) / "ledger.key")
        self.addCleanup(self.store.close)

    def _seed(self, count: int) -> None:
        with self.store.transaction():
            for i in range(count):
                event, created = append_credit(
                    self.store.conn, self.store.signer, f"peer-{i}", f"tsk_{i}"
                )
                assert created

    def test_batches_are_capped_at_max_sync_batch(self) -> None:
        self._seed(150)
        first = export_since(self.store.conn, 0)
        self.assertEqual(len(first), MAX_SYNC_BATCH)
        self.assertEqual([e["seq"] for e in first], list(range(1, MAX_SYNC_BATCH + 1)))
        rest = export_since(self.store.conn, first[-1]["seq"])
        self.assertEqual(len(rest), 50)
        self.assertEqual([e["seq"] for e in rest], list(range(MAX_SYNC_BATCH + 1, 151)))

    def test_limit_is_clamped(self) -> None:
        self._seed(5)
        self.assertEqual(len(export_since(self.store.conn, 0, limit=500)), 5)
        self.assertEqual(export_since(self.store.conn, 0, limit=-3), [])

    def test_beyond_head_returns_empty_page(self) -> None:
        self._seed(3)
        self.assertEqual(export_since(self.store.conn, 3), [])
        self.assertEqual(export_since(self.store.conn, 999), [])

    def test_exports_include_sig_algo(self) -> None:
        self._seed(1)
        page = export_since(self.store.conn, 0)
        self.assertEqual(page[0]["sig_algo"], "hmac-sha256")
        for key in ("seq", "event_id", "event_hash", "signature", "metadata_json"):
            self.assertIn(key, page[0])


class VerifyAndAppendTests(unittest.TestCase):
    """B-3: worker-side mirror append with full per-event verification."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        # A real coordinator chain signed with Ed25519.
        self.coordinator = NodeIdentity.load_or_create(
            root / "coordinator" / "identity.key", node_name="coordinator"
        )
        self.coord_store = Store(
            root / "coord.db", signer=Ed25519Policy.from_identity(self.coordinator)
        )
        self.addCleanup(self.coord_store.close)
        self._seed_chain(3)
        # The worker mirror: fresh sqlite conn + pinned-coordinator policy.
        self.mirror = sqlite3.connect(root / "mirror.db")
        self.mirror.row_factory = sqlite3.Row
        self.addCleanup(self.mirror.close)
        self.worker_policy = Ed25519Policy.from_identity(
            self.coordinator, coordinator_node_id=self.coordinator.node_id
        )

    def _seed_chain(self, count: int) -> None:
        with self.coord_store.transaction():
            for i in range(count):
                append_credit(
                    self.coord_store.conn,
                    self.coord_store.signer,
                    f"peer-{i}",
                    f"tsk_{i}",
                )

    def _exported(self, seq: int) -> dict:
        return export_since(self.coord_store.conn, seq - 1, limit=1)[0]

    def test_valid_event_appends_verified(self) -> None:
        for seq in (1, 2, 3):
            ok, error = verify_and_append(self.mirror, self._exported(seq), self.worker_policy)
            self.assertTrue(ok, error)
        rows = self.mirror.execute("SELECT seq, verified FROM ledger_copy ORDER BY seq").fetchall()
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3])
        self.assertTrue(all(r["verified"] == 1 for r in rows))

    def test_duplicate_reappend_is_idempotent(self) -> None:
        event = self._exported(1)
        self.assertTrue(verify_and_append(self.mirror, event, self.worker_policy)[0])
        ok, error = verify_and_append(self.mirror, event, self.worker_policy)
        self.assertTrue(ok, error)
        count = self.mirror.execute("SELECT COUNT(*) AS n FROM ledger_copy").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_tampered_signature_is_rejected(self) -> None:
        event = self._exported(1)
        event["signature"] = "00" * 64
        ok, error = verify_and_append(self.mirror, event, self.worker_policy)
        self.assertFalse(ok)
        self.assertIn("signature invalid", error)
        self._assert_empty()

    def test_seq_gap_is_rejected(self) -> None:
        self.assertTrue(verify_and_append(self.mirror, self._exported(1), self.worker_policy)[0])
        ok, error = verify_and_append(self.mirror, self._exported(3), self.worker_policy)
        self.assertFalse(ok)
        self.assertIn("seq gap", error)

    def test_prev_hash_linkage_break_is_rejected(self) -> None:
        self.assertTrue(verify_and_append(self.mirror, self._exported(1), self.worker_policy)[0])
        event = self._exported(2)
        event["prev_hash"] = "f" * 64
        ok, error = verify_and_append(self.mirror, event, self.worker_policy)
        self.assertFalse(ok)
        self.assertIn("prev_hash linkage broken", error)

    def test_hmac_tagged_event_is_conflict(self) -> None:
        # Build an HMAC-tagged event row and inject it into the sync stream.
        legacy = Store(Path(self._tmp.name) / "legacy.db", Path(self._tmp.name) / "ledger.key")
        self.addCleanup(legacy.close)
        with legacy.transaction():
            append_credit(legacy.conn, legacy.signer, "peer-h", "tsk_h")
        hmac_event = export_since(legacy.conn, 0)[0]
        ok, error = verify_and_append(self.mirror, hmac_event, self.worker_policy)
        self.assertFalse(ok)
        self.assertIn("CONFLICT", error)
        self.assertIn("hmac-sha256", error)
        self._assert_empty()

    def test_malformed_event_is_rejected(self) -> None:
        ok, error = verify_and_append(self.mirror, {"seq": 1}, self.worker_policy)
        self.assertFalse(ok)
        self.assertIn("malformed", error)
        self._assert_empty()

    def _assert_empty(self) -> None:
        count = self.mirror.execute("SELECT COUNT(*) AS n FROM ledger_copy").fetchone()["n"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
