"""F-4: log redaction rules + an end-to-end capture test.

Rules (idle_compute/log.py):
- node ids truncate to 12 chars; hashes and signatures to 8 hex chars;
- payloads are summarised by shape/counts, never values;
- ``*.key`` paths become ``<key-file>``;
- a handler-level filter enforces the same rules even if a call site forgets.

The capture test runs REAL coordinator + worker + client processes at DEBUG,
including an unauthorized HELLO, then asserts the written logs contain no key
material, no key-file paths, no full signatures and no payload values.
"""

from __future__ import annotations

import io
import json
import logging
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from idle_compute.config import from_dict
from idle_compute.identity import NodeIdentity
from idle_compute.log import (
    KEY_PATH_PLACEHOLDER,
    RedactionFilter,
    install_redaction,
    payload_summary,
    redact_text,
    short_hex,
    short_id,
)
from idle_compute.node import Node
from idle_compute.protocol import (
    ErrorCode,
    MessageType,
    SeqWindow,
    build_envelope,
    encode_envelope,
    parse_message,
    read_frame,
)

ROOT = Path(__file__).resolve().parent.parent
NODE_RUN = [sys.executable, "-m", "idle_compute", "node", "run", "--config"]

SECRET_NUMBER = 987654321


def _identity(tmp: Path, name: str) -> NodeIdentity:
    return NodeIdentity.load_or_create(tmp / name / "identity.key", node_name=name)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _spawn(config_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        NODE_RUN + [str(config_path)],
        cwd=str(ROOT),
        stdout=open(config_path.with_name("proc.log"), "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def _wait(predicate, timeout: float, message: str, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


class RedactionUnitTests(unittest.TestCase):
    def test_short_id_and_short_hex(self) -> None:
        node_id = "12D3KooW" + "a" * 44
        self.assertEqual(len(short_id(node_id)), 13, "12 visible chars + ellipsis")
        self.assertTrue(short_id(node_id).endswith("…"))
        self.assertEqual(short_hex("a" * 128), "aaaaaaaa…")
        self.assertEqual(short_hex("abc"), "abc")

    def test_payload_summary_has_counts_only(self) -> None:
        summary = payload_summary({"numbers": [SECRET_NUMBER, 2, 3]})
        self.assertEqual(summary["numbers_count"], 3)
        self.assertEqual(summary["keys"], ["numbers"])
        self.assertNotIn(str(SECRET_NUMBER), json.dumps(summary))

    def test_redact_text_rules(self) -> None:
        node_id = "12D3KooW" + "a" * 44
        signature = "b" * 128
        digest = "c" * 64
        text = f"peer {node_id} sig {signature} hash {digest} key C:\\secret\\identity.key"
        redacted = redact_text(text)
        self.assertNotIn(node_id, redacted)
        self.assertNotIn(signature, redacted)
        self.assertNotIn(digest, redacted)
        self.assertNotIn("identity.key", redacted)
        self.assertIn(KEY_PATH_PLACEHOLDER, redacted)
        self.assertIn("12D3KooWaaaa…", redacted)
        self.assertIn("bbbbbbbb…", redacted)

    def test_filter_redacts_a_record_rendered_by_args(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("idle_compute.test_filter")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        install_redaction(logger_name="idle_compute.test_filter", handlers=[handler])

        node_id = "12D3KooW" + "d" * 44
        logger.info("peer %s signed with %s", node_id, "e" * 128)
        out = stream.getvalue()
        self.assertNotIn(node_id, out)
        self.assertNotIn("e" * 128, out)
        self.assertIn("12D3KooWdddd…", out)


class LogCaptureTests(unittest.TestCase):
    """Real processes at DEBUG; the written logs must not leak secrets."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="log-capture-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord_dir = self.root / "coord"
        self.worker_dir = self.root / "worker"
        self.client_dir = self.root / "client"
        for path in (self.coord_dir, self.worker_dir, self.client_dir):
            path.mkdir(parents=True, exist_ok=True)

        self.coord_id = _identity(self.root, "coord")
        self.worker_id = _identity(self.root, "worker")
        self.client_id = _identity(self.root, "client")
        self.outsider_id = _identity(self.root, "outsider")

        self.port = _free_port()
        self.coord_cfg = self.coord_dir / "coord.json"
        self.coord_cfg.write_text(
            json.dumps(
                from_dict(
                    {
                        "role": "coordinator",
                        "transport": "tcp",
                        "listen_host": "127.0.0.1",
                        "listen_port": self.port,
                        "data_dir": str(self.coord_dir),
                        "peers": [self.worker_id.node_id, self.client_id.node_id],
                        "poll_interval_seconds": 1,
                        "log_level": "DEBUG",
                        "log_file": str(self.coord_dir / "coordinator.log"),
                    }
                ).to_dict(),
                indent=2,
            ),
            encoding="utf-8",
        )
        self.worker_cfg = self.worker_dir / "worker.json"
        self.worker_cfg.write_text(
            json.dumps(
                from_dict(
                    {
                        "role": "worker",
                        "transport": "tcp",
                        "listen_host": "127.0.0.1",
                        "listen_port": 0,
                        "data_dir": str(self.worker_dir),
                        "peers": [self.coord_id.node_id],
                        "coordinator_node_id": self.coord_id.node_id,
                        "coordinator_addr": f"127.0.0.1:{self.port}",
                        "poll_interval_seconds": 1,
                        "backoff_min": 1,
                        "backoff_max": 4,
                        "log_level": "DEBUG",
                        "log_file": str(self.worker_dir / "worker.log"),
                    }
                ).to_dict(),
                indent=2,
            ),
            encoding="utf-8",
        )
        self.client_cfg = self.client_dir / "client.json"
        self.client_cfg.write_text(
            json.dumps(
                from_dict(
                    {
                        "role": "client",
                        "transport": "tcp",
                        "listen_host": "127.0.0.1",
                        "listen_port": 0,
                        "data_dir": str(self.client_dir),
                        "peers": [self.coord_id.node_id],
                        "coordinator_node_id": self.coord_id.node_id,
                        "coordinator_addr": f"127.0.0.1:{self.port}",
                        "log_level": "DEBUG",
                        "log_file": str(self.client_dir / "client.log"),
                    }
                ).to_dict(),
                indent=2,
            ),
            encoding="utf-8",
        )

    def _start(self) -> tuple[subprocess.Popen, subprocess.Popen]:
        coord = _spawn(self.coord_cfg)
        self.addCleanup(coord.wait, 10.0)
        self.addCleanup(coord.terminate)
        _wait(
            lambda: self._port_open(self.port),
            20.0,
            "coordinator to listen",
        )
        worker = _spawn(self.worker_cfg)
        self.addCleanup(worker.wait, 10.0)
        self.addCleanup(worker.terminate)
        return coord, worker

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "idle_compute", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_three_process_run_logs_no_secrets(self) -> None:
        self._start()

        created = self._cli(
            "node", "create-task", "--config", str(self.client_cfg), "--numbers", str(SECRET_NUMBER)
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        task_id = json.loads(created.stdout)["task_id"]

        def done() -> bool:
            out = self._cli("node", "status", "--config", str(self.client_cfg), "--task-id", task_id)
            if out.returncode != 0:
                return False
            return json.loads(out.stdout).get("status") in {"COMPLETED", "REJECTED", "EXPIRED"}

        _wait(done, 60.0, "the task to finish")

        # Unauthorized HELLO from an identity not on the allowlist: it must be
        # refused, and that adversarial path must not log anything secret.
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            frame = encode_envelope(
                build_envelope(
                    MessageType.HELLO,
                    self.outsider_id.node_id,
                    self.outsider_id.sign,
                    recipient=self.coord_id.node_id,
                    seq=1,
                    body={
                        "node_id": self.outsider_id.node_id,
                        "role": "worker",
                        "listen_addrs": ["127.0.0.1:1"],
                    },
                )
            )
            sock.sendall(frame)
            sock.settimeout(10)
            reply = read_frame(lambda want: sock.recv(want))
            env = parse_message(
                reply,
                transport_peer_id=None,
                verifier=NodeIdentity.verify_signature,
                seq_window=SeqWindow(),
            )
            self.assertEqual(env.type, MessageType.ERROR)
            self.assertEqual(env.body["code"], ErrorCode.UNAUTHORIZED.value)
        finally:
            sock.close()
        time.sleep(0.5)

        # Collect the process outputs: the two node log files plus the client
        # CLI's stdout/stderr (the third process does not run a node loop, so it
        # writes JSON to stdio instead of a log file).
        logs = []
        for path in (
            self.coord_dir / "coordinator.log",
            self.worker_dir / "worker.log",
            self.client_dir / "client.log",
        ):
            if path.exists():
                logs.append(path.read_text(encoding="utf-8", errors="replace"))
        logs.append(created.stdout + created.stderr)
        combined = "\n".join(logs)
        self.assertTrue(combined.strip(), "the run must have produced logs")

        # 1) No key material: the raw key bytes (hex/base64-ish text) never appear.
        for key_path in (
            self.coord_dir / "identity.key",
            self.worker_dir / "identity.key",
            self.client_dir / "identity.key",
        ):
            raw = key_path.read_bytes()
            self.assertNotIn(raw.hex(), combined, "raw identity key bytes leaked into a log")
        # 2) No key-file paths.
        self.assertNotIn(str(self.coord_dir / "identity.key"), combined)
        self.assertNotIn(".key", combined, "a key-file path leaked into a log")
        # 3) No full signatures (128 hex) anywhere.
        self.assertIsNone(re.search(r"\b[0-9a-fA-F]{128}\b", combined), "full signature leaked")
        # 4) No payload values.
        self.assertNotIn(str(SECRET_NUMBER), combined, "payload value leaked into a log")
