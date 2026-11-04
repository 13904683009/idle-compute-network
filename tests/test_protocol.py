"""Tests for idle_compute.protocol: framing, envelope, validation pipeline.

Covers C-2 acceptance: 64KiB frame cap (rejected on the 4-byte header alone),
depth-16 JSON, verify-before-parse ordering (observable), closed type
registry, 16 error codes, seq window, timestamp tolerance, golden wire
vectors (deterministic serialization), and real-Ed25519 sign/verify.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock

from idle_compute.identity import NodeIdentity
from idle_compute.protocol import (
    BODY_SCHEMAS,
    ErrorCode,
    MessageType,
    ProtocolError,
    SeqWindow,
    build_envelope,
    encode_envelope,
    encode_frame,
    error_envelope,
    parse_json,
    parse_message,
    read_frame,
    recv_exact,
    signing_payload,
    validate_body,
)

# Fixed 52-char node-id-shaped strings (not real keypairs; shape only).
# Strict base58 alphabet: no 0, O, I, l.
SENDER = "12D3KooWAbCdEf1234GhJkMn5678PqRsTu9WXYZabcefgmnpqrst"
OTHER = "12D3KooWstuvwxyz123456789ABCDEFGHJKLMNPQRSTUVWXYZabc"
FIXED_TS = "2026-10-26T12:00:00.123456+00:00"
FIXED_BODY = {"node_id": SENDER, "role": "worker", "listen_addrs": ["/ip4/127.0.0.1/tcp/43123"]}

# Golden vectors: computed with the venv interpreter on 2026-10-26 for the
# fixed constants above (see logs/idle-compute/phase2-C-golden.log).
PAYLOAD_GOLDEN = "e4b73fd2b953e27f4ef1aaec12dde1083fbce5c5eb330e0e0ea75dbd2d912099"
WIRE_GOLDEN = (
    "000001bf7b22626f6479223a7b226c697374656e5f6164647273223a5b222f6970342f3132372e302e302e312f7463702f3433313233225d2c226e6f64655f6964223a22313244334b6f6f574162436445663132333447684a6b4d6e35363738507152735475395758595a6162636566676d6e7071727374222c22726f6c65223a22776f726b6572227d2c22696e5f7265706c795f746f223a6e756c6c2c22726563697069656e74223a6e756c6c2c2273656e646572223a22313244334b6f6f574162436445663132333447684a6b4d6e35363738507152735475395758595a6162636566676d6e7071727374222c22736571223a312c227369676e6174757265223a223731373334666532363265383466353162613963643936343762323436613235376131356461643166313665376233353364323935366634303538356465396437313733346665323632653834663531626139636439363437623234366132353761313564616431663136653762333533643239353666343035383564653964222c227473223a22323032362d31302d32365431323a30303a30302e3132333435362b30303a3030222c2274797065223a2248454c4c4f222c2276223a317d"
)


class FakeSigner:
    """Deterministic 128-hex signer for pipeline tests (not real crypto)."""

    def __init__(self, key: bytes = b"golden-key-0000") -> None:
        self.key = key

    def sign(self, payload: str) -> str:
        digest = hmac.new(self.key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest * 2  # 128 hex chars

    def verifier(self, _sender: str, payload: str, signature: str) -> bool:
        return signature == self.sign(payload)


FAKE = FakeSigner()


def make_reader(data: bytes, chunk: int | None = None):
    buf = bytearray(data)

    def read(n: int) -> bytes:
        if not buf:
            return b""
        take = n if chunk is None else min(n, chunk)
        out = bytes(buf[:take])
        del buf[:take]
        return out

    return buf, read


def frame_for(type_, body, sign=FAKE.sign, ts=None, seq=1, recipient=None, sender=SENDER, in_reply_to=None) -> bytes:
    env = build_envelope(
        type_, sender, sign, recipient=recipient, seq=seq, body=body, ts=ts, in_reply_to=in_reply_to
    )
    return encode_envelope(env)


def parse_frame(frame, transport_peer_id=SENDER, verifier=None, seq_window=None, now=None):
    _, read = make_reader(frame)
    return parse_message(
        read_frame(read),
        transport_peer_id=transport_peer_id,
        verifier=verifier or FAKE.verifier,
        seq_window=seq_window or SeqWindow(),
        now=now,
    )


def valid_body(type_: MessageType) -> dict:
    samples = {
        MessageType.HELLO: {"node_id": SENDER, "role": "worker", "listen_addrs": ["/ip4/127.0.0.1/tcp/43123"]},
        MessageType.HELLO_ACK: {"node_id": SENDER, "role": "coordinator", "listen_addrs": ["/ip4/127.0.0.1/tcp/43124"]},
        MessageType.PING: {"nonce": "ping-1"},
        MessageType.PONG: {"nonce": "ping-1"},
        MessageType.ERROR: {"code": 1, "message": "no", "ref_type": None, "ref_seq": None},
        MessageType.TASK_CREATE: {
            "client_ref": "c1",
            "task_type": "numeric_batch_v1",
            "payload": {"numbers": [1, 2, 3]},
            "max_attempts": 3,
            "lease_seconds": 60,
        },
        MessageType.TASK_CREATE_ACK: {"client_ref": "c1", "task_id": "t1"},
        MessageType.CLAIM_REQUEST: {"claim_request_id": "cr1", "task_id": None},
        MessageType.CLAIM_GRANT: {
            "claim_request_id": "cr1",
            "lease": {"lease_id": "l1", "task_id": "t1", "peer_id": SENDER, "expires_at": FIXED_TS},
            "task": {"task_type": "numeric_batch_v1", "payload": {"numbers": [1]}},
        },
        MessageType.CLAIM_DENIED: {"claim_request_id": "cr1"},
        MessageType.SUBMIT_RESULT: {
            "lease_id": "l1",
            "output": {"sum": 3},
            "content_hash": "a" * 64,
            "signature": "b" * 128,
        },
        MessageType.RESULT_ACK: {
            "result_id": "r1",
            "lease_id": "l1",
            "task_id": "t1",
            "validation": "VALID",
            "reason": "ok",
            "credit": None,
        },
        MessageType.STATUS_REQUEST: {"task_id": "t1"},
        MessageType.STATUS_RESPONSE: {
            "task_id": "t1",
            "status": None,
            "result": None,
            "validation": None,
            "credits_count": 0,
        },
        MessageType.LEDGER_SYNC_REQUEST: {"after_seq": 0},
        MessageType.LEDGER_SYNC_RESPONSE: {
            "after_seq": 0,
            "head_seq": 1,
            "events": [
                {
                    "seq": 1,
                    "event_id": "evt_1",
                    # The stored/signed column value is the canonical enum
                    # value ("CREDIT"), not a lowercased label.
                    "event_type": "CREDIT",
                    "peer_id": SENDER,
                    "task_id": "t1",
                    "amount": 1,
                    "idempotency_key": "credit:t1:" + SENDER,
                    "created_at": FIXED_TS,
                    "prev_hash": "0" * 64,
                    "event_hash": "a" * 64,
                    "signature": "b" * 128,
                    "sig_algo": "ed25519",
                    "metadata_json": "{}",
                }
            ],
            "more": False,
        },
    }
    return samples[type_]


class TestFraming(unittest.TestCase):
    def test_roundtrip(self):
        buf, read = make_reader(encode_frame(b"hello frame"))
        self.assertEqual(read_frame(read), b"hello frame")

    def test_recv_exact_one_byte_chunks(self):
        _, read = make_reader(b"abcdef", chunk=1)
        self.assertEqual(recv_exact(read, 4), b"abcd")

    def test_eof_at_boundary_is_none(self):
        _, read = make_reader(b"")
        self.assertIsNone(read_frame(read))

    def test_oversized_header_rejected_without_reading_payload(self):
        header = struct.pack(">I", 65537)  # MAX_FRAME_BYTES + 1
        buf, read = make_reader(header + b"x" * 100)
        with self.assertRaises(ProtocolError) as ctx:
            read_frame(read)
        self.assertEqual(ctx.exception.code, ErrorCode.FRAME_TOO_LARGE)
        self.assertEqual(len(buf), 100, "payload buffer must be untouched")

    def test_truncated_body_raises(self):
        header = struct.pack(">I", 10)
        _, read = make_reader(header + b"abc")
        with self.assertRaises(ProtocolError) as ctx:
            read_frame(read)
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_ENVELOPE)

    def test_encode_frame_rejects_oversize(self):
        with self.assertRaises(ProtocolError) as ctx:
            encode_frame(b"x" * 65537)
        self.assertEqual(ctx.exception.code, ErrorCode.FRAME_TOO_LARGE)

    def test_max_frame_exactly_accepted(self):
        data = b"y" * 65536
        _, read = make_reader(encode_frame(data))
        self.assertEqual(read_frame(read), data)


class TestJsonLimits(unittest.TestCase):
    def test_depth_16_ok_17_rejected(self):
        value = {}
        for _ in range(16):
            value = {"n": value}
        parse_json(json.dumps(value))  # 16 levels: ok
        value = {"n": value}
        with self.assertRaises(ProtocolError) as ctx:
            parse_json(json.dumps(value))  # 17 levels: rejected
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_ENVELOPE)

    def test_invalid_utf8(self):
        with self.assertRaises(ProtocolError):
            parse_json(b"\xff\xfe")

    def test_invalid_json(self):
        with self.assertRaises(ProtocolError):
            parse_json("{not json")


class TestEnvelopeRoundtrip(unittest.TestCase):
    def test_roundtrip_with_real_ed25519(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = NodeIdentity.load_or_create(Path(tmp) / "id.key", "test-node")
            frame = frame_for(
                MessageType.HELLO,
                FIXED_BODY,
                sign=identity.sign,
                sender=identity.node_id,
            )
            env = parse_frame(
                frame,
                transport_peer_id=identity.node_id,
                verifier=lambda sender, payload, sig: NodeIdentity.verify_signature(sender, payload, sig),
            )
        self.assertEqual(env.type, MessageType.HELLO)
        self.assertEqual(env.sender, identity.node_id)
        self.assertEqual(env.body, FIXED_BODY)

    def test_tampered_signature_rejected(self):
        env = build_envelope(MessageType.HELLO, SENDER, FAKE.sign, seq=1, body=FIXED_BODY)
        env.signature = "a" * 128  # valid hex shape, wrong signature
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(encode_envelope(env))
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_SIGNATURE)

    def test_tampered_body_rejected(self):
        env = build_envelope(MessageType.HELLO, SENDER, FAKE.sign, seq=1, body=FIXED_BODY, ts=FIXED_TS)
        env.body = {"node_id": SENDER, "role": "coordinator", "listen_addrs": FIXED_BODY["listen_addrs"]}
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(encode_envelope(env))
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_SIGNATURE, "sig covers the body")

    def test_wrong_signer_rejected(self):
        frame = frame_for(MessageType.HELLO, FIXED_BODY)
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame, verifier=FakeSigner(b"other-key").verifier)
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_SIGNATURE)


class TestPipelineOrder(unittest.TestCase):
    """The order is observable: BAD_SENDER -> signature -> seq -> ts -> body."""

    def test_verify_before_body_schema(self):
        frame = frame_for(MessageType.HELLO, {"bad": "missing keys"}, sign=FakeSigner(b"wrong-key").sign)
        with mock.patch("idle_compute.protocol.validate_body", wraps=validate_body) as schema:
            with self.assertRaises(ProtocolError) as ctx:
                parse_frame(frame)
            self.assertEqual(ctx.exception.code, ErrorCode.BAD_SIGNATURE)
        schema.assert_not_called()

    def test_bad_sender_before_verifier(self):
        calls = []

        def counting_verifier(sender, payload, sig):
            calls.append(sender)
            return True

        frame = frame_for(MessageType.HELLO, FIXED_BODY)
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame, transport_peer_id=OTHER, verifier=counting_verifier)
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_SENDER)
        self.assertEqual(calls, [], "verifier must not run when sender mismatch")

    def test_transport_peer_none_skips_sender_binding(self):
        frame = frame_for(MessageType.HELLO, FIXED_BODY)
        env = parse_frame(frame, transport_peer_id=None)
        self.assertEqual(env.sender, SENDER)

    def test_seq_before_timestamp(self):
        window = SeqWindow()
        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        frame = frame_for(MessageType.PING, {"nonce": "n"}, ts=now.isoformat())
        env = parse_frame(frame, seq_window=window, now=now)
        self.assertEqual(env.seq, 1)
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame, seq_window=window, now=now)
        self.assertEqual(ctx.exception.code, ErrorCode.SEQ_OUT_OF_WINDOW, "replay fails on seq, not ts")

    def test_timestamp_before_body(self):
        frame = frame_for(MessageType.HELLO, {"bad": "body"}, ts=FIXED_TS)
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame, now=datetime(2026, 10, 27, tzinfo=timezone.utc))
        self.assertEqual(ctx.exception.code, ErrorCode.STALE_TIMESTAMP)


class TestSeqWindow(unittest.TestCase):
    def test_accepts_then_rejects_replay(self):
        window = SeqWindow()
        frame = frame_for(MessageType.PING, {"nonce": "n"})
        parse_frame(frame, seq_window=window)
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame, seq_window=window)
        self.assertEqual(ctx.exception.code, ErrorCode.SEQ_OUT_OF_WINDOW)

    def test_gap_within_window_accepted(self):
        window = SeqWindow()
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=1), seq_window=window)
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=5), seq_window=window)

    def test_beyond_window_rejected(self):
        window = SeqWindow()
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=1), seq_window=window)
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=4097), seq_window=window)  # upper edge: ok
        fresh = SeqWindow()
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=1), seq_window=fresh)
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=4098), seq_window=fresh)
        self.assertEqual(ctx.exception.code, ErrorCode.SEQ_OUT_OF_WINDOW)

    def test_window_is_per_peer(self):
        window = SeqWindow()
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, sender=SENDER), seq_window=window)
        parse_frame(
            frame_for(MessageType.PING, {"nonce": "n"}, sender=OTHER, seq=1),
            seq_window=window,
            transport_peer_id=OTHER,
        )
        with self.assertRaises(ProtocolError):
            parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, sender=SENDER, seq=1), seq_window=window)

    def test_persistence_hooks(self):
        saved = []
        window = SeqWindow(load=lambda peer: 100, save=lambda peer, seq: saved.append((peer, seq)))
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=101), seq_window=window)
        self.assertEqual(saved, [(SENDER, 101)])
        with self.assertRaises(ProtocolError):
            parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, seq=100), seq_window=window)

    def test_thread_safety_concurrent_checkers(self):
        window = SeqWindow()
        accepted: list[int] = []

        def hammer(start: int) -> None:
            for seq in range(start, start + 100):
                try:
                    window.check_and_update(SENDER, seq)
                    accepted.append(seq)
                except ProtocolError:
                    pass

        threads = [threading.Thread(target=hammer, args=(s,)) for s in (1, 101)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(accepted), list(range(1, 201)))
        self.assertEqual(window._memory[SENDER], 200)

    def test_seq_zero_is_malformed_shape(self):
        frame = frame_for(MessageType.PING, {"nonce": "n"}, seq=0)
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame)
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_ENVELOPE)


class TestTimestamp(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)

    def ts(self, offset_seconds):
        return (self.NOW + timedelta(seconds=offset_seconds)).isoformat()

    def test_within_tolerance(self):
        parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, ts=self.ts(-299)), now=self.NOW)

    def test_stale_window(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, ts=self.ts(-400)), now=self.NOW)
        self.assertEqual(ctx.exception.code, ErrorCode.STALE_TIMESTAMP)

    def test_far_future(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, ts=self.ts(2000)), now=self.NOW)
        self.assertEqual(ctx.exception.code, ErrorCode.STALE_TIMESTAMP)

    def test_naive_rejected(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, ts="2026-10-26T12:00:00"), now=self.NOW)
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_ENVELOPE)

    def test_garbage_rejected(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame_for(MessageType.PING, {"nonce": "n"}, ts="not-a-date"), now=self.NOW)
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_ENVELOPE)


class TestBodyRegistry(unittest.TestCase):
    def test_all_16_types_have_schemas_and_accept_valid_bodies(self):
        self.assertEqual(len(BODY_SCHEMAS), 16)
        self.assertEqual(len(MessageType), 16)
        for type_ in MessageType:
            validate_body(type_, valid_body(type_))  # must not raise

    def test_extra_key_rejected(self):
        body = valid_body(MessageType.HELLO)
        body["smuggled"] = True
        with self.assertRaises(ProtocolError) as ctx:
            validate_body(MessageType.HELLO, body)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_BODY)

    def test_missing_nullable_key_rejected(self):
        body = valid_body(MessageType.CLAIM_REQUEST)
        del body["task_id"]
        with self.assertRaises(ProtocolError) as ctx:
            validate_body(MessageType.CLAIM_REQUEST, body)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_BODY)

    def test_wrong_field_type_rejected(self):
        body = valid_body(MessageType.HELLO)
        body["role"] = "root"
        with self.assertRaises(ProtocolError):
            validate_body(MessageType.HELLO, body)

    def test_unknown_type_rejected_in_pipeline(self):
        payload = signing_payload(1, "EXEC", SENDER, None, 1, FIXED_TS, None, {})
        raw = {
            "v": 1, "type": "EXEC", "sender": SENDER, "recipient": None, "seq": 1,
            "ts": FIXED_TS, "in_reply_to": None, "body": {},
            "signature": FAKE.sign(payload),
        }
        from idle_compute.crypto import canonical_json
        from idle_compute.protocol import encode_frame

        frame = encode_frame(canonical_json(raw).encode("utf-8"))
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame)
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_TYPE)

    def test_bad_signature_hex_length_is_malformed(self):
        raw = {
            "v": 1, "type": "PING", "sender": SENDER, "recipient": None, "seq": 1,
            "ts": FIXED_TS, "in_reply_to": None, "body": {"nonce": "n"}, "signature": "abcd",
        }
        from idle_compute.crypto import canonical_json
        from idle_compute.protocol import encode_frame

        frame = encode_frame(canonical_json(raw).encode("utf-8"))
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame)
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_ENVELOPE)

    def test_body_size_cap_enforced_after_verify(self):
        body = {
            "client_ref": "c1",
            "task_type": "numeric_batch_v1",
            "payload": {"numbers": [1], "blob": "x" * 33000},
            "max_attempts": 3,
            "lease_seconds": 60,
        }
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(frame_for(MessageType.TASK_CREATE, body))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_BODY)

    def test_recipient_must_be_node_id_shaped(self):
        env = build_envelope(MessageType.PING, SENDER, FAKE.sign, recipient="not-a-node-id", seq=1, body={"nonce": "n"})
        with self.assertRaises(ProtocolError) as ctx:
            parse_frame(encode_envelope(env))
        self.assertEqual(ctx.exception.code, ErrorCode.MALFORMED_ENVELOPE)


class TestErrorEnvelope(unittest.TestCase):
    def test_roundtrip_and_truncation(self):
        env = error_envelope(
            ErrorCode.UNAUTHORIZED,
            "x" * 1000,
            sender=SENDER,
            sign=FAKE.sign,
            recipient=OTHER,
            seq=7,
            in_reply_to=3,
            ref_type="HELLO",
            ref_seq=2,
        )
        parsed = parse_frame(encode_envelope(env), transport_peer_id=SENDER)
        self.assertEqual(parsed.type, MessageType.ERROR)
        self.assertEqual(parsed.body["code"], 1)
        self.assertEqual(len(parsed.body["message"]), 512)
        self.assertEqual(parsed.body["ref_type"], "HELLO")
        self.assertEqual(parsed.body["ref_seq"], 2)

    def test_16_error_codes(self):
        self.assertEqual(len(ErrorCode), 16)
        self.assertEqual([c.value for c in ErrorCode], list(range(1, 17)))


class TestGoldenVectors(unittest.TestCase):
    def test_signing_payload_golden(self):
        payload = signing_payload(1, "HELLO", SENDER, None, 1, FIXED_TS, None, FIXED_BODY)
        self.assertEqual(hashlib.sha256(payload.encode("utf-8")).hexdigest(), PAYLOAD_GOLDEN)

    def test_wire_format_golden(self):
        env = build_envelope(MessageType.HELLO, SENDER, FAKE.sign, seq=1, body=FIXED_BODY, ts=FIXED_TS)
        self.assertEqual(encode_envelope(env).hex(), WIRE_GOLDEN)
        self.assertEqual(len(encode_envelope(env)), 451)

    def test_golden_frame_parses_back(self):
        frame = bytes.fromhex(WIRE_GOLDEN)
        env = parse_frame(frame, transport_peer_id=SENDER, now=datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(env.type, MessageType.HELLO)
        self.assertEqual(env.body, FIXED_BODY)
        self.assertEqual(env.ts, FIXED_TS)

    def test_wire_determinism(self):
        a = encode_envelope(build_envelope(MessageType.PING, SENDER, FAKE.sign, seq=1, body={"nonce": "n"}, ts=FIXED_TS))
        b = encode_envelope(build_envelope(MessageType.PING, SENDER, FAKE.sign, seq=1, body={"nonce": "n"}, ts=FIXED_TS))
        self.assertEqual(a, b)


class TestContainment(unittest.TestCase):
    def test_protocol_has_no_network_imports(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "idle_compute", "protocol.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("import libp2p", source)
        self.assertNotIn("import socket", source)
        self.assertNotIn("import trio", source)


if __name__ == "__main__":
    unittest.main()
