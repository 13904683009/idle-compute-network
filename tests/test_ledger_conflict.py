"""E-2: worker conflict detection, quarantine, and rebuild-from-scratch.

Policy under test (docs/phase2/TASK-BOARD.md E-2):
- a tampered coordinator chain latches a CONFLICT on the worker, which stops
  claiming and never rewinds or forks its mirror;
- the latched conflict is persisted and reported by ``worker ledger status``
  (with the offending seq);
- an hmac-tagged row entering the sync stream is a structured CONFLICT, never
  a silent pass;
- deleting the worker DB (derived state) rebuilds a byte-identical verified
  mirror from seq 0;
- repeated sync rounds / restarts never duplicate a credit on either side.

Real subprocess worker + in-process coordinator over real TCP sockets.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from idle_compute.config import from_dict
from idle_compute.db import Store
from idle_compute.identity import NodeIdentity
from idle_compute.ledger import append_credit, export_since
from idle_compute.node import Node
from idle_compute.signing import Ed25519Policy

ROOT = Path(__file__).resolve().parent.parent
NODE_RUN = [sys.executable, "-m", "idle_compute", "node", "run", "--config"]

EVENT_KEYS = (
    "seq",
    "event_id",
    "event_type",
    "peer_id",
    "task_id",
    "amount",
    "idempotency_key",
    "created_at",
    "prev_hash",
    "event_hash",
    "signature",
    "sig_algo",
    "metadata_json",
)


def _wait_for(predicate, timeout: float, message: str, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {message}")


def _write_config(path: Path, config) -> None:
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


def _read_mirror(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows = conn.execute(
            "SELECT seq, event_id, event_json, verified FROM ledger_copy ORDER BY seq"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [
        {
            "seq": seq,
            "event_id": event_id,
            "event": json.loads(event_json),
            "verified": verified,
        }
        for seq, event_id, event_json, verified in rows
    ]


def _read_conflict(db_path: Path) -> dict | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        row = conn.execute(
            "SELECT value FROM ledger_meta WHERE key='ledger_conflict'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def _coordinator_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM credit_events ORDER BY seq")]
    finally:
        conn.close()


def _task_status(db_path: Path, task_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _active_leases(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM leases WHERE status='ACTIVE'").fetchone()[0]
        )
    finally:
        conn.close()


class ConflictFixture(unittest.TestCase):
    """In-process coordinator + real subprocess worker, 5 seeded credits."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ledger-conflict-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.coord_tmp = root / "coord"
        self.worker_tmp = root / "worker"
        self.client_tmp = root / "client"
        self.coord_tmp.mkdir(parents=True, exist_ok=True)
        self.worker_tmp.mkdir(parents=True, exist_ok=True)
        self.client_tmp.mkdir(parents=True, exist_ok=True)
        self.coordinator_identity = NodeIdentity.load_or_create(
            self.coord_tmp / "identity.key", node_name="coordinator"
        )
        self.worker_identity = NodeIdentity.load_or_create(
            self.worker_tmp / "identity.key", node_name="worker"
        )
        # The client identity must exist before the coordinator starts: the
        # allowlist is static (default deny), so an unknown client would be
        # refused at HELLO.
        self.client_identity = NodeIdentity.load_or_create(
            self.client_tmp / "identity.key", node_name="client"
        )
        self.coord_db = self.coord_tmp / "coordinator.db"
        self.worker_db = self.worker_tmp / "ledger_copy.db"
        self._worker: subprocess.Popen | None = None
        self._seed(5)
        self._start_coordinator()

    # ---------------------------------------------------------------- setup

    def _seed(self, count: int) -> None:
        store = Store(self.coord_db, signer=Ed25519Policy.from_identity(self.coordinator_identity))
        try:
            with store.transaction():
                for i in range(count):
                    _, created = append_credit(
                        store.conn, store.signer, self.worker_identity.node_id, f"tsk_{i}"
                    )
                    self.assertTrue(created)
        finally:
            store.close()

    def _start_coordinator(self) -> None:
        config = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.coord_tmp),
                "peers": [self.worker_identity.node_id, self.client_identity.node_id],
                "poll_interval_seconds": 1,
                "log_level": "WARNING",
                "log_file": str(self.coord_tmp / "coordinator.log"),
            }
        )
        self.coordinator = Node(config)
        self.addCleanup(self.coordinator.shutdown)
        self._coord_thread = threading.Thread(
            target=self.coordinator.run, name="conflict-coordinator", daemon=True
        )
        self._coord_thread.start()
        self.addCleanup(self._coord_thread.join, 10.0)
        _wait_for(
            lambda: bool(self.coordinator.transport.advertised_addrs()), 20.0, "coordinator listen"
        )
        self.coord_addr = self.coordinator.transport.advertised_addrs()[0]

    def start_worker(self) -> subprocess.Popen:
        config = from_dict(
            {
                "role": "worker",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.worker_tmp),
                "peers": [self.coordinator_identity.node_id],
                "coordinator_node_id": self.coordinator_identity.node_id,
                "coordinator_addr": self.coord_addr,
                "poll_interval_seconds": 1,
                "backoff_min": 1,
                "backoff_max": 4,
                "log_level": "WARNING",
                "log_file": str(self.worker_tmp / "worker.log"),
            }
        )
        _write_config(self.worker_tmp / "worker.json", config)
        worker = _spawn_node(self.worker_tmp / "worker.json")
        self.addCleanup(worker.wait, 10.0)
        self.addCleanup(worker.terminate)
        self._worker = worker
        return worker

    def stop_worker(self) -> None:
        worker = self._worker
        if worker is None:
            return
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - harness safety
            worker.kill()
            worker.wait(timeout=10)
        self._worker = None

    # -------------------------------------------------------------- helpers

    def wait_mirror(self, count: int, timeout: float = 60.0) -> None:
        _wait_for(
            lambda: len(_read_mirror(self.worker_db)) >= count,
            timeout,
            f"worker mirror to reach {count} event(s)",
        )

    def wait_conflict(self, timeout: float = 60.0) -> dict:
        holder: dict = {}

        def ready() -> bool:
            conflict = _read_conflict(self.worker_db)
            if conflict is None:
                return False
            holder.update(conflict)
            return True

        _wait_for(ready, timeout, "the worker to latch a ledger conflict")
        return holder

    def tamper(self, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(str(self.coord_db), timeout=10)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def _delete_with_retry(path: Path, attempts: int = 25, delay: float = 0.2) -> None:
    """Delete a file the worker just closed; Windows releases handles lazily."""
    last: OSError | None = None
    for _ in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:  # PermissionError while the handle is closing
            last = exc
            time.sleep(delay)
    raise AssertionError(f"could not delete {path}: {last}")


class ConflictTests(ConflictFixture):
    def test_tampered_chain_stops_the_fresh_mirror_at_the_bad_seq(self) -> None:
        # A malicious/broken coordinator edits event 3's linkage. A worker
        # that has no mirror yet must accept 1 and 2, stop AT 3, and never
        # claim while the conflict is latched.
        self.tamper("UPDATE credit_events SET prev_hash=? WHERE seq=3", ("f" * 64,))

        self.start_worker()
        conflict = self.wait_conflict()
        self.assertEqual(conflict["seq"], 3, "the offending seq must be reported")
        self.assertIn("prev_hash", conflict["reason"])
        self.assertEqual(
            [row["seq"] for row in _read_mirror(self.worker_db)],
            [1, 2],
            "the mirror must stop at the bad event, never skip it",
        )

        # The operator-facing surface must report the same state (E-2): the
        # offending seq, the check, and that claiming is blocked.
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "idle_compute",
                "worker",
                "ledger",
                "status",
                "--config",
                str(self.worker_tmp / "worker.json"),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = json.loads(proc.stdout)
        self.assertEqual(status["mirror_head"], 2)
        self.assertEqual(status["verified"], 2)
        self.assertTrue(status["claim_blocked"])
        self.assertEqual(status["conflict"]["seq"], 3)
        self.assertIn("prev_hash", status["conflict"]["reason"])

        # Quarantine: while the conflict is latched the worker must not claim.
        client = Node(
            from_dict(
                {
                    "role": "client",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(self.client_tmp),
                    "peers": [self.coordinator_identity.node_id],
                    "coordinator_node_id": self.coordinator_identity.node_id,
                    "coordinator_addr": self.coord_addr,
                    "log_level": "WARNING",
                }
            )
        )
        self.addCleanup(client.shutdown)
        task_id = client.client_create_task({"numbers": [1, 2]}, timeout=15.0)["task_id"]
        time.sleep(4.0)
        self.assertEqual(_task_status(self.coord_db, task_id), "QUEUED")
        self.assertEqual(_active_leases(self.coord_db), 0)

    def test_hmac_tagged_row_is_a_structured_conflict_without_truncating(self) -> None:
        self.start_worker()
        self.wait_mirror(5)
        self.stop_worker()
        before = _read_mirror(self.worker_db)

        # Downgrade one row's scheme tag: a network mirror must never accept it,
        # and a re-audit must not rewrite or truncate what is already mirrored.
        self.tamper("UPDATE credit_events SET sig_algo='hmac-sha256' WHERE seq=2")

        self.start_worker()
        conflict = self.wait_conflict()
        self.assertEqual(conflict["seq"], 2)
        self.assertIn("CONFLICT", conflict["reason"])
        self.assertIn("hmac-sha256", conflict["reason"])
        self.assertEqual(
            _read_mirror(self.worker_db),
            before,
            "a re-audit must flag the divergence without rewinding or rewriting the mirror",
        )

    def test_deleting_worker_db_rebuilds_a_byte_identical_mirror(self) -> None:
        self.start_worker()
        self.wait_mirror(5)
        self.stop_worker()

        # The worker DB is derived state: deleting it must be safe.
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.worker_db) + suffix)
            if path.exists():
                _delete_with_retry(path)
        self.assertFalse(self.worker_db.exists())

        self.start_worker()
        self.wait_mirror(5)
        self.assertIsNone(_read_conflict(self.worker_db), "a clean rebuild must not latch a conflict")

        chain = _coordinator_rows(self.coord_db)
        mirror = _read_mirror(self.worker_db)
        self.assertEqual(len(mirror), len(chain))
        for chain_row, mirror_row in zip(chain, mirror):
            self.assertEqual(mirror_row["verified"], 1)
            self.assertEqual(mirror_row["event_id"], chain_row["event_id"])
            for key in EVENT_KEYS:
                self.assertEqual(mirror_row["event"][key], chain_row[key], f"{key} diverged")

    def test_repeated_sync_rounds_never_duplicate_a_credit(self) -> None:
        self.start_worker()
        self.wait_mirror(5)
        self.stop_worker()

        # A second worker run re-pulls the whole chain from its own head (the
        # mirror is already current, so nothing is re-appended) and the
        # coordinator side is untouched: exactly one event per (task, peer).
        self.start_worker()
        self.wait_mirror(5)
        time.sleep(2.0)

        chain = _coordinator_rows(self.coord_db)
        mirror = _read_mirror(self.worker_db)
        self.assertEqual(len(chain), 5)
        self.assertEqual(len(mirror), 5)
        keys = [row["event"]["idempotency_key"] for row in mirror]
        self.assertEqual(len(keys), len(set(keys)), "mirror must not duplicate an idempotency key")
        # Full pages are idempotent: exporting the same range twice yields the
        # same rows, and verify_and_append treats re-appends as no-ops.
        conn = sqlite3.connect(str(self.coord_db))
        conn.row_factory = sqlite3.Row
        try:
            again = export_since(conn, 0, limit=100)
        finally:
            conn.close()
        self.assertEqual([row["event_id"] for row in again], [row["event_id"] for row in chain])
