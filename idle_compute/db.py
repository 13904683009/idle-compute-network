"""SQLite persistence, task state transitions, leases, and validation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import ledger, signing
from .crypto import LocalSigner, content_hash
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


class NegotiationConflict(StoreError):
    """Another lease negotiation/active lease already exists for this task."""


class InvalidNegotiationState(StoreError):
    """The negotiation is not in a state that allows this transition."""


class InviteError(StoreError):
    """Invite invalid/expired/used/role-mismatched."""

    INVALID = "INVITE_INVALID"
    EXPIRED = "INVITE_EXPIRED"
    USED = "INVITE_USED"
    ROLE = "INVITE_ROLE_MISMATCH"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


INVITE_ROLES = ("worker", "client", "any")


class InsufficientCredit(StoreError):
    """The requester cannot pay for a task (earned - held/spent is too low)."""

    code = "INSUFFICIENT_CREDIT"

    def __init__(self, requester_id: str, cost: int, available: int) -> None:
        super().__init__(
            f"insufficient credit for {requester_id}: cost {cost}, available {available}"
        )
        self.requester_id = requester_id
        self.cost = cost
        self.available = available


# credit_holds states
HOLD_HELD = "HELD"
HOLD_SPENT = "SPENT"
HOLD_REFUNDED = "REFUNDED"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta(key,value) VALUES ('schema_version','3');

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

-- D4: two-party lease negotiation. The DB lease row is only created when the
-- coordinator COMMITs an ACCEPTED negotiation (ADR-D0-16); one open
-- negotiation per task and one ACTIVE lease per task are enforced here.
CREATE TABLE IF NOT EXISTS lease_negotiations (
    lease_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    requester_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    requester_signature TEXT NOT NULL,
    worker_signature TEXT NOT NULL DEFAULT '',
    acceptance_json TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    accepted_at TEXT,
    committed_at TEXT,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_negotiation
    ON lease_negotiations(task_id) WHERE state IN ('PROPOSED','ACCEPTED');

-- CREDIT-for-compute: a task is paid from the requester's earned balance when
-- it is created. The hold is part of the same transaction as the task row and
-- is settled exactly once (SPENT on success, REFUNDED on failure/cancel/expiry).
-- The ledger itself is untouched: "available" = earned - held/spent.
CREATE TABLE IF NOT EXISTS credit_holds (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    requester_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS credit_holds_requester ON credit_holds(requester_id);

-- Requester attribution for client visibility (all paid AND free tasks created
-- through create_task_paid / the node TASK_CREATE path). Legacy tasks created
-- via create_task() have no requester and are intentionally not listed.
CREATE TABLE IF NOT EXISTS task_requesters (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    requester_id TEXT NOT NULL,
    cost INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_requesters_requester ON task_requesters(requester_id);

-- One-time invite enrollment: the code itself never lands in the DB (only its
-- SHA-256), expires_at is UTC ISO-8601, and joined_peers makes enrollment
-- survive coordinator restarts without rewriting the config file.
CREATE TABLE IF NOT EXISTS invites (
    invite_hash TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by TEXT
);
CREATE TABLE IF NOT EXISTS joined_peers (
    peer_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    added_at TEXT NOT NULL,
    invite_hash TEXT
);

CREATE TABLE IF NOT EXISTS results (
    result_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    peer_id TEXT NOT NULL,
    output_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    sig_algo TEXT NOT NULL DEFAULT 'hmac-sha256',
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
    sig_algo TEXT NOT NULL DEFAULT 'hmac-sha256',
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_state (
    peer_id TEXT PRIMARY KEY,
    last_seq_sent INTEGER NOT NULL DEFAULT 0,
    last_seq_seen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS claims (
    claim_request_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_refs (
    client_ref TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_leases_task ON leases(task_id);
CREATE INDEX IF NOT EXISTS idx_events_peer ON credit_events(peer_id);
"""


