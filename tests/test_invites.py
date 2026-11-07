"""One-time invite enrollment: store lifecycle and real CLI join."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from idle_compute.cli import _invite_code, _invite_payload, main
from idle_compute.db import InviteError, Store
from idle_compute.protocol import MessageType, ProtocolError, validate_body

ROOT = Path(__file__).resolve().parent.parent
NODE_ID = "12D3KooW" + "A" * 44


def _future(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class InviteHelloShapeTests(unittest.TestCase):
    def test_hello_accepts_optional_invite_only(self) -> None:
        base = {"node_id": NODE_ID, "role": "worker", "listen_addrs": ["127.0.0.1:1234"]}
        validate_body(MessageType.HELLO, dict(base))
        validate_body(MessageType.HELLO, {**base, "invite": "abc"})
        with self.assertRaises(ProtocolError):
            validate_body(MessageType.HELLO, {**base, "extra": "nope"})
        with self.assertRaises(ProtocolError):
            validate_body(MessageType.HELLO, {**base, "invite": "x" * 4097})


class InviteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="invite-store-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = Store(root / "idle.db", root / "ledger.key")
        self.addCleanup(self.store.close)

    def test_consume_once_and_enroll(self) -> None:
        digest = "a" * 64
        self.store.create_invite(digest, "worker", _future())
        self.assertFalse(self.store.has_joined("peer-1"))
        result = self.store.consume_invite(digest, "peer-1", "worker")
        self.assertEqual(result["peer_id"], "peer-1")
        self.assertTrue(self.store.has_joined("peer-1"))
        with self.assertRaises(InviteError) as ctx:
            self.store.consume_invite(digest, "peer-2", "worker")
        self.assertEqual(ctx.exception.code, InviteError.USED)

    def test_expiry_and_role_mismatch(self) -> None:
        self.store.create_invite("b" * 64, "worker", _past())
        with self.assertRaises(InviteError) as ctx:
            self.store.consume_invite("b" * 64, "peer-1", "worker")
        self.assertEqual(ctx.exception.code, InviteError.EXPIRED)

        self.store.create_invite("c" * 64, "worker", _future())
        with self.assertRaises(InviteError) as ctx:
            self.store.consume_invite("c" * 64, "peer-1", "client")
        self.assertEqual(ctx.exception.code, InviteError.ROLE)


class InviteJoinCliTests(unittest.TestCase):
    def test_invite_create_and_real_join(self) -> None:
        with tempfile.TemporaryDirectory(prefix="invite-join-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
            sock.close()
            config_path = root / "coordinator.json"
            config = {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "data_dir": str(root / "coord"),
                "peers": [],
                "log_level": "ERROR",
                "log_file": str(root / "coord.log"),
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            out = StringIO()
            with redirect_stdout(out):
                code = main(["invite", "create", "--config", str(config_path), "--role", "worker"])
            self.assertEqual(code, 0)
            invite = json.loads(out.getvalue())["invite"]
            self.assertTrue(invite)

            proc = subprocess.Popen(
                [sys.executable, "-m", "idle_compute", "node", "run", "--config", str(config_path)],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(lambda: proc.poll() is None and (proc.terminate(), proc.wait(timeout=10)))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("coordinator did not listen")

            joined_out = StringIO()
            errors = StringIO()
            with redirect_stdout(joined_out), redirect_stderr(errors):
                code = main(
                    [
                        "join",
                        "--invite",
                        invite,
                        "--role",
                        "worker",
                        "--data-dir",
                        str(root / "worker"),
                        "--config-out",
                        str(root / "worker.json"),
                        "--timeout",
                        "20",
                    ]
                )
            self.assertEqual(code, 0, errors.getvalue())
            joined = json.loads(joined_out.getvalue())
            self.assertTrue(joined["joined"])
            worker_id = joined["node_id"]
            self.assertTrue(worker_id)

            store = Store(root / "coord" / "coordinator.db", root / "coord" / "ledger.key")
            try:
                self.assertTrue(store.has_joined(worker_id))
            finally:
                store.close()

            # The same invite must not enroll a second node.
            reuse_out = StringIO()
            reuse_err = StringIO()
            with redirect_stdout(reuse_out), redirect_stderr(reuse_err):
                reuse_code = main(
                    [
                        "join",
                        "--invite",
                        invite,
                        "--role",
                        "worker",
                        "--data-dir",
                        str(root / "worker2"),
                        "--config-out",
                        str(root / "worker2.json"),
                        "--timeout",
                        "10",
                    ]
                )
            self.assertNotEqual(reuse_code, 0)
            self.assertIn("已被使用", reuse_err.getvalue())

            # A client invite works through the same path.
            client_out = StringIO()
            with redirect_stdout(client_out):
                self.assertEqual(
                    main(["invite", "create", "--config", str(config_path), "--role", "client"]),
                    0,
                )
            client_invite = json.loads(client_out.getvalue())["invite"]
            client_join_out = StringIO()
            client_err = StringIO()
            with redirect_stdout(client_join_out), redirect_stderr(client_err):
                client_code = main(
                    [
                        "join",
                        "--invite",
                        client_invite,
                        "--role",
                        "client",
                        "--data-dir",
                        str(root / "client"),
                        "--config-out",
                        str(root / "client.json"),
                        "--timeout",
                        "20",
                    ]
                )
            self.assertEqual(client_code, 0, client_err.getvalue())
            client_joined = json.loads(client_join_out.getvalue())
            self.assertTrue(client_joined["next"])
            self.assertTrue((root / "client.json").is_file())
            store = Store(root / "coord" / "coordinator.db", root / "coord" / "ledger.key")
            try:
                rows = {r["peer_id"]: r["role"] for r in store.conn.execute("SELECT peer_id,role FROM joined_peers")}
                self.assertEqual(rows[client_joined["node_id"]], "client")
            finally:
                store.close()

            # Expired invite produces an actionable error.
            base = _invite_payload(invite)
            expired_secret = "expired-test-secret"
            store = Store(root / "coord" / "coordinator.db", root / "coord" / "ledger.key")
            try:
                import hashlib as _hashlib

                store.create_invite(
                    _hashlib.sha256(expired_secret.encode()).hexdigest(), "worker", _past()
                )
            finally:
                store.close()
            expired_code = _invite_code(
                {
                    "v": 1,
                    "secret": expired_secret,
                    "coordinator_node_id": base["coordinator_node_id"],
                    "addr": base["addr"],
                    "role": "worker",
                    "cert_sha256": "",
                    "expires_at": _past(),
                }
            )
            expired_out = StringIO()
            expired_err = StringIO()
            with redirect_stdout(expired_out), redirect_stderr(expired_err):
                expired_rc = main(
                    [
                        "join",
                        "--invite",
                        expired_code,
                        "--role",
                        "worker",
                        "--data-dir",
                        str(root / "worker3"),
                        "--timeout",
                        "10",
                    ]
                )
            self.assertNotEqual(expired_rc, 0)
            self.assertIn("已过期", expired_err.getvalue())

    def test_join_connection_failure_is_actionable(self) -> None:
        code = _invite_code(
            {
                "v": 1,
                "secret": "connection-test-secret",
                "coordinator_node_id": NODE_ID,
                "addr": "127.0.0.1:1",
                "role": "worker",
                "cert_sha256": "",
                "expires_at": _future(),
            }
        )
        with tempfile.TemporaryDirectory(prefix="invite-conn-", ignore_cleanup_errors=True) as tmp:
            out = StringIO()
            err = StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(
                    [
                        "join",
                        "--invite",
                        code,
                        "--role",
                        "worker",
                        "--data-dir",
                        str(Path(tmp) / "worker"),
                        "--timeout",
                        "3",
                    ]
                )
        self.assertNotEqual(rc, 0)
        self.assertIn("无法连接 Coordinator", err.getvalue())


if __name__ == "__main__":
    unittest.main()
