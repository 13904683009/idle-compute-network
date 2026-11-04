"""Node runtime: wire handlers and role loops over NetTransport + protocol.

Roles:

- coordinator: serves HELLO/PING/TASK_CREATE/CLAIM_REQUEST/SUBMIT_RESULT/
  STATUS_REQUEST. Store access is single-threaded: handler threads parse and
  authenticate frames (pure functions), then hand verified envelopes to the
  coordinator loop through a bounded work queue; the loop calls Store and
  writes replies back on the stream. Lease expiry runs on the same loop.
  Per-peer seen-seq persists in the node_state table (aux connection), the
  claims/client_refs tables give idempotent replays.
- worker: dials the coordinator (pull topology), HELLO, then a
  claim -> execute -> submit -> ack cycle; mirrors coordinator-signed
  credit events through ledger.verify_and_append; persists its send seq so
  a restart continues the window instead of tripping the coordinator's
  replay guard; reconnects with 1..8s bounded backoff.
- client: one-shot request/reply flows (create-task/status/ping via cli).

Security invariants (enforced here; never bypassed):

- Every inbound frame goes through protocol.parse_message BEFORE any
  handler: Ed25519 signature (key embedded in the sender node_id), per-
  sender seq window, timestamp tolerance, exact-key body schemas.
- A stream's first frame must be HELLO; HELLO binds the stream to its
  sender (body.node_id == sender); every later frame on that stream must
  carry the same sender. Peers not on the config allowlist are answered
  UNAUTHORIZED and disconnected.
- Task execution uses the closed whitelist registry (tasks.get_task_type)
  on both sides; credit only ever comes from the coordinator store's
  validate_result (whitelist recompute + signature verify + idempotent
  ledger). Worker-side credits are verified (sig + chain) before being
  mirrored; an unverifiable credit is logged and never appended.
- Message content never reaches any dynamic execution path (the
  source-safety scan pins this at the module level).

Threading model: one handler thread per stream (parse/auth/dispatch only),
one coordinator loop thread, one worker loop thread. Node identity,
protocol, and transports are the only shared machinery.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import ledger
from .config import NodeConfig, ValidationError, is_valid_node_id
from .db import DuplicateSubmission, LeaseExpired, Store, TaskUnavailable
from .identity import NodeIdentity
from .models import Lease, LeaseStatus, Task, TaskStatus, ValidationStatus, new_id, utc_now
from .net_transport import (
    Libp2pTransport,
    NetTransport,
    TransportError,
    write_rendezvous_file,
)
from .protocol import (
    ErrorCode,
    MessageType,
    ProtocolError,
    SeqWindow,
    build_envelope,
    encode_envelope,
    error_envelope,
    parse_json,
    parse_message,
    read_frame,
)
from .ratelimit import PeerRateLimiter
from .signing import Ed25519Policy
from .tasks import InvalidTaskInput
from .transport_tcp import TcpTransport
from .worker import Worker

logger = logging.getLogger("idle_compute.node")

HELLO_ACK_TIMEOUT = 15.0
REQUEST_TIMEOUT = 20.0
SUBMIT_ACK_TIMEOUT = 30.0
LEDGER_SYNC_TIMEOUT = 20.0
MAX_SYNC_PAGES_PER_ROUND = 20  # bounded catch-up per worker cycle
WORK_QUEUE_SIZE = 1024
# F-2: hard cap on non-terminal tasks (QUEUED/LEASED/RUNNING/SUBMITTED). The
# coordinator serves TASK_CREATE on its single loop thread, so a count-then-
# insert check is race-free; the 1001st live task is refused with OVERLOAD.
MAX_ACTIVE_TASKS = 1000
_TERMINAL_TASK_STATUSES = ("COMPLETED", "REJECTED", "CANCELLED", "EXPIRED")
REPLY_BUFFER_SIZE = 64

_COORD_HANDLED = frozenset(
    {
        MessageType.TASK_CREATE,
        MessageType.CLAIM_REQUEST,
        MessageType.SUBMIT_RESULT,
        MessageType.STATUS_REQUEST,
        MessageType.LEDGER_SYNC_REQUEST,
    }
)
_WORKER_REPLY_TYPES = frozenset(
    {
        MessageType.HELLO_ACK,
        MessageType.PONG,
        MessageType.ERROR,
        MessageType.CLAIM_GRANT,
        MessageType.CLAIM_DENIED,
        MessageType.RESULT_ACK,
        MessageType.LEDGER_SYNC_RESPONSE,
    }
)
_CLIENT_REPLY_TYPES = frozenset(
    {MessageType.HELLO_ACK, MessageType.PONG, MessageType.ERROR, MessageType.TASK_CREATE_ACK, MessageType.STATUS_RESPONSE}
)


class NodeError(Exception):
    """Runtime failure of a node operation."""


class NodeTimeoutError(NodeError):
    """No matching reply arrived within the deadline."""


class NodeRemoteError(NodeError):
    """The peer answered with a structured ERROR envelope."""


class _ReplyBuffer:
    """Single-consumer mailbox: handler threads put envelopes; the role loop
    picks replies matching a predicate. Unmatched envelopes stay buffered
    (bounded) so a later wait can still match them."""

    def __init__(self, max_items: int = REPLY_BUFFER_SIZE) -> None:
        self._items: deque[Any] = deque()
        self._cond = threading.Condition()
        self._max_items = max_items

    def put(self, env: Any) -> None:
        with self._cond:
            self._items.append(env)
            while len(self._items) > self._max_items:
                self._items.popleft()
            self._cond.notify_all()

    def wait(self, predicate: Callable[[Any], bool], timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for index, env in enumerate(self._items):
                    if predicate(env):
                        del self._items[index]
                        return env
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NodeTimeoutError("no matching reply within timeout")
                self._cond.wait(remaining)


class Node:
    """One configured network node (coordinator, worker, or client)."""

    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self._data_dir = Path(config.data_dir) if config.data_dir else None
        if self._data_dir is None or not str(self._data_dir):
            raise NodeError("data_dir must be a non-empty path for network nodes")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir = self._data_dir / "run"
        self._run_dir.mkdir(parents=True, exist_ok=True)

        self.identity = NodeIdentity.load_or_create(
            self._data_dir / "identity.key", node_name=config.node_id or "node"
        )
        if config.node_id and self.identity.node_id != config.node_id:
            raise NodeError(
                f"identity mismatch: config node_id {config.node_id} does not "
                f"match the identity at {self._data_dir / 'identity.key'} "
                f"({self.identity.node_id})"
            )
        if not is_valid_node_id(self.identity.node_id):
            raise NodeError("identity produced an invalid node id")

        self._policy = Ed25519Policy.from_identity(
            self.identity, coordinator_node_id=config.coordinator_node_id
        )

        # Transports.
        if config.transport == "tcp":
            self.transport: NetTransport = TcpTransport(
                self.identity.node_id, config.listen_host, config.listen_port
            )
        else:
            self.transport = Libp2pTransport(
                self.identity.key_pair,
                [f"/ip4/{config.listen_host}/tcp/{config.listen_port}"],
            )
        self.transport.set_handler(self._on_stream)

        # Coordinator-only state. The Store and mirror connections are
        # opened lazily INSIDE their role loops: sqlite3 pins a connection
        # to its creating thread, and the loops may run on a different
        # thread than __init__ (tests and embedders start them that way).
        self._store: Store | None = None
        self._aux: sqlite3.Connection | None = None
        self._aux_lock = threading.Lock()
        self._db_path = config.db_path or str(self._data_dir / "coordinator.db")
        if config.role == "coordinator":
            self._aux = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
            # Create the schema up front: handler threads read node_state/
            # claims/client_refs through the aux connection from the first
            # inbound HELLO on, which may precede the lazy Store open in the
            # coordinator loop thread.
            bootstrap = Store(self._db_path, signer=self._policy)
            bootstrap.close()

        # Worker-only mirror (verified coordinator-signed credits); opened
        # in the worker loop for the same thread-affinity reason.
        self._mirror: sqlite3.Connection | None = None

        # Seq state: sent seq persists to disk; the coordinator also
        # persists per-peer seen-seq in node_state via the aux connection.
        # Persistence is allowlist-gated: unknown senders never get past
        # HELLO, so recording their window would only grow node_state from
        # hostile input (audit finding node.py:347). Their frames are still
        # window-checked in memory for the lifetime of the process.
        self._seq_file = self._data_dir / "seq.json"
        self._seq_lock = threading.Lock()
        self._send_seq = self._load_seq()
        self._allowlisted = set(self.config.peers)
        if config.role == "coordinator" and self._aux is not None:
            self._seq_window = SeqWindow(
                load=self._seen_load,
                save=self._seen_save,
                keep=self._allowlisted.__contains__,
            )
        else:
            self._seq_window = SeqWindow(keep=self._allowlisted.__contains__)

        self._replies = _ReplyBuffer()
        self._work: queue.Queue[tuple[Any, Any]] = queue.Queue(maxsize=WORK_QUEUE_SIZE)
        # F-1: per-peer ingress limit (20 msg/s, burst 40; 2 trips / 60 s ->
        # close + 30 s quarantine). Keyed by the transport peer id: for libp2p
        # that is the authenticated node id, for TCP the per-connection
        # placeholder until HELLO binds the stream.
        self._ratelimit = PeerRateLimiter()
        self._stopping = threading.Event()
        self._started = False
        self._connected = False
        self._shutdown_done = False
        # File log handler attached by run() and detached in shutdown(); a
        # logging.FileHandler left attached keeps the log file open (and
        # locked on Windows) after the node stops.
        self._log_handler: logging.Handler | None = None
        # E-2 ledger-divergence latch: set when the mirror cannot follow the
        # coordinator chain; the worker stops claiming until a sync round
        # passes cleanly (which only happens once the divergence is fixed).
        self._ledger_conflict: str | None = None
        # E-2 startup re-audit cursor: 0 means "re-verify the whole chain from
        # the beginning", None means "incremental only". Set per worker run in
        # _worker_loop so an in-place edit of an already-mirrored event is not
        # invisible to incremental pulls.
        self._audit_cursor: int | None = None

    # ------------------------------------------------------------------ seq

    def _load_seq(self) -> int:
        try:
            return int(json.loads(self._seq_file.read_text(encoding="utf-8"))["seq"])
        except Exception:
            return 0

    def _persist_seq(self) -> None:
        try:
            tmp = self._seq_file.with_name("seq.json.tmp")
            tmp.write_text(json.dumps({"seq": self._send_seq}), encoding="utf-8")
            os.replace(tmp, self._seq_file)
        except OSError:
            logger.warning("could not persist send seq to %s", self._seq_file)

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._send_seq += 1
            self._persist_seq()
            return self._send_seq

    def _seen_load(self, peer_id: str) -> int:
        # Allowlist gate: unknown senders are rejected at HELLO anyway, so
        # their window never needs (or gets) a persisted row.
        if peer_id not in self._allowlisted:
            return 0
        try:
            row = self._aux_query("SELECT last_seq_seen FROM node_state WHERE peer_id=?", (peer_id,))
        except sqlite3.Error:
            logger.warning("node_state read failed; replay window resets for %s", peer_id[:12])
            return 0
        return int(row[0]) if row else 0

    def _seen_save(self, peer_id: str, seq: int) -> None:
        if peer_id not in self._allowlisted:
            return
        try:
            self._aux_execute(
                "INSERT INTO node_state(peer_id,last_seq_seen) VALUES (?,?) "
                "ON CONFLICT(peer_id) DO UPDATE SET last_seq_seen=excluded.last_seq_seen",
                (peer_id, seq),
            )
        except sqlite3.Error:
            # Persistence is best-effort: a failed save must never kill the
            # handler thread that is mid-parse.
            logger.warning("node_state write failed for %s", peer_id[:12])

    def _aux_query(self, sql: str, params: tuple = ()) -> Any:
        with self._aux_lock:
            return self._aux.execute(sql, params).fetchone()

    def _aux_execute(self, sql: str, params: tuple = ()) -> None:
        with self._aux_lock:
            self._aux.execute(sql, params)
            self._aux.commit()

    # ------------------------------------------------------------- sending

    def _build(self, type_: MessageType, body: dict[str, Any], recipient: str | None = None, in_reply_to: int | None = None) -> bytes:
        seq = self._next_seq()
        envelope = build_envelope(
            type_,
            self.identity.node_id,
            self.identity.sign,
            recipient=recipient,
            seq=seq,
            body=body,
            in_reply_to=in_reply_to,
        )
        return encode_envelope(envelope)

    def _send_to(self, peer_id: str, type_: MessageType, body: dict[str, Any], in_reply_to: int | None = None) -> int:
        seq = self._next_seq()
        envelope = build_envelope(
            type_,
            self.identity.node_id,
            self.identity.sign,
            recipient=peer_id,
            seq=seq,
            body=body,
            in_reply_to=in_reply_to,
        )
        self.transport.send(peer_id, encode_envelope(envelope))
        return seq

    def _write_frame(self, write: Callable[[bytes], None], frame: bytes) -> None:
        try:
            write(frame)
        except Exception as exc:
            logger.debug("reply write failed: %s", exc)

    def _reply(self, write: Callable[[bytes], None], type_: MessageType, body: dict[str, Any], env: Any) -> None:
        self._write_frame(
            write,
            self._build(type_, body, recipient=env.sender, in_reply_to=env.seq),
        )

    def _reply_error(
        self,
        write: Callable[[bytes], None],
        code: ErrorCode,
        message: str,
        env: Any | None = None,
    ) -> None:
        try:
            frame = error_envelope(
                code,
                message,
                sender=self.identity.node_id,
                sign=self.identity.sign,
                recipient=env.sender if env is not None else None,
                seq=self._next_seq(),
                in_reply_to=env.seq if env is not None else None,
                ref_type=env.type.value if env is not None else None,
            )
            self._write_frame(write, encode_envelope(frame))
        except Exception as exc:
            logger.debug("error reply failed: %s", exc)

    # ------------------------------------------------------ inbound stream

    def _on_stream(self, transport_peer: str, read: Callable[[int], bytes], write: Callable[[bytes], None]) -> None:
        """One handler thread per stream: rate gate, frame loop, parse, auth, dispatch."""
        bound: str | None = None
        if self._ratelimit.blocked(transport_peer):
            # A quarantined peer never gets a handler loop: refuse and close.
            logger.warning("refusing stream from quarantined peer %s", transport_peer[:12])
            return
        while not self._stopping.is_set():
            try:
                frame = read_frame(read)
            except ProtocolError:
                return  # framing broken (oversized/truncated): close stream
            if frame is None:
                return
            # F-1: account the frame BEFORE parsing/verification. Keyed by the
            # bound node id once HELLO has run (so a TCP peer cannot dodge its
            # quarantine by reconnecting), by the transport peer before that.
            # Dropped frames are not answered (no amplification); a second trip
            # within the window closes the stream.
            decision = self._ratelimit.check(bound or transport_peer)
            if not decision.allow:
                if decision.close_stream:
                    logger.warning(
                        "rate limit: closing stream for %s (%s)", transport_peer[:12], decision.reason
                    )
                    self._reply_error(write, ErrorCode.OVERLOAD, "rate limited; stream closed", None)
                    return
                logger.debug("rate limit: dropped one frame from %s", transport_peer[:12])
                continue
            try:
                # Libp2p hands us the authenticated transport peer id (muxed
                # conn identity), so the BAD_SENDER stage runs BEFORE the
                # signature check there. TCP's placeholder ("tcp:host:port")
                # is not a node id, so TCP keeps binding-after-parse by
                # design (no transport-level peer auth exists).
                env = parse_message(
                    frame,
                    transport_peer_id=transport_peer if is_valid_node_id(transport_peer) else None,
                    verifier=NodeIdentity.verify_signature,
                    seq_window=self._seq_window,
                )
            except ProtocolError as exc:
                self._on_parse_error(exc, frame, write)
                continue
            if bound is None:
                if env.type is MessageType.ERROR:
                    # An ERROR can legitimately precede the mutual HELLO:
                    # our own first frame may have been rejected before the
                    # handshake (e.g. SEQ_OUT_OF_WINDOW after a rolled-back
                    # seq.json). Route it to the reply buffer so _await can
                    # act on it (seq self-heal); the stream stays unbound
                    # and still demands HELLO before anything else.
                    if not self._on_envelope(env, write):
                        return
                    continue
                if env.type is not MessageType.HELLO:
                    self._reply_error(write, ErrorCode.UNAUTHORIZED, "HELLO required as first frame", env)
                    return
                if env.sender != env.body["node_id"]:
                    self._reply_error(write, ErrorCode.BAD_SENDER, "HELLO body node_id != sender", env)
                    return
                if env.sender not in self.config.peers:
                    self._reply_error(write, ErrorCode.UNAUTHORIZED, "sender not in allowlist", env)
                    return
                if self._ratelimit.blocked(env.sender):
                    # The node id is quarantined: refuse the handshake so a TCP
                    # reconnect cannot reset the bucket.
                    self._reply_error(write, ErrorCode.OVERLOAD, "peer is rate limited", env)
                    return
                bound = env.sender
                # Mutual HELLO handshake: the dialer's gate also demands a
                # HELLO as ITS first inbound frame, so our own HELLO goes
                # out BEFORE the HELLO_ACK. A peer replying to our HELLO
                # with another HELLO is answered with an ack only (below),
                # which terminates the exchange.
                self._reply(write, MessageType.HELLO, self._hello_body(), env)
                self._reply(write, MessageType.HELLO_ACK, self._hello_body(), env)
                continue
            if env.sender != bound:
                self._reply_error(write, ErrorCode.BAD_SENDER, "sender mismatch on bound stream", env)
                return
            if env.type is MessageType.HELLO:
                self._reply(write, MessageType.HELLO_ACK, self._hello_body(), env)
                continue
            if not self._on_envelope(env, write):
                return

    def _on_parse_error(self, exc: ProtocolError, frame: bytes, write: Callable[[bytes], None]) -> None:
        """Best-effort structured ERROR for a rejected frame (never fatal)."""
        try:
            raw = parse_json(frame)
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        sender = raw.get("sender")
        if not is_valid_node_id(sender):
            return
        raw_type = raw.get("type")
        try:
            ref_type = MessageType(raw_type).value if isinstance(raw_type, str) else None
        except ValueError:
            # The wire type is not a registered message type: echoing it
            # would violate the ERROR schema's ref_type bound, so omit it.
            ref_type = None
        try:
            reply = error_envelope(
                exc.code,
                exc.message,
                sender=self.identity.node_id,
                sign=self.identity.sign,
                recipient=sender,
                seq=self._next_seq(),
                in_reply_to=raw.get("seq") if isinstance(raw.get("seq"), int) else None,
                ref_type=ref_type,
            )
            self._write_frame(write, encode_envelope(reply))
        except Exception:
            return

    def _on_envelope(self, env: Any, write: Callable[[bytes], None]) -> bool:
        """Dispatch an authenticated envelope; False closes the stream."""
        if env.type is MessageType.PING:
            self._reply(
                write,
                MessageType.PONG,
                {"nonce": env.body["nonce"]},
                env,
            )
            return True
        if self.config.role == "coordinator":
            # Handshake/heartbeat acknowledgments carry nothing actionable.
            if env.type in (MessageType.HELLO_ACK, MessageType.PONG):
                return True
            # Never answer an ERROR with an ERROR: two coordinators doing so
            # would echo REJECTED frames back and forth without bound.
            if env.type is MessageType.ERROR:
                return True
            if env.type in _COORD_HANDLED:
                try:
                    self._work.put_nowait((env, write))
                except queue.Full:
                    self._reply_error(write, ErrorCode.OVERLOAD, "coordinator work queue full", env)
            else:
                self._reply_error(write, ErrorCode.REJECTED, f"coordinator does not handle {env.type.value}", env)
            return True
        if self.config.role == "worker":
            if env.type in _WORKER_REPLY_TYPES:
                self._replies.put(env)
            else:
                self._reply_error(write, ErrorCode.REJECTED, f"worker does not handle {env.type.value}", env)
            return True
        if env.type in _CLIENT_REPLY_TYPES:
            self._replies.put(env)
        else:
            self._reply_error(write, ErrorCode.REJECTED, f"client does not handle {env.type.value}", env)
        return True

    def _hello_body(self) -> dict[str, Any]:
        addrs = self.transport.advertised_addrs() or [
            f"{self.config.listen_host}:{self.config.listen_port}"
        ]
        return {
            "node_id": self.identity.node_id,
            "role": self.config.role,
            "listen_addrs": addrs[:1],
        }

    # ------------------------------------------------- coordinator handlers

    def _handle_coordinator(self, env: Any, write: Callable[[bytes], None]) -> None:
        try:
            if env.type is MessageType.TASK_CREATE:
                self._handle_task_create(env, write)
            elif env.type is MessageType.CLAIM_REQUEST:
                self._handle_claim_request(env, write)
            elif env.type is MessageType.SUBMIT_RESULT:
                self._handle_submit_result(env, write)
            elif env.type is MessageType.STATUS_REQUEST:
                self._handle_status_request(env, write)
            elif env.type is MessageType.LEDGER_SYNC_REQUEST:
                self._handle_ledger_sync_request(env, write)
        except Exception:
            logger.exception("coordinator handler failed for %s", env.type.value)
            self._reply_error(write, ErrorCode.INTERNAL, "internal error", env)

    def _handle_ledger_sync_request(self, env: Any, write: Callable[[bytes], None]) -> None:
        """Serve one LEDGER_SYNC page (read-only, no state change).

        Pages are clamped by count AND serialized size (ledger.export_page),
        so a response always fits the wire body cap; ``more`` tells the
        puller whether to request the next page. Runs on the coordinator
        loop thread, so the Store connection is this thread's own.
        """
        conn = self._store.conn
        after_seq = int(env.body["after_seq"])
        events, more = ledger.export_page(conn, after_seq)
        self._reply(
            write,
            MessageType.LEDGER_SYNC_RESPONSE,
            {
                "after_seq": after_seq,
                "head_seq": ledger.head_seq(conn),
                "events": events,
                "more": more,
            },
            env,
        )

    def _handle_task_create(self, env: Any, write: Callable[[bytes], None]) -> None:
        client_ref = env.body["client_ref"]
        row = self._aux_query("SELECT task_id FROM client_refs WHERE client_ref=?", (client_ref,))
        if row is not None:
            task_id = row[0]
        else:
            # F-2: bounded task pool. Counted on the actor thread, so the
            # check-then-insert cannot race with another create.
            placeholders = ",".join("?" for _ in _TERMINAL_TASK_STATUSES)
            live = self._store.conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE status NOT IN ({placeholders})",
                _TERMINAL_TASK_STATUSES,
            ).fetchone()[0]
            if int(live) >= MAX_ACTIVE_TASKS:
                self._reply_error(write, ErrorCode.OVERLOAD, "task pool is full", env)
                return
            try:
                task = self._store.create_task(
                    env.body["task_type"],
                    env.body["payload"],
                    max_attempts=env.body["max_attempts"],
                    lease_seconds=env.body["lease_seconds"],
                )
            except (ValueError, InvalidTaskInput) as exc:
                self._reply_error(write, ErrorCode.INVALID_BODY, str(exc)[:200], env)
                return
            task_id = task.task_id
            self._aux_execute(
                "INSERT OR IGNORE INTO client_refs(client_ref,task_id,created_at) VALUES (?,?,?)",
                (client_ref, task_id, utc_now()),
            )
        self._reply(write, MessageType.TASK_CREATE_ACK, {"client_ref": client_ref, "task_id": task_id}, env)

    def _handle_claim_request(self, env: Any, write: Callable[[bytes], None]) -> None:
        claim_request_id = env.body["claim_request_id"]
        # Idempotent replay: a retried CLAIM_REQUEST with a known id gets the
        # SAME lease back (claims table), never a second one. A known id
        # NEVER mints a fresh lease: the worker generates a new id per claim
        # cycle, so the only caller of a remap path would be an attacker
        # replaying a stolen id to mint leases on queued tasks (audit
        # finding node.py:503). Known id + (missing/expired/foreign lease)
        # is simply denied.
        lease: Lease | None = None
        row = self._aux_query(
            "SELECT lease_id FROM claims WHERE claim_request_id=?", (claim_request_id,)
        )
        if row is not None:
            existing = self._store.get_lease(row[0])
            if (
                existing is not None
                and existing.peer_id == env.sender
                and existing.status is LeaseStatus.ACTIVE
            ):
                lease = existing
        if lease is None:
            if row is None:
                lease = self._store.claim_task(env.sender, env.body["task_id"])
                if lease is not None:
                    self._aux_execute(
                        "INSERT OR IGNORE INTO claims(claim_request_id,lease_id,task_id,worker_id,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (claim_request_id, lease.lease_id, lease.task_id, env.sender, utc_now()),
                    )
            if lease is None:
                self._reply(write, MessageType.CLAIM_DENIED, {"claim_request_id": claim_request_id}, env)
                return
        task = self._store.get_task(lease.task_id)
        if task is None:
            self._reply(write, MessageType.CLAIM_DENIED, {"claim_request_id": claim_request_id}, env)
            return
        # The grant hands the worker the task: LEASED -> RUNNING now, so the
        # later SUBMIT_RESULT meets the state machine. An expired lease is
        # answered with a denial; the worker re-claims on the next cycle.
        if task.status is TaskStatus.LEASED:
            try:
                self._store.start_task(lease.lease_id, env.sender)
            except (LeaseExpired, TaskUnavailable) as exc:
                self._reply(write, MessageType.CLAIM_DENIED, {"claim_request_id": claim_request_id}, env)
                return
        self._reply(
            write,
            MessageType.CLAIM_GRANT,
            {
                "claim_request_id": claim_request_id,
                "lease": {
                    "lease_id": lease.lease_id,
                    "task_id": lease.task_id,
                    "peer_id": lease.peer_id,
                    "expires_at": lease.expires_at,
                },
                "task": {"task_type": task.task_type, "payload": task.payload},
            },
            env,
        )

    def _handle_submit_result(self, env: Any, write: Callable[[bytes], None]) -> None:
        body = env.body
        try:
            result = self._store.submit_result(
                body["lease_id"],
                env.sender,
                body["output"],
                body["content_hash"],
                body["signature"],
            )
        except DuplicateSubmission:
            # Idempotent replay: answer with the SAME RESULT_ACK the original
            # submission earned, so a worker whose ack was lost still
            # receives its credit event instead of stalling forever on a
            # bare DUPLICATE error (audit finding node.py:566). No state
            # changes happen on this path.
            existing = self._store.get_result_by_lease(body["lease_id"])
            if existing is not None and existing.peer_id == env.sender:
                validation = self._store.get_validation(existing.result_id)
                if validation is not None:
                    credit = None
                    if validation.status is ValidationStatus.VALID:
                        credit = self._credit_row_for(existing.result_id)
                        if credit is None:
                            self._reply_error(write, ErrorCode.INTERNAL, "credit event missing", env)
                            return
                    self._reply(
                        write,
                        MessageType.RESULT_ACK,
                        {
                            "result_id": existing.result_id,
                            "lease_id": existing.lease_id,
                            "task_id": existing.task_id,
                            "validation": (
                                "VALID" if validation.status is ValidationStatus.VALID else "INVALID"
                            ),
                            "reason": validation.reason,
                            "credit": credit,
                        },
                        env,
                    )
                    return
            self._reply_error(write, ErrorCode.DUPLICATE, "a result already exists for this lease", env)
            return
        except LeaseExpired:
            self._reply_error(write, ErrorCode.LEASE_EXPIRED, "lease expired before submission", env)
            return
        except TaskUnavailable as exc:
            self._reply_error(write, ErrorCode.TASK_UNAVAILABLE, str(exc)[:200], env)
            return
        validation = self._store.validate_result(result.result_id, self.identity.node_id)
        if validation.status is ValidationStatus.VALID:
            credit = self._credit_row_for(result.result_id)
            if credit is None:
                # Ledger wrote the event but export cannot see it: do NOT
                # claim credit on the wire; worker mirrors stay empty.
                logger.error("VALID validation %s without a credit event", result.result_id)
                self._reply_error(write, ErrorCode.INTERNAL, "credit event missing", env)
                return
            self._reply(
                write,
                MessageType.RESULT_ACK,
                {
                    "result_id": result.result_id,
                    "lease_id": result.lease_id,
                    "task_id": result.task_id,
                    "validation": "VALID",
                    "reason": validation.reason,
                    "credit": credit,
                },
                env,
            )
            return
        self._reply(
            write,
            MessageType.RESULT_ACK,
            {
                "result_id": result.result_id,
                "lease_id": result.lease_id,
                "task_id": result.task_id,
                "validation": "INVALID",
                "reason": validation.reason,
                "credit": None,
            },
            env,
        )

    def _credit_row_for(self, result_id: str) -> dict[str, Any] | None:
        """The credit event for a result, in export_since row format.

        export_since batches at MAX_SYNC_BATCH (100): paginate to the chain
        head so a credit event beyond the first batch is still found (audit
        finding node.py:614). Bounded by the ledger length.
        """
        after_seq = 0
        while True:
            rows = ledger.export_since(self._store.conn, after_seq)
            if not rows:
                return None
            for row in rows:
                try:
                    metadata = json.loads(row.get("metadata_json") or "{}")
                except ValueError:
                    continue
                if metadata.get("result_id") == result_id:
                    return row
            after_seq = rows[-1]["seq"]

    def _handle_status_request(self, env: Any, write: Callable[[bytes], None]) -> None:
        task_id = env.body["task_id"]
        task = self._store.get_task(task_id)
        if task is None:
            self._reply_error(write, ErrorCode.NOT_FOUND, "task not found", env)
            return
        result_status: str | None = None
        validation_status: str | None = None
        if task.result_id:
            result = self._store.get_result(task.result_id)
            if result is not None:
                result_status = result.status.value
            validation = self._store.get_validation(task.result_id)
            if validation is not None:
                validation_status = validation.status.value
        credits_count = sum(
            1
            for event in ledger.iter_events(self._store.conn)
            if event.task_id == task_id and event.event_type.value == "CREDIT"
        )
        self._reply(
            write,
            MessageType.STATUS_RESPONSE,
            {
                "task_id": task_id,
                "status": task.status.value,
                "result": result_status,
                "validation": validation_status,
                "credits_count": credits_count,
            },
            env,
        )

    # ------------------------------------------------------------ run loop

    def _configure_logging(self) -> None:
        level = getattr(logging, self.config.log_level, logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            handlers=[logging.StreamHandler()],
            force=False,
        )
        logging.getLogger("idle_compute").setLevel(level)
        if not self.config.log_file:
            return
        # The file handler belongs to THIS node and is detached again in
        # shutdown(): attaching it permanently (e.g. via basicConfig) would
        # keep the file open for the process lifetime and, on Windows, locked
        # against deletion long after the node stopped.
        self._detach_log_handler()
        try:
            handler: logging.Handler = logging.FileHandler(self.config.log_file, encoding="utf-8")
        except OSError:
            logger.warning("cannot open log file %s", self.config.log_file)
            return
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger("idle_compute").addHandler(handler)
        self._log_handler = handler

    def _detach_log_handler(self) -> None:
        handler, self._log_handler = self._log_handler, None
        if handler is None:
            return
        try:
            logging.getLogger("idle_compute").removeHandler(handler)
        finally:
            handler.close()

    def _ensure_transport_started(self) -> None:
        if not self._started:
            self.transport.start()
            self._started = True
            addrs = self.transport.advertised_addrs() or [
                f"{self.config.listen_host}:{self.config.listen_port}"
            ]
            write_rendezvous_file(
                self._run_dir / f"{self.identity.node_id}.addr",
                self.identity.node_id,
                addrs[0],
            )
            logger.info(
                "node started role=%s node_id=%s addr=%s",
                self.config.role,
                self.identity.node_id,
                addrs[0],
            )

    def run(self) -> int:
        """Run the configured role until SIGINT/stop; returns the exit code."""
        self._configure_logging()
        self._loop_thread: threading.Thread | None = threading.current_thread()
        try:
            self._ensure_transport_started()
            if self.config.role == "coordinator":
                self._coordinator_loop()
            elif self.config.role == "worker":
                self._worker_loop()
            else:
                while not self._stopping.is_set():
                    self._stopping.wait(1.0)
        except KeyboardInterrupt:
            pass
        except Exception:
            logger.exception("node run failed")
            self.shutdown()
            return 1
        self.shutdown()
        logger.info("node stopped")
        return 0

    def _coordinator_loop(self) -> None:
        # Opened on THIS thread: the Store connection is single-threaded
        # (actor model) and sqlite3 requires the creating thread.
        self._store = Store(self._db_path, signer=self._policy)
        try:
            while not self._stopping.is_set():
                try:
                    self._store.expire_leases()
                except Exception:
                    logger.exception("lease expiry failed")
                try:
                    env, write = self._work.get(timeout=self.config.poll_interval_seconds)
                except queue.Empty:
                    continue
                self._handle_coordinator(env, write)
        finally:
            # Close on THIS thread. sqlite3 connections are thread-affine, so
            # closing from Node.shutdown()'s thread raises ProgrammingError;
            # swallowing that left the DB file locked after a clean shutdown
            # (found by the E-1 worker-mirror test teardown).
            store, self._store = self._store, None
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.exception("closing the coordinator store failed")

    def _worker_loop(self) -> None:
        # Opened on THIS thread (see _coordinator_loop). The ledger's
        # verify_and_append indexes rows by column name, so the mirror
        # connection needs sqlite3.Row rows.
        self._mirror = sqlite3.connect(
            str(self._data_dir / "ledger_copy.db"), timeout=10
        )
        self._mirror.row_factory = sqlite3.Row
        ledger.ensure_ledger_copy(self._mirror)
        # Every worker start re-verifies the mirrored chain from seq 0 before
        # trusting it (resumable across rounds), so an in-place edit of an
        # already-mirrored event is detected rather than skipped.
        self._audit_cursor = 0
        coordinator = self.config.coordinator_node_id
        if not coordinator or not self.config.coordinator_addr:
            raise NodeError("worker requires coordinator_node_id and coordinator_addr")
        backoff = self.config.backoff_min
        try:
            while not self._stopping.is_set():
                try:
                    self._ensure_connected()
                    self._send_to(coordinator, MessageType.PING, {"nonce": new_id("png_")})
                    self._sync_ledger(coordinator)
                    if not self._ledger_conflict:
                        try:
                            self._claim_cycle(coordinator)
                        except NodeTimeoutError:
                            pass  # nothing claimable this round
                    self._stopping.wait(self.config.poll_interval_seconds)
                    # Reset only after a FULL healthy cycle: dial-then-fail
                    # loops must keep doubling, not reconnect at backoff_min
                    # forever (audit finding node.py:744).
                    backoff = self.config.backoff_min
                except (TransportError, NodeError) as exc:
                    logger.warning("worker link failed: %s", exc)
                    self._connected = False
                    self._stopping.wait(backoff)
                    backoff = min(backoff * 2, self.config.backoff_max)
        finally:
            # Same thread-affinity rule as the coordinator store above: the
            # mirror connection must be closed by the thread that opened it.
            mirror, self._mirror = self._mirror, None
            if mirror is not None:
                try:
                    mirror.close()
                except Exception:
                    logger.exception("closing the mirror connection failed")

    def _mirror_head_seq(self) -> int:
        row = self._mirror.execute(
            "SELECT COALESCE(MAX(seq), 0) AS h FROM ledger_copy"
        ).fetchone()
        return int(row[0])

    def _sync_ledger(self, coordinator: str) -> None:
        """Pull LEDGER_SYNC pages until the mirror reaches the chain head.

        The first round after a worker start begins at seq 0 (``_audit_cursor``):
        every already-mirrored event is re-verified and compared with the
        coordinator copy, so an in-place edit of an old event is a conflict
        instead of an invisible change. Later rounds start at the mirror head.

        Every event is fully verified before it is appended
        (ledger.verify_and_append: shape -> scheme -> duplicate -> seq
        continuity -> prev_hash linkage -> hash -> signature -> amount).
        Any failure latches ``self._ledger_conflict`` and stops this round
        (E-2 policy): the worker never skips a bad event, never overwrites
        a mirror row, and stops claiming while the chains diverge.
        """
        after = self._audit_cursor if self._audit_cursor is not None else self._mirror_head_seq()
        for _ in range(MAX_SYNC_PAGES_PER_ROUND):
            seq = self._send_to(
                coordinator,
                MessageType.LEDGER_SYNC_REQUEST,
                {"after_seq": after},
            )
            try:
                env = self._await(
                    lambda e: e.type is MessageType.LEDGER_SYNC_RESPONSE,
                    LEDGER_SYNC_TIMEOUT,
                    seq,
                )
            except NodeTimeoutError:
                logger.warning("ledger sync timed out; retrying next round")
                return
            except NodeRemoteError as exc:
                logger.warning("ledger sync refused: %s", exc)
                return
            body = env.body
            head = int(body["head_seq"])
            for event in body["events"]:
                ok, reason = ledger.verify_and_append(self._mirror, event, self._policy)
                if not ok:
                    self._latch_conflict(reason, event.get("seq"))
                    return
            if body["events"]:
                after = int(body["events"][-1]["seq"])
            if self._audit_cursor is not None:
                # Startup re-audit in progress: persist the cursor so a round
                # that hits MAX_SYNC_PAGES_PER_ROUND resumes where it stopped
                # (the mirror head would skip the un-audited middle).
                self._audit_cursor = after
                if not body["more"]:
                    self._audit_cursor = None
            if head < after:
                # The coordinator chain is SHORTER than the mirror: the
                # coordinator lost its ledger (or was replaced). Never
                # silently truncate the mirror -- stop and wait for the
                # operator to rebuild (E-2 policy).
                self._latch_conflict(f"coordinator head {head} < mirror head {after}", head + 1)
                return
            if not body["more"]:
                break
        # A clean round clears any earlier latch (the persisted copy too), so a
        # divergence fixed upstream lets the worker resume claiming next cycle.
        self._clear_conflict()

    def _latch_conflict(self, reason: str, seq: int | None = None) -> None:
        """Record a ledger divergence (memory + mirror meta) and stop claiming.

        The persisted copy is what ``worker ledger status`` reports and what
        survives a restart. It is advisory only: every sync round re-verifies
        the chain, so a stale record can never mask a real divergence.
        """
        self._ledger_conflict = str(reason)
        try:
            ledger.record_conflict(self._mirror, self._ledger_conflict, seq)
        except Exception:
            logger.exception("could not persist the ledger conflict state")
        logger.error(
            "ledger conflict (seq=%s): %s [worker stops claiming until it clears]",
            seq,
            self._ledger_conflict,
        )

    def _clear_conflict(self) -> None:
        self._ledger_conflict = None
        try:
            ledger.clear_conflict(self._mirror)
        except Exception:
            logger.exception("could not clear the persisted ledger conflict state")

    def _ensure_connected(self) -> None:
        if self._connected:
            return
        coordinator = self.config.coordinator_node_id
        self.transport.dial(coordinator, [self.config.coordinator_addr])
        addrs = self.transport.advertised_addrs() or [
            f"{self.config.listen_host}:{self.config.listen_port}"
        ]
        seq = self._send_to(
            coordinator,
            MessageType.HELLO,
            {"node_id": self.identity.node_id, "role": "worker", "listen_addrs": addrs[:1]},
        )
        self._await(
            lambda e: e.type is MessageType.HELLO_ACK and e.sender == coordinator,
            HELLO_ACK_TIMEOUT,
            seq,
        )
        self._connected = True
        logger.info("connected to coordinator %s", coordinator[:12])

    def _await(self, predicate: Callable[[Any], bool], timeout: float, sent_seq: int | None) -> Any:
        def match(env: Any) -> bool:
            # A reply is only accepted for the message we actually sent:
            # stale acks from an earlier timed-out handshake must never
            # satisfy a later wait (audit findings node.py:770/771).
            if sent_seq is not None and env.in_reply_to != sent_seq:
                return False
            if env.type is MessageType.ERROR:
                return True
            return predicate(env)

        env = self._replies.wait(match, timeout)
        if env.type is MessageType.ERROR:
            if env.body.get("code") == ErrorCode.SEQ_OUT_OF_WINDOW.value:
                # Self-heal a rolled-back seq.json: jump past the peer's
                # last_seen (it is in the error message) so the retried
                # handshake is accepted instead of wedging forever.
                self._recover_seq_from(env.body.get("message", ""))
            raise NodeRemoteError(env.body.get("message", "remote error"))
        return env

    def _recover_seq_from(self, message: str) -> None:
        match = re.search(r"last_seen (\d+)", message)
        if match is None:
            return
        with self._seq_lock:
            self._send_seq = max(self._send_seq, int(match.group(1)))
            self._persist_seq()

    def _claim_cycle(self, coordinator: str) -> None:
        claim_request_id = new_id("crq_")
        seq = self._send_to(
            coordinator,
            MessageType.CLAIM_REQUEST,
            {"claim_request_id": claim_request_id, "task_id": None},
        )
        claim_wait = float(self.config.poll_interval_seconds) * 2 + 2
        try:
            env = self._await(
                lambda e: e.type in (MessageType.CLAIM_GRANT, MessageType.CLAIM_DENIED)
                and e.body["claim_request_id"] == claim_request_id,
                claim_wait,
                seq,
            )
        except NodeTimeoutError:
            # A grant that arrives while the worker was busy executing the
            # previous task is not lost work: drain it once before the next
            # claim cycle, so its lease (still ACTIVE until expiry) is used
            # instead of burning a task attempt on an abandoned grant.
            try:
                env = self._await(
                    lambda e: e.type in (MessageType.CLAIM_GRANT, MessageType.CLAIM_DENIED)
                    and e.body["claim_request_id"] == claim_request_id,
                    0.0,
                    seq,
                )
            except NodeTimeoutError:
                return
        if env.type is MessageType.CLAIM_DENIED:
            return
        lease_body = env.body["lease"]
        task_body = env.body["task"]
        task = Task(
            task_id=lease_body["task_id"],
            task_type=task_body["task_type"],
            payload=task_body["payload"],
        )
        lease = Lease(
            lease_id=lease_body["lease_id"],
            task_id=lease_body["task_id"],
            peer_id=lease_body["peer_id"],
            status=LeaseStatus.ACTIVE,
            expires_at=lease_body["expires_at"],
        )
        try:
            output, digest, signature = Worker(self.identity.node_id, self._policy).execute(task, lease)
        except InvalidTaskInput as exc:
            logger.warning("refusing task %s: %s", task.task_id, exc)
            return
        seq = self._send_to(
            coordinator,
            MessageType.SUBMIT_RESULT,
            {
                "lease_id": lease.lease_id,
                "output": output,
                "content_hash": digest,
                "signature": signature,
            },
        )
        ack = self._await(
            lambda e: e.type is MessageType.RESULT_ACK and e.body["lease_id"] == lease.lease_id,
            SUBMIT_ACK_TIMEOUT,
            seq,
        )
        if ack.body["validation"] == "VALID":
            credit = ack.body["credit"]
            if credit is not None:
                ok, reason = ledger.verify_and_append(self._mirror, credit, self._policy)
                if ok:
                    logger.info("task %s validated; credit mirrored", lease.task_id)
                else:
                    logger.error("credit event rejected by mirror: %s", reason)
            else:
                logger.error("VALID ack without credit event")
        else:
            logger.warning("result INVALID: %s", ack.body.get("reason"))

    # --------------------------------------------------------- client ops

    def _client_ensure_connected(self, timeout: float) -> None:
        self._configure_logging()
        self._ensure_transport_started()
        if self._connected:
            return
        coordinator = self.config.coordinator_node_id
        if not coordinator or not self.config.coordinator_addr:
            raise NodeError("client requires coordinator_node_id and coordinator_addr")
        self.transport.dial(coordinator, [self.config.coordinator_addr])
        addrs = self.transport.advertised_addrs() or [
            f"{self.config.listen_host}:{self.config.listen_port}"
        ]
        seq = self._send_to(
            coordinator,
            MessageType.HELLO,
            {"node_id": self.identity.node_id, "role": "client", "listen_addrs": addrs[:1]},
        )
        self._await(
            lambda e: e.type is MessageType.HELLO_ACK and e.sender == coordinator,
            HELLO_ACK_TIMEOUT,
            seq,
        )
        self._connected = True

    def client_create_task(
        self,
        payload: dict[str, Any],
        max_attempts: int = 3,
        lease_seconds: int = 30,
        timeout: float = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        self._client_ensure_connected(timeout)
        client_ref = new_id("clr_")
        seq = self._send_to(
            self.config.coordinator_node_id,
            MessageType.TASK_CREATE,
            {
                "client_ref": client_ref,
                "task_type": "numeric_batch_v1",
                "payload": payload,
                "max_attempts": max_attempts,
                "lease_seconds": lease_seconds,
            },
        )
        ack = self._await(
            lambda e: e.type is MessageType.TASK_CREATE_ACK and e.body["client_ref"] == client_ref,
            timeout,
            seq,
        )
        return ack.body

    def client_status(self, task_id: str, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        self._client_ensure_connected(timeout)
        seq = self._send_to(
            self.config.coordinator_node_id,
            MessageType.STATUS_REQUEST,
            {"task_id": task_id},
        )
        env = self._await(
            lambda e: e.type is MessageType.STATUS_RESPONSE and e.body["task_id"] == task_id,
            timeout,
            seq,
        )
        return env.body

    def client_ping(self, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        started = time.monotonic()
        self._client_ensure_connected(timeout)
        nonce = new_id("png_")
        seq = self._send_to(self.config.coordinator_node_id, MessageType.PING, {"nonce": nonce})
        env = self._await(
            lambda e: e.type is MessageType.PONG and e.body["nonce"] == nonce,
            timeout,
            seq,
        )
        return {"pong": True, "rtt_ms": round((time.monotonic() - started) * 1000, 1)}

    # ----------------------------------------------------------- shutdown

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stopping.set()
        try:
            self.transport.stop()
        except Exception as exc:
            logger.debug("transport stop failed: %s", exc)
        # Drain the role loop BEFORE closing anything else. The role loops
        # close their own sqlite connections in a ``finally`` block: those
        # connections are thread-affine, so closing them from here raises
        # ProgrammingError and, if swallowed, would leave the DB file locked
        # after a clean shutdown.
        loop_thread = getattr(self, "_loop_thread", None)
        if loop_thread is not None and loop_thread is not threading.current_thread():
            loop_thread.join(timeout=self.config.poll_interval_seconds + 1.0)
        # ``_aux`` is the one connection opened on the constructing thread
        # (check_same_thread=False + a lock), so it is safe to close here.
        try:
            if self._aux is not None:
                self._aux.close()
        except Exception as exc:
            logger.debug("closing the aux connection failed: %s", exc)
        # Detach the per-node file handler last: after this the log file is
        # closed and (on Windows) no longer locked against deletion.
        self._detach_log_handler()