def _expires_after(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _is_expired(expires_at: str, now: str) -> bool:
    return datetime.fromisoformat(expires_at) <= datetime.fromisoformat(now)


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Additive schema v2 migration for v1 databases (in place).

    v1 tables lack the sig_algo columns; existing rows default to
    'hmac-sha256' and are NEVER re-signed. The new node_state/claims/
    client_refs tables come from SCHEMA (CREATE IF NOT EXISTS).
    """
    if "sig_algo" not in _column_names(conn, "results"):
        conn.execute(
            "ALTER TABLE results ADD COLUMN sig_algo TEXT NOT NULL DEFAULT 'hmac-sha256'"
        )
    if "sig_algo" not in _column_names(conn, "credit_events"):
        conn.execute(
            "ALTER TABLE credit_events ADD COLUMN sig_algo TEXT NOT NULL DEFAULT 'hmac-sha256'"
        )
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")


class Store:
    def __init__(
        self,
        db_path: str | Path,
        key_path: str | Path | None = None,
        *,
        signer: signing.SigningPolicy | None = None,
    ) -> None:
        """Open (creating if needed) the store.

        Legacy mode (default, signer=None): HMAC policy over the local
        ledger.key file — the Phase 1 single-process path, unchanged.

        Network mode (signer=policy): the policy is injected by the caller
        (Ed25519Policy); no HMAC key file is created or read.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            _migrate_schema(self.conn)
            self.conn.commit()
            if signer is not None:
                self.signer = signer
                self.key_path = None
            else:
                self.key_path = Path(key_path) if key_path else self.db_path.parent / "ledger.key"
                self.signer = signing.HmacLegacyPolicy(
                    LocalSigner.load_or_create(self.key_path)
                )
        except Exception:
            # Failed init must not leak the open handle; on Windows a leaked
            # handle keeps a corrupt DB file locked until GC.
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    @property
    def _scheme(self) -> str:
        return getattr(self.signer, "scheme", "hmac-sha256")

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

    def get_result_by_lease(self, lease_id: str) -> Result | None:
        """The result submitted under ``lease_id`` (idempotent resubmission)."""
        row = self.conn.execute(
            "SELECT * FROM results WHERE lease_id=?", (lease_id,)
        ).fetchone()
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
        with self.transaction():
            task_id = self._insert_task_locked(task_type, payload, max_attempts, lease_seconds)
        return self.get_task(task_id)  # type: ignore[return-value]

    def _insert_task_locked(
        self,
        task_type: str,
        payload: dict[str, Any],
        max_attempts: int,
        lease_seconds: int,
    ) -> str:
        task_cls = get_task_type(task_type)
        task_cls.validate(payload)
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts must be 1..10")
        if lease_seconds < 1 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be 1..3600")
        task_id = new_id("tsk_")
        shard_id = new_id("shd_")
        now = utc_now()
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
        return task_id

    def available_credit(self, requester_id: str) -> int:
        """Earned CREDIT minus HELD+SPENT holds (the ledger itself is untouched)."""
        return self._available_locked(requester_id)

    def _available_locked(self, requester_id: str) -> int:
        # Sum the CREDIT rows directly: the balance check must not depend on a
        # full ledger verification (a tampered chain is reported by the ledger
        # commands, not by task creation).
        earned_row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS earned FROM credit_events WHERE peer_id=?",
            (requester_id,),
        ).fetchone()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS held FROM credit_holds "
            "WHERE requester_id=? AND state IN (?,?)",
            (requester_id, HOLD_HELD, HOLD_SPENT),
        ).fetchone()
        return int(earned_row["earned"]) - int(row["held"])

    def get_credit_hold(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM credit_holds WHERE task_id=?", (task_id,)).fetchone()
        return None if row is None else dict(row)

    def create_task_paid(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        requester_id: str,
        cost: int,
        max_attempts: int = 3,
        lease_seconds: int = 30,
    ) -> Task:
        """Create a task and charge ``cost`` CREDIT in ONE transaction.

        ``available = earned - HELD - SPENT``. On insufficient funds the
        transaction rolls back before any task row/hold exists.
        """
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise ValueError("cost must be an int >= 0")
        with self.transaction():
            available = self._available_locked(requester_id)
            if cost > 0 and available < cost:
                raise InsufficientCredit(requester_id, cost, available)
            task_id = self._insert_task_locked(task_type, payload, max_attempts, lease_seconds)
            now = utc_now()
            self.conn.execute(
                "INSERT INTO task_requesters(task_id,requester_id,cost,created_at) VALUES (?,?,?,?)",
                (task_id, requester_id, cost, now),
            )
            if cost > 0:
                self.conn.execute(
                    "INSERT INTO credit_holds(task_id,requester_id,amount,state,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (task_id, requester_id, cost, HOLD_HELD, now, now),
                )
        return self.get_task(task_id)  # type: ignore[return-value]

    def _settle_hold_locked(self, task_id: str, state: str, now: str) -> int:
        """HELD -> SPENT/REFUNDED exactly once (idempotent for already-settled)."""
        cursor = self.conn.execute(
            "UPDATE credit_holds SET state=?, updated_at=? WHERE task_id=? AND state=?",
            (state, now, task_id, HOLD_HELD),
        )
        return int(cursor.rowcount)

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
                    self._settle_hold_locked(row["task_id"], HOLD_REFUNDED, now)
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
                if target is TaskStatus.EXPIRED:
                    self._settle_hold_locked(row["task_id"], HOLD_REFUNDED, now)
            count += 1
        self._expire_negotiations_locked(now)
        # A negotiation can only authorize execution while its lease is still
        # active. Once the lease expires (or is completed/released) the stale
        # ACTIVE row must not look like a live authorization, so it expires
        # too -- restart/recovery therefore fails closed.
        self.conn.execute(
            "UPDATE lease_negotiations SET state='EXPIRED', updated_at=? "
            "WHERE state='ACTIVE' AND lease_id NOT IN ("
            "SELECT lease_id FROM leases WHERE status='active' AND expires_at > ?)",
            (now, now),
        )
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
                self._settle_hold_locked(row["task_id"], HOLD_REFUNDED, now)
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
            # Duplicate check BEFORE the lease-status check: after validation
            # the lease is RELEASED, so a replayed submission must still be
            # answered as a duplicate (idempotent replay), never as an
            # expired lease. No state changes happen on this path.
            existing_result = self.conn.execute(
                "SELECT result_id FROM results WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if existing_result:
                raise DuplicateSubmission("a result already exists for this lease")
            if lease.status is not LeaseStatus.ACTIVE or _is_expired(lease.expires_at, now):
                self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.EXPIRED.value, lease_id))
                raise LeaseExpired("lease expired before submission")
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
                    "(result_id,task_id,lease_id,peer_id,output_json,content_hash,signature,sig_algo,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        result_id,
                        task.task_id,
                        lease_id,
                        peer_id,
                        json.dumps(output, ensure_ascii=False, sort_keys=True),
                        content_hash_value,
                        signature,
                        self._scheme,
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
            signature_payload = self.signer.result_payload(
                result.task_id, result.lease_id, result.peer_id, result.content_hash
            )
            # Direct DB tampering can yield NULL signature/content_hash rows even
            # though the columns are NOT NULL; guard before verifying (mirrors the
            # ledger verify guard) and treat as an invalid signature. The row's
            # sig_algo tag must match the store's scheme (a flipped tag is a
            # tamper signal, not a downgrade path).
            sig_row = self.conn.execute(
                "SELECT sig_algo FROM results WHERE result_id=?", (result_id,)
            ).fetchone()
            signature_ok = (
                isinstance(result.signature, str)
                and isinstance(result.content_hash, str)
                and sig_row is not None
                and sig_row["sig_algo"] == self._scheme
                and self.signer.verify_result(result.peer_id, signature_payload, result.signature)
            )
            output_ok = task_type.verify(task.payload, result.output)
            hash_ok = result.content_hash == content_hash(result.output) == expected_hash

            if signature_ok and output_ok and hash_ok:
                self.conn.execute("UPDATE results SET status=? WHERE result_id=?", (ResultStatus.VALIDATED.value, result_id))
                self.conn.execute("UPDATE task_shards SET status=?, output_hash=? WHERE task_id=?", ("completed", expected_hash, task.task_id))
                self.conn.execute("UPDATE leases SET status=? WHERE lease_id=?", (LeaseStatus.RELEASED.value, result.lease_id))
                self.conn.execute(
                    "UPDATE lease_negotiations SET state='COMPLETED', updated_at=? "
                    "WHERE lease_id=? AND state='ACTIVE'",
                    (now, result.lease_id),
                )
                ensure_transition(task.status, TaskStatus.VALIDATED)
                ensure_transition(TaskStatus.VALIDATED, TaskStatus.COMPLETED)
                self.conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (TaskStatus.COMPLETED.value, now, task.task_id),
                )
                self._settle_hold_locked(task.task_id, HOLD_SPENT, now)
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
            self.conn.execute(
                "UPDATE lease_negotiations SET state='FAILED', updated_at=? "
                "WHERE lease_id=? AND state='ACTIVE'",
                (now, result.lease_id),
            )
            ensure_transition(task.status, TaskStatus.REJECTED)
            self.conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (TaskStatus.REJECTED.value, now, task.task_id),
            )
            self._settle_hold_locked(task.task_id, HOLD_REFUNDED, now)
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
            self._settle_hold_locked(task_id, HOLD_REFUNDED, now)
            return self.get_task(task_id)  # type: ignore[return-value]

    def balances(self) -> dict[str, int]:
        return ledger.rebuild_ledger(self.conn, self.signer)["balances"]

    def worker_tasks(self, peer_id: str, limit: int = 50) -> dict[str, Any]:
        """Tasks this peer submitted results for, with the CREDIT earned.

        Sources only: tasks, results, credit_events. ``credit`` is the sum of
        this peer's credit events for that task (0 for rejected results).
        """
        if not isinstance(peer_id, str) or not peer_id:
            raise ValueError("peer_id must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an int in 1..1000")
        rows = self.conn.execute(
            "SELECT t.task_id, t.status AS task_status, r.result_id, "
            "r.status AS result_status, r.created_at AS submitted_at, "
            "COALESCE((SELECT SUM(e.amount) FROM credit_events e "
            "WHERE e.peer_id=? AND e.task_id=t.task_id),0) AS credit "
            "FROM results r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.peer_id=? ORDER BY r.created_at DESC, t.task_id LIMIT ?",
            (peer_id, peer_id, limit),
        ).fetchall()
        tasks = [dict(row) for row in rows]
        return {
            "peer": peer_id,
            "count": len(tasks),
            "total_credit": sum(int(task["credit"]) for task in tasks),
            "tasks": tasks,
        }

    def client_tasks(self, peer_id: str, limit: int = 50) -> dict[str, Any]:
        """Tasks this peer created as requester, with their CREDIT state.

        Sources: tasks, task_requesters, credit_holds. Paid tasks show
        HELD/SPENT/REFUNDED; free tasks (cost 0) show FREE. Legacy tasks
        created via create_task() have no requester attribution and are not
        listed (they are outside the client-create path).
        """
        if not isinstance(peer_id, str) or not peer_id:
            raise ValueError("peer_id must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an int in 1..1000")
        rows = self.conn.execute(
            "SELECT task_id, task_status, cost, created_at, hold_state FROM ("
            "SELECT t.task_id AS task_id, t.status AS task_status, q.cost AS cost, "
            "q.created_at AS created_at, h.state AS hold_state "
            "FROM task_requesters q JOIN tasks t ON t.task_id=q.task_id "
            "LEFT JOIN credit_holds h ON h.task_id=q.task_id "
            "WHERE q.requester_id=? "
            "UNION "
            "SELECT t.task_id AS task_id, t.status AS task_status, h.amount AS cost, "
            "h.created_at AS created_at, h.state AS hold_state "
            "FROM credit_holds h JOIN tasks t ON t.task_id=h.task_id "
            "LEFT JOIN task_requesters q ON q.task_id=h.task_id "
            "WHERE h.requester_id=? AND q.task_id IS NULL"
            ") ORDER BY created_at DESC, task_id LIMIT ?",
            (peer_id, peer_id, limit),
        ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            cost = int(row["cost"])
            state = row["hold_state"] or ("FREE" if cost == 0 else "UNKNOWN")
            tasks.append(
                {
                    "task_id": row["task_id"],
                    "task_status": row["task_status"],
                    "cost": cost,
                    "credit_state": state,
                    "created_at": row["created_at"],
                }
            )
        return {
            "peer": peer_id,
            "count": len(tasks),
            "total_spent": sum(task["cost"] for task in tasks if task["credit_state"] == "SPENT"),
            "tasks": tasks,
        }

    def create_invite(
        self, invite_hash: str, role: str, expires_at: str, now: str | None = None
    ) -> None:
        if not isinstance(invite_hash, str) or len(invite_hash) < 16:
            raise ValueError("invite_hash must be a non-empty digest")
        if role not in INVITE_ROLES:
            raise ValueError(f"role must be one of {', '.join(INVITE_ROLES)}")
        created = now or utc_now()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO invites(invite_hash,role,created_at,expires_at) VALUES (?,?,?,?)",
                (invite_hash, role, created, expires_at),
            )

    def consume_invite(
        self, invite_hash: str, peer_id: str, role: str, now: str | None = None
    ) -> dict[str, Any]:
        """Consume an invite once and enroll ``peer_id``; atomically."""
        moment = now or utc_now()
        with self.transaction():
            row = self.conn.execute(
                "SELECT * FROM invites WHERE invite_hash=?", (invite_hash,)
            ).fetchone()
            if row is None:
                raise InviteError(InviteError.INVALID, "invite not found")
            if row["used_at"] is not None:
                raise InviteError(InviteError.USED, "invite already used")
            try:
                expired = datetime.fromisoformat(row["expires_at"]) <= datetime.fromisoformat(moment)
            except ValueError:
                raise InviteError(InviteError.INVALID, "invite expiry malformed") from None
            if expired:
                raise InviteError(InviteError.EXPIRED, "invite expired")
            if row["role"] not in ("any", role):
                raise InviteError(
                    InviteError.ROLE,
                    f"invite is for role {row['role']!r}, not {role!r}",
                )
            self.conn.execute(
                "UPDATE invites SET used_at=?, used_by=? WHERE invite_hash=? AND used_at IS NULL",
                (moment, peer_id, invite_hash),
            )
            self.conn.execute(
                "INSERT INTO joined_peers(peer_id,role,added_at,invite_hash) VALUES (?,?,?,?) "
                "ON CONFLICT(peer_id) DO UPDATE SET role=excluded.role, "
                "invite_hash=excluded.invite_hash",
                (peer_id, role, moment, invite_hash),
            )
            return {"peer_id": peer_id, "role": role, "used_at": moment}

    def has_joined(self, peer_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM joined_peers WHERE peer_id=?", (peer_id,)
        ).fetchone()
        return row is not None

    def credit_summary(self) -> dict[str, dict[str, int]]:
        """Per-peer earned/held/spent/refunded/available (no ledger rewrite).

        ``available`` uses ``available_credit``; held/spent/refunded are
        aggregated from ``credit_holds``; earned = available + held + spent.
        """
        peers: set[str] = set()
        for row in self.conn.execute("SELECT DISTINCT peer_id FROM credit_events"):
            peers.add(row["peer_id"])
        for row in self.conn.execute("SELECT DISTINCT requester_id FROM credit_holds"):
            peers.add(row["requester_id"])
        summary: dict[str, dict[str, int]] = {}
        for peer in sorted(peers):
            amounts = {"held": 0, "spent": 0, "refunded": 0}
            rows = self.conn.execute(
                "SELECT state, COALESCE(SUM(amount),0) AS total FROM credit_holds "
                "WHERE requester_id=? GROUP BY state",
                (peer,),
            ).fetchall()
            for row in rows:
                key = str(row["state"]).lower()
                if key in amounts:
                    amounts[key] = int(row["total"])
            available = self.available_credit(peer)
            summary[peer] = {
                "earned": available + amounts["held"] + amounts["spent"],
                "held": amounts["held"],
                "spent": amounts["spent"],
                "refunded": amounts["refunded"],
                "available": available,
            }
        return summary

    def list_events(self, peer_id: str | None = None):
        return ledger.iter_events(self.conn, peer_id)

    def verify_ledger(self) -> tuple[bool, list[str]]:
        return ledger.verify_ledger(self.conn, self.signer)

    def rebuild_ledger(self) -> dict[str, Any]:
        return ledger.rebuild_ledger(self.conn, self.signer)

    # ------------------------------------------------ D4 lease negotiation

    def create_negotiation(
        self,
        *,
        lease_id: str,
        task_id: str,
        requester_id: str,
        worker_id: str,
        authority_id: str,
        terms_json: str,
        terms_hash: str,
        requester_signature: str,
        expires_at: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist a PROPOSED negotiation; one open negotiation per task."""
        now = now or utc_now()
        with self.transaction():
            self._expire_locked(now)
            task = self.get_task(task_id)
            if task is None or task.status is not TaskStatus.QUEUED:
                raise TaskUnavailable("task is not available for a lease proposal")
            try:
                self.conn.execute(
                    "INSERT INTO lease_negotiations(lease_id,task_id,requester_id,worker_id,authority_id,"
                    "terms_json,terms_hash,requester_signature,state,created_at,expires_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,'PROPOSED',?,?,?)",
                    (
                        lease_id,
                        task_id,
                        requester_id,
                        worker_id,
                        authority_id,
                        terms_json,
                        terms_hash,
                        requester_signature,
                        now,
                        expires_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise NegotiationConflict("an open negotiation already exists for this task") from exc
        return self.get_negotiation(lease_id)  # type: ignore[return-value]

    def get_negotiation(self, lease_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM lease_negotiations WHERE lease_id=?", (lease_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def open_negotiation_for_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM lease_negotiations WHERE task_id=? AND state IN ('PROPOSED','ACCEPTED')",
            (task_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def accept_negotiation(
        self,
        lease_id: str,
        worker_id: str,
        worker_signature: str,
        acceptance_json: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """PROPOSED -> ACCEPTED. Idempotent for the same signature."""
        now = now or utc_now()
        with self.transaction():
            self._expire_locked(now)
            row = self.get_negotiation(lease_id)
            if row is None:
                raise TaskUnavailable("negotiation not found")
            if row["worker_id"] != worker_id:
                raise InvalidNegotiationState("lease is addressed to another worker")
            if row["state"] == "ACCEPTED":
                if row["worker_signature"] == worker_signature:
                    return row
                raise InvalidNegotiationState("lease was already accepted with another signature")
            if row["state"] != "PROPOSED":
                raise InvalidNegotiationState(f"cannot accept a {row['state']} negotiation")
            self.conn.execute(
                "UPDATE lease_negotiations SET state='ACCEPTED', worker_signature=?, acceptance_json=?, "
                "accepted_at=?, updated_at=? WHERE lease_id=?",
                (worker_signature, acceptance_json, now, now, lease_id),
            )
        return self.get_negotiation(lease_id)  # type: ignore[return-value]

    def reject_negotiation(
        self, lease_id: str, reason: str, now: str | None = None
    ) -> dict[str, Any]:
        """PROPOSED/ACCEPTED -> REJECTED. Idempotent."""
        now = now or utc_now()
        with self.transaction():
            row = self.get_negotiation(lease_id)
            if row is None:
                raise TaskUnavailable("negotiation not found")
            if row["state"] == "REJECTED":
                return row
            if row["state"] not in ("PROPOSED", "ACCEPTED"):
                raise InvalidNegotiationState(f"cannot reject a {row['state']} negotiation")
            self.conn.execute(
                "UPDATE lease_negotiations SET state='REJECTED', reason=?, updated_at=? WHERE lease_id=?",
                (reason, now, lease_id),
            )
        return self.get_negotiation(lease_id)  # type: ignore[return-value]

    def cancel_negotiation(
        self, lease_id: str, reason: str, now: str | None = None
    ) -> dict[str, Any]:
        """PROPOSED/ACCEPTED -> CANCELLED. Idempotent; ACTIVE is not cancellable here."""
        now = now or utc_now()
        with self.transaction():
            row = self.get_negotiation(lease_id)
            if row is None:
                raise TaskUnavailable("negotiation not found")
            if row["state"] == "CANCELLED":
                return row
            if row["state"] not in ("PROPOSED", "ACCEPTED"):
                raise InvalidNegotiationState(f"cannot cancel a {row['state']} negotiation")
            self.conn.execute(
                "UPDATE lease_negotiations SET state='CANCELLED', reason=?, updated_at=? WHERE lease_id=?",
                (reason, now, lease_id),
            )
        return self.get_negotiation(lease_id)  # type: ignore[return-value]

    def commit_negotiation(
        self, lease_id: str, now: str | None = None
    ) -> tuple[Task, Lease]:
        """ACCEPTED -> ACTIVE: create the DB lease the result flow validates.

        Atomic: task QUEUED->LEASED, leases row, negotiation ACTIVE. Any
        conflict (other active lease / reused lease id) aborts the transaction.
        """
        now = now or utc_now()
        with self.transaction():
            self._expire_locked(now)
            row = self.get_negotiation(lease_id)
            if row is None:
                raise TaskUnavailable("negotiation not found")
            if row["state"] == "ACTIVE":
                lease = self.get_lease(lease_id)
                task = self.get_task(row["task_id"])
                if lease is not None and task is not None:
                    return task, lease
                raise InvalidNegotiationState("ACTIVE negotiation without its lease row")
            if row["state"] != "ACCEPTED":
                raise InvalidNegotiationState(f"cannot commit a {row['state']} negotiation")
            if _is_expired(row["expires_at"], now):
                raise LeaseExpired("negotiation expired before commit")
            task = self.get_task(row["task_id"])
            if task is None:
                raise TaskUnavailable("task not found")
            if task.status is not TaskStatus.QUEUED:
                raise TaskUnavailable("task is no longer queued")
            ensure_transition(TaskStatus.QUEUED, TaskStatus.LEASED)
            self.conn.execute(
                "INSERT OR IGNORE INTO peers(peer_id,display_name,created_at) VALUES (?,?,?)",
                (row["worker_id"], "", utc_now()),
            )
            try:
                self.conn.execute(
                    "INSERT INTO leases(lease_id,task_id,peer_id,status,expires_at,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (lease_id, row["task_id"], row["worker_id"], LeaseStatus.ACTIVE.value, row["expires_at"], now),
                )
            except sqlite3.IntegrityError as exc:
                raise NegotiationConflict("another active lease already exists for this task") from exc
            self.conn.execute(
                "UPDATE tasks SET status=?, attempts=attempts+1, updated_at=? WHERE task_id=?",
                (TaskStatus.LEASED.value, now, row["task_id"]),
            )
            self.conn.execute(
                "UPDATE lease_negotiations SET state='ACTIVE', committed_at=?, updated_at=? WHERE lease_id=?",
                (now, now, lease_id),
            )
        return self.get_task(row["task_id"]), self.get_lease(lease_id)  # type: ignore[return-value]

    def expire_negotiations(self, now: str | None = None) -> int:
        now = now or utc_now()
        with self.transaction():
            return self._expire_negotiations_locked(now)

    def _expire_negotiations_locked(self, now: str) -> int:
        cursor = self.conn.execute(
            "UPDATE lease_negotiations SET state='EXPIRED', updated_at=? "
            "WHERE state IN ('PROPOSED','ACCEPTED') AND expires_at <= ?",
            (now, now),
        )
        return int(cursor.rowcount)

    def create_negotiation_for_lease(
        self,
        *,
        lease_id: str,
        task_id: str,
        requester_id: str,
        worker_id: str,
        authority_id: str,
        terms_json: str,
        terms_hash: str,
        requester_signature: str,
        expires_at: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """D4 node flow: attach a PROPOSED negotiation to an EXISTING lease.

        ``claim_task`` already created the ACTIVE ``leases`` row (one_active_lease
        arbiter); the negotiation only decides whether the worker is allowed to
        EXECUTE it. Re-proposing the same lease is idempotent.
        """
        now = now or utc_now()
        with self.transaction():
            self._expire_locked(now)
            lease = self.get_lease(lease_id)
            if lease is None or lease.task_id != task_id or lease.peer_id != worker_id:
                raise TaskUnavailable("lease does not bind this task/worker")
            if lease.status is not LeaseStatus.ACTIVE:
                raise LeaseExpired("lease is not active")
            existing = self.get_negotiation(lease_id)
            if existing is not None:
                if existing["terms_hash"] != terms_hash:
                    raise NegotiationConflict("lease was already proposed with other terms")
                return existing
            try:
                self.conn.execute(
                    "INSERT INTO lease_negotiations(lease_id,task_id,requester_id,worker_id,authority_id,"
                    "terms_json,terms_hash,requester_signature,state,created_at,expires_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,'PROPOSED',?,?,?)",
                    (
                        lease_id,
                        task_id,
                        requester_id,
                        worker_id,
                        authority_id,
                        terms_json,
                        terms_hash,
                        requester_signature,
                        now,
                        expires_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise NegotiationConflict("another open negotiation already exists for this task") from exc
        return self.get_negotiation(lease_id)  # type: ignore[return-value]

    def activate_negotiation(self, lease_id: str, now: str | None = None) -> dict[str, Any]:
        """ACCEPTED -> ACTIVE for a lease that already exists (node flow)."""
        now = now or utc_now()
        with self.transaction():
            self._expire_locked(now)
            row = self.get_negotiation(lease_id)
            if row is None:
                raise TaskUnavailable("negotiation not found")
            if row["state"] == "ACTIVE":
                return row
            if row["state"] != "ACCEPTED":
                raise InvalidNegotiationState(f"cannot activate a {row['state']} negotiation")
            if _is_expired(row["expires_at"], now):
                raise LeaseExpired("negotiation expired before activation")
            lease = self.get_lease(lease_id)
            if lease is None or lease.status is not LeaseStatus.ACTIVE:
                raise LeaseExpired("lease is not active")
            self.conn.execute(
                "UPDATE lease_negotiations SET state='ACTIVE', committed_at=?, updated_at=? WHERE lease_id=?",
                (now, now, lease_id),
            )
        return self.get_negotiation(lease_id)  # type: ignore[return-value]
