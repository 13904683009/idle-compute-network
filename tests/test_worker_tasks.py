"""Worker-side visibility: `worker-tasks` CLI + Store.worker_tasks."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from idle_compute.cli import main
from idle_compute.crypto import content_hash, result_signing_payload
from idle_compute.db import Store
from idle_compute.worker import Worker


class WorkerTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="worker-tasks-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "idle.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)

    def _complete(self, peer: str, numbers: list[int]) -> str:
        task = self.store.create_task("numeric_batch_v1", {"numbers": numbers})
        lease = self.store.claim_task(peer, task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, peer)
        worker = Worker(peer, self.store.signer)
        output, digest, signature = worker.execute(self.store.get_task(task.task_id), lease)
        result = self.store.submit_result(lease.lease_id, peer, output, digest, signature)
        self.store.validate_result(result.result_id, "validator")
        return task.task_id

    def _rejected(self, peer: str, numbers: list[int]) -> str:
        task = self.store.create_task("numeric_batch_v1", {"numbers": numbers})
        lease = self.store.claim_task(peer, task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, peer)
        wrong = {"numbers": numbers, "count": len(numbers), "sum": 999, "sum_squares": 0}
        digest = content_hash(wrong)
        signature = self.store.signer.sign(
            result_signing_payload(task.task_id, lease.lease_id, peer, digest)
        )
        result = self.store.submit_result(lease.lease_id, peer, wrong, digest, signature)
        validation = self.store.validate_result(result.result_id, "validator")
        self.assertEqual(validation.status.value, "INVALID")
        return task.task_id

    def test_store_lists_completed_and_rejected_tasks_with_credit(self) -> None:
        completed = self._complete("worker", [1, 2])
        rejected = self._rejected("worker", [3, 4])
        other = self._complete("other-worker", [5, 6])
        result = self.store.worker_tasks("worker")
        self.assertEqual(result["peer"], "worker")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total_credit"], 1)
        by_id = {task["task_id"]: task for task in result["tasks"]}
        self.assertEqual(by_id[completed]["task_status"], "COMPLETED")
        self.assertEqual(by_id[completed]["credit"], 1)
        self.assertEqual(by_id[rejected]["task_status"], "REJECTED")
        self.assertEqual(by_id[rejected]["credit"], 0)
        self.assertNotIn(other, by_id)

    def test_cli_worker_tasks_outputs_json(self) -> None:
        task_id = self._complete("worker", [7, 8])
        out = StringIO()
        with redirect_stdout(out):
            code = main(["worker-tasks", "--db", str(self.db), "--peer", "worker", "--limit", "10"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["total_credit"], 1)
        self.assertEqual(payload["tasks"][0]["task_id"], task_id)
        self.assertEqual(payload["tasks"][0]["task_status"], "COMPLETED")

    def test_unknown_worker_returns_empty_list(self) -> None:
        payload = self.store.worker_tasks("nobody")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["total_credit"], 0)
        self.assertEqual(payload["tasks"], [])


if __name__ == "__main__":
    unittest.main()
