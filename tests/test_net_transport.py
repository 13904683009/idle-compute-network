"""C-1: Libp2pTransport — real two-node loopback over a rendezvous file,
subprocess smoke test, error paths, oversized raw relay, concurrent sends,
and libp2p import containment.

The transport relays bytes only (protocol.py owns framing), so every echo
handler here speaks the wire frame format: ``read_frame`` (4-byte BE length
header + body) and ``encode_frame``. The oversized test deliberately bypasses
framing to prove the transport does NOT enforce the 64 KiB cap itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from idle_compute.identity import NodeIdentity, is_valid_node_id
from idle_compute.net_transport import (
    NETWORK_PROTOCOL_ID,
    Libp2pTransport,
    TransportError,
    read_rendezvous_file,
    write_rendezvous_file,
)
from idle_compute.protocol import encode_frame, read_frame

PACKAGE = Path(__file__).resolve().parent.parent / "idle_compute"
REPO_ROOT = PACKAGE.parent
HELPER = Path(__file__).resolve().parent / "helpers" / "transport_smoke.py"
LOOPBACK = "/ip4/127.0.0.1/tcp/0"  # port 0 = OS-assigned

def suppress_posix_warning() -> None:
    # Python 3.12 unittest (TextTestRunner.run) inserts a 'default' filter in
    # FRONT of module-level filters, so this is re-asserted from setUpClass /
    # setUp too. The smoke child (no unittest) relies on its own module call.
    warnings.filterwarnings(
        "ignore", message="POSIX 0600 permissions cannot be enforced on Windows"
    )


suppress_posix_warning()


def wait_until(predicate, timeout=20.0, message="") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message or f"condition not met within {timeout}s")


def make_loop_echo(sink: list) -> callable:
    """Handler: read one wire frame, record (peer_id, body), echo it back
    in place; loops until the stream dies so it serves any number of frames
    and both echo directions with a single handler."""

    def handler(peer_id: str, read, write) -> None:
        while True:
            try:
                body = read_frame(read)
            except Exception:
                return
            if body is None:
                return
            sink.append((peer_id, body))
            try:
                write(encode_frame(body))
            except Exception:
                return

    return handler


def make_record_only(sink: list) -> callable:
    """Handler: read one wire frame, record (peer_id, body), never echo.

    Used on the dialer side now that dialed outbound streams are handler-
    served like inbound ones (contract symmetry with TcpTransport).
    """

    def handler(peer_id: str, read, write) -> None:
        while True:
            try:
                body = read_frame(read)
            except Exception:
                return
            if body is None:
                return
            sink.append((peer_id, body))

    return handler


class TwoNodePairTests(unittest.TestCase):
    """Two REAL NodeIdentity instances + two REAL Libp2pTransport instances
    in one process, wired through rendezvous files on OS-assigned ports."""

    @classmethod
    def setUpClass(cls) -> None:
        suppress_posix_warning()
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.identity_a = NodeIdentity.load_or_create(
            root / "a" / "identity.key", node_name="a"
        )
        cls.identity_b = NodeIdentity.load_or_create(
            root / "b" / "identity.key", node_name="b"
        )
        cls.transport_a = Libp2pTransport(cls.identity_a.key_pair, [LOOPBACK])
        cls.transport_b = Libp2pTransport(cls.identity_b.key_pair, [LOOPBACK])
        cls.transport_a.start()
        cls.transport_b.start()
        addrs_a = cls.transport_a.advertised_addrs()
        addrs_b = cls.transport_b.advertised_addrs()
        if not addrs_a or not addrs_b:
            raise AssertionError(
                f"no advertised addrs after start: A={addrs_a!r} B={addrs_b!r}"
            )
        cls.addr_file_a = root / "a.addr"
        cls.addr_file_b = root / "b.addr"
        write_rendezvous_file(
            cls.addr_file_a, cls.transport_a.local_peer_id, addrs_a[0]
        )
        write_rendezvous_file(
            cls.addr_file_b, cls.transport_b.local_peer_id, addrs_b[0]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.transport_a.stop()
        cls.transport_b.stop()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        # Fresh outbound streams per test: loop-echo handler threads from the
        # previous test deliberately keep their old streams open forever, so
        # each test starts on a new stream.
        self.transport_a._streams.clear()
        self.transport_b._streams.clear()

    def test_rendezvous_roundtrip_bidirectional_echo_and_identity(self) -> None:
        ta, tb = self.transport_a, self.transport_b
        seen_a: list = []
        seen_b: list = []
        # Dialer side records replies through its own handler (handler-served
        # outbound streams); responder side echoes once. Record-only on the
        # dialer prevents an echo storm between two loop-echo handlers.
        ta.set_handler(make_record_only(seen_a))
        tb.set_handler(make_loop_echo(seen_b))
        # Local identity: transport peer id IS the identity's node id.
        self.assertEqual(ta.local_peer_id, self.identity_a.node_id)
        self.assertEqual(tb.local_peer_id, self.identity_b.node_id)
        self.assertTrue(is_valid_node_id(ta.local_peer_id))
        # Rendezvous round trip: write -> read -> dial -> send.
        info_b = read_rendezvous_file(self.addr_file_b)
        self.assertEqual(info_b["node_id"], tb.local_peer_id)
        self.assertEqual(info_b["protocol"], NETWORK_PROTOCOL_ID)
        ta.dial(tb.local_peer_id, [info_b["addr"]])
        ta.send(tb.local_peer_id, encode_frame(b"ping-from-a"))
        wait_until(lambda: bool(seen_a), message="A's handler never saw the echo")
        wait_until(lambda: bool(seen_b), message="B never observed the inbound stream")
        # Identity binding: B's inbound handler saw A's node id.
        self.assertEqual(seen_b[0][0], ta.local_peer_id)
        self.assertEqual(seen_b[0][1], b"ping-from-a")
        # A's own handler saw B's echo on A's dialed stream.
        self.assertEqual(seen_a[0][0], tb.local_peer_id)
        self.assertEqual(seen_a[0][1], b"ping-from-a")
        # Reverse direction through A's rendezvous file.
        info_a = read_rendezvous_file(self.addr_file_a)
        self.assertEqual(info_a["node_id"], ta.local_peer_id)
        tb.dial(ta.local_peer_id, [info_a["addr"]])
        tb.send(ta.local_peer_id, encode_frame(b"pong-from-b"))
        wait_until(lambda: len(seen_a) >= 2, message="A never observed the reverse frame")
        # Identity binding in the other direction.
        self.assertEqual(seen_a[-1][0], tb.local_peer_id)
        self.assertEqual(seen_a[-1][1], b"pong-from-b")

    def test_outbound_stream_served_by_own_handler(self) -> None:
        """Contract symmetry with TcpTransport: the dialer's own handler
        receives replies on ITS dialed stream (no reverse dial, no
        private-API reads) — the node layer's request/reply pattern."""
        ta, tb = self.transport_a, self.transport_b
        seen_a: list = []
        tb.set_handler(make_loop_echo([]))
        ta.set_handler(make_record_only(seen_a))
        info_b = read_rendezvous_file(self.addr_file_b)
        ta.dial(tb.local_peer_id, [info_b["addr"]])
        ta.send(tb.local_peer_id, encode_frame(b"request-from-a"))
        # A's own handler (fresh thread on the dialed stream) observes the
        # echo — exactly how a node reads its peer's replies.
        wait_until(lambda: bool(seen_a), message="A's handler never saw the echo")
        self.assertEqual(seen_a[0][0], tb.local_peer_id)
        self.assertEqual(seen_a[0][1], b"request-from-a")

    def test_start_twice_is_noop_and_transport_keeps_working(self) -> None:
        self.transport_a.start()
        self.transport_a.start()
        addrs = self.transport_a.advertised_addrs()
        self.assertTrue(addrs)
        self.assertTrue(all(a.startswith("/ip4/127.0.0.1/tcp/") for a in addrs))
        seen: list = []
        self.transport_b.set_handler(make_loop_echo([]))
        self.transport_a.set_handler(make_record_only(seen))
        self.transport_a.dial(
            self.transport_b.local_peer_id, self.transport_b.advertised_addrs()
        )
        self.transport_a.send(
            self.transport_b.local_peer_id, encode_frame(b"after-noop-start")
        )
        wait_until(lambda: bool(seen), message="echo never reached A's handler")
        self.assertEqual(seen[0][1], b"after-noop-start")

    def test_oversized_100kb_frame_relayed_raw(self) -> None:
        # 100 KB exceeds protocol's 64 KiB cap: the transport must relay it
        # raw and NOT enforce the cap (the node layer does).
        payload = os.urandom(100 * 1024)
        seen: list = []

        def raw_echo(peer_id: str, read, write) -> None:
            got = b""
            while len(got) < len(payload):
                chunk = read(len(payload) - len(got))
                if not chunk:
                    break
                got += chunk
            seen.append((peer_id, got))
            if got:
                write(got)

        seen_a: list = []

        def record_all(peer_id: str, read, write) -> None:
            got = b""
            while len(got) < len(payload):
                chunk = read(len(payload) - len(got))
                if not chunk:
                    break
                got += chunk
            seen_a.append((peer_id, got))

        self.transport_b.set_handler(raw_echo)
        self.transport_a.set_handler(record_all)
        self.transport_a.dial(
            self.transport_b.local_peer_id, self.transport_b.advertised_addrs()
        )
        self.transport_a.send(self.transport_b.local_peer_id, payload)
        wait_until(lambda: bool(seen_a), message="echo never reached A's handler")
        self.assertEqual(seen_a[0][0], self.transport_b.local_peer_id)
        self.assertEqual(seen_a[0][1], payload)
        wait_until(lambda: bool(seen), message="B never received the 100KB frame")
        self.assertEqual(seen[0][0], self.transport_a.local_peer_id)
        self.assertEqual(seen[0][1], payload)

    def test_concurrent_sends_from_two_threads_are_uncorrupted(self) -> None:
        seen_b: list = []
        seen_a: list = []
        self.transport_b.set_handler(make_loop_echo(seen_b))
        self.transport_a.set_handler(make_record_only(seen_a))
        b_id = self.transport_b.local_peer_id
        self.transport_a.dial(b_id, self.transport_b.advertised_addrs())
        warm = b"warmup-frame"
        self.transport_a.send(b_id, encode_frame(warm))
        payloads = [b"A" * 16000, b"B" * 16000]
        errors: list = []

        def worker(payload: bytes) -> None:
            try:
                self.transport_a.send(b_id, encode_frame(payload))
            except Exception as error:  # noqa: BLE001 - recorded for the assert
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        wait_until(lambda: len(seen_a) >= 3, message="A's handler did not see all three echoes")
        self.assertEqual(sorted(body for _, body in seen_a), sorted([warm] + payloads))
        wait_until(lambda: len(seen_b) >= 3, message="B did not observe all three frames")
        self.assertEqual(seen_b[0][1], warm)
        self.assertEqual(sorted(body for _, body in seen_b[1:3]), sorted(payloads))

    def test_outbound_stream_reused_per_peer(self) -> None:
        seen_a: list = []
        self.transport_b.set_handler(make_loop_echo([]))
        self.transport_a.set_handler(make_record_only(seen_a))
        b_id = self.transport_b.local_peer_id
        self.transport_a.dial(b_id, self.transport_b.advertised_addrs())
        self.transport_a.send(b_id, encode_frame(b"first"))
        wait_until(lambda: len(seen_a) >= 1, message="first echo never arrived")
        stream_one = self.transport_a._streams[b_id]
        self.transport_a.send(b_id, encode_frame(b"second"))
        wait_until(lambda: len(seen_a) >= 2, message="second echo never arrived")
        stream_two = self.transport_a._streams[b_id]
        self.assertIs(stream_one, stream_two)
        self.assertEqual([body for _, body in seen_a], [b"first", b"second"])


