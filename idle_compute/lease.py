"""Two-party task lease protocol (D1).

A lease is the bridge between "matching chose this worker" and the existing
authoritative task record. It is signed by BOTH sides:

- the requester (in MVP: the coordinator, see ADR-D0-2) signs the terms;
- the worker signs an acceptance over a small, fixed subset
  (``lease_id``, ``worker_node_id``, ``task_spec_hash``, ``lease_version``,
  ``expires_at``) so an acceptance cannot be replayed onto different terms.

The lease does NOT replace ``leases`` / ``one_active_lease`` in db.py: the
negotiation is recorded separately and only the winner creates the DB lease
(ADR-D0-16). Domain separation keeps lease signatures distinct from envelope,
result and credit signatures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .crypto import canonical_json
from .identity import NodeIdentity, is_valid_node_id

SCHEMA_VERSION = 1
LEASE_DOMAIN = "idle-compute-lease-v1"
ACCEPT_DOMAIN = "idle-compute-lease-accept-v1"
MAX_HASH = 64
MAX_POLICY = 1024
_HEX64_PATTERN = r"[0-9a-f]{64}"

_TERMS_KEYS = (
    "schema_version",
    "lease_id",
    "task_id",
    "requester_node_id",
    "worker_node_id",
    "task_spec_hash",
    "input_manifest_hash",
    "lease_version",
    "issued_at",
    "expires_at",
    "execution_deadline",
    "verification_policy",
    "reward_policy",
)
_SIGNED_KEYS = _TERMS_KEYS + ("requester_signature", "worker_signature")


class LeaseError(Exception):
    SCHEMA = "SCHEMA"
    SIGNATURE = "SIGNATURE"
    EXPIRED = "EXPIRED"
    WRONG_WORKER = "WRONG_WORKER"
    NOT_ACCEPTED = "NOT_ACCEPTED"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LeaseError(LeaseError.SCHEMA, message)


def _exact(value: Any, keys: tuple[str, ...], label: str) -> dict:
    _require(isinstance(value, dict), f"{label} must be an object")
    if set(value) != set(keys):
        raise LeaseError(LeaseError.SCHEMA, f"{label} keys must be exactly {sorted(keys)}")
    return value


def _canonical(domain: str, payload: dict) -> str:
    return canonical_json({"domain": domain, **payload})


def _verify_signature(node_id: str, payload: dict, domain: str, signature: str, label: str) -> None:
    if not isinstance(signature, str) or len(signature) != 128:
        raise LeaseError(LeaseError.SIGNATURE, f"{label} signature must be 128 hex chars")
    if not NodeIdentity.verify_signature(node_id, _canonical(domain, payload), signature):
        raise LeaseError(LeaseError.SIGNATURE, f"{label} signature does not match the payload")


@dataclass(frozen=True)
class TaskLeaseTerms:
    lease_id: str
    task_id: str
    requester_node_id: str
    worker_node_id: str
    task_spec_hash: str
    input_manifest_hash: str
    lease_version: int
    issued_at: int
    expires_at: int
    execution_deadline: int
    verification_policy: dict[str, Any]
    reward_policy: dict[str, Any]
    schema_version: int = SCHEMA_VERSION
    requester_signature: str = ""
    worker_signature: str = ""

    # -- payloads -----------------------------------------------------------

    def terms(self) -> dict[str, Any]:
        """The signed-by-requester body (no signatures)."""
        return {
            "schema_version": self.schema_version,
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "requester_node_id": self.requester_node_id,
            "worker_node_id": self.worker_node_id,
            "task_spec_hash": self.task_spec_hash,
            "input_manifest_hash": self.input_manifest_hash,
            "lease_version": self.lease_version,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "execution_deadline": self.execution_deadline,
            "verification_policy": dict(self.verification_policy),
            "reward_policy": dict(self.reward_policy),
        }

    def acceptance(self) -> dict[str, Any]:
        """The small body the worker signs (cannot be replayed onto other terms)."""
        return {
            "lease_id": self.lease_id,
            "worker_node_id": self.worker_node_id,
            "task_spec_hash": self.task_spec_hash,
            "lease_version": self.lease_version,
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.terms(),
            "requester_signature": self.requester_signature,
            "worker_signature": self.worker_signature,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "TaskLeaseTerms":
        data = _exact(value, _SIGNED_KEYS, "lease")
        terms = cls(
            lease_id=data["lease_id"],
            task_id=data["task_id"],
            requester_node_id=data["requester_node_id"],
            worker_node_id=data["worker_node_id"],
            task_spec_hash=data["task_spec_hash"],
            input_manifest_hash=data["input_manifest_hash"],
            lease_version=data["lease_version"],
            issued_at=data["issued_at"],
            expires_at=data["expires_at"],
            execution_deadline=data["execution_deadline"],
            verification_policy=dict(data["verification_policy"]),
            reward_policy=dict(data["reward_policy"]),
            schema_version=data["schema_version"],
            requester_signature=data["requester_signature"],
            worker_signature=data["worker_signature"],
        )
        _validate_terms(terms)
        return terms

    # -- signing ------------------------------------------------------------

    def sign_proposal(self, requester: NodeIdentity) -> "TaskLeaseTerms":
        _validate_terms(self)
        if requester.node_id != self.requester_node_id:
            raise LeaseError(LeaseError.SCHEMA, "signer is not the requester of this lease")
        return replace(
            self,
            requester_signature=requester.sign(_canonical(LEASE_DOMAIN, self.terms())),
        )

    def sign_accept(self, worker: NodeIdentity) -> "TaskLeaseTerms":
        _validate_terms(self)
        if worker.node_id != self.worker_node_id:
            raise LeaseError(LeaseError.WRONG_WORKER, "signer is not the worker of this lease")
        if not self.requester_signature:
            raise LeaseError(LeaseError.NOT_ACCEPTED, "cannot accept a lease without a requester signature")
        return replace(
            self,
            worker_signature=worker.sign(_canonical(ACCEPT_DOMAIN, self.acceptance())),
        )

    # -- verification -------------------------------------------------------

    def verify_proposal(self, now: int | None = None) -> None:
        _validate_terms(self)
        _verify_signature(
            self.requester_node_id, self.terms(), LEASE_DOMAIN, self.requester_signature, "requester"
        )
        if now is not None:
            if self.issued_at > now:
                raise LeaseError(LeaseError.SCHEMA, "lease issued_at is in the future")
            if self.expires_at <= now:
                raise LeaseError(LeaseError.EXPIRED, "lease has expired")

    def verify_accept(self, now: int | None = None) -> None:
        self.verify_proposal(now)
        if not self.worker_signature:
            raise LeaseError(LeaseError.NOT_ACCEPTED, "worker has not accepted this lease")
        _verify_signature(
            self.worker_node_id, self.acceptance(), ACCEPT_DOMAIN, self.worker_signature, "worker"
        )

    def verify_for_worker(self, worker_node_id: str, now: int) -> None:
        """The worker-side gate before executing anything (scheme §9, items 1-8)."""
        _validate_terms(self)
        if self.worker_node_id != worker_node_id:
            raise LeaseError(LeaseError.WRONG_WORKER, "lease is addressed to another worker")
        if self.expires_at <= now:
            raise LeaseError(LeaseError.EXPIRED, "lease has expired")
        if self.execution_deadline <= now:
            raise LeaseError(LeaseError.EXPIRED, "execution deadline has passed")
        _verify_signature(
            self.requester_node_id, self.terms(), LEASE_DOMAIN, self.requester_signature, "requester"
        )
        if self.worker_signature:
            _verify_signature(
                self.worker_node_id, self.acceptance(), ACCEPT_DOMAIN, self.worker_signature, "worker"
            )


def _validate_terms(terms: TaskLeaseTerms) -> None:
    _require(terms.schema_version == SCHEMA_VERSION, "unsupported schema_version")
    for label, value in (
        ("lease_id", terms.lease_id),
        ("task_id", terms.task_id),
    ):
        _require(isinstance(value, str) and 1 <= len(value) <= 128, f"{label} must be 1..128 chars")
    _require(is_valid_node_id(terms.requester_node_id), "requester_node_id is not a valid node id")
    _require(is_valid_node_id(terms.worker_node_id), "worker_node_id is not a valid node id")
    _require(terms.requester_node_id != terms.worker_node_id, "requester and worker must differ")
    _require(bool(re.fullmatch(_HEX64_PATTERN, terms.task_spec_hash)), "task_spec_hash must be 64 lowercase hex chars")
    _require(
        terms.input_manifest_hash == "" or bool(re.fullmatch(_HEX64_PATTERN, terms.input_manifest_hash)),
        "input_manifest_hash must be empty or 64 lowercase hex chars",
    )
    _require(_is_int(terms.lease_version) and terms.lease_version >= 1, "lease_version must be >= 1")
    _require(_is_int(terms.issued_at) and terms.issued_at >= 0, "issued_at must be >= 0")
    _require(_is_int(terms.expires_at) and terms.expires_at > terms.issued_at, "expires_at must be after issued_at")
    _require(
        _is_int(terms.execution_deadline) and terms.execution_deadline >= terms.expires_at,
        "execution_deadline must be >= expires_at",
    )
    policy = terms.verification_policy
    _require(isinstance(policy, dict), "verification_policy must be an object")
    _require(set(policy) == {"mode", "required"}, "verification_policy keys must be exactly ['mode', 'required']")
    _require(policy["mode"] == "RECOMPUTE", "verification_policy.mode must be RECOMPUTE")
    _require(isinstance(policy["required"], bool), "verification_policy.required must be a bool")
    reward = terms.reward_policy
    _require(isinstance(reward, dict), "reward_policy must be an object")
    _require(
        isinstance(canonical_json(reward), str) and len(canonical_json(reward)) <= MAX_POLICY,
        f"reward_policy must serialize to <= {MAX_POLICY} bytes",
    )
    _require(
        isinstance(reward.get("amount", 1), int) and not isinstance(reward.get("amount", 1), bool),
        "reward_policy.amount must be an int",
    )
    _require(reward.get("amount", 1) >= 1, "reward_policy.amount must be >= 1")
