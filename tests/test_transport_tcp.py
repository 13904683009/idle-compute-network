"""Tests for idle_compute.transport_tcp (pure-stdlib TcpTransport).

Covers the NetTransport contract end to end: in-process echo over a real
TCP socket pair with rendezvous-file dialing, a real subprocess echo
peer (tests/helpers/tcp_smoke.py), raw relay of an oversized 100 KB
frame, the error paths (unknown peer, closed port, malformed address),
stop() idempotence and thread safety, uncorrupted concurrent sends from
two threads, and one handler thread per accepted connection.

All sockets bind to 127.0.0.1 (loopback only).
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable

from idle_compute.net_transport import (
    TransportError,
    read_rendezvous_file,
    write_rendezvous_file,
)
from idle_compute.protocol import encode_frame, read_frame, recv_exact
from idle_compute.transport_tcp import TcpTransport

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
SMOKE_SCRIPT = REPO_ROOT / "tests" / "helpers" / "tcp_smoke.py"

Read = Callable[[int], bytes]
Write = Callable[[bytes], None]
Handler = Callable[[str, Read, Write], None]


def wait_until(condition: Callable[[], bool], timeout: float = 15.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout:.1f}s waiting for {what}")


def make_echo_handler(records: list | None = None) -> Handler:
    """Read protocol frames, optionally record them, echo each back."""
    def on_stream(peer_id: str, read: Read, write: Write) -> None:
        while True:
            frame = read_frame(read)
            if frame is None:
                return
            if records is not None:
                records.append(frame)
            write(encode_frame(frame))
    return on_stream


def make_collector(records: list) -> Handler:
    """Read protocol frames and record them; never writes (no echo loop)."""
    def on_stream(peer_id: str, read: Read, write: Write) -> None:
        while True:
            frame = read_frame(read)
            if frame is None:
                return
            records.append(frame)
    return on_stream


def make_raw_echo_handler() -> Handler:
    """Echo 4-byte-length-prefixed frames WITHOUT any size cap.

    Deliberately not using protocol framing: this is what proves the
    transport relays oversized frames raw (the cap lives in protocol.py).
    """
    def on_stream(peer_id: str, read: Read, write: Write) -> None:
        header = recv_exact(read, 4)
        if header is None:
            return
        (length,) = struct.unpack(">I", header)
        body = recv_exact(read, length)
        if body is None:
            return
        write(header + body)
    return on_stream


def make_raw_collector(records: list) -> Handler:
    """Record uncapped length-prefixed frames; never writes."""
    def on_stream(peer_id: str, read: Read, write: Write) -> None:
        header = recv_exact(read, 4)
        if header is None:
            return
        (length,) = struct.unpack(">I", header)
        body = recv_exact(read, length)
        if body is None:
            return
        records.append(body)
    return on_stream


def free_closed_port() -> int:
    """A 127.0.0.1 port that is currently closed (best effort)."""
    probe = TcpTransport("port-probe")
    probe.start()
    port = int(probe.advertised_addrs()[0].rsplit(":", 1)[1])
    probe.stop()
    return port


class TcpTransportTests(unittest.TestCase):
    def test_advertised_addrs_lifecycle_and_local_peer_id(self) -> None:
        t = TcpTransport("node-x", listen_port=0)
        self.addCleanup(t.stop)
        self.assertEqual(t.local_peer_id, "node-x")
        self.assertEqual(t.advertised_addrs(), [])  # nothing before start()
        t.start()
        addrs = t.advertised_addrs()
        self.assertEqual(len(addrs), 1)
        host, port_text = addrs[0].rsplit(":", 1)
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(int(port_text), 0)  # port 0 became OS-assigned
        t.stop()
        self.assertEqual(t.advertised_addrs(), [])

    def test_rendezvous_dial_echo_both_directions(self) -> None:
        a_records: list = []
        b_records: list = []
        a = TcpTransport("node-a")
        b = TcpTransport("node-b")
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        a.set_handler(make_echo_handler(a_records))
        b.set_handler(make_echo_handler(b_records))
        a.start()
        b.start()
        with tempfile.TemporaryDirectory() as tmp:
            rdir = Path(tmp)
            write_rendezvous_file(rdir / "a.addr", "node-a", a.advertised_addrs()[0])
            write_rendezvous_file(rdir / "b.addr", "node-b", b.advertised_addrs()[0])
            info_a = read_rendezvous_file(rdir / "a.addr")
            info_b = read_rendezvous_file(rdir / "b.addr")
            self.assertEqual(info_a["node_id"], "node-a")
            b.dial(info_a["node_id"], [info_a["addr"]])
            a.dial(info_b["node_id"], [info_b["addr"]])
            # B -> A, echoed by A and read back by B on its outbound conn.
            b.send("node-a", encode_frame(b"hello-from-b"))
            wait_until(lambda: b"hello-from-b" in b_records, what="B sees its echo")
            self.assertIn(b"hello-from-b", a_records)
            # A -> B, echoed by B and read back by A on its outbound conn.
            a.send("node-b", encode_frame(b"hello-from-a"))
            wait_until(lambda: b"hello-from-a" in a_records, what="A sees its echo")
            self.assertIn(b"hello-from-a", b_records)

    def test_dial_uses_first_reachable_addr(self) -> None:
        dead = free_closed_port()
        a = TcpTransport("node-a")
        b = TcpTransport("node-b")
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        a.set_handler(make_echo_handler())
        b.set_handler(make_collector([]))
        a.start()
        b.start()
        addrs = [f"127.0.0.1:{dead}", a.advertised_addrs()[0]]
        b.dial("node-a", addrs)  # must skip the dead port, connect to the live one
        b.send("node-a", encode_frame(b"reachable"))  # must not raise

    def test_send_unknown_peer_raises(self) -> None:
        b = TcpTransport("node-b")
        self.addCleanup(b.stop)
        b.start()
        with self.assertRaises(TransportError):
            b.send("nobody", b"data")
        never = TcpTransport("never-started")
        with self.assertRaises(TransportError):
            never.send("nobody", b"data")

    def test_dial_errors(self) -> None:
        dead = free_closed_port()
        b = TcpTransport("node-b")
        self.addCleanup(b.stop)
        b.start()
        with self.assertRaises(TransportError):
            b.dial("ghost", [f"127.0.0.1:{dead}"])  # closed port
        with self.assertRaises(TransportError):
            b.dial("ghost", [])  # no addresses
        with self.assertRaises(TransportError):
            b.dial("ghost", ["not-an-address"])
        with self.assertRaises(TransportError):
            b.dial("ghost", ["127.0.0.1:99999"])  # port out of range
        never = TcpTransport("never-started")
        with self.assertRaises(TransportError):
            never.dial("ghost", ["127.0.0.1:1"])  # before start()

    def test_stop_idempotent_and_thread_safe(self) -> None:
        never = TcpTransport("never-started")
        never.stop()
        never.stop()  # safe before start()
        a = TcpTransport("node-a")
        b = TcpTransport("node-b")
        self.addCleanup(b.stop)
        a.set_handler(make_echo_handler())
        b.set_handler(make_echo_handler())
        a.start()
        b.start()
        b.dial("node-a", a.advertised_addrs())
        b.send("node-a", encode_frame(b"ping"))
        errors: list = []
        barrier = threading.Barrier(3)

        def stopper() -> None:
            try:
                barrier.wait(timeout=10)
                a.stop()
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=stopper, name="stopper-1")
        t2 = threading.Thread(target=stopper, name="stopper-2")
        t1.start()
        t2.start()
        barrier.wait(timeout=10)
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(errors, [])
        a.stop()
        a.stop()  # still idempotent afterwards
        self.assertEqual(a.advertised_addrs(), [])
        with self.assertRaises(TransportError):
            a.send("node-b", b"x")
        with self.assertRaises(TransportError):
            a.dial("node-b", b.advertised_addrs())
        a.start()  # restart after stop() works
        self.assertEqual(len(a.advertised_addrs()), 1)
        a.stop()
        b.stop()

    def test_concurrent_sends_uncorrupted(self) -> None:
        n_threads, per_thread = 2, 25
        a = TcpTransport("node-a")
        b = TcpTransport("node-b")
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        received: list = []
        a.set_handler(make_echo_handler())
        b.set_handler(make_collector(received))
        a.start()
        b.start()
        b.dial("node-a", a.advertised_addrs())

        def payload(tid: int, seq: int) -> bytes:
            return f"t{tid}-s{seq}".encode() + bytes([tid, seq]) * 31

        expected = {payload(tid, seq) for tid in range(n_threads) for seq in range(per_thread)}
        errors: list = []
        barrier = threading.Barrier(n_threads + 1)

        def sender(tid: int) -> None:
            try:
                barrier.wait(timeout=10)
                for seq in range(per_thread):
                    b.send("node-a", encode_frame(payload(tid, seq)))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=sender, args=(tid,), name=f"sender-{tid}")
            for tid in range(n_threads)
        ]
        for th in threads:
            th.start()
        barrier.wait(timeout=10)
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(errors, [])
        wait_until(
            lambda: len(received) == n_threads * per_thread,
            timeout=30,
            what="all echoes received",
        )
        self.assertEqual(set(received), expected)  # nothing lost or mangled

    def test_second_accepted_conn_gets_own_handler_thread(self) -> None:
        a = TcpTransport("node-a")
        b = TcpTransport("node-b")
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        arrived: list = []
        release = threading.Event()

        def on_stream(peer_id: str, read: Read, write: Write) -> None:
            frame = read_frame(read)
            if frame is None:
                return
            arrived.append((threading.get_ident(), frame))
            release.wait(timeout=10)  # both handler threads park here together
            write(encode_frame(frame))

        a.set_handler(on_stream)
        received: list = []
        b.set_handler(make_collector(received))
        a.start()
        b.start()
        addr = a.advertised_addrs()[0]
        # Two TCP connections from the same process to the same listener,
        # under two different peer ids (peer_id is the pool key).
        b.dial("a-conn-1", [addr])
        b.dial("a-conn-2", [addr])
        b.send("a-conn-1", encode_frame(b"first"))
        b.send("a-conn-2", encode_frame(b"second"))
        wait_until(lambda: len(arrived) == 2, what="both handler threads arrived")
        idents = {ident for ident, _ in arrived}
        self.assertEqual(len(idents), 2, f"expected 2 distinct handler threads, got {arrived}")
        release.set()
        wait_until(lambda: len(received) == 2, what="both echoes received")
        self.assertEqual(set(received), {b"first", b"second"})

    def test_oversized_frame_relayed_raw(self) -> None:
        # 100 KB: deliberately larger than protocol.MAX_FRAME_BYTES (64 KB)
        # to prove the transport itself imposes no cap.
        payload = bytes(range(256)) * 390 + bytes(range(160))
        self.assertEqual(len(payload), 100_000)
        a = TcpTransport("node-a")
        b = TcpTransport("node-b")
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        received: list = []
        a.set_handler(make_raw_echo_handler())
        b.set_handler(make_raw_collector(received))
        a.start()
        b.start()
        b.dial("node-a", a.advertised_addrs())
        b.send("node-a", struct.pack(">I", len(payload)) + payload)
        wait_until(lambda: len(received) == 1, timeout=30, what="100 KB echo")
        self.assertEqual(received[0], payload)

    def test_real_subprocess_echo(self) -> None:
        if not VENV_PYTHON.is_file():
            self.skipTest(f"venv python not found: {VENV_PYTHON}")
        proc = subprocess.Popen(
            [str(VENV_PYTHON), str(SMOKE_SCRIPT), "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(REPO_ROOT),
            bufsize=1,
        )
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(
                line.startswith("READY "), f"smoke helper did not report ready: {line!r}"
            )
            port = int(line.split()[1])
            t = TcpTransport("smoke-client")
            self.addCleanup(t.stop)
            received: list = []
            got = threading.Event()

            def on_stream(peer_id: str, read: Read, write: Write) -> None:
                try:
                    frame = read_frame(read)
                except Exception:
                    received.append(None)
                    got.set()
                    return
                received.append(frame)
                got.set()

            t.set_handler(on_stream)
            t.start()
            t.dial("smoke-server", [f"127.0.0.1:{port}"])
            payload = b"real subprocess smoke ping"
            t.send("smoke-server", encode_frame(payload))
            self.assertTrue(got.wait(timeout=15), "no echo from smoke helper")
            self.assertEqual(received, [payload])
            t.stop()
            exit_code = proc.wait(timeout=15)
            self.assertEqual(exit_code, 0, "smoke helper exited non-zero")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
