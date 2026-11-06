"""Client-side visibility: `client-tasks` CLI + Store.client_tasks."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from idle_compute.cli import main
from idle_compute.db import Store
from idle_compute.worker import Worker


class ClientTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="client-tasks-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "idle.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)

    def _earn(self, peer: str, times: int = 1) -> None:
        for _ in range(times):
            task = self.store.create_task("numeric_batch_v1", {"numbers": [1, 2]})
            lease = self.store.claim_task(peer, task.task_id)
            assert lease is not None
            self.store.start_task(lease.lease_id, peer)
            worker = Worker(peer, self.store.signer)
            output, digest, signature = worker.execute(self.store.get_task(task.task_id), lease)
            result = self.store.submit_result(lease.lease_id, peer, output, digest, signature)
            self.store.validate_result(result.result_id, "validator")

    def _complete_paid(self, requester: str) -> str:
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [3, 4]}, requester_id=requester, cost=1
        )
        lease = self.store.claim_task("worker", task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, "worker")
        worker = Worker("worker", self.store.signer)
        output, digest, signature = worker.execute(self.store.get_task(task.task_id), lease)
        result = self.store.submit_result(lease.lease_id, "worker", output, digest, signature)
        self.store.validate_result(result.result_id, "validator")
        return task.task_id

    def test_lists_free_paid_held_spent_refunded(self) -> None:
        self._earn("buyer", times=3)
        spent_id = self._complete_paid("buyer")

        refunded = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [5]}, requester_id="buyer", cost=1
        )
        self.store.cancel_task(refunded.task_id, "test cancel")

        free = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [6]}, requester_id="buyer", cost=0
        )
        held = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [7]}, requester_id="buyer", cost=1
        )

        payload = self.store.client_tasks("buyer")
        self.assertEqual(payload["count"], 4)
        self.assertEqual(payload["total_spent"], 1)
        by_id = {task["task_id"]: task for task in payload["tasks"]}
        self.assertEqual(by_id[spent_id]["credit_state"], "SPENT")
        self.assertEqual(by_id[spent_id]["cost"], 1)
        self.assertEqual(by_id[refunded.task_id]["credit_state"], "REFUNDED")
        self.assertEqual(by_id[free.task_id]["credit_state"], "FREE")
        self.assertEqual(by_id[free.task_id]["cost"], 0)
        self.assertEqual(by_id[held.task_id]["credit_state"], "HELD")

    def test_legacy_tasks_without_requester_are_not_listed(self) -> None:
        legacy = self.store.create_task("numeric_batch_v1", {"numbers": [1]})
        self._earn("buyer")
        self.assertEqual(self.store.client_tasks("buyer")["count"], 0)
        self.assertNotIn(legacy.task_id, [t["task_id"] for t in self.store.client_tasks("buyer")["tasks"]])

    def test_cli_client_tasks_outputs_json(self) -> None:
        self._earn("buyer")
        task_id = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [8]}, requester_id="buyer", cost=0
        ).task_id
        out = StringIO()
        with redirect_stdout(out):
            code = main(["client-tasks", "--db", str(self.db), "--peer", "buyer"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tasks"][0]["task_id"], task_id)
        self.assertEqual(payload["tasks"][0]["credit_state"], "FREE")


if __name__ == "__main__":
    unittest.main()
