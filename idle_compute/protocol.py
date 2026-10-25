"""Versioned wire protocol: framing, envelopes, strict validation.

Wire format (protocol id ``/idle-compute/1.0.0``):

- Frame: 4-byte big-endian length prefix + UTF-8 JSON envelope. A frame
  longer than MAX_FRAME_BYTES is rejected after reading ONLY the 4-byte
  header — the payload buffer is never allocated for an oversized frame.
- Envelope: ``{v, type, sender, recipient, seq, ts, in_reply_to, body,
  signature}``. The signature covers the canonical
  ``idle-compute-node-msg-v2`` payload (crypto.node_msg_payload), so
  seq/ts/recipient/body are all integrity-protected. The signer's public
  key is embedded in the sender's node_id multihash (no key distribution);
  the verifier is injected by the caller.
- Validation pipeline (order is observable and tested):
  depth-limited JSON parse -> envelope shape -> BAD_SENDER (sender must
  equal the transport peer) -> SIGNATURE (verified BEFORE the body is
  schema-validated) -> per-sender seq window -> timestamp tolerance ->
  closed body registry.

This module is deliberately libp2p-free: it imports only stdlib plus
idle_compute.crypto, so the framing/envelope codecs are unit-testable
without any network stack.
"""

from __future__ import annotations

import json
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from .crypto import canonical_json, node_msg_payload
from .models import utc_now

PROTOCOL_VERSION = 1
PROTOCOL_NAME = "/idle-compute/1.0.0"
NETWORK_PROTOCOL_ID = PROTOCOL_NAME  # canonical wire protocol id

MAX_FRAME_BYTES = 64 * 1024  # 65536
MAX_BODY_BYTES = 32 * 1024  # 32768
MAX_JSON_DEPTH = 16
TS_TOLERANCE_SECONDS = 300
TS_STALE_SECONDS = 600
SEQ_WINDOW = 4096
SIG_HEX_LENGTH = 128
HASH_HEX_LENGTH = 64
MAX_LISTEN_ADDRS = 8
MAX_ADDR_CHARS = 200
MAX_STRING = 512
MAX_REF_ID = 128

_B58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_NODE_ID_PREFIX = "12D3KooW"
_NODE_ID_LENGTH = 52
_HEX = frozenset("0123456789abcdefABCDEF")


class ErrorCode(Enum):
    """The 16 structured wire error codes."""

    UNAUTHORIZED = 1  # sender not on the allowlist
    BAD_SIGNATURE = 2  # envelope signature invalid
    MALFORMED_ENVELOPE = 3  # shape/type/encoding errors
    UNSUPPORTED_TYPE = 4  # message type not in the closed registry
    INVALID_BODY = 5  # body failed the registry schema
    STALE_TIMESTAMP = 6  # ts outside the tolerance window
    SEQ_OUT_OF_WINDOW = 7  # seq not in (last_seen, last_seen+4096]
    BAD_SENDER = 8  # sender != transport peer
    FRAME_TOO_LARGE = 9  # frame exceeds the wire limit
    TASK_UNAVAILABLE = 10  # nothing claimable / wrong state
    LEASE_EXPIRED = 11
    DUPLICATE = 12  # conflicting duplicate (claim/submit)
    REJECTED = 13  # result failed validation
    NOT_FOUND = 14  # unknown task/lease/result
    OVERLOAD = 15  # resource limits hit
    INTERNAL = 16  # unexpected internal failure


