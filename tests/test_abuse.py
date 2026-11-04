"""F-2: error isolation and resource caps on a RUNNING node.

Acceptance covered here:
- the 1001st live task is refused with OVERLOAD (bounded task pool);
- a stream that dies mid-abuse does not affect other bound streams;
- 200 malformed frames leave the node serving (in-process and in a real
  subprocess, i.e. the process itself survives abuse).

Already covered elsewhere (referenced, not duplicated):
- oversized frame rejected before allocation + node stays up:
  tests/test_node.py::test_oversized_frame_closes_stream_but_node_stays_up;
- garbage frame / illegal body keep the node serving:
  test_garbage_frame_no_reply_stream_survives,
  test_illegal_body_gets_error_and_stream_survives;
- 50 msg/s flood trips the token bucket: tests/test_ratelimit.py.
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from idle_compute.config import from_dict
from idle_compute.db import Store
from idle_compute.identity import NodeIdentity
from idle_compute.node import MAX_ACTIVE_TASKS, Node
from idle_compute.protocol import MessageType, ProtocolError, SeqWindow, build_envelope, encode_envelope, parse_message, read_frame
from idle_compute.transport_tcp import TcpTransport

ROOT = Path(__file__).resolve().parent.parent
NODE_RUN = [sys.executable, "-m", "idle_compute", "node", "run", "--config"]


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_port(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"node never listened on {host}:{port}")


class RawPeer:
    """Minimal bare TCP peer (same shape as tests/test_ratelimit.py)."""

    def __init__(self, identity: NodeIdentity, coord_id: str) -> None:
        self.identity = identity
        self.coord_id = coord_id
        self.frames: list = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = threading.Event()
        self._seq = 0
        self._window = SeqWindow()
        self.transport = TcpTransport(identity.node_id, "127.0.0.1", 0)
        self.transport.set_handler(self._record)
        self.transport.start()

    def _record(self, peer, read, write) -> None:
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
                        self.errors.append(exc.code.value)
                        self._cond.notify_all()
                    continue
                with self._lock:
                    self.frames.append(env)
                    self._cond.notify_all()
        finally:
            self._closed.set()

    def dial(self, addr: str) -> None:
        self.transport.dial(self.coord_id, [addr])

    def send_craft(self, type_: MessageType, body: dict) -> None:
        self._seq += 1
        frame = encode_envelope(
            build_envelope(
                type_,
                self.identity.node_id,
                self.identity.sign,
                recipient=self.coord_id,
                seq=self._seq,
                body=body,
            )
        )
        self.transport.send(self.coord_id, frame)

    def hello(self) -> None:
        self.send_craft(
            MessageType.HELLO,
            {"node_id": self.identity.node_id, "role": "worker", "listen_addrs": ["127.0.0.1:1"]},
        )

    def wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                for index, env in enumerate(self.frames):
                    if predicate(env):
                        return self.frames.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def stop(self) -> None:
        self.transport.stop()


class AbuseFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="abuse-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord_dir = self.root / "coord"
        self.coord_dir.mkdir(parents=True, exist_ok=True)
        self.coord_identity = NodeIdentity.load_or_create(
            self.coord_dir / "identity.key", node_name="coordinator"
        )
        self.abuser_id = NodeIdentity.load_or_create(
            self.root / "abuser" / "identity.key", node_name="abuser"
        )
        self.good_id = NodeIdentity.load_or_create(
            self.root / "good" / "identity.key", node_name="good"
        )
        # Created up front: the allowlist is static, so the client identity must
        # exist before the coordinator starts.
        self.client_id = NodeIdentity.load_or_create(
            self.root / "client" / "identity.key", node_name="client"
        )

    def start_inprocess(self, listen_port: int = 0) -> None:
        config = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": listen_port,
                "data_dir": str(self.coord_dir),
                "peers": [self.abuser_id.node_id, self.good_id.node_id, self.client_id.node_id],
                "poll_interval_seconds": 1,
                "log_level": "WARNING",
                "log_file": str(self.coord_dir / "coordinator.log"),
            }
        )
        self.node = Node(config)
        self.addCleanup(self.node.shutdown)
        self._thread = threading.Thread(target=self.node.run, name="abuse-coordinator", daemon=True)
        self._thread.start()
        self.addCleanup(self._thread.join, 10.0)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.node.transport.advertised_addrs():
            time.sleep(0.05)
        self.addr = self.node.transport.advertised_addrs()[0]

    def bind(self, peer: RawPeer) -> None:
        peer.dial(self.addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 15.0))


class AbuseTests(AbuseFixture):
    def test_task_pool_cap_returns_overload_on_the_1001st(self) -> None:
        # Fill the pool BEFORE the node opens the store.
        store = Store(self.coord_dir / "coordinator.db")
        try:
            for i in range(MAX_ACTIVE_TASKS):
                store.create_task("numeric_batch_v1", {"numbers": [i % 7]})
        finally:
            store.close()
        self.start_inprocess()

        peer = RawPeer(self.abuser_id, self.coord_identity.node_id)
        self.addCleanup(peer.stop)
        self.bind(peer)
        peer.send_craft(
            MessageType.TASK_CREATE,
            {
                "client_ref": "over-cap",
                "task_type": "numeric_batch_v1",
                "payload": {"numbers": [1]},
                "max_attempts": 3,
                "lease_seconds": 30,
            },
        )
        error = peer.wait_for(
            lambda e: e.type is MessageType.ERROR and e.body["code"] == 15, 10.0  # OVERLOAD
        )
        self.assertIsNotNone(error, "the capped task pool must answer OVERLOAD")
        self.assertIn("full", error.body["message"])

    def test_stream_failure_does_not_affect_another_bound_stream(self) -> None:
        self.start_inprocess()
        good = RawPeer(self.good_id, self.coord_identity.node_id)
        self.addCleanup(good.stop)
        self.bind(good)
        victim = RawPeer(self.abuser_id, self.coord_identity.node_id)
        self.addCleanup(victim.stop)
        self.bind(victim)

        # Kill the victim's stream at the framing layer (declared length but
        # truncated payload -> the handler closes that stream).
        victim.transport.send(victim.coord_id, struct.pack(">I", 50) + b"{\"x\":1}")
        time.sleep(0.5)

        good.send_craft(MessageType.PING, {"nonce": "still-alive"})
        pong = good.wait_for(lambda e: e.type is MessageType.PONG, 10.0)
        self.assertIsNotNone(pong, "a dead stream must not disturb another bound stream")
        self.assertEqual(pong.body["nonce"], "still-alive")

    def test_200_malformed_frames_leave_the_node_serving(self) -> None:
        self.start_inprocess()
        abuser = RawPeer(self.abuser_id, self.coord_identity.node_id)
        self.addCleanup(abuser.stop)
        try:
            abuser.dial(self.addr)
            for _ in range(200):
                # Valid framing, invalid JSON: parsed, rejected, counted.
                abuser.transport.send(abuser.coord_id, struct.pack(">I", 3) + b"{x}")
        except Exception:
            pass  # the rate limiter may close the stream mid-fuzz; expected
        time.sleep(0.5)

        self.assertTrue(self._thread.is_alive(), "the node loop must survive the fuzz")
        fresh = RawPeer(self.good_id, self.coord_identity.node_id)
        self.addCleanup(fresh.stop)
        self.bind(fresh)
        fresh.send_craft(MessageType.PING, {"nonce": "post-fuzz"})
        self.assertIsNotNone(fresh.wait_for(lambda e: e.type is MessageType.PONG, 10.0))

    def test_node_process_survives_abuse(self) -> None:
        port = _free_port()
        config = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "data_dir": str(self.coord_dir),
                "peers": [self.abuser_id.node_id, self.good_id.node_id, self.client_id.node_id],
                "poll_interval_seconds": 1,
                "log_level": "WARNING",
                "log_file": str(self.coord_dir / "coordinator.log"),
            }
        )
        cfg_path = self.coord_dir / "coord.json"
        cfg_path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
        proc = subprocess.Popen(
            NODE_RUN + [str(cfg_path)],
            cwd=str(ROOT),
            stdout=open(self.coord_dir / "proc.log", "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(proc.wait, 10.0)
        self.addCleanup(proc.terminate)
        _wait_port("127.0.0.1", port)

        # Abuse the real process over a raw socket.
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            for _ in range(200):
                try:
                    sock.sendall(struct.pack(">I", 3) + b"{x}")
                except OSError:
                    break
        finally:
            sock.close()
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "the node process must still be alive after the abuse")

        # And it still serves a real client.
        client_cfg = from_dict(
            {
                "role": "client",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.root / "client"),
                "peers": [self.coord_identity.node_id],
                "coordinator_node_id": self.coord_identity.node_id,
                "coordinator_addr": f"127.0.0.1:{port}",
                "log_level": "WARNING",
            }
        )
        (self.root / "client").mkdir(exist_ok=True)
        client = Node(client_cfg)
        try:
            pong = client.client_ping(timeout=15.0)
        finally:
            client.shutdown()
        self.assertTrue(pong["pong"], "the abused process must still answer a real client")
