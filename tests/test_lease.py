"""D1: two-party TaskLease protocol tests (domains, replay, worker gate)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from idle_compute.identity import NodeIdentity
from idle_compute.lease import ACCEPT_DOMAIN, LEASE_DOMAIN, LeaseError, TaskLeaseTerms

NOW = 1_800_000_000
HASH_A = "a" * 64
HASH_B = "b" * 64


def terms(requester: str, worker: str, **overrides) -> TaskLeaseTerms:
    base = TaskLeaseTerms(
        lease_id="lea_1",
        task_id="tsk_1",
        requester_node_id=requester,
        worker_node_id=worker,
        task_spec_hash=HASH_A,
        input_manifest_hash="",
        lease_version=1,
        issued_at=NOW,
        expires_at=NOW + 60,
        execution_deadline=NOW + 120,
        verification_policy={"mode": "RECOMPUTE", "required": True},
        reward_policy={"amount": 1},
    )
    return replace(base, **overrides)


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="lease-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.requester = NodeIdentity.load_or_create(root / "r" / "identity.key", node_name="r")
        self.worker = NodeIdentity.load_or_create(root / "w" / "identity.key", node_name="w")
        self.other_worker = NodeIdentity.load_or_create(root / "x" / "identity.key", node_name="x")

    def signed(self) -> TaskLeaseTerms:
        return terms(self.requester.node_id, self.worker.node_id).sign_proposal(self.requester)

    def test_proposal_and_acceptance_verify(self) -> None:
        lease = self.signed()
        lease.verify_proposal(NOW)
        accepted = lease.sign_accept(self.worker)
        accepted.verify_accept(NOW)
        accepted.verify_for_worker(self.worker.node_id, NOW)
        self.assertEqual(TaskLeaseTerms.from_dict(accepted.to_dict()).acceptance(), accepted.acceptance())

    def test_domains_are_distinct(self) -> None:
        self.assertNotEqual(LEASE_DOMAIN, ACCEPT_DOMAIN)

    def test_tampered_terms_break_the_proposal_signature(self) -> None:
        lease = self.signed()
        tampered = replace(lease, task_spec_hash=HASH_B)
        with self.assertRaises(LeaseError) as ctx:
            tampered.verify_proposal(NOW)
        self.assertEqual(ctx.exception.code, LeaseError.SIGNATURE)

    def test_wrong_worker_cannot_accept(self) -> None:
        lease = self.signed()
        with self.assertRaises(LeaseError) as ctx:
            lease.sign_accept(self.other_worker)
        self.assertEqual(ctx.exception.code, LeaseError.WRONG_WORKER)

    def test_acceptance_cannot_be_replayed_onto_other_terms(self) -> None:
        accepted = self.signed().sign_accept(self.worker)
        replayed = replace(accepted, task_spec_hash=HASH_B)
        with self.assertRaises(LeaseError) as ctx:
            replayed.verify_accept(NOW)
        self.assertEqual(ctx.exception.code, LeaseError.SIGNATURE)

    def test_expired_lease_is_rejected(self) -> None:
        lease = self.signed()
        with self.assertRaises(LeaseError) as ctx:
            lease.verify_for_worker(self.worker.node_id, NOW + 61)
        self.assertEqual(ctx.exception.code, LeaseError.EXPIRED)

    def test_wrong_worker_gate(self) -> None:
        lease = self.signed()
        with self.assertRaises(LeaseError) as ctx:
            lease.verify_for_worker(self.other_worker.node_id, NOW)
        self.assertEqual(ctx.exception.code, LeaseError.WRONG_WORKER)

    def test_accept_without_proposal_is_refused(self) -> None:
        unsigned = terms(self.requester.node_id, self.worker.node_id)
        with self.assertRaises(LeaseError) as ctx:
            unsigned.sign_accept(self.worker)
        self.assertEqual(ctx.exception.code, LeaseError.NOT_ACCEPTED)

    def test_execution_deadline_must_not_precede_expiry(self) -> None:
        with self.assertRaises(LeaseError):
            terms(self.requester.node_id, self.worker.node_id, execution_deadline=NOW + 30).sign_proposal(
                self.requester
            )

    def test_schema_is_exact(self) -> None:
        lease = self.signed().to_dict()
        lease["extra"] = "x"
        with self.assertRaises(LeaseError):
            TaskLeaseTerms.from_dict(lease)

    def test_requester_and_worker_must_differ(self) -> None:
        with self.assertRaises(LeaseError):
            terms(self.requester.node_id, self.requester.node_id).sign_proposal(self.requester)


if __name__ == "__main__":
    unittest.main()
