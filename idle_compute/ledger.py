"""Append-only, hash-chained credit ledger with per-row signature schemes.

Rows are tagged ``sig_algo``: 'hmac-sha256' (Phase 1 legacy local path) or
'ed25519' (network path). verify_ledger verifies each row with its tagged
scheme and never lets a row of the wrong scheme pass silently.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .crypto import GENESIS_HASH, content_hash, credit_body
from .models import CreditEvent, CreditEventType, new_id, utc_now
from .protocol import LEDGER_PAGE_BYTES, MAX_SYNC_PAGE_EVENTS
from .signing import Ed25519Policy, HmacLegacyPolicy, SigningPolicy


class LedgerError(Exception):
    """Raised when the ledger cannot be verified or rebuilt."""


def _body(event_id: str, event_type: str, peer_id: str, task_id: str, amount: int, idempotency_key: str, created_at: str, prev_hash: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Phase 1 (v1) credit event body, HMAC scheme."""
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


def _body_builder_for(sig_algo: str):
    """Body builder matching the row's tagged scheme (v1 HMAC / v2 Ed25519)."""
    if sig_algo == "ed25519":
        return credit_body
    return _body


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
    signer: SigningPolicy,
    peer_id: str,
    task_id: str,
    amount: int = 1,
    metadata: dict[str, Any] | None = None,
) -> tuple[CreditEvent, bool]:
    """Append one credit; idempotency key prevents double credit.

    The caller owns the transaction. Returns (event, created). The row is
    tagged with the signer's scheme and signed with the matching payload
    builder (v1 body for HMAC, v2 body for Ed25519).
    """
    if amount <= 0:
        raise ValueError("credit amount must be positive")
    metadata = metadata or {}
    key = f"credit:{task_id}:{peer_id}"
    existing = get_by_idempotency(conn, key)
    if existing:
        return existing, False

    sig_algo = getattr(signer, "scheme", "hmac-sha256")
    previous = conn.execute("SELECT event_hash FROM credit_events ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = previous["event_hash"] if previous else GENESIS_HASH
    event_id = new_id("evt_")
    created_at = utc_now()
    body = _body_builder_for(sig_algo)(
        event_id, CreditEventType.CREDIT.value, peer_id, task_id, amount, key, created_at, prev_hash, metadata
    )
    event_hash = content_hash(body)
    signature = signer.sign(event_hash)
    try:
        conn.execute(
            "INSERT INTO credit_events "
            "(event_id,event_type,peer_id,task_id,amount,idempotency_key,created_at,prev_hash,event_hash,signature,sig_algo,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                sig_algo,
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


def verify_ledger(conn: sqlite3.Connection, signer: SigningPolicy) -> tuple[bool, list[str]]:
    # A raw LocalSigner (Phase 1 API) is HMAC-capable; wrap it so the scheme
    # dispatch below sees the same policy surface as Store provides.
    if not isinstance(signer, (HmacLegacyPolicy, Ed25519Policy)) and not hasattr(signer, "verify_hmac"):
        signer = HmacLegacyPolicy(signer)
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
        sig_algo = row["sig_algo"] if "sig_algo" in row.keys() else "hmac-sha256"
        body = _body_builder_for(sig_algo)(
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
        # Per-row scheme dispatch: a row whose scheme this path cannot verify
        # is a structured CONFLICT error, never a silent pass (closes the
        # mixed-chain hole). Capability vs validity are reported distinctly.
        if sig_algo == "ed25519":
            if not getattr(signer, "ed25519_capable", False):
                errors.append(
                    f"event {event.event_id}: CONFLICT: ed25519-tagged row cannot "
                    "be verified (no Ed25519 verifier configured on this path)"
                )
            elif not signer.verify_ed25519(event.event_hash, event.signature):
                errors.append(f"event {event.event_id}: signature invalid")
        elif sig_algo == "hmac-sha256":
            if not getattr(signer, "hmac_capable", False):
                errors.append(
                    f"event {event.event_id}: CONFLICT: hmac-sha256-tagged row "
                    "cannot be verified (no HMAC verifier configured on this path)"
                )
            elif not signer.verify_hmac(event.event_hash, event.signature):
                errors.append(f"event {event.event_id}: signature invalid")
        else:
            errors.append(f"event {event.event_id}: CONFLICT: unknown sig_algo {sig_algo!r}")
        if event.amount <= 0:
            errors.append(f"event {event.event_id}: non-positive amount")
        prev = event.event_hash
    return not errors, errors


def rebuild_ledger(conn: sqlite3.Connection, signer: SigningPolicy) -> dict[str, Any]:
    ok, errors = verify_ledger(conn, signer)
    if not ok:
        raise LedgerError("; ".join(errors[:10]))
    balances: dict[str, int] = {}
    task_credits: dict[str, int] = {}
    for event in iter_events(conn):
        balances[event.peer_id] = balances.get(event.peer_id, 0) + event.amount
        task_credits[event.task_id] = task_credits.get(event.task_id, 0) + event.amount
    return {"valid": True, "events": len(iter_events(conn)), "balances": balances, "task_credits": task_credits}


MAX_SYNC_BATCH = MAX_SYNC_PAGE_EVENTS  # count clamp per sync page (100)


def export_since(conn: sqlite3.Connection, after_seq: int, limit: int = MAX_SYNC_BATCH) -> list[dict[str, Any]]:
    """Batch-export events with seq > after_seq for LEDGER_SYNC pulls.

    Limit is clamped to MAX_SYNC_BATCH so one response can never exceed the
    wire limit regardless of the caller.
    """
    limit = max(0, min(int(limit), MAX_SYNC_BATCH))
    rows = conn.execute(
        "SELECT * FROM credit_events WHERE seq > ? ORDER BY seq LIMIT ?",
        (after_seq, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def head_seq(conn: sqlite3.Connection) -> int:
    """The chain's current head (0 when empty)."""
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS h FROM credit_events").fetchone()
    return int(row["h"])


def export_page(
    conn: sqlite3.Connection,
    after_seq: int,
    limit: int = MAX_SYNC_BATCH,
    max_bytes: int = LEDGER_PAGE_BYTES,
) -> tuple[list[dict[str, Any]], bool]:
    """Export one LEDGER_SYNC page: events with seq > after_seq, clamped by
    COUNT (MAX_SYNC_BATCH) and by serialized SIZE (max_bytes) so a response
    always fits the wire body cap. Returns (events, more) where ``more`` is
    True when rows beyond this page exist (the puller must request the next
    page with after_seq = last returned seq)."""
    limit = max(0, min(int(limit), MAX_SYNC_BATCH))
    rows = conn.execute(
        "SELECT * FROM credit_events WHERE seq > ? ORDER BY seq LIMIT ?",
        (after_seq, limit),
    ).fetchall()
    events: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        event = dict(row)
        item = len(json.dumps(event, ensure_ascii=False, sort_keys=True))
        if events and size + item > max_bytes:
            break
        events.append(event)
        size += item
    if not events:
        return [], False
    last_seq = events[-1]["seq"]
    more = (
        conn.execute("SELECT 1 FROM credit_events WHERE seq > ? LIMIT 1", (last_seq,)).fetchone()
        is not None
    )
    return events, more


LEDGER_COPY_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_copy (
    seq INTEGER PRIMARY KEY,
    event_id TEXT UNIQUE NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    event_json TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_ledger_copy(conn: sqlite3.Connection) -> None:
    """Create the worker-side ledger_copy mirror table if missing."""
    conn.executescript(LEDGER_COPY_SCHEMA)
    conn.commit()


def verify_and_append(
    conn: sqlite3.Connection, event: dict[str, Any], policy: SigningPolicy
) -> tuple[bool, str | None]:
    """Verify one exported event and append it to the ledger_copy mirror.

    Verification order: shape -> scheme -> duplicate -> seq continuity ->
    prev_hash linkage -> hash -> signature -> amount. Any failure returns
    (False, reason) and the event is never appended. Re-appending an
    already-present identical event is an idempotent no-op ((True, None));
    a conflicting duplicate is an error.
    """
    ensure_ledger_copy(conn)
    required = (
        "seq", "event_id", "event_type", "peer_id", "task_id", "amount",
        "idempotency_key", "created_at", "prev_hash", "event_hash",
        "signature", "sig_algo", "metadata_json",
    )
    if not isinstance(event, dict) or any(key not in event for key in required):
        return False, "malformed event (missing required field)"
    event_id = str(event["event_id"])
    sig_algo = event["sig_algo"]
    if sig_algo != "ed25519":
        return (
            False,
            f"CONFLICT: event {event_id}: sig_algo={sig_algo!r} rejected "
            "(network mirror accepts ed25519 rows only)",
        )
    seq = int(event["seq"])
    existing = conn.execute(
        "SELECT seq FROM ledger_copy WHERE event_id=?", (event_id,)
    ).fetchone()
    if existing is not None:
        if int(existing["seq"]) == seq:
            return True, None  # idempotent re-append of the identical event
        return False, f"event {event_id}: duplicate event_id/idempotency_key conflict"
    last = conn.execute("SELECT seq, event_json FROM ledger_copy ORDER BY seq DESC LIMIT 1").fetchone()
    if last is None:
        expected_seq = 1
        expected_prev = GENESIS_HASH
    else:
        expected_seq = int(last["seq"]) + 1
        expected_prev = json.loads(last["event_json"]).get("event_hash")
    if seq != expected_seq:
        return False, f"event {event_id}: seq gap (expected {expected_seq}, got {seq})"
    if event["prev_hash"] != expected_prev:
        return False, f"event {event_id}: prev_hash linkage broken"
    try:
        metadata = json.loads(event["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        return False, f"event {event_id}: malformed metadata_json"
    body = credit_body(
        str(event["event_id"]),
        str(event["event_type"]),
        str(event["peer_id"]),
        str(event["task_id"]),
        int(event["amount"]),
        str(event["idempotency_key"]),
        str(event["created_at"]),
        str(event["prev_hash"]),
        metadata,
    )
    recomputed = content_hash(body)
    if recomputed != event["event_hash"]:
        return False, f"event {event_id}: event_hash mismatch"
    signature = event["signature"]
    if not isinstance(signature, str) or not policy.verify_credit(recomputed, signature):
        return False, f"event {event_id}: credit signature invalid"
    if int(event["amount"]) <= 0:
        return False, f"event {event_id}: non-positive amount"
    try:
        conn.execute(
            "INSERT INTO ledger_copy(seq,event_id,idempotency_key,event_json,verified) "
            "VALUES (?,?,?,?,1)",
            (
                seq,
                event_id,
                str(event["idempotency_key"]),
                json.dumps(event, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # The duplicate check above makes a plain re-append idempotent; a
        # conflict here means the same event_id/idempotency_key already
        # exists under a different seq (mirror state diverged).
        return False, f"event {event_id}: duplicate event_id/idempotency_key conflict"
    return True, None
