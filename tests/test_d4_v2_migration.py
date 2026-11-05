"""D4 gap recheck fix: a real v2 database (with tasks/leases, no D4 table)
upgrades to v3 without losing data, and repeated opens do not re-migrate.

This closes the D4-ACCEPTANCE-MATRIX E2 claim, which previously had no test
with existing task/lease rows (only the v1 ledger fixture existed).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from idle_compute.db import Store
from idle_compute.models import LeaseStatus, TaskStatus


class V2MigrationWithDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-v2-migrate-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "coordinator.db"
        self.key = self.root / "ledger.key"

    def _make_v2_with_data(self) -> tuple[str, str]:
        store = Store(self.db, self.key)
        task = store.create_task("numeric_batch_v1", {"numbers": [1, 2]})
        lease = store.claim_task("peer-a", task.task_id)
        assert lease is not None
        store.close()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            conn.execute("DROP TABLE lease_negotiations")  # v2 did not have it
            conn.commit()
        finally:
            conn.close()
        return task.task_id, lease.lease_id

    def test_v2_with_tasks_and_leases_upgrades_preserving_data(self) -> None:
        task_id, lease_id = self._make_v2_with_data()
        store = Store(self.db, self.key)
        self.addCleanup(store.close)
        version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(version, "3")
        table = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lease_negotiations'"
        ).fetchone()
        self.assertIsNotNone(table)
        self.assertEqual(store.get_task(task_id).status, TaskStatus.LEASED)
        lease = store.get_lease(lease_id)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.status, LeaseStatus.ACTIVE)

    def test_repeated_open_does_not_remigrate(self) -> None:
        task_id, _ = self._make_v2_with_data()
        for _ in range(2):
            store = Store(self.db, self.key)
            try:
                version = store.conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                self.assertEqual(version, "3")
                self.assertIsNotNone(store.get_task(task_id))
                self.assertIsNotNone(
                    store.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='lease_negotiations'"
                    ).fetchone()
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