class ProtocolError(Exception):
    """Structured wire error; ``code`` is an ErrorCode member."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MessageType(str, Enum):
    """Closed registry of wire message types (14)."""

    HELLO = "HELLO"
    HELLO_ACK = "HELLO_ACK"
    PING = "PING"
    PONG = "PONG"
    ERROR = "ERROR"
    TASK_CREATE = "TASK_CREATE"
    TASK_CREATE_ACK = "TASK_CREATE_ACK"
    CLAIM_REQUEST = "CLAIM_REQUEST"
    CLAIM_GRANT = "CLAIM_GRANT"
    CLAIM_DENIED = "CLAIM_DENIED"
    SUBMIT_RESULT = "SUBMIT_RESULT"
    RESULT_ACK = "RESULT_ACK"
    STATUS_REQUEST = "STATUS_REQUEST"
    STATUS_RESPONSE = "STATUS_RESPONSE"


# ---------------------------------------------------------------------------
# Framing


def encode_frame(data: bytes) -> bytes:
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError(
            ErrorCode.FRAME_TOO_LARGE,
            f"frame of {len(data)} bytes exceeds {MAX_FRAME_BYTES}",
        )
    return struct.pack(">I", len(data)) + data


def recv_exact(read: Callable[[int], bytes], n: int) -> bytes | None:
    """Read exactly ``n`` bytes; None on EOF before any byte of this read."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(read: Callable[[int], bytes]) -> bytes | None:
    """Read one frame; None on clean EOF at the frame boundary.

    The length check runs on the 4-byte header alone: an oversized frame is
    rejected before any payload buffer is allocated (observable by callers).
    """
    header = recv_exact(read, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(
            ErrorCode.FRAME_TOO_LARGE,
            f"declared frame length {length} exceeds {MAX_FRAME_BYTES}",
        )
    body = recv_exact(read, length)
    if body is None:
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "truncated frame body")
    return body


# ---------------------------------------------------------------------------
# Depth-limited JSON


def _check_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ProtocolError(
            ErrorCode.MALFORMED_ENVELOPE,
            f"JSON nesting deeper than {MAX_JSON_DEPTH}",
        )
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)


