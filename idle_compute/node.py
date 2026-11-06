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
from dataclasses import replace
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import ledger
from .capability import CapabilityError, CapabilityManifest, CapabilityRevocation
from .config import NodeConfig, ValidationError, is_valid_node_id
from .db import (
    DuplicateSubmission,
    InsufficientCredit,
    InvalidNegotiationState,
    LeaseExpired,
    NegotiationConflict,
    Store,
    StoreError,
    TaskUnavailable,
)
from .discovery import (
    ACCEPTED,
    AnnouncementCache,
    DiscoveryStateStore,
    build_announcement,
    build_manifest,
)
from .identity import NodeIdentity
from .crypto import canonical_json, content_hash
from .lease import LeaseCommit, LeaseError, TaskLeaseTerms, authorization_from_commit, terms_hash
from .log import install_redaction
from .registry import LocalRegistryAdapter, RegistryError
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
from .tasks import InvalidTaskInput, get_task_type
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
        MessageType.LEASE_ACCEPT,
        MessageType.LEASE_REJECT,
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
        MessageType.LEASE_PROPOSE,
        MessageType.LEASE_COMMIT,
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
                self.identity.node_id, config.listen_host, config.listen_port, tls=config.tls
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
        # D2 discovery: the cache (and its sqlite state) is created lazily so
        # constructing a Node for validation does not touch the filesystem.
        # D4 final: a Node ALWAYS runs the dual-signed lease path. This guard
        # catches a hand-built NodeConfig that bypassed from_dict(), so no
        # constructor path can enable unsigned execution.
        if self.config.lease.mode != "dual_signed":
            raise ValidationError(
                "lease.mode must be 'dual_signed': the legacy coordinator-grant "
                "execution path is not available to network nodes"
            )
        # D4 discovery/capability state...
        self._discovery: AnnouncementCache | None = None
        self._discovery_store: DiscoveryStateStore | None = None
        self._discovery_lock = threading.Lock()
        self._registry: LocalRegistryAdapter | None = None
        self._publish_sequence = 0
        self._last_publish_at = 0.0
        # D4: leases the coordinator cancelled while this worker was negotiating.
        self._cancelled_leases: set[str] = set()

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
                    local_node_id=self.identity.node_id,
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
                    logger.warning(
                        "connection rejected peer=%s reason=not in allowlist",
                        env.sender[:12],
                    )
                    self._reply_error(write, ErrorCode.UNAUTHORIZED, "sender not in allowlist", env)
                    return
                if self._ratelimit.blocked(env.sender):
                    # The node id is quarantined: refuse the handshake so a TCP
                    # reconnect cannot reset the bucket.
                    self._reply_error(write, ErrorCode.OVERLOAD, "peer is rate limited", env)
                    return
                bound = env.sender
                logger.info(
                    "connection established peer=%s transport=%s",
                    env.sender[:12],
                    transport_peer[:16],
                )
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
            try:
                if not self._on_envelope(env, write):
                    return
            except Exception:
                # A bug in one handler must not kill the stream silently: log
                # it, tell the peer, keep the node serving other streams.
                logger.exception("envelope handler failed for %s", env.type.value)
                self._reply_error(write, ErrorCode.INTERNAL, "internal error", env)
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

    # ------------------------------------------------------- discovery (D2)

    def _registry_adapter(self) -> LocalRegistryAdapter:
        """Lazily open the local simulated registry (D5, same data_dir)."""
        adapter = self._registry
        if adapter is not None:
            return adapter
        with self._discovery_lock:
            if self._registry is None:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                self._registry = LocalRegistryAdapter(self._data_dir / "registry.db")
            return self._registry

    def _register_manifest(self, manifest: CapabilityManifest) -> None:
        """Persist a signed manifest commitment; registry failure is logged, not fatal."""
        try:
            self._registry_adapter().register(manifest, now=int(time.time()))
        except (RegistryError, CapabilityError) as exc:
            logger.warning("registry write failed for %s: %s", manifest.node_id[:12], exc)

    def _register_own_capability(self) -> None:
        template = self.config.capability
        if not template:
            return
        try:
            manifest = build_manifest(template, self.identity, now=int(time.time()))
            self._register_manifest(manifest)
        except (CapabilityError, ValueError) as exc:
            logger.warning("own capability registration failed: %s", exc)

    def revoke_capability(self, capability_version: int, reason: str = "revoked") -> None:
        """Revoke this node's own capability version in the cache AND registry."""
        revocation = CapabilityRevocation(
            node_id=self.identity.node_id,
            capability_version=int(capability_version),
            issued_at=int(time.time()),
            reason=reason,
        ).sign(self.identity)
        cache = self._discovery_cache()
        with self._discovery_lock:
            cache.revoke(revocation, now=time.time())
        self._registry_adapter().revoke(revocation, now=int(time.time()))

    def match_candidates(self, requirement: Any, *, now: int | None = None) -> Any:
        """D5: registry-first matching over the in-memory announcement cache."""
        from .matcher import Candidate, match as match_capabilities

        moment = int(time.time()) if now is None else int(now)
        cache = self._discovery_cache()
        with self._discovery_lock:
            entries = cache.lookup(include_unreachable=True, now=moment)
        candidates = [Candidate.from_cache_entry(entry) for entry in entries]
        return match_capabilities(
            requirement, candidates, now=moment, registry=self._registry_adapter()
        )

    def _discovery_cache(self) -> AnnouncementCache:
        """Lazily open the announcement cache + its persisted state file."""
        cache = self._discovery
        if cache is not None:
            return cache
        with self._discovery_lock:
            if self._discovery is not None:
                return self._discovery
            self._data_dir.mkdir(parents=True, exist_ok=True)
            cfg = self.config.discovery
            self._discovery_store = DiscoveryStateStore(self._data_dir / "discovery.db")
            self._discovery = AnnouncementCache(
                max_entries=cfg.max_entries,
                max_ttl_seconds=cfg.max_announcement_ttl_seconds,
                offline_after_seconds=cfg.offline_after_seconds,
                state_store=self._discovery_store,
            )
            return self._discovery

    def _handle_discovery(self, env: Any, write: Callable[[bytes], None]) -> bool:
        cache = self._discovery_cache()
        now = time.time()
        if env.type is MessageType.CAPABILITY_ANNOUNCE:
            with self._discovery_lock:
                result = cache.ingest(
                    env.body["manifest"],
                    env.body["announcement"],
                    now=now,
                    sender_node_id=env.sender,
                )
            if result.accepted:
                try:
                    self._register_manifest(CapabilityManifest.from_dict(env.body["manifest"]))
                except CapabilityError as exc:
                    logger.warning("peer manifest registration failed: %s", exc)
                logger.debug("capability announcement accepted from %s", env.sender[:12])
            else:
                logger.info(
                    "capability announcement rejected from %s: %s (%s)",
                    env.sender[:12],
                    result.code,
                    result.detail,
                )
            return True
        if env.type is MessageType.CAPABILITY_QUERY:
            with self._discovery_lock:
                peers = cache.lookup(
                    task_type=env.body["task_type"],
                    runtime=env.body["runtime"],
                    now=now,
                )
                payload = [
                    {"manifest": peer.manifest.to_dict(), "announcement": peer.announcement.to_dict()}
                    for peer in peers
                ]
            logger.debug(
                "capability query from %s matched %d peers", env.sender[:12], len(payload)
            )
            self._reply(
                write,
                MessageType.CAPABILITY_RESPONSE,
                {"query_seq": env.seq, "peers": payload},
                env,
            )
            return True
        if env.type is MessageType.CAPABILITY_RESPONSE:
            accepted = 0
            accepted_manifests: list[dict[str, Any]] = []
            with self._discovery_lock:
                for entry in env.body["peers"]:
                    if not isinstance(entry, dict):
                        continue
                    result = cache.ingest(
                        entry.get("manifest"), entry.get("announcement"), now=now
                    )
                    if result.accepted:
                        accepted += 1
                        accepted_manifests.append(entry["manifest"])
            for raw_manifest in accepted_manifests:
                try:
                    self._register_manifest(CapabilityManifest.from_dict(raw_manifest))
                except CapabilityError as exc:
                    logger.warning("peer manifest registration failed: %s", exc)
            self._replies.put(env)  # let a waiting query_capabilities() see it
            logger.debug(
                "capability response from %s: %d/%d entries accepted",
                env.sender[:12],
                accepted,
                len(env.body["peers"]),
            )
            return True
        return False

    def publish_announcement(self) -> dict[str, Any]:
        """Build+sign this node's announcement and broadcast it (best effort).

        The node's own sequence is persisted before sending, so a restart
        continues monotonically instead of being rejected as a rollback.
        """
        template = self.config.capability
        if not template:
            raise NodeError("config has no 'capability' section to announce")
        now = int(time.time())
        manifest = build_manifest(template, self.identity, now=now)
        # D5: publish registers the signed commitment locally (registry-first
        # matching looks here before the in-memory announcement cache).
        self._register_manifest(manifest)
        cache = self._discovery_cache()
        with self._discovery_lock:
            if self._publish_sequence == 0 and self._discovery_store is not None:
                self._publish_sequence = self._discovery_store.last_sequence(self.identity.node_id)
            self._publish_sequence += 1
            sequence = self._publish_sequence
            ttl = min(60, self.config.discovery.max_announcement_ttl_seconds)
            announcement = build_announcement(
                manifest, template, self.identity, now=now, sequence=sequence, ttl_seconds=ttl
            )
            if self._discovery_store is not None:
                try:
                    self._discovery_store.record(
                        self.identity.node_id,
                        sequence,
                        manifest.capability_version,
                        manifest.hash(),
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    )
                except sqlite3.Error as exc:
                    raise NodeError(f"cannot persist own announcement sequence: {exc}") from exc
        body = {"manifest": manifest.to_dict(), "announcement": announcement.to_dict()}
        sent = failed = 0
        for peer in self.config.peers:
            try:
                self._send_to(peer, MessageType.CAPABILITY_ANNOUNCE, body)
                sent += 1
            except TransportError as exc:
                failed += 1
                logger.debug("announcement to %s failed: %s", peer[:12], exc)
        self._last_publish_at = time.time()
        return {"sequence": sequence, "sent": sent, "failed": failed}

    def query_capabilities(
        self,
        peer_id: str,
        *,
        task_type: str | None = None,
        runtime: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Ask ``peer_id`` for its cached capabilities (directed request/reply)."""
        timeout = float(self.config.discovery.query_timeout_seconds if timeout is None else timeout)
        if not self._connected:
            # A client Node has not dialed yet; workers are already connected.
            self._client_ensure_connected(timeout)
        seq = self._send_to(
            peer_id,
            MessageType.CAPABILITY_QUERY,
            {"task_type": task_type, "runtime": runtime},
        )
        env = self._await(
            lambda e: e.type is MessageType.CAPABILITY_RESPONSE and e.body.get("query_seq") == seq,
            timeout,
            seq,
        )
        return list(env.body["peers"])

    def _discovery_tick(self) -> None:
        """Sweep expiry/liveness and publish our announcement when due."""
        cfg = self.config.discovery
        now = time.time()
        cache = self._discovery_cache()
        with self._discovery_lock:
            swept = cache.sweep(now)
        if swept["removed"] or swept["unreachable"]:
            logger.debug("discovery sweep: %s", swept)
        if self.config.capability and (now - self._last_publish_at) >= cfg.announce_interval_seconds:
            try:
                stats = self.publish_announcement()
                logger.info(
                    "published capability announcement seq=%s sent=%s failed=%s",
                    stats["sequence"],
                    stats["sent"],
                    stats["failed"],
                )
            except (NodeError, CapabilityError, TransportError) as exc:
                logger.warning("capability announcement failed: %s", exc)

    def _on_envelope(self, env: Any, write: Callable[[bytes], None]) -> bool:
        """Dispatch an authenticated envelope; False closes the stream."""
        # Liveness: any authenticated frame from a cached peer proves it is
        # reachable right now (valid announcement != online).
        if self._discovery is not None and env.sender:
            with self._discovery_lock:
                self._discovery.mark_alive(env.sender)
        if env.type in (
            MessageType.CAPABILITY_ANNOUNCE,
            MessageType.CAPABILITY_QUERY,
            MessageType.CAPABILITY_RESPONSE,
        ):
            return self._handle_discovery(env, write)
        if env.type is MessageType.LEASE_CANCEL:
            # A cancelled lease must never be executed by this worker, even if
            # the commit already arrived.
            self._cancelled_leases.add(env.body["lease_id"])
            logger.info("lease %s cancelled by the coordinator", env.body["lease_id"])
            return True
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
            elif env.type is MessageType.LEASE_ACCEPT:
                self._handle_lease_accept(env, write)
            elif env.type is MessageType.LEASE_REJECT:
                self._handle_lease_reject(env, write)
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
        created = row is None
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
                logger.info("task create rejected requester=%s reason=pool_full", env.sender[:12])
                self._reply_error(write, ErrorCode.OVERLOAD, "task pool is full", env)
                return
            try:
                task = self._store.create_task_paid(
                    env.body["task_type"],
                    env.body["payload"],
                    requester_id=env.sender,
                    cost=self.config.credits.task_cost,
                    max_attempts=env.body["max_attempts"],
                    lease_seconds=env.body["lease_seconds"],
                )
            except InsufficientCredit as exc:
                logger.info(
                    "task create rejected requester=%s reason=insufficient_credit cost=%s available=%s",
                    env.sender[:12],
                    exc.cost,
                    exc.available,
                )
                self._reply_error(
                    write,
                    ErrorCode.INSUFFICIENT_CREDIT,
                    f"INSUFFICIENT_CREDIT: cost={exc.cost} available={exc.available}",
                    env,
                )
                return
            except (ValueError, InvalidTaskInput) as exc:
                self._reply_error(write, ErrorCode.INVALID_BODY, str(exc)[:200], env)
                return
            task_id = task.task_id
            self._aux_execute(
                "INSERT OR IGNORE INTO client_refs(client_ref,task_id,created_at) VALUES (?,?,?)",
                (client_ref, task_id, utc_now()),
            )
        if created:
            logger.info(
                "task %s CREATED requester=%s status=QUEUED",
                task_id,
                env.sender[:12],
            )
        else:
            logger.info("task %s CREATE replay (idempotent)", task_id)
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
        # D4: CLAIM_GRANT stays as a compatibility notification; EXECUTION of
        # this task requires the dual-signed lease negotiated below.
        logger.info(
            "task %s LEASED lease=%s worker=%s",
            task.task_id,
            lease.lease_id,
            env.sender[:12],
        )
        if self.config.lease.mode == "dual_signed":
            try:
                self._propose_lease(lease, task, env, write)
            except (StoreError, ValueError) as exc:
                logger.warning("lease proposal failed for %s: %s", lease.lease_id, exc)
                self._reply_error(write, ErrorCode.REJECTED, "lease proposal failed", env)

    # ------------------------------------------------------ D4 lease flow

    def _propose_lease(
        self, lease: Lease, task: Task, env: Any, write: Callable[[bytes], None]
    ) -> None:
        """Sign the terms for an already-claimed lease and send LEASE_PROPOSE."""
        existing = self._store.get_negotiation(lease.lease_id)
        if existing is not None:
            terms = TaskLeaseTerms.from_dict(json.loads(existing["terms_json"]))
        else:
            now = int(time.time())
            try:
                expires = int(datetime.fromisoformat(lease.expires_at).timestamp())
            except ValueError:
                expires = now + 30
            spec = content_hash({"task_type": task.task_type, "payload": task.payload})
            terms = TaskLeaseTerms(
                lease_id=lease.lease_id,
                task_id=lease.task_id,
                requester_node_id=self.identity.node_id,
                worker_node_id=lease.peer_id,
                task_spec_hash=spec,
                input_manifest_hash="",
                lease_version=1,
                issued_at=now,
                expires_at=expires,
                execution_deadline=expires,
                verification_policy={"mode": "RECOMPUTE", "required": False},
                reward_policy={"amount": 1},
            ).sign_proposal(self.identity)
            self._store.create_negotiation_for_lease(
                lease_id=lease.lease_id,
                task_id=lease.task_id,
                requester_id=self.identity.node_id,
                worker_id=lease.peer_id,
                authority_id=self.identity.node_id,
                terms_json=canonical_json(terms.to_dict()),
                terms_hash=terms_hash(terms),
                requester_signature=terms.requester_signature,
                expires_at=lease.expires_at,
            )
        self._reply(
            write,
            MessageType.LEASE_PROPOSE,
            {
                "lease_id": lease.lease_id,
                "task_id": lease.task_id,
                "terms": terms.to_dict(),
                "task": {"task_type": task.task_type, "payload": task.payload},
            },
            env,
        )

    def _handle_lease_accept(self, env: Any, write: Callable[[bytes], None]) -> None:
        body = env.body
        lease_id = body["lease_id"]
        row = self._store.get_negotiation(lease_id)
        if row is None or row["worker_id"] != env.sender:
            self._reply_error(write, ErrorCode.UNAUTHORIZED, "no such lease for this worker", env)
            return
        try:
            terms = TaskLeaseTerms.from_dict(json.loads(row["terms_json"]))
            acceptance = body["acceptance"]
            if body["terms_hash"] != row["terms_hash"] or terms_hash(terms) != row["terms_hash"]:
                raise LeaseError(LeaseError.SCHEMA, "terms hash mismatch")
            accepted = replace(terms, worker_signature=acceptance.get("signature", ""))
            accepted.verify_accept(int(time.time()))
            self._store.accept_negotiation(
                lease_id, env.sender, acceptance["signature"], canonical_json(acceptance)
            )
            self._store.activate_negotiation(lease_id)
            commit = LeaseCommit(
                lease_id=terms.lease_id,
                task_id=terms.task_id,
                requester_node_id=terms.requester_node_id,
                worker_node_id=terms.worker_node_id,
                terms_hash=row["terms_hash"],
                committed_at=int(time.time()),
                expires_at=terms.expires_at,
            ).sign(self.identity)
            self._reply(
                write,
                MessageType.LEASE_COMMIT,
                {
                    "lease_id": lease_id,
                    "terms_hash": row["terms_hash"],
                    "commit": commit.to_dict(),
                },
                env,
            )
        except (LeaseError, StoreError, ValueError, KeyError) as exc:
            logger.warning("LEASE_ACCEPT rejected for %s: %s", lease_id, exc)
            self._reply_error(write, ErrorCode.REJECTED, f"lease not activated: {exc}"[:200], env)

    def _handle_lease_reject(self, env: Any, write: Callable[[bytes], None]) -> None:
        lease_id = env.body["lease_id"]
        try:
            self._store.reject_negotiation(lease_id, env.body.get("reason") or "worker rejected")
        except (StoreError, InvalidNegotiationState) as exc:
            logger.info("LEASE_REJECT for %s could not be applied: %s", lease_id, exc)
            return
        logger.info("worker %s rejected lease %s", env.sender[:12], lease_id)

    def cancel_lease(self, lease_id: str, reason: str = "cancelled") -> None:
        """Coordinator-side cancellation; cancels the negotiation and notifies."""
        row = self._store.get_negotiation(lease_id)
        self._store.cancel_negotiation(lease_id, reason)
        if row is not None:
            try:
                self._send_to(
                    row["worker_id"], MessageType.LEASE_CANCEL, {"lease_id": lease_id, "reason": reason}
                )
            except TransportError as exc:
                logger.debug("LEASE_CANCEL to %s failed: %s", row["worker_id"][:12], exc)

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
            logger.info(
                "task %s COMPLETED validation=VALID credit=1",
                result.task_id,
            )
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
        logger.info(
            "task %s REJECTED validation=INVALID reason=%s",
            result.task_id,
            str(validation.reason)[:120],
        )
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
        # F-4: handler-level redaction covers every idle_compute.* record
        # (a logger's own filters are not re-applied during propagation).
        redaction = install_redaction(handlers=logging.getLogger().handlers)
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
        handler.addFilter(redaction)
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
                "node started role=%s node_id=%s listen=%s tls=%s",
                self.config.role,
                self.identity.node_id,
                addrs[0],
                "on" if self.config.tls is not None else "off",
            )

    def run(self) -> int:
        """Run the configured role until SIGINT/stop; returns the exit code."""
        self._configure_logging()
        self._loop_thread: threading.Thread | None = threading.current_thread()
        try:
            # D5: persist this node's capability commitment at startup (before
            # any announcement) so the registry is authoritative from boot.
            if self.config.capability:
                self._register_own_capability()
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
                    self._discovery_tick()
                except Exception:
                    logger.exception("discovery tick failed")
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
                    try:
                        self._discovery_tick()
                    except Exception:
                        logger.exception("discovery tick failed")
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
        try:
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
        except (TransportError, NodeError) as exc:
            logger.warning(
                "connection to coordinator %s at %s failed: %s",
                str(coordinator)[:12],
                self.config.coordinator_addr,
                exc,
            )
            raise
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
        # D4 final: the ONLY path from a claim to execution is the dual-signed
        # negotiation. The historic CLAIM_GRANT body below is unreachable
        # (Node.__init__ rejects any non-dual mode); it is kept only until the
        # Worker legacy API is removed, and it can never authorize execution.
        self._claim_cycle_dual(coordinator)
        return
        claim_request_id = new_id("crq_")  # noqa: unreachable legacy body
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
        self._execute_and_submit(task, lease)

    def _execute_and_submit(
        self,
        task: Task,
        lease: Lease,
        *,
        authorization: Any = None,
        require_dual_signed: bool = False,
    ) -> None:
        coordinator = self.config.coordinator_node_id
        if coordinator is None:
            logger.error("worker has no coordinator configured")
            return
        try:
            output, digest, signature = Worker(self.identity.node_id, self._policy).execute(
                task,
                lease,
                authorization=authorization,
                require_dual_signed=require_dual_signed,
            )
        except (InvalidTaskInput, LeaseError) as exc:
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

    def _may_execute(self, lease_id: str) -> bool:
        """D4 §8.2: a lease cancelled before execution must never run.

        The worker only learns about cancellation through LEASE_CANCEL; the
        check is applied after the commit is verified and immediately before
        the single execution entry, so a cancel that arrives first wins.
        """
        return lease_id not in self._cancelled_leases

    def _claim_cycle_dual(self, coordinator: str) -> None:
        """D4: CLAIM_REQUEST -> LEASE_PROPOSE -> ACCEPT -> COMMIT -> execute."""
        claim_request_id = new_id("crq_")
        seq = self._send_to(
            coordinator,
            MessageType.CLAIM_REQUEST,
            {"claim_request_id": claim_request_id, "task_id": None},
        )
        timeout = float(self.config.lease.proposal_timeout_seconds)
        try:
            env = self._await(
                lambda e: e.type in (MessageType.LEASE_PROPOSE, MessageType.CLAIM_DENIED),
                timeout,
                seq,
            )
        except NodeTimeoutError:
            return
        if env.type is MessageType.CLAIM_DENIED:
            return
        try:
            terms = TaskLeaseTerms.from_dict(env.body["terms"])
            now = int(time.time())
            terms.verify_for_worker(self.identity.node_id, now)
            if terms.requester_node_id != coordinator:
                logger.warning("lease proposal from a non-coordinator requester")
                return
            if terms.lease_id != env.body["lease_id"] or terms.task_id != env.body["task_id"]:
                logger.warning("lease proposal binding mismatch")
                return
            task_body = env.body["task"]
            get_task_type(task_body["task_type"])  # whitelist enforced before signing
            accepted = terms.sign_accept(self.identity)
            accept_seq = self._send_to(
                coordinator,
                MessageType.LEASE_ACCEPT,
                {
                    "lease_id": terms.lease_id,
                    "terms_hash": terms_hash(terms),
                    "acceptance": {**accepted.acceptance(), "signature": accepted.worker_signature},
                },
            )
            commit_env = self._await(
                lambda e: e.type is MessageType.LEASE_COMMIT and e.body["lease_id"] == terms.lease_id,
                timeout,
                accept_seq,
            )
            commit = LeaseCommit.from_dict(commit_env.body["commit"])
            commit.verify(int(time.time()))
            if (
                commit.terms_hash != terms_hash(terms)
                or commit.worker_node_id != self.identity.node_id
                or commit.requester_node_id != coordinator
                or commit.lease_id != terms.lease_id
            ):
                logger.warning("lease commit does not bind this worker/terms")
                return
        except (LeaseError, InvalidTaskInput, ValueError, KeyError) as exc:
            logger.warning("lease negotiation rejected: %s", exc)
            return
        if not self._may_execute(terms.lease_id):
            logger.warning("lease %s was cancelled before execution", terms.lease_id)
            return
        authorization = authorization_from_commit(commit, accepted, authority_node_id=coordinator)
        task = Task(
            task_id=terms.task_id,
            task_type=task_body["task_type"],
            payload=task_body["payload"],
        )
        lease = Lease(
            lease_id=terms.lease_id,
            task_id=terms.task_id,
            peer_id=self.identity.node_id,
            status=LeaseStatus.ACTIVE,
            expires_at=datetime.fromtimestamp(terms.expires_at, timezone.utc).isoformat(),
        )
        self._execute_and_submit(
            task, lease, authorization=authorization, require_dual_signed=True
        )

    # --------------------------------------------------------- client ops

    def _client_ensure_connected(self, timeout: float) -> None:
        self._configure_logging()
        self._ensure_transport_started()
        if self._connected:
            return
        coordinator = self.config.coordinator_node_id
        if not coordinator or not self.config.coordinator_addr:
            raise NodeError("client requires coordinator_node_id and coordinator_addr")
        try:
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
        except (TransportError, NodeError) as exc:
            logger.warning(
                "connection to coordinator %s at %s failed: %s",
                str(coordinator)[:12],
                self.config.coordinator_addr,
                exc,
            )
            raise
        self._connected = True
        logger.info(
            "connected to coordinator %s at %s",
            coordinator[:12],
            self.config.coordinator_addr,
        )

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
        # D2 discovery state (opened lazily on whichever thread first needed
        # it; the sqlite connection is only touched under _discovery_lock).
        try:
            if self._discovery_store is not None:
                self._discovery_store.close()
        except Exception as exc:
            logger.debug("closing the discovery state failed: %s", exc)
        self._discovery_store = None
        self._discovery = None
        try:
            if self._registry is not None:
                self._registry.close()
        except Exception as exc:
            logger.debug("closing the registry failed: %s", exc)
        self._registry = None
        # Detach the per-node file handler last: after this the log file is
        # closed and (on Windows) no longer locked against deletion.
        self._detach_log_handler()
