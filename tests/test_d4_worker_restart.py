"""D4 gap 6: worker restart / disconnect semantics (deterministic, no sleep).

Facts under test:
- the worker persists NO lease/authorization state; a fresh Node has no
  cancelled set and no local lease files;
- after (re)start the worker ONLY re-claims; it never executes without a fresh
  LEASE_COMMIT;
- an authorization captured before a restart is rejected after expiry, so an
  old commit cannot be replayed.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from idle_compute.config import from_dict
from idle_compute.identity import NodeIdentity
from idle_compute.lease import (
    ExecutionAuthorization,
    LeaseCommit,
    LeaseError,
    require_authorization,
)
from idle_compute.node import Node, NodeTimeoutError
from idle_compute.protocol import MessageType

NOW = int(time.time())


class WorkerRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="d4-restart-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord = NodeIdentity.load_or_create(self.root / "c" / "identity.key", node_name="c")
        self.worker = NodeIdentity.load_or_create(self.root / "w" / "identity.key", node_name="w")
        self.config = from_dict(
            {
                "role": "worker",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.root / "worker-data"),
                "peers": [self.coord.node_id],
                "coordinator_node_id": self.coord.node_id,
                "coordinator_addr": "127.0.0.1:1",
                "log_level": "ERROR",
            }
        )

    def _node(self) -> Node:
        node = Node(self.config)
        self.addCleanup(node.shutdown)
        return node

    def test_worker_persists_no_lease_or_authorization_state(self) -> None:
        node = self._node()
        data_dir = Path(self.config.data_dir)
        files = sorted(p.name for p in data_dir.glob("*") if p.is_file())
        self.assertNotIn("coordinator.db", files)
        self.assertFalse([name for name in files if "lease" in name.lower()])
        self.assertEqual(node._cancelled_leases, set())
        # Nothing on disk can be turned into an authorization: a fresh node
        # starts with no commit and therefore cannot execute.
        self.assertFalse(hasattr(node, "_authorization"))

    def test_restart_reclaims_and_never_executes_without_a_fresh_commit(self) -> None:
        node = self._node()
        sent: list[MessageType] = []
        executed: list[bool] = []

        def fake_send(peer, type_, body):
            sent.append(type_)
            return 1

        def fake_await(*args, **kwargs):
            raise NodeTimeoutError("no proposal in this deterministic test")

        node._send_to = fake_send  # type: ignore[assignment]
        node._await = fake_await  # type: ignore[assignment]
        node._execute_and_submit = lambda *a, **k: executed.append(True)  # type: ignore[assignment]

        node._claim_cycle_dual(self.coord.node_id)

        self.assertEqual(sent, [MessageType.CLAIM_REQUEST], "restart must start with a fresh claim")
        self.assertEqual(executed, [], "no execution without a freshly verified commit")

    def test_old_authorization_is_rejected_after_restart_and_expiry(self) -> None:
        terms = self._terms()
        commit = LeaseCommit(
            lease_id=terms.lease_id,
            task_id=terms.task_id,
            requester_node_id=terms.requester_node_id,
            worker_node_id=terms.worker_node_id,
            terms_hash="a" * 64,
            committed_at=NOW,
            expires_at=NOW + 5,
        ).sign(self.coord)
        authorization = ExecutionAuthorization(
            lease_id=terms.lease_id,
            task_id=terms.task_id,
            worker_node_id=terms.worker_node_id,
            requester_node_id=terms.requester_node_id,
            authority_node_id=self.coord.node_id,
            terms_hash="a" * 64,
            expires_at=NOW + 5,
            dual_signed=True,
            commit=commit,
        )
        # A fresh worker still rejects the old authorization once it expires.
        with self.assertRaises(LeaseError) as ctx:
            require_authorization(
                authorization,
                task_id=terms.task_id,
                worker_node_id=self.worker.node_id,
                now=NOW + 6,
                require_dual_signed=True,
            )
        self.assertEqual(ctx.exception.code, LeaseError.EXPIRED)

    def _terms(self):
        from idle_compute.lease import TaskLeaseTerms

        return TaskLeaseTerms(
            lease_id="lea_restart",
            task_id="task_restart",
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


if __name__ == "__main__":
    unittest.main()