def parse_json(text: bytes | str) -> Any:
    """Parse wire JSON with a nesting-depth limit (17 levels rejected)."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "frame is not valid UTF-8") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, f"invalid JSON: {exc}") from exc
    _check_depth(value)
    return value


# ---------------------------------------------------------------------------
# Envelopes


@dataclass
class Envelope:
    v: int
    type: MessageType
    sender: str
    recipient: str | None
    seq: int
    ts: str
    in_reply_to: int | None
    body: dict[str, Any]
    signature: str


def _looks_like_node_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _NODE_ID_LENGTH
        and value.startswith(_NODE_ID_PREFIX)
        and all(ch in _B58_ALPHABET for ch in value)
    )


def signing_payload(v: int, type_: str, sender: str, recipient: str | None, seq: int, ts: str, in_reply_to: int | None, body: dict[str, Any]) -> str:
    return node_msg_payload(v, type_, sender, recipient, seq, ts, in_reply_to, body)


def build_envelope(
    type_: MessageType,
    sender: str,
    sign: Callable[[str], str],
    recipient: str | None = None,
    seq: int = 1,
    body: dict[str, Any] | None = None,
    in_reply_to: int | None = None,
    ts: str | None = None,
) -> Envelope:
    """Build and sign an envelope; ``sign(payload_str) -> hex signature``."""
    envelope = Envelope(
        v=PROTOCOL_VERSION,
        type=type_,
        sender=sender,
        recipient=recipient,
        seq=seq,
        ts=ts or utc_now(),
        in_reply_to=in_reply_to,
        body=body or {},
        signature="",
    )
    payload = signing_payload(
        envelope.v,
        envelope.type.value,
        envelope.sender,
        envelope.recipient,
        envelope.seq,
        envelope.ts,
        envelope.in_reply_to,
        envelope.body,
    )
    envelope.signature = sign(payload)
    return envelope


def envelope_to_dict(envelope: Envelope) -> dict[str, Any]:
    return {
        "v": envelope.v,
        "type": envelope.type.value,
        "sender": envelope.sender,
        "recipient": envelope.recipient,
        "seq": envelope.seq,
        "ts": envelope.ts,
        "in_reply_to": envelope.in_reply_to,
        "body": envelope.body,
        "signature": envelope.signature,
    }


def encode_envelope(envelope: Envelope) -> bytes:
    return encode_frame(canonical_json(envelope_to_dict(envelope)).encode("utf-8"))


def error_envelope(
    code: ErrorCode,
    message: str,
    *,
    sender: str,
    sign: Callable[[str], str],
    recipient: str | None,
    seq: int,
    in_reply_to: int | None = None,
    ref_type: str | None = None,
    ref_seq: int | None = None,
) -> Envelope:
    """Build an ERROR envelope (injected ``sign`` keeps this module key-free)."""
    return build_envelope(
        MessageType.ERROR,
        sender,
        sign,
        recipient=recipient,
        seq=seq,
        body={"code": code.value, "message": message[:MAX_STRING], "ref_type": ref_type, "ref_seq": ref_seq},
        in_reply_to=in_reply_to,
    )


# ---------------------------------------------------------------------------
# Timestamp and seq-window checks


def _parse_timestamp(ts: Any) -> datetime:
    if not isinstance(ts, str):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "ts must be a string")
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, f"malformed ts: {ts[:64]!r}") from exc
    if parsed.tzinfo is None:
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "ts must be timezone-aware")
    return parsed


def check_timestamp(ts: Any, now: datetime | None = None) -> None:
    """Accept ts within +/-TS_TOLERANCE_SECONDS; reject beyond as stale."""
    parsed = _parse_timestamp(ts)
    now = now or datetime.now(timezone.utc)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    diff = abs((now - parsed).total_seconds())
    if diff <= TS_TOLERANCE_SECONDS:
        return
    if diff <= TS_STALE_SECONDS:
        raise ProtocolError(ErrorCode.STALE_TIMESTAMP, f"ts {diff:.0f}s off (stale)")
    raise ProtocolError(ErrorCode.STALE_TIMESTAMP, f"ts {diff:.0f}s off (too old/future)")


class SeqWindow:
    """Per-sender seq window: accepted iff last_seen < seq <= last_seen+4096.

    Persistence hooks (``load``/``save``) are optional; the coordinator
    wires them to the node_state table (F-1), plain nodes default to
    in-memory windows.

    The in-memory map is bounded by ``max_entries``: hostile connectors can
    present an unlimited number of fresh self-signed identities, and every
    accepted frame records one entry. On overflow, entries that fail the
    ``keep`` predicate are dropped first; if the bound is still exceeded
    the map is cleared (replay protection degrades for one window's worth
    of peers rather than the process growing without bound).
    """

    def __init__(
        self,
        load: Callable[[str], int] | None = None,
        save: Callable[[str, int], None] | None = None,
        max_entries: int = 1024,
        keep: Callable[[str], bool] | None = None,
    ) -> None:
        self._memory: dict[str, int] = {}
        self._lock = threading.RLock()  # check_and_update nests a _last_seen call
        self._load = load
        self._save = save
        self._max_entries = max_entries
        self._keep = keep

    def _evict_if_needed(self) -> None:
        if len(self._memory) <= self._max_entries:
            return
        if self._keep is not None:
            for peer_id in [key for key in self._memory if not self._keep(key)]:
                del self._memory[peer_id]
        if len(self._memory) > self._max_entries:
            self._memory.clear()

    def _last_seen(self, peer_id: str) -> int:
        with self._lock:
            if peer_id in self._memory:
                return self._memory[peer_id]
            value = self._load(peer_id) if self._load else 0
            self._memory[peer_id] = value
            self._evict_if_needed()
            return value

    def check_and_update(self, peer_id: str, seq: int) -> None:
        # Thread-safe: the node layer calls this from one handler thread per
        # stream, so the check-then-record sequence must be atomic.
        with self._lock:
            if not isinstance(seq, int) or seq < 1:
                raise ProtocolError(ErrorCode.SEQ_OUT_OF_WINDOW, f"invalid seq: {seq!r}")
            last = self._last_seen(peer_id)
            if seq <= last:
                raise ProtocolError(
                    ErrorCode.SEQ_OUT_OF_WINDOW,
                    f"seq {seq} <= last_seen {last} for {peer_id[:12]}...",
                )
            if seq > last + SEQ_WINDOW:
                raise ProtocolError(
                    ErrorCode.SEQ_OUT_OF_WINDOW,
                    f"seq {seq} beyond window {last + SEQ_WINDOW} for {peer_id[:12]}...",
                )
            self._memory[peer_id] = seq
            self._evict_if_needed()
            if self._save:
                self._save(peer_id, seq)


# ---------------------------------------------------------------------------
# Body registry (closed; strict exact-key validation)


def _is_hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(ch in _HEX for ch in value)


def _is_ref_id(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_REF_ID


def _is_short_str(value: Any, max_len: int = MAX_STRING) -> bool:
    return isinstance(value, str) and len(value) <= max_len


def _is_role(value: Any) -> bool:
    return value in ("coordinator", "worker", "client")


def _is_listen_addrs(value: Any) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_LISTEN_ADDRS
        and all(_is_short_str(item, MAX_ADDR_CHARS) for item in value)
    )


def _is_task_payload(value: Any) -> bool:
    # Shape-only check; the whitelist (tasks.NumericBatchTask) re-validates
    # bounds/values at execution time — the network never bypasses it.
    return isinstance(value, dict)


def _is_lease_dict(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _is_ref_id(value.get("lease_id"))
        and _is_ref_id(value.get("task_id"))
        and _is_ref_id(value.get("peer_id"))
        and _is_short_str(value.get("expires_at"))
    )


def _is_task_dict(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("task_type") == "numeric_batch_v1"
        and isinstance(value.get("payload"), dict)
    )


def _is_credit_event(value: Any) -> bool:
    # The 13-key shape verify_and_append (ledger.py) needs; the worker mirror
    # re-verifies signature + chain, so the wire shape check stays shallow.
    required = (
        "seq", "event_id", "event_type", "peer_id", "task_id", "amount",
        "idempotency_key", "created_at", "prev_hash", "event_hash",
        "signature", "sig_algo", "metadata_json",
    )
    return isinstance(value, dict) and all(key in value for key in required)


def _check_hello(value: Any) -> None:
    if not (
        isinstance(value, dict)
        and _looks_like_node_id(value.get("node_id"))
        and _is_role(value.get("role"))
        and _is_listen_addrs(value.get("listen_addrs"))
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "HELLO body must be {node_id, role, listen_addrs}")


def _check_hello_ack(value: Any) -> None:
    _check_hello(value)  # same shape


def _check_ping(value: Any) -> None:
    if not (isinstance(value, dict) and _is_short_str(value.get("nonce"), 64)):
        raise ProtocolError(ErrorCode.INVALID_BODY, "PING body must be {nonce: str<=64}")


def _check_pong(value: Any) -> None:
    _check_ping(value)


def _check_error(value: Any) -> None:
    code = value.get("code") if isinstance(value, dict) else None
    if not (
        isinstance(value, dict)
        and isinstance(code, int)
        and 1 <= code <= len(ErrorCode)
        and _is_short_str(value.get("message"))
        and (value.get("ref_type") is None or _is_short_str(value.get("ref_type"), 32))
        and (value.get("ref_seq") is None or isinstance(value.get("ref_seq"), int))
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "ERROR body must be {code 1..16, message<=512, ref_type?, ref_seq?}")


def _check_task_create(value: Any) -> None:
    if not (
        isinstance(value, dict)
        and _is_ref_id(value.get("client_ref"))
        and value.get("task_type") == "numeric_batch_v1"
        and _is_task_payload(value.get("payload"))
        and isinstance(value.get("max_attempts"), int)
        and 1 <= value["max_attempts"] <= 10
        and isinstance(value.get("lease_seconds"), int)
        and 1 <= value["lease_seconds"] <= 3600
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "TASK_CREATE body shape invalid (whitelist bounds re-checked at the store)")


def _check_task_create_ack(value: Any) -> None:
    if not (isinstance(value, dict) and _is_ref_id(value.get("client_ref")) and _is_ref_id(value.get("task_id"))):
        raise ProtocolError(ErrorCode.INVALID_BODY, "TASK_CREATE_ACK body must be {client_ref, task_id}")


def _check_claim_request(value: Any) -> None:
    if not (
        isinstance(value, dict)
        and _is_ref_id(value.get("claim_request_id"))
        and (value.get("task_id") is None or _is_ref_id(value.get("task_id")))
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "CLAIM_REQUEST body must be {claim_request_id, task_id?}")


def _check_claim_grant(value: Any) -> None:
    if not (
        isinstance(value, dict)
        and _is_ref_id(value.get("claim_request_id"))
        and _is_lease_dict(value.get("lease"))
        and _is_task_dict(value.get("task"))
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "CLAIM_GRANT body must be {claim_request_id, lease, task}")


def _check_claim_denied(value: Any) -> None:
    if not (isinstance(value, dict) and _is_ref_id(value.get("claim_request_id"))):
        raise ProtocolError(ErrorCode.INVALID_BODY, "CLAIM_DENIED body must be {claim_request_id}")


def _check_submit_result(value: Any) -> None:
    if not (
        isinstance(value, dict)
        and _is_ref_id(value.get("lease_id"))
        and isinstance(value.get("output"), dict)
        and _is_hex(value.get("content_hash"), HASH_HEX_LENGTH)
        and _is_hex(value.get("signature"), SIG_HEX_LENGTH)
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "SUBMIT_RESULT body must be {lease_id, output, content_hash(64hex), signature(128hex)}")


def _check_result_ack(value: Any) -> None:
    if not (
        isinstance(value, dict)
        and _is_ref_id(value.get("result_id"))
        and _is_ref_id(value.get("lease_id"))
        and _is_ref_id(value.get("task_id"))
        and value.get("validation") in ("VALID", "INVALID")
        and _is_short_str(value.get("reason"))
        and (value.get("credit") is None or _is_credit_event(value.get("credit")))
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "RESULT_ACK body must be {result_id, lease_id, task_id, validation, reason, credit?}")


def _check_status_request(value: Any) -> None:
    if not (isinstance(value, dict) and _is_ref_id(value.get("task_id"))):
        raise ProtocolError(ErrorCode.INVALID_BODY, "STATUS_REQUEST body must be {task_id}")


def _check_status_response(value: Any) -> None:
    if not (
        isinstance(value, dict)
        and _is_ref_id(value.get("task_id"))
        and (value.get("status") is None or _is_short_str(value.get("status"), 32))
        and (value.get("result") is None or _is_short_str(value.get("result"), 32))
        and (value.get("validation") is None or _is_short_str(value.get("validation"), 32))
        and isinstance(value.get("credits_count"), int)
        and value["credits_count"] >= 0
    ):
        raise ProtocolError(ErrorCode.INVALID_BODY, "STATUS_RESPONSE body shape invalid")


BODY_SCHEMAS: dict[MessageType, Callable[[Any], None]] = {
    MessageType.HELLO: _check_hello,
    MessageType.HELLO_ACK: _check_hello_ack,
    MessageType.PING: _check_ping,
    MessageType.PONG: _check_pong,
    MessageType.ERROR: _check_error,
    MessageType.TASK_CREATE: _check_task_create,
    MessageType.TASK_CREATE_ACK: _check_task_create_ack,
    MessageType.CLAIM_REQUEST: _check_claim_request,
    MessageType.CLAIM_GRANT: _check_claim_grant,
    MessageType.CLAIM_DENIED: _check_claim_denied,
    MessageType.SUBMIT_RESULT: _check_submit_result,
    MessageType.RESULT_ACK: _check_result_ack,
    MessageType.STATUS_REQUEST: _check_status_request,
    MessageType.STATUS_RESPONSE: _check_status_response,
}


def _keys_exact(value: dict[str, Any], *keys: str) -> None:
    if set(value) != set(keys):
        raise ProtocolError(
            ErrorCode.INVALID_BODY,
            f"body keys must be exactly {sorted(keys)}",
        )


# Nullable keys (e.g. task_id in CLAIM_REQUEST) are always PRESENT with a
# null value on the wire — optional-then-absent keys are rejected, so no
# field can be smuggled past the registry.
_BODY_KEYS: dict[MessageType, tuple[str, ...]] = {
    MessageType.HELLO: ("node_id", "role", "listen_addrs"),
    MessageType.HELLO_ACK: ("node_id", "role", "listen_addrs"),
    MessageType.PING: ("nonce",),
    MessageType.PONG: ("nonce",),
    MessageType.ERROR: ("code", "message", "ref_type", "ref_seq"),
    MessageType.TASK_CREATE: ("client_ref", "task_type", "payload", "max_attempts", "lease_seconds"),
    MessageType.TASK_CREATE_ACK: ("client_ref", "task_id"),
    MessageType.CLAIM_REQUEST: ("claim_request_id", "task_id"),
    MessageType.CLAIM_GRANT: ("claim_request_id", "lease", "task"),
    MessageType.CLAIM_DENIED: ("claim_request_id",),
    MessageType.SUBMIT_RESULT: ("lease_id", "output", "content_hash", "signature"),
    MessageType.RESULT_ACK: ("result_id", "lease_id", "task_id", "validation", "reason", "credit"),
    MessageType.STATUS_REQUEST: ("task_id",),
    MessageType.STATUS_RESPONSE: ("task_id", "status", "result", "validation", "credits_count"),
}


def validate_body(type_: MessageType, body: Any) -> dict[str, Any]:
    """Run the closed-registry schema check for ``type_`` (exact key set)."""
    checker = BODY_SCHEMAS.get(type_)
    if checker is None:
        raise ProtocolError(ErrorCode.UNSUPPORTED_TYPE, f"no schema for message type {type_!r}")
    if not isinstance(body, dict):
        raise ProtocolError(ErrorCode.INVALID_BODY, "body must be a JSON object")
    _keys_exact(body, *_BODY_KEYS[type_])
    checker(body)
    return body


# ---------------------------------------------------------------------------
# The full parse pipeline


def parse_message(
    data: bytes,
    *,
    transport_peer_id: str | None,
    verifier: Callable[[str, str, str], bool],
    seq_window: SeqWindow,
    now: datetime | None = None,
) -> Envelope:
    """Parse and validate one frame; raises ProtocolError on rejection.

    Order (tested to be observable): parse -> shape -> BAD_SENDER ->
    signature -> seq window -> timestamp -> body schema. The verifier and
    the body schema run in exactly that order, so a message with both a bad
    signature and an invalid body is rejected as BAD_SIGNATURE and the body
    is never schema-validated.
    """
    value = parse_json(data)
    if not isinstance(value, dict):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "envelope must be a JSON object")

    if value.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, f"unsupported protocol version {value.get('v')!r}")
    type_str = value.get("type")
    if not isinstance(type_str, str):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "type must be a string")
    try:
        type_ = MessageType(type_str)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.UNSUPPORTED_TYPE, f"unknown message type {type_str[:32]!r}") from exc

    sender = value.get("sender")
    if not _looks_like_node_id(sender):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "sender is not a valid node id")
    recipient = value.get("recipient")
    if recipient is not None and not _looks_like_node_id(recipient):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "recipient is not a valid node id")
    seq = value.get("seq")
    if not isinstance(seq, int) or seq < 1:
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "seq must be a positive int")
    ts = value.get("ts")
    in_reply_to = value.get("in_reply_to")
    if in_reply_to is not None and not isinstance(in_reply_to, int):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "in_reply_to must be an int or null")
    body = value.get("body")
    if not isinstance(body, dict):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "body must be a JSON object")
    signature = value.get("signature")
    if not isinstance(signature, str) or not _is_hex(signature, SIG_HEX_LENGTH):
        raise ProtocolError(ErrorCode.MALFORMED_ENVELOPE, "signature must be 128 hex chars")

    if transport_peer_id is not None and sender != transport_peer_id:
        raise ProtocolError(
            ErrorCode.BAD_SENDER,
            f"envelope sender != transport peer ({transport_peer_id[:12]}...)",
        )

    payload = signing_payload(PROTOCOL_VERSION, type_str, sender, recipient, seq, ts, in_reply_to, body)
    if not verifier(sender, payload, signature):
        raise ProtocolError(ErrorCode.BAD_SIGNATURE, "envelope signature invalid")

    seq_window.check_and_update(sender, seq)
    check_timestamp(ts, now=now)

    if len(canonical_json(body)) > MAX_BODY_BYTES:
        raise ProtocolError(ErrorCode.INVALID_BODY, f"body larger than {MAX_BODY_BYTES} bytes")
    validate_body(type_, body)

    return Envelope(
        v=PROTOCOL_VERSION,
        type=type_,
        sender=sender,
        recipient=recipient,
        seq=seq,
        ts=ts,
        in_reply_to=in_reply_to,
        body=body,
        signature=signature,
    )
