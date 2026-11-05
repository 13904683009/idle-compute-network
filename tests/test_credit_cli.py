"""CLI credit visibility: `credit` subcommand + Store.credit_summary.

Data sources only: Store.available_credit (earned minus holds) and the
credit_holds aggregation; the ledger is not rewritten.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from idle_compute.cli import main
from idle_compute.db import HOLD_HELD, HOLD_REFUNDED, HOLD_SPENT, Store
from idle_compute.worker import Worker


class CreditSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="credit-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "idle.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)

    def _earn(self, peer: str = "buyer") -> None:
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1, 2]})
        lease = self.store.claim_task(peer, task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, peer)
        worker = Worker(peer, self.store.signer)
        output, digest, signature = worker.execute(self.store.get_task(task.task_id), lease)
        result = self.store.submit_result(lease.lease_id, peer, output, digest, signature)
        self.store.validate_result(result.result_id, "validator")

    def _run(self, *args: str) -> dict:
        out = StringIO()
        with redirect_stdout(out):
            code = main([*args, "--db", str(self.db)])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_summary_tracks_held_spent_and_refunded(self) -> None:
        self._earn()
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        summary = self.store.credit_summary()["buyer"]
        self.assertEqual(summary, {"earned": 1, "held": 1, "spent": 0, "refunded": 0, "available": 0})

        # Complete the paid task -> SPENT.
        lease = self.store.claim_task("worker", task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, "worker")
        worker = Worker("worker", self.store.signer)
        output, digest, signature = worker.execute(self.store.get_task(task.task_id), lease)
        result = self.store.submit_result(lease.lease_id, "worker", output, digest, signature)
        self.store.validate_result(result.result_id, "validator")
        self.assertEqual(self.store.get_credit_hold(task.task_id)["state"], HOLD_SPENT)
        summary = self.store.credit_summary()["buyer"]
        self.assertEqual(summary, {"earned": 1, "held": 0, "spent": 1, "refunded": 0, "available": 0})

        # A cancelled task refunds the hold.
        self._earn("buyer")  # earn another credit (available 1)
        task2 = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [2]}, requester_id="buyer", cost=1
        )
        self.store.cancel_task(task2.task_id, "test")
        self.assertEqual(self.store.get_credit_hold(task2.task_id)["state"], HOLD_REFUNDED)
        summary = self.store.credit_summary()["buyer"]
        self.assertEqual(summary, {"earned": 2, "held": 0, "spent": 1, "refunded": 1, "available": 1})

    def test_cli_credit_for_peer(self) -> None:
        self._earn()
        self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        payload = self._run("credit", "--peer", "buyer")
        self.assertEqual(
            payload["credit"]["buyer"],
            {"earned": 1, "held": 1, "spent": 0, "refunded": 0, "available": 0},
        )
        self.assertEqual(self.store.get_credit_hold(
            self.store.conn.execute("SELECT task_id FROM credit_holds").fetchone()[0]
        )["state"], HOLD_HELD)

    def test_cli_unknown_peer_is_zeroed(self) -> None:
        payload = self._run("credit", "--peer", "nobody")
        self.assertEqual(
            payload["credit"]["nobody"],
            {"earned": 0, "held": 0, "spent": 0, "refunded": 0, "available": 0},
        )

    def test_cli_lists_all_peers_sorted(self) -> None:
        self._earn("worker")
        self._earn("buyer")
        self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        payload = self._run("credit")
        self.assertEqual(list(payload["credit"]), ["buyer", "worker"])
        self.assertEqual(payload["credit"]["worker"]["earned"], 1)
        self.assertEqual(payload["credit"]["buyer"]["held"], 1)


if __name__ == "__main__":
    unittest.main()
