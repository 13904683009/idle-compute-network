"""F-3: wire-boundary input validation + registry-only dispatch.

Hostile inputs are sent as REAL frames to a REAL coordinator and the executor
is instrumented: nothing may reach it. The registry is the only dispatch point,
and the source-safety scan (tests/test_source_safety.py) pins the import and
capability rules that keep it that way.
"""

from __future__ import annotations

import json
import struct
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from idle_compute.config import from_dict
from idle_compute.identity import NodeIdentity
from idle_compute.node import Node
from idle_compute.protocol import ErrorCode, MessageType
from idle_compute.tasks import NumericBatchTask

try:  # discover runs with tests/ on sys.path; `tests.test_x` uses the package
    from helpers.peers import RawPeer
except ImportError:  # pragma: no cover - import style depends on the runner
    from tests.helpers.peers import RawPeer

MAX_ATTEMPTS = 3
LEASE_SECONDS = 30


def _task_body(client_ref: str, task_type: str, payload, numbers=None) -> dict:
    return {
        "client_ref": client_ref,
        "task_type": task_type,
        "payload": payload,
        "max_attempts": MAX_ATTEMPTS,
        "lease_seconds": LEASE_SECONDS,
    }


class WireBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="wire-boundary-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord_dir = self.root / "coord"
        self.coord_dir.mkdir(parents=True, exist_ok=True)
        self.coord_identity = NodeIdentity.load_or_create(
            self.coord_dir / "identity.key", node_name="coordinator"
        )
        self.hostile_id = NodeIdentity.load_or_create(
            self.root / "hostile" / "identity.key", node_name="hostile"
        )
        self._start_node()

    def _start_node(self) -> None:
        config = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.coord_dir),
                "peers": [self.hostile_id.node_id],
                "poll_interval_seconds": 1,
                "log_level": "WARNING",
                "log_file": str(self.coord_dir / "coordinator.log"),
            }
        )
        self.node = Node(config)
        self.addCleanup(self.node.shutdown)
        self._thread = threading.Thread(target=self.node.run, name="boundary-node", daemon=True)
        self._thread.start()
        self.addCleanup(self._thread.join, 10.0)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.node.transport.advertised_addrs():
            time.sleep(0.05)
        self.addr = self.node.transport.advertised_addrs()[0]

    def _peer(self) -> RawPeer:
        peer = RawPeer(self.hostile_id, self.coord_identity.node_id)
        self.addCleanup(peer.stop)
        peer.dial(self.addr)
        peer.hello()
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.HELLO_ACK, 15.0))
        return peer

    # ------------------------------------------------------------------ tests

    def test_hostile_payloads_never_reach_the_executor(self) -> None:
        peer = self._peer()
        calls: list = []
        original = NumericBatchTask.execute

        def counting_execute(payload):
            calls.append(payload)
            return original(payload)

        with mock.patch.object(NumericBatchTask, "execute", staticmethod(counting_execute)):
            cases = {
                "too-many-numbers": _task_body("c1", "numeric_batch_v1", {"numbers": list(range(5000))}),
                "bool-not-int": _task_body("c2", "numeric_batch_v1", {"numbers": [1, True, 3]}),
                "float-not-int": _task_body("c3", "numeric_batch_v1", {"numbers": [1.5]}),
                "string-not-int": _task_body("c4", "numeric_batch_v1", {"numbers": ["1; rm -rf /"]}),
                "extra-payload-key": _task_body(
                    "c5", "numeric_batch_v1", {"numbers": [1], "shell": "rm -rf /"}
                ),
                "nested-list": _task_body("c6", "numeric_batch_v1", {"numbers": [[[1]]]}),
                "non-whitelisted-type": _task_body("c7", "__import__('os')", {"numbers": [1]}),
                "shell-type": _task_body("c8", "numeric_batch_v1; rm -rf /", {"numbers": [1]}),
            }
            for ref, body in cases.items():
                peer.send_craft(MessageType.TASK_CREATE, body)
                error = peer.wait_for(
                    lambda e, r=ref: e.type is MessageType.ERROR, 10.0
                )
                self.assertIsNotNone(error, f"{ref}: hostile task must be rejected")
                self.assertIn(
                    error.body["code"],
                    {ErrorCode.INVALID_BODY.value, ErrorCode.UNSUPPORTED_TYPE.value},
                    f"{ref}: unexpected code {error.body['code']}",
                )
            self.assertEqual(calls, [], f"the executor must never run hostile input: {calls}")

            # Positive control: the instrumentation is live.
            NumericBatchTask.execute({"numbers": [1, 2]})
            self.assertEqual(len(calls), 1)

        # The node still serves a valid task after all that.
        peer.send_craft(MessageType.TASK_CREATE, _task_body("valid", "numeric_batch_v1", {"numbers": [1, 2]}))
        ack = peer.wait_for(lambda e: e.type is MessageType.TASK_CREATE_ACK, 10.0)
        self.assertIsNotNone(ack, "a valid task must still be accepted")
        self.assertEqual(ack.body["client_ref"], "valid")

    def test_unknown_message_type_is_rejected_by_the_closed_registry(self) -> None:
        peer = self._peer()
        raw = json.dumps(
            {
                "v": 2,
                "type": "TASK_PUBLISH",
                "sender": peer.identity.node_id,
                "recipient": self.coord_identity.node_id,
                "seq": 999,
                "ts": "2026-11-04T00:00:00+00:00",
                "in_reply_to": None,
                "body": {},
                "signature": "0" * 128,
            }
        ).encode("utf-8")
        peer.send(struct.pack(">I", len(raw)) + raw)
        # The frame is rejected (an unsigned frame fails signature verification
        # before the registry stage, which is the documented order), and the
        # closed registry itself is pinned directly below.
        error = peer.wait_for(lambda e: e.type is MessageType.ERROR, 10.0)
        self.assertIsNotNone(error, "an unknown type must be rejected")
        # Type decode can fail as MALFORMED_ENVELOPE (shape stage) or
        # UNSUPPORTED_TYPE (registry stage) depending on where the decoder
        # rejects the unknown label; BAD_SIGNATURE is also acceptable because
        # the frame is deliberately unsigned. Any rejection is fine here.
        self.assertIn(
            error.body["code"],
            {
                ErrorCode.UNSUPPORTED_TYPE.value,
                ErrorCode.BAD_SIGNATURE.value,
                ErrorCode.MALFORMED_ENVELOPE.value,
            },
        )

        # Registry-level assertion, independent of signature ordering.
        from idle_compute.protocol import ProtocolError, parse_message

        with self.assertRaises(ProtocolError) as ctx:
            parse_message(
                raw,
                transport_peer_id=None,
                verifier=lambda payload, sig, sender: True,
                seq_window=None,
            )
        self.assertIn(
            ctx.exception.code,
            {ErrorCode.UNSUPPORTED_TYPE, ErrorCode.MALFORMED_ENVELOPE},
            "the closed registry must reject an unknown message type",
        )

        peer.send_craft(MessageType.PING, {"nonce": "after-unknown-type"})
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.PONG, 10.0))

    def test_deeply_nested_json_is_dropped_without_reaching_the_node(self) -> None:
        peer = self._peer()
        nested = "1"
        for _ in range(20):
            nested = "[" + nested + "]"
        raw = ('{"v":2,"type":"PING","sender":"%s","recipient":"%s","seq":77,'
               '"ts":"2026-11-04T00:00:00+00:00","in_reply_to":null,'
               '"body":{"nonce":%s},"signature":"%s"}' % (
                   peer.identity.node_id, self.coord_identity.node_id, nested, "0" * 128
               )).encode("utf-8")
        peer.send(struct.pack(">I", len(raw)) + raw)
        # A frame that cannot be depth-parsed is dropped without an ERROR
        # reply: `_on_parse_error` re-parses the raw frame to address the reply
        # and refuses to guess when that fails. The invariants that matter are
        # "no processing" and "the node keeps serving".
        time.sleep(0.5)
        self.assertIsNone(
            peer.wait_for(lambda e: e.type is MessageType.PONG, 1.0),
            "a depth-bomb frame must not be processed as a PING",
        )
        self.assertTrue(self._thread.is_alive())
        peer.send_craft(MessageType.PING, {"nonce": "after-depth"})
        self.assertIsNotNone(peer.wait_for(lambda e: e.type is MessageType.PONG, 10.0))
