"""Shared raw-TCP peer helpers for wire-level tests.

Importable both ways the suite runs:
- ``python -m unittest discover -s tests`` puts ``tests/`` on sys.path;
- ``python -m unittest tests.test_x`` imports the namespace package ``tests``.

The peer speaks the real protocol (build/verify frames) against a real node;
it is deliberately not a Node object, so tests can drive malformed or hostile
bytes by hand.
"""

from __future__ import annotations

import threading
import time

from idle_compute.identity import NodeIdentity
from idle_compute.protocol import (
    MessageType,
    ProtocolError,
    SeqWindow,
    build_envelope,
    encode_envelope,
    parse_message,
    read_frame,
)
from idle_compute.transport_tcp import TcpTransport


class RawPeer:
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

    # ---------------------------------------------------------------- wire

    def dial(self, addr: str) -> None:
        self.transport.dial(self.coord_id, [addr])

    def craft(self, type_: MessageType, body: dict) -> bytes:
        self._seq += 1
        return encode_envelope(
            build_envelope(
                type_,
                self.identity.node_id,
                self.identity.sign,
                recipient=self.coord_id,
                seq=self._seq,
                body=body,
            )
        )

    def send(self, frame: bytes) -> None:
        self.transport.send(self.coord_id, frame)

    def send_craft(self, type_: MessageType, body: dict) -> bytes:
        frame = self.craft(type_, body)
        self.send(frame)
        return frame

    def hello(self) -> bytes:
        return self.send_craft(
            MessageType.HELLO,
            {"node_id": self.identity.node_id, "role": "worker", "listen_addrs": ["127.0.0.1:1"]},
        )

    # -------------------------------------------------------------- expect

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

    def expect_error(self, code: int, timeout: float = 10.0):
        return self.wait_for(
            lambda e: e.type is MessageType.ERROR and e.body["code"] == code, timeout
        )

    def wait_closed(self, timeout: float) -> bool:
        return self._closed.wait(timeout)

    def stop(self) -> None:
        self.transport.stop()
