"""SQLite persistence, task state transitions, leases, and validation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import ledger
from .crypto import LocalSigner, content_hash, result_signing_payload
from .models import (
    Lease,
    LeaseStatus,
    Result,
    ResultStatus,
    Task,
    TaskShard,
    TaskStatus,
    Validation,
    ValidationStatus,
    new_id,
    utc_now,
)
from .state_machine import ensure_transition
from .tasks import NumericBatchTask, get_task_type


class StoreError(Exception):
    """Base error for persistence/state operations."""


class TaskUnavailable(StoreError):
    """No claimable task or the task is in the wrong state."""


class LeaseExpired(StoreError):
    """The lease has expired."""


class DuplicateSubmission(StoreError):
    """A result for this task/lease already exists."""


class DuplicateValidation(StoreError):
    """A validation for this result already exists."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta(key,value) VALUES ('schema_version','1');

CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    lease_seconds INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_id TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS task_shards (
    shard_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    shard_index INTEGER NOT NULL,
    shard_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    output_hash TEXT
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease
    ON leases(task_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS results (
    result_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    peer_id TEXT NOT NULL,
    output_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, lease_id)
);

CREATE TABLE IF NOT EXISTS validations (
    validation_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL UNIQUE REFERENCES results(result_id),
    task_id TEXT NOT NULL,
    validator_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    expected_hash TEXT NOT NULL,
    actual_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_leases_task ON leases(task_id);
CREATE INDEX IF NOT EXISTS idx_events_peer ON credit_events(peer_id);
"""


def _expires_after(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _is_expired(expires_at: str, now: str) -> bool:
    return datetime.fromisoformat(expires_at) <= datetime.fromisoformat(now)


class Store:
    def __init__(self, db_path: str | Path, key_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path = Path(key_path) if key_path else self.db_path.parent / "ledger.key"
        self.conn = sqlite3.connect(str(self.db_path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self.signer = LocalSigner.load_or_create(self.key_path)
        except Exception:
            # Failed init must not leak the open handle; on Windows a leaked
            # handle keeps a corrupt DB file locked until GC.
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- readers ----------

    def _task_from_row(self, row: sqlite3.Row) -> Task:
        shard_rows = self.conn.execute(
            "SELECT * FROM task_shards WHERE task_id=? ORDER BY shard_index", (row["task_id"],)
        ).fetchall()
        shards = [
            TaskShard(
                shard_id=s["shard_id"],
                task_id=s["task_id"],
                shard_index=int(s["shard_index"]),
                shard_count=int(s["shard_count"]),
                payload=json.loads(s["payload_json"]),
                status=s["status"],
                output_hash=s["output_hash"],
            )
            for s in shard_rows
        ]
        return Task(
            task_id=row["task_id"],
            task_type=row["task_type"],
            payload=json.loads(row["payload_json"]),
            status=TaskStatus(row["status"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            lease_seconds=int(row["lease_seconds"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_id=row["result_id"],
            cancel_reason=row["cancel_reason"],
            shards=shards,
        )

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(self) -> list[Task]:
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY created_at, rowid").fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_lease(self, lease_id: str) -> Lease | None:
        row = self.conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        if not row:
            return None
        return Lease(
            lease_id=row["lease_id"],
            task_id=row["task_id"],
            peer_id=row["peer_id"],
            status=LeaseStatus(row["status"]),
            expires_at=row["expires_at"],
            created_at=row["created_at"],
        )

    def get_result(self, result_id: str) -> Result | None:
        row = self.conn.execute("SELECT * FROM results WHERE result_id=?", (result_id,)).fetchone()
        if not row:
            return None
        return Result(
            result_id=row["result_id"],
            task_id=row["task_id"],
            lease_id=row["lease_id"],
            peer_id=row["peer_id"],
            output=json.loads(row["output_json"]),
            content_hash=row["content_hash"],
            signature=row["signature"],
            status=ResultStatus(row["status"]),
            created_at=row["created_at"],
        )

    def get_validation(self, result_id: str) -> Validation | None:
        row = self.conn.execute("SELECT * FROM validations WHERE result_id=?", (result_id,)).fetchone()
        if not row:
            return None
        return Validation(
            validation_id=row["validation_id"],
            result_id=row["result_id"],
            task_id=row["task_id"],
            validator_id=row["validator_id"],
            status=ValidationStatus(row["status"]),
            reason=row["reason"],
            expected_hash=row["expected_hash"],
            actual_hash=row["actual_hash"],
            created_at=row["created_at"],
        )

    # ---------- writes ----------

    def ensure_peer(self, peer_id: str, display_name: str = "") -> None:
        if not peer_id or len(peer_id) > 128:
            raise ValueError("peer_id must be 1..128 characters")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO peers(peer_id,display_name,created_at) VALUES (?,?,?)",
                (peer_id, display_name, utc_now()),
            )

    def create_task(
        self,
        task_type: str,
        payload: dict[str, Any],
        max_attempts: int = 3,
        lease_seconds: int = 30,
    ) -> Task:
        task_cls = get_task_type(task_type)
        task_cls.validate(payload)
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts must be 1..10")
        if lease_seconds < 1 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be 1..3600")
        task_id = new_id("tsk_")
        shard_id = new_id("shd_")
        now = utc_now()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO tasks "
                "(task_id,task_type,payload_json,status,attempts,max_attempts,lease_seconds,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    task_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    TaskStatus.QUEUED.value,
                    0,
                    max_attempts,
                    lease_seconds,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO task_shards(shard_id,task_id,shard_index,shard_count,payload_json,status) "
                "VALUES (?,?,?,?,?,?)",
                (shard_id, task_id, 0, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), "queued"),
            )
        return self.get_task(task_id)  # type: ignore[return-value]

    def _expire_locked(self, now: str) -> int:
        rows = self.conn.execute(
            "SELECT l.*, t.status AS task_status, t.attempts, t.max_attempts "
            "FROM leases l JOIN tasks t ON t.task_id=l.task_id "
            "WHERE l.status='active' AND l.expires_at <= ?",
            (now,),
        ).fetchall()
        count = 0
        for row in rows:
            self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.EXPIRED.value, row["lease_id"]))
            if row["task_status"] in (TaskStatus.LEASED.value, TaskStatus.RUNNING.value):
                if int(row["attempts"]) < int(row["max_attempts"]):
                    ensure_transition(TaskStatus(row["task_status"]), TaskStatus.QUEUED)
                    self.conn.execute(
                        "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                        (TaskStatus.QUEUED.value, now, row["task_id"]),
                    )
                else:
                    ensure_transition(TaskStatus(row["task_status"]), TaskStatus.EXPIRED)
                    self.conn.execute(
                        "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                        (TaskStatus.EXPIRED.value, now, row["task_id"]),
                    )
            elif row["task_status"] == TaskStatus.SUBMITTED.value:
                # Result submitted but never validated (crash window). Reject
                # the stale result and hand the task back for retry; otherwise
                # it would stay wedged in SUBMITTED, which claim_task never
                # selects.
                self.conn.execute(
                    "UPDATE results SET status=? WHERE task_id=? AND lease_id=? AND status=?",
                    (ResultStatus.REJECTED.value, row["task_id"], row["lease_id"], ResultStatus.SUBMITTED.value),
                )
                if int(row["attempts"]) < int(row["max_attempts"]):
                    ensure_transition(TaskStatus.SUBMITTED, TaskStatus.QUEUED)
                    target = TaskStatus.QUEUED
                else:
                    ensure_transition(TaskStatus.SUBMITTED, TaskStatus.EXPIRED)
                    target = TaskStatus.EXPIRED
                self.conn.execute(
                    "UPDATE tasks SET status=?, result_id=NULL, updated_at=? WHERE task_id=?",
                    (target.value, now, row["task_id"]),
                )
            count += 1
        return count

    def expire_leases(self, now: str | None = None) -> int:
        now = now or utc_now()
        with self.transaction():
            return self._expire_locked(now)

    def recover(self, now: str | None = None) -> int:
        """Crash-recovery entry point; currently lease-expiry-only."""
        return self.expire_leases(now)

    def claim_task(self, peer_id: str, task_id: str | None = None, now: str | None = None) -> Lease | None:
        now = now or utc_now()
        self.ensure_peer(peer_id)
        with self.transaction():
            self._expire_locked(now)
            if task_id:
                row = self.conn.execute(
                    "SELECT * FROM tasks WHERE task_id=? AND status=?", (task_id, TaskStatus.QUEUED.value)
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY created_at, rowid LIMIT 1",
                    (TaskStatus.QUEUED.value,),
                ).fetchone()
            if not row:
                return None
            if int(row["attempts"]) >= int(row["max_attempts"]):
                ensure_transition(TaskStatus.QUEUED, TaskStatus.EXPIRED)
                self.conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (TaskStatus.EXPIRED.value, now, row["task_id"]),
                )
                return None
            ensure_transition(TaskStatus(row["status"]), TaskStatus.LEASED)
            lease_id = new_id("lea_")
            expires_at = _expires_after(now, int(row["lease_seconds"]))
            self.conn.execute(
                "UPDATE tasks SET status=?, attempts=attempts+1, updated_at=? WHERE task_id=?",
                (TaskStatus.LEASED.value, now, row["task_id"]),
            )
            self.conn.execute(
                "INSERT INTO leases(lease_id,task_id,peer_id,status,expires_at,created_at) VALUES (?,?,?,?,?,?)",
                (lease_id, row["task_id"], peer_id, LeaseStatus.ACTIVE.value, expires_at, now),
            )
            return Lease(
                lease_id=lease_id,
                task_id=row["task_id"],
                peer_id=peer_id,
                status=LeaseStatus.ACTIVE,
                expires_at=expires_at,
                created_at=now,
            )

    def start_task(self, lease_id: str, peer_id: str, now: str | None = None) -> Task:
        now = now or utc_now()
        with self.transaction():
            lease = self.get_lease(lease_id)
            if not lease or lease.peer_id != peer_id:
                raise TaskUnavailable("lease not found for this peer")
            if lease.status is not LeaseStatus.ACTIVE:
                raise LeaseExpired("lease is not active")
            if _is_expired(lease.expires_at, now):
                self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.EXPIRED.value, lease_id))
                raise LeaseExpired("lease expired")
            task = self.get_task(lease.task_id)
            if not task:
                raise TaskUnavailable("task not found")
            ensure_transition(task.status, TaskStatus.RUNNING)
            self.conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (TaskStatus.RUNNING.value, now, task.task_id),
            )
            return self.get_task(task.task_id)  # type: ignore[return-value]

    def submit_result(
        self,
        lease_id: str,
        peer_id: str,
        output: dict[str, Any],
        content_hash_value: str,
        signature: str,
        now: str | None = None,
    ) -> Result:
        now = now or utc_now()
        with self.transaction():
            lease = self.get_lease(lease_id)
            if not lease or lease.peer_id != peer_id:
                raise TaskUnavailable("lease not found for this peer")
            if lease.status is not LeaseStatus.ACTIVE or _is_expired(lease.expires_at, now):
                self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.EXPIRED.value, lease_id))
                raise LeaseExpired("lease expired before submission")
            existing_result = self.conn.execute(
                "SELECT result_id FROM results WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if existing_result:
                raise DuplicateSubmission("a result already exists for this lease")
            task = self.get_task(lease.task_id)
            if not task:
                raise TaskUnavailable("task not found")
            if task.status not in (TaskStatus.LEASED, TaskStatus.RUNNING):
                raise TaskUnavailable(f"task is not runnable: {task.status.value}")
            ensure_transition(task.status, TaskStatus.SUBMITTED)
            result_id = new_id("res_")
            try:
                self.conn.execute(
                    "INSERT INTO results "
                    "(result_id,task_id,lease_id,peer_id,output_json,content_hash,signature,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        result_id,
                        task.task_id,
                        lease_id,
                        peer_id,
                        json.dumps(output, ensure_ascii=False, sort_keys=True),
                        content_hash_value,
                        signature,
                        ResultStatus.SUBMITTED.value,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateSubmission("a result already exists for this lease") from exc
            self.conn.execute(
                "UPDATE tasks SET status=?, result_id=?, updated_at=? WHERE task_id=?",
                (TaskStatus.SUBMITTED.value, result_id, now, task.task_id),
            )
            result = self.get_result(result_id)
            assert result is not None
            return result

    def validate_result(self, result_id: str, validator_id: str, now: str | None = None) -> Validation:
        now = now or utc_now()
        with self.transaction():
            existing = self.get_validation(result_id)
            if existing:
                return existing
            result = self.get_result(result_id)
            if not result:
                raise TaskUnavailable("result not found")
            task = self.get_task(result.task_id)
            if not task:
                raise TaskUnavailable("task not found")
            lease = self.get_lease(result.lease_id)
            if not lease:
                raise TaskUnavailable("lease not found")
            if result.status is not ResultStatus.SUBMITTED:
                raise TaskUnavailable(f"result is not submitted: {result.status.value}")
            if lease.status is not LeaseStatus.ACTIVE or _is_expired(lease.expires_at, now):
                self.conn.execute("UPDATE results SET status=? WHERE result_id=?", (ResultStatus.REJECTED.value, result_id))
                self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.EXPIRED.value, result.lease_id))
                if task.status in (TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.SUBMITTED):
                    if task.attempts < task.max_attempts:
                        ensure_transition(task.status, TaskStatus.QUEUED)
                        target = TaskStatus.QUEUED
                    else:
                        ensure_transition(task.status, TaskStatus.EXPIRED)
                        target = TaskStatus.EXPIRED
                    self.conn.execute(
                        "UPDATE tasks SET status=?, result_id=NULL, updated_at=? WHERE task_id=?",
                        (target.value, now, task.task_id),
                    )
                else:
                    self.conn.execute(
                        "UPDATE tasks SET result_id=NULL, updated_at=? WHERE task_id=?",
                        (now, task.task_id),
                    )
                validation = Validation(
                    validation_id=new_id("val_"),
                    result_id=result_id,
                    task_id=task.task_id,
                    validator_id=validator_id,
                    status=ValidationStatus.INVALID,
                    reason="lease expired",
                    expected_hash="",
                    actual_hash=result.content_hash or "",
                    created_at=now,
                )
                self._insert_validation(validation)
                return validation

            task_type = get_task_type(task.task_type)
            expected = task_type.execute(task.payload)
            expected_hash = content_hash(expected)
            signature_payload = result_signing_payload(result.task_id, result.lease_id, result.peer_id, result.content_hash)
            # Direct DB tampering can yield NULL signature/content_hash rows even
            # though the columns are NOT NULL; guard before hmac (mirrors the
            # ledger verify guard) and treat as an invalid signature.
            signature_ok = (
                isinstance(result.signature, str)
                and isinstance(result.content_hash, str)
                and self.signer.verify(signature_payload, result.signature)
            )
            output_ok = task_type.verify(task.payload, result.output)
            hash_ok = result.content_hash == content_hash(result.output) == expected_hash

            if signature_ok and output_ok and hash_ok:
                self.conn.execute("UPDATE results SET status=? WHERE result_id=?", (ResultStatus.VALIDATED.value, result_id))
                self.conn.execute("UPDATE task_shards SET status=?, output_hash=? WHERE task_id=?", ("completed", expected_hash, task.task_id))
                self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.RELEASED.value, result.lease_id))
                ensure_transition(task.status, TaskStatus.VALIDATED)
                ensure_transition(TaskStatus.VALIDATED, TaskStatus.COMPLETED)
                self.conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (TaskStatus.COMPLETED.value, now, task.task_id),
                )
                ledger.append_credit(self.conn, self.signer, result.peer_id, task.task_id, 1, {"result_id": result_id})
                validation = Validation(
                    validation_id=new_id("val_"),
                    result_id=result_id,
                    task_id=task.task_id,
                    validator_id=validator_id,
                    status=ValidationStatus.VALID,
                    reason="verified",
                    expected_hash=expected_hash,
                    actual_hash=result.content_hash,
                    created_at=now,
                )
                self._insert_validation(validation)
                return validation

            reasons = []
            if not signature_ok:
                reasons.append("invalid signature")
            if not output_ok:
                reasons.append("output does not match whitelist recomputation")
            if not hash_ok:
                reasons.append("content hash mismatch")
            self.conn.execute("UPDATE results SET status=? WHERE result_id=?", (ResultStatus.REJECTED.value, result_id))
            self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.RELEASED.value, result.lease_id))
            ensure_transition(task.status, TaskStatus.REJECTED)
            self.conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (TaskStatus.REJECTED.value, now, task.task_id),
            )
            validation = Validation(
                validation_id=new_id("val_"),
                result_id=result_id,
                task_id=task.task_id,
                validator_id=validator_id,
                status=ValidationStatus.INVALID,
                reason="; ".join(reasons),
                expected_hash=expected_hash,
                actual_hash=result.content_hash or "",
                created_at=now,
            )
            self._insert_validation(validation)
            return validation

    def _insert_validation(self, validation: Validation) -> None:
        try:
            self.conn.execute(
                "INSERT INTO validations "
                "(validation_id,result_id,task_id,validator_id,status,reason,expected_hash,actual_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    validation.validation_id,
                    validation.result_id,
                    validation.task_id,
                    validation.validator_id,
                    validation.status.value,
                    validation.reason,
                    validation.expected_hash,
                    validation.actual_hash,
                    validation.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateValidation("validation already exists for this result") from exc

    def cancel_task(self, task_id: str, reason: str = "cancelled by user", now: str | None = None) -> Task:
        now = now or utc_now()
        with self.transaction():
            task = self.get_task(task_id)
            if not task:
                raise TaskUnavailable("task not found")
            ensure_transition(task.status, TaskStatus.CANCELLED)
            self.conn.execute(
                "UPDATE leases SET status=? WHERE task_id=? AND status=?",
                (LeaseStatus.CANCELLED.value, task_id, LeaseStatus.ACTIVE.value),
            )
            self.conn.execute(
                "UPDATE results SET status=? WHERE task_id=? AND status=?",
                (ResultStatus.REJECTED.value, task_id, ResultStatus.SUBMITTED.value),
            )
            self.conn.execute(
                "UPDATE tasks SET status=?, cancel_reason=?, updated_at=? WHERE task_id=?",
                (TaskStatus.CANCELLED.value, reason, now, task_id),
            )
            return self.get_task(task_id)  # type: ignore[return-value]

    def balances(self) -> dict[str, int]:
        return ledger.rebuild_ledger(self.conn, self.signer)["balances"]

    def list_events(self, peer_id: str | None = None):
        return ledger.iter_events(self.conn, peer_id)

    def verify_ledger(self) -> tuple[bool, list[str]]:
        return ledger.verify_ledger(self.conn, self.signer)

    def rebuild_ledger(self) -> dict[str, Any]:
        return ledger.rebuild_ledger(self.conn, self.signer)
