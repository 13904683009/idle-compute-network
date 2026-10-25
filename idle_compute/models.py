"""Protocol data models for the local idle-compute MVP."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUBMITTED = "SUBMITTED"
    VALIDATED = "VALIDATED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class LeaseStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    CANCELLED = "cancelled"


class ResultStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"


class ValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    REVIEW = "REVIEW"


class CreditEventType(str, Enum):
    CREDIT = "CREDIT"


@dataclass
class Peer:
    peer_id: str
    display_name: str = ""
    created_at: str = field(default_factory=utc_now)


@dataclass
class TaskShard:
    shard_id: str
    task_id: str
    shard_index: int
    shard_count: int
    payload: dict[str, Any]
    status: str = "queued"
    output_hash: str | None = None


@dataclass
class Task:
    task_id: str
    task_type: str
    payload: dict[str, Any]
    status: TaskStatus = TaskStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    lease_seconds: int = 30
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    result_id: str | None = None
    cancel_reason: str | None = None
    shards: list[TaskShard] = field(default_factory=list)


@dataclass
class Lease:
    lease_id: str
    task_id: str
    peer_id: str
    status: LeaseStatus = LeaseStatus.ACTIVE
    expires_at: str = ""
    created_at: str = field(default_factory=utc_now)


@dataclass
class Result:
    result_id: str
    task_id: str
    lease_id: str
    peer_id: str
    output: dict[str, Any]
    content_hash: str
    signature: str
    status: ResultStatus = ResultStatus.SUBMITTED
    created_at: str = field(default_factory=utc_now)


@dataclass
class Validation:
    validation_id: str
    result_id: str
    task_id: str
    validator_id: str
    status: ValidationStatus
    reason: str
    expected_hash: str
    actual_hash: str
    created_at: str = field(default_factory=utc_now)


@dataclass
class CreditEvent:
    event_id: str
    event_type: CreditEventType
    peer_id: str
    task_id: str
    amount: int
    idempotency_key: str
    created_at: str
    prev_hash: str
    event_hash: str
    signature: str
    metadata: dict[str, Any] = field(default_factory=dict)
