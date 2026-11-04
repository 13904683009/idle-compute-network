"""E2E tests for the Phase C node runtime (node.py + CLI).

The core credit-cycle tests are REAL multi-process: the coordinator and
worker run as separate Python interpreter processes
(``python -m idle_compute node run``) over the TCP transport, and every
assertion crosses a real socket between processes. Manual hostile peers
are bare TcpTransport instances driven frame-by-frame against the real
coordinator (real sockets, no Node object on the hostile side). The
libp2p-credit, ping, and coordinator-restart tests run the coordinator
IN-PROCESS (a Node on a thread) — still real transports and real
sockets, but not separate interpreter processes; each such test says so
in its docstring.

Covered invariants (per the Phase C directive):
- happy path: task created by a client process, claimed/executed/submitted
  by a worker process, whitelist-recomputed, credit event on the ledger,
  worker mirrors the coordinator-signed credit (both TCP and libp2p);
- forged result (valid signature over wrong content): INVALID, no credit;
- tampered HELLO signature: BAD_SIGNATURE, stream never binds;
- unauthorized HELLO (not on the allowlist): UNAUTHORIZED, dropped;
- illegal body, garbage frame, oversized frame;
- duplicate CLAIM_REQUEST -> same lease (no second lease);
- duplicate SUBMIT_RESULT -> the original RESULT_ACK for the same peer
  (idempotent, no double credit) and a non-disclosing refusal for a
  foreign peer;
- an ERROR sent to the coordinator is never answered (no echo storm);
- hostile wire 'type' strings still get schema-valid errors (ref_type clamped);
- abandoned lease -> task requeued after expiry;
- worker restart keeps working (persisted seq on both sides);
- coordinator restart -> worker reconnects and completes work;
- clean shutdown returns exit code 0 (same path as SIGINT).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from idle_compute.crypto import content_hash, result_signing_payload
from idle_compute.identity import NodeIdentity
from idle_compute.models import Lease, LeaseStatus, Task
from idle_compute.node import Node
from idle_compute.protocol import (
    ErrorCode,
    MessageType,
    ProtocolError,
    SeqWindow,
    build_envelope,
    encode_envelope,
    encode_frame,
    parse_message,
    read_frame,
)
from idle_compute.config import NodeConfig, from_dict
from idle_compute.signing import Ed25519Policy
from idle_compute.transport_tcp import TcpTransport
from idle_compute.worker import Worker

ROOT = Path(__file__).resolve().parent.parent

NODE_RUN = [sys.executable, "-m", "idle_compute", "node", "run", "--config"]


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _identity(tmp_path: Path, name: str) -> NodeIdentity:
    return NodeIdentity.load_or_create(tmp_path / "identity.key", node_name=name)


def _coord_config(tmp_path: Path, peers: list[str], listen_port: int = 0, poll: int = 1) -> NodeConfig:
    return from_dict(
        {
            "role": "coordinator",
            "transport": "tcp",
            "listen_host": "127.0.0.1",
            "listen_port": listen_port,
            "data_dir": str(tmp_path),
            "peers": peers,
            "poll_interval_seconds": poll,
            "log_level": "WARNING",
            "log_file": str(tmp_path / "coordinator.log"),
        }
    )


def _worker_config(tmp_path: Path, coord_id: str, coord_addr: str, poll: int = 1) -> NodeConfig:
    return from_dict(
        {
            "role": "worker",
            "transport": "tcp",
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "data_dir": str(tmp_path),
            "peers": [coord_id],
            "coordinator_node_id": coord_id,
            "coordinator_addr": coord_addr,
            "poll_interval_seconds": poll,
            "backoff_min": 1,
            "backoff_max": 4,
            "log_level": "WARNING",
            "log_file": str(tmp_path / "worker.log"),
        }
    )


def _client_config(tmp_path: Path, coord_id: str, coord_addr: str) -> NodeConfig:
    return from_dict(
        {
            "role": "client",
            "transport": "tcp",
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "data_dir": str(tmp_path),
            "peers": [coord_id],
            "coordinator_node_id": coord_id,
            "coordinator_addr": coord_addr,
            "poll_interval_seconds": 1,
            "log_level": "WARNING",
        }
    )


def _write_config(path: Path, config: NodeConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")


def _spawn_node(config_path: Path) -> subprocess.Popen:
    log_path = config_path.with_name(config_path.stem + "-proc.log")
    return subprocess.Popen(
        NODE_RUN + [str(config_path)],
        cwd=str(ROOT),
        stdout=open(log_path, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def _wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {message}")


def _rendezvous_addr(data_dir: Path, node_id: str) -> str:
    data = json.loads((data_dir / "run" / f"{node_id}.addr").read_text(encoding="utf-8"))
    return data["addr"]


class ManualPeer:
    """A bare TCP peer driven frame-by-frame: hostile/manual wire behavior.

    Deliberately NOT a Node: the recorder handler accepts anything (no
    HELLO gate), so the peer can observe every coordinator reply verbatim.
    """

    def __init__(self, identity: NodeIdentity):
        self.identity = identity
        self.coord_id: str | None = None
        self.transport = TcpTransport(identity.node_id, "127.0.0.1", 0)
        self.transport.set_handler(self._record)
        self.transport.start()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frames: list = []
        self._errors: list[tuple[int, str]] = []
        self._closed = threading.Event()
        self._seq = 0
        self._window = SeqWindow()

    def _record(self, peer, read, write):
        try:
            while True:
                try:
                    frame = read_frame(read)
                except ProtocolError:
                    return
                if frame is None:
                    return
                try:
                    env = parse_message(
                        frame,
                        transport_peer_id=None,
                        verifier=NodeIdentity.verify_signature,
                        seq_window=self._window,
                    )
                except ProtocolError as exc:
                    with self._lock:
                        self._errors.append((exc.code.value, exc.message))
                        self._cond.notify_all()
                    continue
                with self._lock:
                    self._frames.append(env)
                    self._cond.notify_all()
        finally:
            self._closed.set()

    def connect(self, coord_id: str, addr: str) -> None:
        self.coord_id = coord_id
        self.transport.dial(coord_id, [addr])

    def craft(self, type_, body, in_reply_to=None, signer=None, seq=None, recipient=None) -> bytes:
        signer = signer or self.identity
        if seq is None:
            self._seq += 1
            seq = self._seq
        return encode_envelope(
            build_envelope(
                type_,
                signer.node_id,
                signer.sign,
                recipient=recipient if recipient is not None else self.coord_id,
                seq=seq,
                body=body,
                in_reply_to=in_reply_to,
            )
        )

    def send(self, data: bytes) -> None:
        self.transport.send(self.coord_id, data)

    def send_craft(self, type_, body, in_reply_to=None, signer=None, recipient=None) -> None:
        self.send(
            self.craft(
                type_, body, in_reply_to=in_reply_to, signer=signer, recipient=recipient
            )
        )

    def hello(self) -> None:
        self.send_craft(
            MessageType.HELLO,
            {"node_id": self.identity.node_id, "role": "worker", "listen_addrs": ["127.0.0.1:1"]},
        )

    def wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                for index, env in enumerate(self._frames):
                    if predicate(env):
                        return self._frames.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def expect_error(self, code: ErrorCode, timeout: float):
        return self.wait_for(
            lambda e: e.type is MessageType.ERROR and e.body["code"] == code.value,
            timeout,
        )

    def wait_closed(self, timeout: float) -> bool:
        return self._closed.wait(timeout)

    def stop(self) -> None:
        self.transport.stop()


class NodeTestsBase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp_root = Path(tempfile.mkdtemp(prefix="node-e2e-"))
        self.addCleanup(self._cleanup_tmp)

    def _cleanup_tmp(self):
        import shutil

        shutil.rmtree(self._tmp_root, ignore_errors=True)

    def tmp(self, name: str) -> Path:
        path = self._tmp_root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _wait_mirror_count(worker_tmp: Path, want: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            mirror = sqlite3.connect(str(worker_tmp / "ledger_copy.db"))
            try:
                count = mirror.execute("SELECT COUNT(*) FROM ledger_copy").fetchone()[0]
            finally:
                mirror.close()
            if count >= want:
                return
            time.sleep(0.2)
        raise AssertionError(f"worker mirror never reached {want} event(s)")


class InProcessCoordinatorMixin:
    """Start a coordinator Node in-process (thread) and yield (node, addr)."""

    def start_coordinator(self, tmp_path: Path, peers: list[str], poll: int = 1) -> tuple[Node, str]:
        config = _coord_config(tmp_path, peers, listen_port=0, poll=poll)
        node = Node(config)
        result: list[int] = []

        def run():
            result.append(node.run())

        thread = threading.Thread(target=run, name="test-coordinator", daemon=True)
        thread.start()
        self.addCleanup(node.shutdown)
        self.addCleanup(thread.join, 5.0)
        _wait_for(lambda: bool(node.transport.advertised_addrs()), 15.0, "coordinator listen")
        return node, node.transport.advertised_addrs()[0]


class HappyPathTests(InProcessCoordinatorMixin, NodeTestsBase):
    """Real multi-process happy path: create -> claim -> execute -> validate -> credit."""

    def test_ping_pong_client_process(self) -> None:
        """A client process PINGs a coordinator process; PONG crosses the wire."""
        coord_tmp = self.tmp("coord")
        client_tmp = self.tmp("client")
        coord_identity = _identity(coord_tmp, "coord")
        client_identity = _identity(client_tmp, "client")
        node, addr = self.start_coordinator(coord_tmp, [client_identity.node_id])
        client = Node(_client_config(client_tmp, coord_identity.node_id, addr))
        self.addCleanup(client.shutdown)
        pong = client.client_ping(timeout=15.0)
        self.assertTrue(pong["pong"])
        self.assertGreaterEqual(pong["rtt_ms"], 0.0)
        self.assertIsNotNone(node)

    def test_full_credit_cycle_worker_subprocess(self) -> None:
        """Coordinator process + worker process: one task, one credit, mirrored."""
        coord_tmp = self.tmp("coord")
        worker_tmp = self.tmp("worker")
        client_tmp = self.tmp("client")
        coord_identity = _identity(coord_tmp, "coord")
        worker_identity = _identity(worker_tmp, "worker")
        client_identity = _identity(client_tmp, "client")
        coord_port = _free_port()
        coord_config = _coord_config(coord_tmp, [worker_identity.node_id, client_identity.node_id], coord_port)
        _write_config(coord_tmp / "coord.json", coord_config)
        coordinator = _spawn_node(coord_tmp / "coord.json")
        self.addCleanup(coordinator.wait, 10.0)
        self.addCleanup(coordinator.terminate)  # cleanups run LIFO: kill, then wait
        _wait_for(
            lambda: (coord_tmp / "run" / f"{coord_identity.node_id}.addr").exists(),
            20.0,
            "coordinator rendezvous file",
        )
        coord_addr = _rendezvous_addr(coord_tmp, coord_identity.node_id)

        worker_config = _worker_config(worker_tmp, coord_identity.node_id, coord_addr)
        _write_config(worker_tmp / "worker.json", worker_config)
        worker = _spawn_node(worker_tmp / "worker.json")
        self.addCleanup(worker.wait, 10.0)
        self.addCleanup(worker.terminate)  # cleanups run LIFO: kill, then wait

        client = Node(_client_config(client_tmp, coord_identity.node_id, coord_addr))
        self.addCleanup(client.shutdown)
        ack = client.client_create_task({"numbers": [1, 2, 3]}, timeout=15.0)
        task_id = ack["task_id"]

        status = self._wait_validated(client, task_id)
        self.assertEqual(status["validation"], "VALID")
        self.assertEqual(status["credits_count"], 1)

        # The worker mirrored the coordinator-signed credit event. Wait for
        # the append: the RESULT_ACK and the mirror write are two instants.
        self._wait_mirror_count(worker_tmp, 1)
        mirror = sqlite3.connect(str(worker_tmp / "ledger_copy.db"))
        try:
            rows = mirror.execute("SELECT event_json, verified FROM ledger_copy").fetchall()
        finally:
            mirror.close()
        self.assertEqual(len(rows), 1)
        event = json.loads(rows[0][0])
        self.assertEqual(rows[0][1], 1, "mirrored events must be verified")
        self.assertEqual(event["amount"], 1)
        self.assertEqual(event["task_id"], task_id)
        # The credit is paid to the worker that did the work — not to the
        # coordinator, the client, or anyone else.
        self.assertEqual(event["peer_id"], worker_identity.node_id)

    def test_full_credit_cycle_libp2p_inprocess(self) -> None:
        """The whole stack also runs over the libp2p transport (in-process)."""
        coord_tmp = self.tmp("coord")
        worker_tmp = self.tmp("worker")
        client_tmp = self.tmp("client")
        coord_identity = _identity(coord_tmp, "coord")
        worker_identity = _identity(worker_tmp, "worker")
        client_identity = _identity(client_tmp, "client")
        coord_cfg = from_dict(
            {
                "role": "coordinator",
                "transport": "libp2p",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(coord_tmp),
                "peers": [worker_identity.node_id, client_identity.node_id],
                "poll_interval_seconds": 1,
                "log_level": "WARNING",
            }
        )
        coordinator = Node(coord_cfg)
        self.addCleanup(coordinator.shutdown)
        thread = threading.Thread(target=coordinator.run, name="libp2p-coordinator", daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        _wait_for(lambda: bool(coordinator.transport.advertised_addrs()), 30.0, "libp2p listen")
        coord_addr = coordinator.transport.advertised_addrs()[0]

        worker_cfg = from_dict(
            {
                "role": "worker",
                "transport": "libp2p",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(worker_tmp),
                "peers": [coord_identity.node_id],
                "coordinator_node_id": coord_identity.node_id,
                "coordinator_addr": coord_addr,
                "poll_interval_seconds": 1,
                "backoff_min": 1,
                "backoff_max": 4,
                "log_level": "WARNING",
            }
        )
        worker_node = Node(worker_cfg)
        self.addCleanup(worker_node.shutdown)
        worker_thread = threading.Thread(target=worker_node.run, name="libp2p-worker", daemon=True)
        worker_thread.start()
        self.addCleanup(worker_thread.join, 10.0)

        client_cfg = from_dict(
            {
                "role": "client",
                "transport": "libp2p",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(client_tmp),
                "peers": [coord_identity.node_id],
                "coordinator_node_id": coord_identity.node_id,
                "coordinator_addr": coord_addr,
                "log_level": "WARNING",
            }
        )
        client = Node(client_cfg)
        self.addCleanup(client.shutdown)
        ack = client.client_create_task({"numbers": [4, 5]}, timeout=15.0)
        task_id = ack["task_id"]
        status = self._wait_validated(client, task_id, timeout=30.0)
        self.assertEqual(status["credits_count"], 1)

        mirror = sqlite3.connect(str(worker_tmp / "ledger_copy.db"))
        try:
            count = mirror.execute("SELECT COUNT(*) FROM ledger_copy").fetchone()[0]
        finally:
            mirror.close()
        self.assertEqual(count, 1)

    def _wait_validated(self, client: Node, task_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = client.client_status(task_id, timeout=10.0)
            # Wire vocabulary: validation VALID/INVALID (ValidationStatus),
            # task status COMPLETED (TaskStatus). Both are terminal here.
            if last["validation"] == "VALID":
                return last
            time.sleep(0.5)
        raise AssertionError(f"task {task_id} never validated; last status: {last}")


class AdversarialWireTests(InProcessCoordinatorMixin, NodeTestsBase):
    """Hostile frames against a real coordinator: nothing may earn credit."""

    def _setup_coordinator(self, tmp_name: str, peer_identities: list[NodeIdentity]) -> tuple[Node, str]:
        tmp_path = self.tmp(tmp_name)
        node, addr = self.start_coordinator(tmp_path, [p.node_id for p in peer_identities])
        return node, addr

    def _coord_db(self, node: Node) -> sqlite3.Connection:
        # The Store opens lazily inside the coordinator loop thread.
        _wait_for(lambda: node._store is not None, 10.0, "coordinator store open")
        conn = sqlite3.connect(str(node._store.db_path))
        self.addCleanup(conn.close)
        return conn

    def test_tampered_hello_signature_rejected(self) -> None:
        """BAD_SIGNATURE on the HELLO; the stream never binds, no service."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)

        env = build_envelope(
            MessageType.HELLO,
            peer.identity.node_id,
            peer.identity.sign,
            recipient=node.identity.node_id,
            seq=1,
            body={"node_id": peer.identity.node_id, "role": "worker", "listen_addrs": ["127.0.0.1:1"]},
        )
        env.signature = "a" * 128  # tamper: valid length, wrong signature
        peer.send(encode_envelope(env))

        error = peer.expect_error(ErrorCode.BAD_SIGNATURE, 5.0)
        self.assertIsNotNone(error, "coordinator must answer a tampered HELLO with BAD_SIGNATURE")

        # The tampered HELLO never bound the stream: a later valid PING must
        # still be refused with UNAUTHORIZED (HELLO-first gate).
        peer.send_craft(MessageType.PING, {"nonce": "nope"})
        error = peer.expect_error(ErrorCode.UNAUTHORIZED, 5.0)
        self.assertIsNotNone(error, "unbound stream must refuse PING with UNAUTHORIZED")

    def test_unauthorized_hello_rejected(self) -> None:
        """A valid identity that is NOT on the allowlist: UNAUTHORIZED, dropped."""
        coord_tmp = self.tmp("coord")
        allowed = _identity(self.tmp("allowed"), "allowed")
        rogue = ManualPeer(_identity(self.tmp("rogue"), "rogue"))
        self.addCleanup(rogue.stop)
        node, addr = self.start_coordinator(coord_tmp, [allowed.node_id])
        rogue.connect(node.identity.node_id, addr)
        rogue.hello()
        error = rogue.expect_error(ErrorCode.UNAUTHORIZED, 5.0)
        self.assertIsNotNone(error, "non-allowlisted HELLO must be answered UNAUTHORIZED")
        self.assertTrue(rogue.wait_closed(5.0), "coordinator must drop the unauthorized stream")

        # Seq persistence is allowlist-gated: the rejected stranger's frame
        # passed parse (valid signature + window) but must NOT have grown
        # node_state — hostile connectors must not grow the DB (audit
        # finding node.py:347).
        db = self._coord_db(node)
        row = db.execute(
            "SELECT COUNT(*) FROM node_state WHERE peer_id=?", (rogue.identity.node_id,)
        ).fetchone()
        self.assertEqual(row[0], 0, "non-allowlisted senders must not persist seq state")

    def test_hello_identity_mismatch_rejected(self) -> None:
        """HELLO whose body claims a DIFFERENT node_id than the sender: BAD_SENDER."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)

        # Signature is valid for the SENDER; the body lies about the node_id.
        peer.send_craft(
            MessageType.HELLO,
            {"node_id": node.identity.node_id, "role": "worker", "listen_addrs": ["127.0.0.1:1"]},
        )
        error = peer.expect_error(ErrorCode.BAD_SENDER, 5.0)
        self.assertIsNotNone(error, "identity mismatch must be answered BAD_SENDER")
        self.assertTrue(peer.wait_closed(5.0), "mismatched HELLO must close the stream")

    def test_illegal_body_gets_error_and_stream_survives(self) -> None:
        """A signed frame with an illegal body: INVALID_BODY; stream stays usable."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        ack = peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0)
        self.assertIsNotNone(ack)

        peer.send_craft(MessageType.PING, {"nonce": 12345})  # signed, but nonce is not a string
        error = peer.expect_error(ErrorCode.INVALID_BODY, 5.0)
        self.assertIsNotNone(error, "illegal PING body must be answered INVALID_BODY")

        peer.send_craft(MessageType.PING, {"nonce": "still-alive"})
        pong = peer.wait_for(
            lambda e: e.type is MessageType.PONG and e.body["nonce"] == "still-alive",
            5.0,
        )
        self.assertIsNotNone(pong, "stream must survive an illegal frame")

    def test_garbage_frame_no_reply_stream_survives(self) -> None:
        """Undecodable bytes are dropped silently; the stream keeps working."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        peer.send(encode_frame(b"this is not json at all"))
        # Undecodable bytes must be dropped SILENTLY: the coordinator may
        # not answer with a (well-formed) ERROR envelope either. Scan the
        # frame buffer, not _errors — a structured ERROR reply parses fine
        # and would land in _frames, never in _errors.
        time.sleep(0.5)
        with peer._lock:
            error_replies = [e for e in peer._frames if e.type is MessageType.ERROR]
        self.assertEqual(error_replies, [], "garbage must not produce an ERROR reply")

        peer.send_craft(MessageType.PING, {"nonce": "after-garbage"})
        pong = peer.wait_for(
            lambda e: e.type is MessageType.PONG and e.body["nonce"] == "after-garbage",
            5.0,
        )
        self.assertIsNotNone(pong, "stream must survive a garbage frame")

    def test_oversized_frame_closes_stream_but_node_stays_up(self) -> None:
        """A >64KiB declared frame is rejected on the header; stream closed."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        other = ManualPeer(_identity(self.tmp("other"), "other"))
        self.addCleanup(peer.stop)
        self.addCleanup(other.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id, other.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        peer.send(struct.pack(">I", 70_000))  # oversized header, no payload
        self.assertTrue(peer.wait_closed(5.0), "coordinator must close the stream on an oversized frame")

        # The coordinator itself stays healthy for other peers.
        other.connect(node.identity.node_id, addr)
        other.hello()
        self.assertIsNotNone(other.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))
        other.send_craft(MessageType.PING, {"nonce": "healthy"})
        self.assertIsNotNone(
            other.wait_for(
                lambda e: e.type is MessageType.PONG and e.body["nonce"] == "healthy",
                5.0,
            ),
            "PONG must echo our nonce (content, not just a PONG-shaped frame)",
        )

    def test_forged_result_rejected_no_credit(self) -> None:
        """A worker submits wrong content with a VALID signature: INVALID, no credit."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        peer.send_craft(
            MessageType.TASK_CREATE,
            {
                "client_ref": "clr-forged-1",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [10, 20, 30]},
                "max_attempts": 3,
                "lease_seconds": 30,
            },
        )
        ack = peer.wait_for(lambda e: e.type is MessageType.TASK_CREATE_ACK, 5.0)
        self.assertIsNotNone(ack, "task creation must succeed")
        task_id = ack.body["task_id"]

        peer.send_craft(MessageType.CLAIM_REQUEST, {"claim_request_id": "crq-forged-1", "task_id": None})
        grant = peer.wait_for(lambda e: e.type is MessageType.CLAIM_GRANT, 5.0)
        self.assertIsNotNone(grant, "claim must be granted")
        lease = grant.body["lease"]

        # Forged output: shape-valid, content wrong, signature VALID over the
        # forged content — the attacker signs what they submit.
        forged = {"count": 3, "sum": 999, "sum_squares": 1}
        digest = content_hash(forged)
        payload = result_signing_payload(task_id, lease["lease_id"], peer.identity.node_id, digest)
        signature = peer.identity.sign(payload)
        peer.send_craft(
            MessageType.SUBMIT_RESULT,
            {
                "lease_id": lease["lease_id"],
                "output": forged,
                "content_hash": digest,
                "signature": signature,
            },
        )
        result_ack = peer.wait_for(lambda e: e.type is MessageType.RESULT_ACK, 5.0)
        self.assertIsNotNone(result_ack)
        self.assertEqual(result_ack.body["validation"], "INVALID")
        self.assertIsNone(result_ack.body["credit"], "a forged result must never carry credit")

        peer.send_craft(MessageType.STATUS_REQUEST, {"task_id": task_id})
        status = peer.wait_for(lambda e: e.type is MessageType.STATUS_RESPONSE, 5.0)
        self.assertIsNotNone(status)
        self.assertEqual(status.body["validation"], "INVALID")
        self.assertEqual(status.body["credits_count"], 0)

        db = self._coord_db(node)
        count = db.execute("SELECT COUNT(*) FROM credit_events").fetchone()[0]
        self.assertEqual(count, 0, "no credit event may exist for a forged result")

    def test_duplicate_claim_request_returns_same_lease(self) -> None:
        """A retried CLAIM_REQUEST with the same id gets the SAME lease back."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        peer.send_craft(
            MessageType.TASK_CREATE,
            {
                "client_ref": "clr-dup-claim-1",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [7]},
                "max_attempts": 3,
                "lease_seconds": 30,
            },
        )
        task_id = peer.wait_for(lambda e: e.type is MessageType.TASK_CREATE_ACK, 5.0).body["task_id"]

        peer.send_craft(MessageType.CLAIM_REQUEST, {"claim_request_id": "crq-dup-1", "task_id": None})
        first = peer.wait_for(lambda e: e.type is MessageType.CLAIM_GRANT, 5.0)
        self.assertIsNotNone(first)
        peer.send_craft(MessageType.CLAIM_REQUEST, {"claim_request_id": "crq-dup-1", "task_id": None})
        second = peer.wait_for(lambda e: e.type is MessageType.CLAIM_GRANT, 5.0)
        self.assertIsNotNone(second)

        self.assertEqual(first.body["lease"]["lease_id"], second.body["lease"]["lease_id"])
        db = self._coord_db(node)
        count = db.execute("SELECT COUNT(*) FROM leases WHERE task_id=?", (task_id,)).fetchone()[0]
        self.assertEqual(count, 1, "a replayed claim must not create a second lease")

    def test_second_peer_claim_replay_denied_no_new_lease(self) -> None:
        """A DIFFERENT peer replaying a known claim id: DENIED, no lease minted."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        thief = ManualPeer(_identity(self.tmp("thief"), "thief"))
        self.addCleanup(peer.stop)
        self.addCleanup(thief.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id, thief.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))
        thief.connect(node.identity.node_id, addr)
        thief.hello()
        self.assertIsNotNone(thief.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        peer.send_craft(
            MessageType.TASK_CREATE,
            {
                "client_ref": "clr-thief-1",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [5]},
                "max_attempts": 3,
                "lease_seconds": 30,
            },
        )
        task_id = peer.wait_for(lambda e: e.type is MessageType.TASK_CREATE_ACK, 5.0).body["task_id"]

        peer.send_craft(MessageType.CLAIM_REQUEST, {"claim_request_id": "crq-thief-1", "task_id": None})
        grant = peer.wait_for(lambda e: e.type is MessageType.CLAIM_GRANT, 5.0)
        self.assertIsNotNone(grant, "the honest claim must be granted")

        # The thief replays the SAME claim_request_id: known id + foreign
        # owner must be denied WITHOUT minting a second lease on the task.
        thief.send_craft(MessageType.CLAIM_REQUEST, {"claim_request_id": "crq-thief-1", "task_id": None})
        denied = thief.wait_for(
            lambda e: e.type is MessageType.CLAIM_DENIED and e.body["claim_request_id"] == "crq-thief-1",
            5.0,
        )
        self.assertIsNotNone(denied, "a foreign claim replay must be denied")

        db = self._coord_db(node)
        count = db.execute("SELECT COUNT(*) FROM leases WHERE task_id=?", (task_id,)).fetchone()[0]
        self.assertEqual(count, 1, "a foreign replay must not mint a second lease")
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM claims WHERE claim_request_id=?", ("crq-thief-1",)).fetchone()[0],
            1,
            "the claims row must still map to the original lease",
        )

    def test_duplicate_submission_no_double_credit(self) -> None:
        """Resubmitting the same result replays the SAME ack, exactly one credit."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        other = ManualPeer(_identity(self.tmp("other"), "other"))
        self.addCleanup(peer.stop)
        self.addCleanup(other.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id, other.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        peer.send_craft(
            MessageType.TASK_CREATE,
            {
                "client_ref": "clr-dup-submit-1",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [2, 4]},
                "max_attempts": 3,
                "lease_seconds": 30,
            },
        )
        task_id = peer.wait_for(lambda e: e.type is MessageType.TASK_CREATE_ACK, 5.0).body["task_id"]
        peer.send_craft(MessageType.CLAIM_REQUEST, {"claim_request_id": "crq-dup-submit-1", "task_id": None})
        grant = peer.wait_for(lambda e: e.type is MessageType.CLAIM_GRANT, 5.0)
        self.assertIsNotNone(grant)
        lease = grant.body["lease"]

        task = Task(task_id=task_id, task_type="numeric_batch_v1", payload={"numbers": [2, 4]})
        lease_obj = Lease(
            lease_id=lease["lease_id"],
            task_id=task_id,
            peer_id=peer.identity.node_id,
            status=LeaseStatus.ACTIVE,
            expires_at=lease["expires_at"],
        )
        output, digest, signature = Worker(
            peer.identity.node_id, Ed25519Policy.from_identity(peer.identity)
        ).execute(task, lease_obj)
        submit = {
            "lease_id": lease["lease_id"],
            "output": output,
            "content_hash": digest,
            "signature": signature,
        }

        peer.send_craft(MessageType.SUBMIT_RESULT, submit)
        first_ack = peer.wait_for(lambda e: e.type is MessageType.RESULT_ACK, 5.0)
        self.assertIsNotNone(first_ack)
        self.assertEqual(first_ack.body["validation"], "VALID")
        self.assertIsNotNone(first_ack.body["credit"])

        # Idempotent replay (same peer): the SAME RESULT_ACK comes back —
        # a worker whose original ack was lost still receives its credit
        # event instead of a bare DUPLICATE error.
        peer.send_craft(MessageType.SUBMIT_RESULT, submit)
        replay_ack = peer.wait_for(lambda e: e.type is MessageType.RESULT_ACK, 5.0)
        self.assertIsNotNone(replay_ack, "a replayed submission must replay the same RESULT_ACK")
        self.assertEqual(replay_ack.body["result_id"], first_ack.body["result_id"])
        self.assertEqual(replay_ack.body["validation"], "VALID")
        self.assertIsNotNone(replay_ack.body["credit"])

        # A DIFFERENT peer replaying the same lease is refused at the
        # ownership gate (TASK_UNAVAILABLE -- the lease is not theirs, and
        # the reply must not disclose that a result exists): credit is
        # never delivered to a stranger.
        other.connect(node.identity.node_id, addr)
        other.hello()
        self.assertIsNotNone(other.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))
        other.send_craft(MessageType.SUBMIT_RESULT, submit)
        refused = other.expect_error(ErrorCode.TASK_UNAVAILABLE, 5.0)
        self.assertIsNotNone(refused, "a foreign replay must be refused without disclosure")

        peer.send_craft(MessageType.STATUS_REQUEST, {"task_id": task_id})
        status = peer.wait_for(lambda e: e.type is MessageType.STATUS_RESPONSE, 5.0)
        self.assertEqual(status.body["credits_count"], 1, "duplicate submission must not double-count credit")
        db = self._coord_db(node)
        count = db.execute("SELECT COUNT(*) FROM credit_events WHERE task_id=?", (task_id,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_abandoned_lease_requeued_after_expiry(self) -> None:
        """A claim that never submits expires and the task becomes claimable again."""
        coord_tmp = self.tmp("coord")
        first_peer = ManualPeer(_identity(self.tmp("first"), "first"))
        second_peer = ManualPeer(_identity(self.tmp("second"), "second"))
        self.addCleanup(first_peer.stop)
        self.addCleanup(second_peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [first_peer.identity.node_id, second_peer.identity.node_id])
        first_peer.connect(node.identity.node_id, addr)
        first_peer.hello()
        self.assertIsNotNone(first_peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        first_peer.send_craft(
            MessageType.TASK_CREATE,
            {
                "client_ref": "clr-abandon-1",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [9]},
                "max_attempts": 3,
                "lease_seconds": 2,  # expires quickly
            },
        )
        task_id = first_peer.wait_for(lambda e: e.type is MessageType.TASK_CREATE_ACK, 5.0).body["task_id"]
        first_peer.send_craft(MessageType.CLAIM_REQUEST, {"claim_request_id": "crq-abandon-1", "task_id": None})
        first_grant = first_peer.wait_for(lambda e: e.type is MessageType.CLAIM_GRANT, 5.0)
        self.assertIsNotNone(first_grant)
        first_lease_id = first_grant.body["lease"]["lease_id"]
        first_peer.stop()  # abandon without submitting

        # After expiry the task returns to the queue; a different peer gets it.
        second_peer.connect(node.identity.node_id, addr)
        second_peer.hello()
        self.assertIsNotNone(second_peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))
        second_grant = None
        deadline = time.monotonic() + 15.0
        while second_grant is None and time.monotonic() < deadline:
            second_peer.send_craft(
                MessageType.CLAIM_REQUEST, {"claim_request_id": f"crq-abandon-2-{time.monotonic():.0f}", "task_id": None}
            )
            second_grant = second_peer.wait_for(lambda e: e.type is MessageType.CLAIM_GRANT, 5.0)
            time.sleep(0.5)
        self.assertIsNotNone(second_grant, "the abandoned task must become claimable after expiry")
        self.assertEqual(second_grant.body["lease"]["task_id"], task_id)
        self.assertNotEqual(second_grant.body["lease"]["lease_id"], first_lease_id)

    def test_error_envelope_never_answered(self) -> None:
        """An ERROR sent to the coordinator must not itself be answered:
        replying to an ERROR would let a hostile peer start an echo storm."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        # Schema-valid ERROR body (all four registered keys), so the
        # envelope parses and reaches the dispatch path -- which must not
        # answer it. (A malformed body would instead take the parse-error
        # path, whose ERROR reply is legitimate.)
        peer.send_craft(
            MessageType.ERROR,
            {
                "code": ErrorCode.INTERNAL.value,
                "message": "probe",
                "ref_type": None,
                "ref_seq": None,
            },
        )
        time.sleep(1.0)
        with peer._lock:
            self.assertNotIn(
                MessageType.ERROR,
                [env.type for env in peer._frames],
                "coordinator must not answer an ERROR",
            )
        # The stream must survive: the same connection still serves PING.
        peer.send_craft(MessageType.PING, {"nonce": "still-alive"})
        pong = peer.wait_for(
            lambda e: e.type is MessageType.PONG and e.body["nonce"] == "still-alive",
            5.0,
        )
        self.assertIsNotNone(pong, "the stream must stay usable after the ERROR")

    def test_hostile_wire_type_string_gets_schema_valid_error(self) -> None:
        """A 100-char unknown wire 'type' must not crash the parser, and any
        reply must satisfy the ERROR schema (ref_type clamped or absent)."""
        coord_tmp = self.tmp("coord")
        peer = ManualPeer(_identity(self.tmp("peer"), "peer"))
        self.addCleanup(peer.stop)
        node, addr = self.start_coordinator(coord_tmp, [peer.identity.node_id])
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 5.0))

        # A well-formed envelope whose "type" was replaced by a hostile
        # 100-char string. The parse pipeline fails at the type gate
        # (UNSUPPORTED_TYPE) before the signature, so the coordinator's
        # parse-error reply path is what gets exercised.
        framed = peer.craft(MessageType.PING, {"nonce": "boom"})
        good = json.loads(framed[4:].decode("utf-8"))  # drop the 4-byte length header
        good["type"] = "x" * 100
        peer.send(encode_frame(json.dumps(good).encode("utf-8")))

        errors_before = len(peer._errors)
        error = peer.wait_for(lambda e: e.type is MessageType.ERROR, 5.0)
        if error is not None:
            # Reaching here already proves the reply parses (the recorder
            # stores only schema-valid envelopes). The ref_type must be
            # absent or a registered wire type -- never the hostile string.
            self.assertEqual(error.body["code"], ErrorCode.UNSUPPORTED_TYPE.value)
            ref_type = error.body.get("ref_type")
            self.assertTrue(
                ref_type is None or ref_type in {t.value for t in MessageType},
                f"ref_type {ref_type!r} must be absent or a valid message type",
            )
        # Whether or not a reply arrived, the wire must never emit an
        # ERROR that violates its own schema.
        self.assertEqual(len(peer._errors), errors_before, "reply must be schema-valid")

    def test_message_for_another_recipient_is_not_processed(self) -> None:
        """D1: a signature-valid PING addressed to a third node is refused.

        The envelope signature is valid and the sender is allowlisted, so only
        the recipient check can stop it. Observable: ERROR(UNAUTHORIZED) and
        no PONG.
        """
        base = self.tmp("recipient-case")
        coord_tmp = base / "coord"
        coord_tmp.mkdir(exist_ok=True)
        peer_tmp = base / "peer"
        peer_tmp.mkdir(exist_ok=True)
        other_tmp = base / "other"
        other_tmp.mkdir(exist_ok=True)
        peer_identity = _identity(peer_tmp, "peer")
        other_identity = _identity(other_tmp, "other")
        self.assertNotEqual(peer_identity.node_id, other_identity.node_id)
        node, addr = self.start_coordinator(coord_tmp, [peer_identity.node_id])
        self.addCleanup(node.shutdown)

        peer = ManualPeer(peer_identity)
        self.addCleanup(peer.stop)
        peer.connect(node.identity.node_id, addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 10.0))

        peer.send_craft(MessageType.PING, {"nonce": "wrong-target"}, recipient=other_identity.node_id)
        error = peer.expect_error(ErrorCode.UNAUTHORIZED, 10.0)
        self.assertIsNotNone(error, "a message for another node must be refused")
        self.assertIsNone(
            peer.wait_for(lambda e: e.type is MessageType.PONG, 1.0),
            "a misaddressed PING must never be processed",
        )


class ResilienceTests(NodeTestsBase):
    """Restart/disconnect behavior across real processes."""

    def _setup_subprocesses(self, coord_tmp, worker_tmp, client_tmp):
        coord_identity = _identity(coord_tmp, "coord")
        worker_identity = _identity(worker_tmp, "worker")
        client_identity = _identity(client_tmp, "client")
        coord_port = _free_port()
        coord_config = _coord_config(coord_tmp, [worker_identity.node_id, client_identity.node_id], coord_port)
        _write_config(coord_tmp / "coord.json", coord_config)
        coordinator = _spawn_node(coord_tmp / "coord.json")
        _wait_for(
            lambda: (coord_tmp / "run" / f"{coord_identity.node_id}.addr").exists(),
            20.0,
            "coordinator rendezvous file",
        )
        coord_addr = _rendezvous_addr(coord_tmp, coord_identity.node_id)
        worker_config = _worker_config(worker_tmp, coord_identity.node_id, coord_addr)
        _write_config(worker_tmp / "worker.json", worker_config)
        return coord_identity, worker_identity, client_identity, coord_addr, coordinator

    def test_worker_restart_recovery_persisted_seq(self) -> None:
        """Kill the worker after one credit; a restarted worker keeps working."""
        coord_tmp = self.tmp("coord")
        worker_tmp = self.tmp("worker")
        client_tmp = self.tmp("client")
        coord_identity, worker_identity, client_identity, coord_addr, coordinator = self._setup_subprocesses(
            coord_tmp, worker_tmp, client_tmp
        )
        self.addCleanup(coordinator.wait, 10.0)
        self.addCleanup(coordinator.terminate)  # cleanups run LIFO: kill, then wait

        client = Node(_client_config(client_tmp, coord_identity.node_id, coord_addr))
        self.addCleanup(client.shutdown)

        def run_worker():
            worker = _spawn_node(worker_tmp / "worker.json")
            return worker

        def stop_worker(worker):
            worker.terminate()
            worker.wait(10.0)

        worker = run_worker()
        try:
            ack = client.client_create_task({"numbers": [1, 1]}, timeout=15.0)
            status = self._wait_validated(client, ack["task_id"])
            self.assertEqual(status["credits_count"], 1)
            # Wait for the mirror write before the kill: the RESULT_ACK and
            # the worker's mirror append are two different instants.
            self._wait_mirror_count(worker_tmp, 1)
            # The coordinator's per-peer seq window must be PERSISTED (not
            # just claimed): node_state holds the worker's row with a
            # positive last_seq_seen before the worker is killed.
            coord_db = sqlite3.connect(str(coord_tmp / "coordinator.db"))
            try:
                seen_row = coord_db.execute(
                    "SELECT last_seq_seen FROM node_state WHERE peer_id=?",
                    (worker_identity.node_id,),
                ).fetchone()
            finally:
                coord_db.close()
            self.assertIsNotNone(seen_row, "node_state must persist the worker's seq window")
            self.assertGreater(seen_row[0], 0)
        finally:
            stop_worker(worker)

        # Restart the SAME worker identity (seq.json + ledger mirror persist);
        # the coordinator's per-peer seen-seq persists in node_state, so the
        # restarted worker must not be rejected as a replay.
        worker = run_worker()
        try:
            ack = client.client_create_task({"numbers": [2, 2, 2]}, timeout=15.0)
            status = self._wait_validated(client, ack["task_id"])
            self.assertEqual(status["credits_count"], 1)
        finally:
            stop_worker(worker)

        mirror = sqlite3.connect(str(worker_tmp / "ledger_copy.db"))
        try:
            count = mirror.execute("SELECT COUNT(*) FROM ledger_copy").fetchone()[0]
        finally:
            mirror.close()
        self.assertEqual(count, 2, "both credits must be mirrored across the restart")

    def test_worker_reconnects_after_coordinator_restart(self) -> None:
        """Kill and restart the coordinator; the running worker reconnects and finishes."""
        coord_tmp = self.tmp("coord")
        worker_tmp = self.tmp("worker")
        client_tmp = self.tmp("client")
        coord_identity, worker_identity, client_identity, coord_addr, coordinator = self._setup_subprocesses(
            coord_tmp, worker_tmp, client_tmp
        )

        worker = _spawn_node(worker_tmp / "worker.json")
        try:
            # Give the worker time to connect before we kill the coordinator.
            # Same client identity/data_dir as the post-restart client below.
            pre_kill_client = Node(_client_config(client_tmp, coord_identity.node_id, coord_addr))
            self.addCleanup(pre_kill_client.shutdown)
            pre_kill_client.client_ping(timeout=15.0)
            time.sleep(2.0)

            coordinator.terminate()
            coordinator.wait(10.0)
            pre_kill_client.shutdown()  # its connection is dead; stop seq writes
            (coord_tmp / "run" / f"{coord_identity.node_id}.addr").unlink(missing_ok=True)

            coordinator = _spawn_node(coord_tmp / "coord.json")
            _wait_for(
                lambda: (coord_tmp / "run" / f"{coord_identity.node_id}.addr").exists(),
                20.0,
                "coordinator restart",
            )

            # The old client's connection died with the coordinator; a fresh
            # client (same identity, same data_dir) drives the post-restart flow.
            client = Node(_client_config(client_tmp, coord_identity.node_id, coord_addr))
            self.addCleanup(client.shutdown)
            ack = client.client_create_task({"numbers": [5]}, timeout=15.0)
            status = self._wait_validated(client, ack["task_id"], timeout=45.0)
            self.assertEqual(status["credits_count"], 1)
        finally:
            worker.terminate()
            worker.wait(10.0)
            coordinator.terminate()
            coordinator.wait(10.0)

    def test_worker_seq_regression_self_heals(self) -> None:
        """A rolled-back seq.json must not wedge the worker forever: the
        coordinator's SEQ_OUT_OF_WINDOW error carries the persisted
        last_seen, and the worker jumps past it and reconnects."""
        coord_tmp = self.tmp("coord")
        worker_tmp = self.tmp("worker")
        client_tmp = self.tmp("client")
        coord_identity, worker_identity, client_identity, coord_addr, coordinator = self._setup_subprocesses(
            coord_tmp, worker_tmp, client_tmp
        )
        self.addCleanup(coordinator.wait, 10.0)
        self.addCleanup(coordinator.terminate)

        client = Node(_client_config(client_tmp, coord_identity.node_id, coord_addr))
        self.addCleanup(client.shutdown)

        worker = _spawn_node(worker_tmp / "worker.json")
        try:
            ack = client.client_create_task({"numbers": [3]}, timeout=15.0)
            status = self._wait_validated(client, ack["task_id"])
            self.assertEqual(status["credits_count"], 1)
            self._wait_mirror_count(worker_tmp, 1)
            # Precondition: the coordinator has a persisted window for the
            # worker, so a rolled-back worker seq IS behind on restart.
            coord_db = sqlite3.connect(str(coord_tmp / "coordinator.db"))
            try:
                seen = coord_db.execute(
                    "SELECT last_seq_seen FROM node_state WHERE peer_id=?",
                    (worker_identity.node_id,),
                ).fetchone()
            finally:
                coord_db.close()
            self.assertIsNotNone(seen, "precondition: coordinator persists the worker window")
            self.assertGreater(seen[0], 0)
        finally:
            worker.terminate()
            worker.wait(10.0)

        # Roll the worker's send-seq back to zero (simulates a lost disk /
        # restored backup): its next HELLO is behind the coordinator's
        # window and would be rejected as a replay -- unless the worker
        # self-heals from the SEQ_OUT_OF_WINDOW error.
        (worker_tmp / "seq.json").unlink(missing_ok=True)

        worker = _spawn_node(worker_tmp / "worker.json")
        try:
            ack = client.client_create_task({"numbers": [4, 4]}, timeout=15.0)
            status = self._wait_validated(client, ack["task_id"], timeout=45.0)
            self.assertEqual(status["credits_count"], 1)
        finally:
            worker.terminate()
            worker.wait(10.0)

    def _wait_validated(self, client: Node, task_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = client.client_status(task_id, timeout=10.0)
            # Wire vocabulary: validation VALID/INVALID (ValidationStatus).
            if last["validation"] == "VALID":
                return last
            time.sleep(0.5)
        raise AssertionError(f"task {task_id} never validated; last status: {last}")


class ShutdownTests(NodeTestsBase):
    def test_clean_shutdown_returns_zero(self) -> None:
        """run() ends with exit code 0 on clean stop (the SIGINT path)."""
        coord_tmp = self.tmp("coord")
        identity = _identity(coord_tmp, "coord")
        node = Node(_coord_config(coord_tmp, [], listen_port=0))
        self.addCleanup(node.shutdown)
        result: list[int] = []
        thread = threading.Thread(
            target=lambda: result.append(node.run()), name="shutdown-test-node", daemon=True
        )
        thread.start()
        _wait_for(lambda: bool(node.transport.advertised_addrs()), 15.0, "node start")
        # KeyboardInterrupt and this event take the same path: finally
        # shutdown() then return 0.
        node._stopping.set()
        thread.join(10.0)
        self.assertFalse(thread.is_alive(), "node must exit its run loop")
        self.assertEqual(result, [0])

    def test_shutdown_closes_the_store_on_the_loop_thread(self) -> None:
        """A clean shutdown must release coordinator.db (no leaked sqlite handle).

        Regression: shutdown() used to close the Store from the caller's
        thread; sqlite3 refuses cross-thread use, the ProgrammingError was
        swallowed, and the DB file stayed locked after shutdown (caught by the
        E-1 worker-mirror test teardown). Renaming a locked file fails on
        Windows, so rename is the observable assertion.
        """
        coord_tmp = self.tmp("coord-db")
        _identity(coord_tmp, "coord")
        node = Node(_coord_config(coord_tmp, [], listen_port=0))
        thread = threading.Thread(target=node.run, name="shutdown-db-node", daemon=True)
        thread.start()
        _wait_for(lambda: bool(node.transport.advertised_addrs()), 15.0, "node start")
        node.shutdown()
        thread.join(10.0)
        self.assertFalse(thread.is_alive(), "node must exit its run loop")

        db = coord_tmp / "coordinator.db"
        self.assertTrue(db.exists(), "the coordinator DB must have been created")
        probe = db.with_suffix(".db.released")
        db.rename(probe)  # PermissionError here means the file is still locked
        self.assertTrue(probe.exists())
        probe.unlink()


if __name__ == "__main__":
    unittest.main()
