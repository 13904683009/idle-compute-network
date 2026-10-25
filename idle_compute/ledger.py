"""Append-only, hash-chained, HMAC-signed credit ledger."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .crypto import GENESIS_HASH, LocalSigner, content_hash
from .models import CreditEvent, CreditEventType, new_id, utc_now


class LedgerError(Exception):
    """Raised when the ledger cannot be verified or rebuilt."""


def _body(event_id: str, event_type: str, peer_id: str, task_id: str, amount: int, idempotency_key: str, created_at: str, prev_hash: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain": "idle-compute-credit-v1",
        "event_id": event_id,
        "event_type": event_type,
        "peer_id": peer_id,
        "task_id": task_id,
        "amount": amount,
        "idempotency_key": idempotency_key,
        "created_at": created_at,
        "prev_hash": prev_hash,
        "metadata": metadata,
    }


def _row_to_event(row: sqlite3.Row) -> CreditEvent:
    return CreditEvent(
        event_id=row["event_id"],
        event_type=CreditEventType(row["event_type"]),
        peer_id=row["peer_id"],
        task_id=row["task_id"],
        amount=int(row["amount"]),
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        prev_hash=row["prev_hash"],
        event_hash=row["event_hash"],
        signature=row["signature"],
        metadata=json.loads(row["metadata_json"]),
    )


def get_by_idempotency(conn: sqlite3.Connection, key: str) -> CreditEvent | None:
    row = conn.execute("SELECT * FROM credit_events WHERE idempotency_key=?", (key,)).fetchone()
    return _row_to_event(row) if row else None


def append_credit(
    conn: sqlite3.Connection,
    signer: LocalSigner,
    peer_id: str,
    task_id: str,
    amount: int = 1,
    metadata: dict[str, Any] | None = None,
) -> tuple[CreditEvent, bool]:
    """Append one credit; idempotency key prevents double credit.

    The caller owns the transaction. Returns (event, created).
    """
    if amount <= 0:
        raise ValueError("credit amount must be positive")
    metadata = metadata or {}
    key = f"credit:{task_id}:{peer_id}"
    existing = get_by_idempotency(conn, key)
    if existing:
        return existing, False

    previous = conn.execute("SELECT event_hash FROM credit_events ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = previous["event_hash"] if previous else GENESIS_HASH
    event_id = new_id("evt_")
    created_at = utc_now()
    body = _body(event_id, CreditEventType.CREDIT.value, peer_id, task_id, amount, key, created_at, prev_hash, metadata)
    event_hash = content_hash(body)
    signature = signer.sign(event_hash)
    try:
        conn.execute(
            "INSERT INTO credit_events "
            "(event_id,event_type,peer_id,task_id,amount,idempotency_key,created_at,prev_hash,event_hash,signature,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                CreditEventType.CREDIT.value,
                peer_id,
                task_id,
                amount,
                key,
                created_at,
                prev_hash,
                event_hash,
                signature,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
    except sqlite3.IntegrityError:
        existing = get_by_idempotency(conn, key)
        if existing:
            return existing, False
        raise
    return CreditEvent(
        event_id=event_id,
        event_type=CreditEventType.CREDIT,
        peer_id=peer_id,
        task_id=task_id,
        amount=amount,
        idempotency_key=key,
        created_at=created_at,
        prev_hash=prev_hash,
        event_hash=event_hash,
        signature=signature,
        metadata=metadata,
    ), True


def iter_events(conn: sqlite3.Connection, peer_id: str | None = None) -> list[CreditEvent]:
    if peer_id:
        rows = conn.execute("SELECT * FROM credit_events WHERE peer_id=? ORDER BY seq", (peer_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM credit_events ORDER BY seq").fetchall()
    return [_row_to_event(row) for row in rows]


def iter_events_lenient(conn: sqlite3.Connection, peer_id: str | None = None) -> tuple[list[CreditEvent], list[str]]:
    """Like iter_events, but skips malformed rows and reports them instead of raising.

    Used by read-only views (status/events) so a tampered ledger yields a
    structured notice rather than an exception. Rebuild paths must keep using
    the strict iter_events, which only runs after verify_ledger passes.
    """
    if peer_id:
        rows = conn.execute("SELECT * FROM credit_events WHERE peer_id=? ORDER BY seq", (peer_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM credit_events ORDER BY seq").fetchall()
    events: list[CreditEvent] = []
    errors: list[str] = []
    for row in rows:
        try:
            events.append(_row_to_event(row))
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            errors.append(f"event seq={row['seq']}: malformed row (invalid event_type, metadata, or NULL field)")
    return events, errors


def verify_ledger(conn: sqlite3.Connection, signer: LocalSigner) -> tuple[bool, list[str]]:
    errors: list[str] = []
    prev = GENESIS_HASH
    rows = conn.execute("SELECT * FROM credit_events ORDER BY seq").fetchall()
    for row in rows:
        try:
            event = _row_to_event(row)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            errors.append(f"event seq={row['seq']}: malformed row (invalid event_type, metadata, or NULL field)")
            continue
        if not isinstance(event.event_hash, str) or not isinstance(event.signature, str):
            errors.append(f"event seq={row['seq']}: missing event_hash or signature")
            continue
        if event.prev_hash != prev:
            errors.append(f"event {event.event_id}: prev_hash mismatch")
        body = _body(
            event.event_id,
            event.event_type.value,
            event.peer_id,
            event.task_id,
            event.amount,
            event.idempotency_key,
            event.created_at,
            event.prev_hash,
            event.metadata,
        )
        recomputed = content_hash(body)
        if recomputed != event.event_hash:
            errors.append(f"event {event.event_id}: event_hash mismatch")
        if not signer.verify(event.event_hash, event.signature):
            errors.append(f"event {event.event_id}: signature invalid")
        if event.amount <= 0:
            errors.append(f"event {event.event_id}: non-positive amount")
        prev = event.event_hash
    return not errors, errors


def rebuild_ledger(conn: sqlite3.Connection, signer: LocalSigner) -> dict[str, Any]:
    ok, errors = verify_ledger(conn, signer)
    if not ok:
        raise LedgerError("; ".join(errors[:10]))
    balances: dict[str, int] = {}
    task_credits: dict[str, int] = {}
    for event in iter_events(conn):
        balances[event.peer_id] = balances.get(event.peer_id, 0) + event.amount
        task_credits[event.task_id] = task_credits.get(event.task_id, 0) + event.amount
    return {"valid": True, "events": len(iter_events(conn)), "balances": balances, "task_credits": task_credits}