class TransportErrorTests(unittest.TestCase):
    """Error paths and lifecycle edge cases (own transport instances)."""

    @classmethod
    def setUpClass(cls) -> None:
        suppress_posix_warning()
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.identity = NodeIdentity.load_or_create(
            root / "e" / "identity.key", node_name="e"
        )
        cls.other_identity = NodeIdentity.load_or_create(
            root / "o" / "identity.key", node_name="o"
        )
        cls.transport = Libp2pTransport(cls.identity.key_pair, [LOOPBACK])
        cls.transport.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.transport.stop()
        cls._tmp.cleanup()

    def test_send_to_unknown_peer_raises_transport_error(self) -> None:
        # A valid node id that was never dialed and never started anywhere.
        unknown = self.other_identity.node_id
        started = time.monotonic()
        with self.assertRaises(TransportError):
            self.transport.send(unknown, b"hello")
        self.assertLess(time.monotonic() - started, 10.0)

    def test_dial_dead_address_raises_transport_error(self) -> None:
        # Port 1 needs admin rights to bind: effectively always closed.
        with self.assertRaises(TransportError):
            self.transport.dial(
                self.other_identity.node_id, ["/ip4/127.0.0.1/tcp/1"]
            )

    def test_dial_malformed_peer_id_raises_transport_error(self) -> None:
        with self.assertRaises(TransportError):
            self.transport.dial("not-a-peer-id", [LOOPBACK])

    def test_unstarted_transport_rejects_send_and_dial(self) -> None:
        fresh = Libp2pTransport(self.other_identity.key_pair, [LOOPBACK])
        self.assertEqual(fresh.local_peer_id, self.other_identity.node_id)
        self.assertEqual(fresh.advertised_addrs(), [])
        with self.assertRaises(TransportError):
            fresh.send(self.identity.node_id, b"data")
        with self.assertRaises(TransportError):
            fresh.dial(self.identity.node_id, [LOOPBACK])

    def test_missing_key_pair_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Libp2pTransport(None, [LOOPBACK])  # type: ignore[arg-type]

    def test_trio_version_pin_enforced(self) -> None:
        fresh = Libp2pTransport(self.other_identity.key_pair, [LOOPBACK])
        with mock.patch("trio.__version__", "0.29.1"):
            with self.assertRaises(RuntimeError):
                fresh.start()

    def test_stop_is_idempotent_from_any_thread(self) -> None:
        transport = Libp2pTransport(self.other_identity.key_pair, [LOOPBACK])
        transport.start()
        self.assertTrue(transport.advertised_addrs())
        # stop() from a foreign thread first, then again from the main thread.
        stopped = threading.Event()

        def stopper() -> None:
            transport.stop()
            stopped.set()

        thread = threading.Thread(target=stopper)
        thread.start()
        thread.join(timeout=15)
        self.assertTrue(stopped.is_set(), "stop() from a worker thread hung")
        transport.stop()
        transport.stop()
        self.assertEqual(transport.advertised_addrs(), [])
        with self.assertRaises(TransportError):
            transport.send(self.identity.node_id, b"after-stop")

    def test_stop_before_start_is_a_noop(self) -> None:
        fresh = Libp2pTransport(self.other_identity.key_pair, [LOOPBACK])
        fresh.stop()
        fresh.stop()


