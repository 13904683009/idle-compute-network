"""Local coordinator for the idle-compute MVP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .crypto import content_hash, result_signing_payload
from .db import LeaseExpired, Store, TaskUnavailable
from .ledger import get_by_idempotency, iter_events_lenient
from .models import Lease, Result, Task, Validation
from .tasks import TASK_TYPE, NumericBatchTask
from .worker import Worker


def _task_dict(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "payload": task.payload,
        "status": task.status.value,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "lease_seconds": task.lease_seconds,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "result_id": task.result_id,
        "cancel_reason": task.cancel_reason,
        "shards": [
            {
                "shard_id": shard.shard_id,
                "shard_index": shard.shard_index,
                "shard_count": shard.shard_count,
                "status": shard.status,
                "output_hash": shard.output_hash,
            }
            for shard in task.shards
        ],
    }


def _lease_dict(lease: Lease) -> dict[str, Any]:
    return {
        "lease_id": lease.lease_id,
        "task_id": lease.task_id,
        "peer_id": lease.peer_id,
        "status": lease.status.value,
        "expires_at": lease.expires_at,
        "created_at": lease.created_at,
    }


def _result_dict(result: Result) -> dict[str, Any]:
    return {
        "result_id": result.result_id,
        "task_id": result.task_id,
        "lease_id": result.lease_id,
        "peer_id": result.peer_id,
        "content_hash": result.content_hash,
        "status": result.status.value,
        "created_at": result.created_at,
    }


def _validation_dict(validation: Validation) -> dict[str, Any]:
    return {
        "validation_id": validation.validation_id,
        "result_id": validation.result_id,
        "task_id": validation.task_id,
        "validator_id": validation.validator_id,
        "status": validation.status.value,
        "reason": validation.reason,
        "expected_hash": validation.expected_hash,
        "actual_hash": validation.actual_hash,
        "created_at": validation.created_at,
    }


class Coordinator:
    def __init__(self, db_path: str | Path, key_path: str | Path | None = None) -> None:
        self.store = Store(db_path, key_path)
        self.validator_id = "local-validator"

    def close(self) -> None:
        self.store.close()

    def create_numeric_task(
        self, numbers: list[int], max_attempts: int = 3, lease_seconds: int = 30
    ) -> dict[str, Any]:
        payload = {"numbers": list(numbers)}
        NumericBatchTask.validate(payload)
        task = self.store.create_task(TASK_TYPE, payload, max_attempts, lease_seconds)
        return _task_dict(task)

    def claim(self, peer_id: str, task_id: str | None = None) -> dict[str, Any] | None:
        lease = self.store.claim_task(peer_id, task_id)
        return _lease_dict(lease) if lease else None

    def run_one(self, peer_id: str, task_id: str | None = None) -> dict[str, Any]:
        lease = self.store.claim_task(peer_id, task_id)
        if not lease:
            return {"status": "idle", "peer_id": peer_id}
        try:
            task = self.store.start_task(lease.lease_id, peer_id)
            worker = Worker(peer_id, self.store.signer)
            output, digest, signature = worker.execute(task, lease)
            result = self.store.submit_result(lease.lease_id, peer_id, output, digest, signature)
            validation = self.store.validate_result(result.result_id, self.validator_id)
            credit = get_by_idempotency(self.store.conn, f"credit:{task.task_id}:{peer_id}")
            # Re-fetch: validate_result transitions the lease (RELEASED/EXPIRED).
            current_lease = self.store.get_lease(lease.lease_id)
            return {
                "status": validation.status.value.lower(),
                "task": _task_dict(self.store.get_task(task.task_id)),  # type: ignore[arg-type]
                "lease": _lease_dict(current_lease),  # type: ignore[arg-type]
                "result": _result_dict(self.store.get_result(result.result_id)),  # type: ignore[arg-type]
                "validation": _validation_dict(validation),
                "credit_event_id": credit.event_id if credit else None,
            }
        except LeaseExpired as exc:
            self.store.expire_leases()
            return {"status": "expired", "peer_id": peer_id, "reason": str(exc)}

    def submit_output(self, lease_id: str, peer_id: str, output: dict[str, Any]) -> dict[str, Any]:
        lease = self.store.get_lease(lease_id)
        if not lease:
            raise TaskUnavailable("lease not found")
        digest = content_hash(output)
        signature = self.store.signer.sign(
            result_signing_payload(lease.task_id, lease_id, peer_id, digest)
        )
        result = self.store.submit_result(lease_id, peer_id, output, digest, signature)
        return _result_dict(result)

    def validate(self, result_id: str) -> dict[str, Any]:
        return _validation_dict(self.store.validate_result(result_id, self.validator_id))

    def status(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        if not task:
            return None
        lease_row = self.store.conn.execute(
            "SELECT * FROM leases WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        lease = None
        if lease_row:
            lease = {
                "lease_id": lease_row["lease_id"],
                "peer_id": lease_row["peer_id"],
                "status": lease_row["status"],
                "expires_at": lease_row["expires_at"],
            }
        result = self.store.get_result(task.result_id) if task.result_id else None
        validation = self.store.get_validation(result.result_id) if result else None
        events, ledger_errors = iter_events_lenient(self.store.conn)
        credits = [
            {
                "event_id": event.event_id,
                "peer_id": event.peer_id,
                "task_id": event.task_id,
                "amount": event.amount,
                "idempotency_key": event.idempotency_key,
            }
            for event in events
            if event.task_id == task_id
        ]
        return {
            "task": _task_dict(task),
            "latest_lease": lease,
            "result": _result_dict(result) if result else None,
            "validation": _validation_dict(validation) if validation else None,
            "credits": credits,
            "credits_errors": ledger_errors,
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        return [_task_dict(task) for task in self.store.list_tasks()]

    def cancel(self, task_id: str, reason: str = "cancelled by user") -> dict[str, Any]:
        return _task_dict(self.store.cancel_task(task_id, reason))

    def expire(self) -> int:
        return self.store.expire_leases()

    def events(self, peer_id: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        events, errors = iter_events_lenient(self.store.conn, peer_id)
        return [
            {
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "peer_id": event.peer_id,
                "task_id": event.task_id,
                "amount": event.amount,
                "idempotency_key": event.idempotency_key,
                "created_at": event.created_at,
                "prev_hash": event.prev_hash,
                "event_hash": event.event_hash,
                "signature": event.signature,
            }
            for event in events
        ], errors

    def balances(self) -> dict[str, int]:
        return self.store.balances()

    def verify_ledger(self) -> dict[str, Any]:
        ok, errors = self.store.verify_ledger()
        # Count via raw SQL: iter_events would re-raise on malformed rows and
        # hide the structured errors that verify_ledger just computed.
        count = self.store.conn.execute("SELECT COUNT(*) AS n FROM credit_events").fetchone()["n"]
        return {"valid": ok, "errors": errors, "events": int(count)}

    def rebuild_ledger(self) -> dict[str, Any]:
        return self.store.rebuild_ledger()
