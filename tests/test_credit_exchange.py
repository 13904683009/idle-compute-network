"""CREDIT-for-compute closure: pay for a task with earned CREDIT.

Semantics under test:
- available = earned ledger CREDIT - (HELD + SPENT holds); the ledger itself is
  untouched (no negative/CREDIT event rewrite);
- create_task_paid checks the balance and inserts task + HELD hold in ONE
  transaction; insufficient funds or invalid input rolls everything back;
- success -> SPENT, failure/cancel/expiry -> REFUNDED, exactly once;
- a coordinator with credits.task_cost > 0 rejects a poor client with the
  explicit INSUFFICIENT_CREDIT wire code (17).
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from idle_compute.config import from_dict
from idle_compute.crypto import content_hash, result_signing_payload
from idle_compute.db import (
    HOLD_HELD,
    HOLD_REFUNDED,
    HOLD_SPENT,
    InsufficientCredit,
    Store,
)
from idle_compute.identity import NodeIdentity
from idle_compute.node import Node
from idle_compute.protocol import ErrorCode, MessageType
from idle_compute.tasks import InvalidTaskInput
from idle_compute.worker import Worker


def _future(days: int = 1) -> str:
    return datetime.fromtimestamp(time.time() + days * 86400, timezone.utc).isoformat()


class CreditExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="credit-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "coordinator.db"
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
            validation = self.store.validate_result(result.result_id, "validator")
            self.assertEqual(validation.status.value, "VALID")

    def _count_tasks(self) -> int:
        return int(self.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    # ------------------------------------------------------------- store API

    def test_insufficient_credit_rolls_back_the_task(self) -> None:
        with self.assertRaises(InsufficientCredit) as ctx:
            self.store.create_task_paid(
                "numeric_batch_v1", {"numbers": [1]}, requester_id="poor", cost=1
            )
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_CREDIT")
        self.assertEqual(ctx.exception.available, 0)
        self.assertEqual(self._count_tasks(), 0)
        self.assertIsNone(
            self.store.conn.execute("SELECT task_id FROM credit_holds").fetchone()
        )

    def test_invalid_payload_rolls_back_without_charging(self) -> None:
        self._earn("buyer")
        before = self.store.available_credit("buyer")
        with self.assertRaises(InvalidTaskInput):
            self.store.create_task_paid(
                "numeric_batch_v1", {"numbers": "not-a-list"}, requester_id="buyer", cost=1
            )
        self.assertEqual(self.store.available_credit("buyer"), before)
        self.assertEqual(self._count_tasks(), 1, "only the earned task exists")
        self.assertIsNone(
            self.store.conn.execute(
                "SELECT task_id FROM credit_holds WHERE requester_id='buyer'"
            ).fetchone()
        )

    def test_successful_purchase_holds_credit(self) -> None:
        self._earn("buyer")
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        hold = self.store.get_credit_hold(task.task_id)
        self.assertEqual(hold["state"], HOLD_HELD)
        self.assertEqual(hold["requester_id"], "buyer")
        self.assertEqual(hold["amount"], 1)
        self.assertEqual(self.store.available_credit("buyer"), 0)

    def test_second_purchase_rejected_while_first_is_held(self) -> None:
        self._earn("buyer")
        self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        with self.assertRaises(InsufficientCredit):
            self.store.create_task_paid(
                "numeric_batch_v1", {"numbers": [2]}, requester_id="buyer", cost=1
            )
        self.assertEqual(self._count_tasks(), 2, "earn task + first paid task only")

    def test_zero_cost_creates_no_hold(self) -> None:
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="anyone", cost=0
        )
        self.assertIsNone(self.store.get_credit_hold(task.task_id))

    def test_completion_spends_hold(self) -> None:
        self._earn("buyer")
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1, 2]}, requester_id="buyer", cost=1
        )
        lease = self.store.claim_task("worker", task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, "worker")
        worker = Worker("worker", self.store.signer)
        output, digest, signature = worker.execute(self.store.get_task(task.task_id), lease)
        result = self.store.submit_result(lease.lease_id, "worker", output, digest, signature)
        self.store.validate_result(result.result_id, "validator")
        hold = self.store.get_credit_hold(task.task_id)
        self.assertEqual(hold["state"], HOLD_SPENT)
        self.assertEqual(self.store.available_credit("buyer"), 0)

    def test_cancel_refunds_hold(self) -> None:
        self._earn("buyer")
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        self.store.cancel_task(task.task_id, "test cancel")
        self.assertEqual(self.store.get_credit_hold(task.task_id)["state"], HOLD_REFUNDED)
        self.assertEqual(self.store.available_credit("buyer"), 1)
        # The refunded credit can buy again.
        self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [2]}, requester_id="buyer", cost=1
        )

    def test_expiry_refunds_hold(self) -> None:
        self._earn("buyer")
        task = self.store.create_task_paid(
            "numeric_batch_v1",
            {"numbers": [1]},
            requester_id="buyer",
            cost=1,
            max_attempts=1,
        )
        lease = self.store.claim_task("worker", task.task_id)
        assert lease is not None
        self.store.expire_leases(now=_future())
        self.assertEqual(self.store.get_task(task.task_id).status.value, "EXPIRED")
        self.assertEqual(self.store.get_credit_hold(task.task_id)["state"], HOLD_REFUNDED)
        self.assertEqual(self.store.available_credit("buyer"), 1)

    def test_invalid_result_refunds_hold(self) -> None:
        self._earn("buyer")
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1, 2]}, requester_id="buyer", cost=1
        )
        lease = self.store.claim_task("worker", task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, "worker")
        wrong = {"numbers": [1, 2], "count": 2, "sum": 999, "sum_squares": 5}
        payload = result_signing_payload(task.task_id, lease.lease_id, "worker", content_hash(wrong))
        signature = self.store.signer.sign(payload)
        result = self.store.submit_result(
            lease.lease_id, "worker", wrong, content_hash(wrong), signature
        )
        validation = self.store.validate_result(result.result_id, "validator")
        self.assertEqual(validation.status.value, "INVALID")
        self.assertEqual(self.store.get_credit_hold(task.task_id)["state"], HOLD_REFUNDED)
        self.assertEqual(self.store.available_credit("buyer"), 1)


class NodeInsufficientCreditTests(unittest.TestCase):
    """The node handler itself rejects a poor requester with code 17."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="credit-node-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.client = NodeIdentity.load_or_create(self.root / "cl" / "identity.key", node_name="cl")
        self.node = Node(
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(self.root / "coord"),
                    "peers": [self.client.node_id],
                    "credits": {"task_cost": 1},
                    "log_level": "ERROR",
                }
            )
        )
        self.addCleanup(self.node.shutdown)
        self.store = Store(self.root / "coord" / "coordinator.db", self.root / "coord" / "ledger.key")
        self.addCleanup(self.store.close)
        self.node._store = self.store

    def test_poor_client_is_rejected_with_explicit_code(self) -> None:
        env = SimpleNamespace(
            type=MessageType.TASK_CREATE,
            sender=self.client.node_id,
            seq=1,
            body={
                "client_ref": "c1",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [1]},
                "max_attempts": 2,
                "lease_seconds": 30,
            },
        )
        frames: list[bytes] = []
        self.node._handle_task_create(env, frames.append)
        self.assertEqual(len(frames), 1)
        body = json.loads(frames[0][4:].decode("utf-8"))["body"]
        self.assertEqual(body["code"], ErrorCode.INSUFFICIENT_CREDIT.value)
        self.assertIn("INSUFFICIENT_CREDIT", body["message"])
        self.assertEqual(
            int(self.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]), 0
        )


if __name__ == "__main__":
    unittest.main()
