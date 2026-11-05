"""C5 minimal disconnect/restart semantics (no automatic recovery).

Proves that after a process restart:
- a paid task is still QUEUED (never auto-ACTIVE / auto-executed);
- its CREDIT hold is still HELD (not SPENT);
- there is no lease / lease negotiation yet;
- only an explicit claim can create a lease, and only an explicit
  proposal -> accept -> activate sequence can authorize execution.

This is a durability/fail-closed test. It does NOT implement or claim
automatic reconnection/recovery (explicitly out of scope; D6 design item).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from idle_compute.db import HOLD_HELD, InvalidNegotiationState, Store, TaskUnavailable
from idle_compute.identity import NodeIdentity
from idle_compute.lease import TaskLeaseTerms, terms_hash
from idle_compute.worker import Worker

ROOT = Path(__file__).resolve().parent.parent
NOW = int(time.time())


class C5RestartSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="c5-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "coordinator.db"
        self.key = self.root / "ledger.key"
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.worker = NodeIdentity.load_or_create(self.root / "w" / "identity.key", node_name="w")

    def _earn(self, peer: str = "buyer") -> None:
        task = self.store.create_task("numeric_batch_v1", {"numbers": [1, 2]})
        lease = self.store.claim_task(peer, task.task_id)
        assert lease is not None
        self.store.start_task(lease.lease_id, peer)
        worker = Worker(peer, self.store.signer)
        output, digest, signature = worker.execute(self.store.get_task(task.task_id), lease)
        result = self.store.submit_result(lease.lease_id, peer, output, digest, signature)
        self.store.validate_result(result.result_id, "validator")

    def _counts(self, task_id: str) -> dict[str, int]:
        return {
            "leases": int(self.store.conn.execute("SELECT COUNT(*) FROM leases WHERE task_id=?", (task_id,)).fetchone()[0]),
            "negotiations": int(
                self.store.conn.execute("SELECT COUNT(*) FROM lease_negotiations WHERE task_id=?", (task_id,)).fetchone()[0]
            ),
            "results": int(self.store.conn.execute("SELECT COUNT(*) FROM results WHERE task_id=?", (task_id,)).fetchone()[0]),
        }

    def _process_view(self, task_id: str) -> dict:
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from idle_compute.db import Store\n"
            "s = Store(sys.argv[1], sys.argv[2])\n"
            "status = s.conn.execute('SELECT status FROM tasks WHERE task_id=?', (sys.argv[3],)).fetchone()[0]\n"
            "hold = s.conn.execute('SELECT state FROM credit_holds WHERE task_id=?', (sys.argv[3],)).fetchone()\n"
            "leases = s.conn.execute('SELECT COUNT(*) FROM leases WHERE task_id=?', (sys.argv[3],)).fetchone()[0]\n"
            "negs = s.conn.execute('SELECT COUNT(*) FROM lease_negotiations WHERE task_id=?', (sys.argv[3],)).fetchone()[0]\n"
            "print(json.dumps({'status': status, 'hold': None if hold is None else hold[0], "
            "'leases': leases, 'negotiations': negs}))\n"
            "s.close()\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.db), str(self.key), task_id],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip())

    def test_restart_keeps_task_queued_and_hold_held(self) -> None:
        self._earn("buyer")
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        self.assertEqual(task.status.value, "QUEUED")
        self.assertEqual(self.store.get_credit_hold(task.task_id)["state"], HOLD_HELD)
        self.assertEqual(self._counts(task.task_id), {"leases": 0, "negotiations": 0, "results": 0})

        # Real process boundary: close, then another process opens the same DB.
        self.store.close()
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)
        view = self._process_view(task.task_id)
        self.assertEqual(view["status"], "QUEUED")
        self.assertEqual(view["hold"], HOLD_HELD)
        self.assertEqual(view["leases"], 0)
        self.assertEqual(view["negotiations"], 0)

        # The reopened store does not auto-create a lease/authorization either.
        self.assertEqual(self.store.get_task(task.task_id).status.value, "QUEUED")
        self.assertEqual(self.store.available_credit("buyer"), 0)

    def test_only_explicit_claim_and_dual_sign_flow_can_authorize(self) -> None:
        self._earn("buyer")
        task = self.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [1]}, requester_id="buyer", cost=1
        )
        self.store.close()
        self.store = Store(self.db, self.key)
        self.addCleanup(self.store.close)

        # A claim (worker action) creates the lease, but NOT an execution
        # authorization: there is no negotiation until the coordinator proposes.
        lease = self.store.claim_task(self.worker.node_id, task.task_id)
        assert lease is not None
        self.assertEqual(self.store.get_task(task.task_id).status.value, "LEASED")
        self.assertIsNone(self.store.get_negotiation(lease.lease_id))
        with self.assertRaises((InvalidNegotiationState, TaskUnavailable)):
            self.store.activate_negotiation(lease.lease_id)
        self.assertEqual(self.store.get_credit_hold(task.task_id)["state"], HOLD_HELD)

        # The explicit proposal starts PROPOSED; restart alone never ACTIVE.
        terms = TaskLeaseTerms(
            lease_id=lease.lease_id,
            task_id=task.task_id,
            requester_node_id=self.coord.node_id,
            worker_node_id=self.worker.node_id,
            task_spec_hash="a" * 64,
            input_manifest_hash="",
            lease_version=1,
            issued_at=NOW,
            expires_at=NOW + 60,
            execution_deadline=NOW + 60,
            verification_policy={"mode": "RECOMPUTE", "required": False},
            reward_policy={"amount": 1},
        ).sign_proposal(self.coord)
        self.store.create_negotiation_for_lease(
            lease_id=lease.lease_id,
            task_id=task.task_id,
            requester_id=self.coord.node_id,
            worker_id=self.worker.node_id,
            authority_id=self.coord.node_id,
            terms_json=json.dumps(terms.to_dict()),
            terms_hash=terms_hash(terms),
            requester_signature=terms.requester_signature,
            expires_at=datetime.fromtimestamp(NOW + 60, timezone.utc).isoformat(),
        )
        self.assertEqual(self.store.get_negotiation(lease.lease_id)["state"], "PROPOSED")
        with self.assertRaises(InvalidNegotiationState):
            self.store.activate_negotiation(lease.lease_id)
        self.assertEqual(self.store.get_negotiation(lease.lease_id)["state"], "PROPOSED")


if __name__ == "__main__":
    unittest.main()