class SubprocessSmokeTests(unittest.TestCase):
    """One REAL subprocess: the helper starts a transport on an OS-assigned
    loopback port, publishes a rendezvous file and echoes; this process
    dials it, sends and receives."""

    def setUp(self) -> None:
        suppress_posix_warning()

    def _kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_real_subprocess_echo_over_rendezvous_file(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        rendezvous_path = root / "smoke.addr"
        proc = subprocess.Popen(
            [sys.executable, str(HELPER), str(rendezvous_path)],
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._kill, proc)
        lines: list[str] = []

        def pump() -> None:
            for raw in proc.stdout:
                lines.append(raw.strip())

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_thread.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not lines and proc.poll() is None:
            time.sleep(0.05)
        if not lines:
            stderr_tail = proc.stderr.read() if proc.poll() is not None else ""
            self.fail(
                f"smoke child produced no READY line "
                f"(rc={proc.poll()}, stderr={stderr_tail!r})"
            )
        self.assertEqual(lines[0].split()[0], "READY")
        node_id = lines[0].split()[1]
        self.assertTrue(is_valid_node_id(node_id))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not rendezvous_path.exists():
            time.sleep(0.05)
        info = read_rendezvous_file(rendezvous_path)
        self.assertEqual(info["node_id"], node_id)
        self.assertTrue(info["addr"].startswith("/ip4/127.0.0.1/tcp/"))
        # Parent transport in THIS process dials the child and exchanges.
        identity = NodeIdentity.load_or_create(
            root / "parent" / "identity.key", node_name="parent"
        )
        transport = Libp2pTransport(identity.key_pair, [LOOPBACK])
        received: list = []

        def record(peer_id: str, read, write) -> None:
            try:
                body = read_frame(read)
            except Exception:
                return
            if body is not None:
                received.append((peer_id, body))

        transport.set_handler(record)
        transport.start()
        try:
            transport.dial(node_id, [info["addr"]])
            payload = b"smoke-ping-via-subprocess"
            transport.send(node_id, encode_frame(payload))
            wait_until(
                lambda: bool(received), timeout=20,
                message="parent never received the echo",
            )
            self.assertEqual(received[0][1], payload)
            # Identity binding across two real processes.
            self.assertEqual(received[0][0], node_id)
        finally:
            transport.stop()
        proc.stdin.close()  # helper exits on stdin EOF
        rc = proc.wait(timeout=30)
        self.assertEqual(rc, 0, f"smoke child exited with {rc}")
        pump_thread.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


class ImportContainmentTests(unittest.TestCase):
    """libp2p imports may appear ONLY in identity.py and net_transport.py."""

    def test_libp2p_imports_confined_to_sanctioned_modules(self) -> None:
        # Note: several package files mention "libp2p" in prose (docstrings,
        # error messages), so containment is enforced on actual import
        # statements — the guarantee that matters on the wire.
        sanctioned = {"identity.py", "net_transport.py"}
        offenders = []
        for path in sorted(PACKAGE.glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("import libp2p") or stripped.startswith(
                    "from libp2p"
                ):
                    offenders.append(path.name)
                    break
        self.assertEqual(sorted(set(offenders)), sorted(sanctioned))
        # The sanctioned modules must actually keep their libp2p imports.
        for name in sanctioned:
            text = (PACKAGE / name).read_text(encoding="utf-8")
            self.assertIn("libp2p", text, f"{name} no longer references libp2p")


if __name__ == "__main__":
    unittest.main()
