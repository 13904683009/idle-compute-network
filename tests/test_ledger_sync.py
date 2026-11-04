"""E-1: LEDGER_SYNC paginated pull + worker mirror verification.

Two layers:

1. Page semantics against a real Ed25519-signed coordinator chain (no
   sockets): count and byte clamping, the ``more`` flag, an empty page past
   the head, wire-schema parity of the exported rows, and "a corrupt event
   stops the mirror at that seq" (the mirror never skips, never overwrites).
2. A REAL subprocess worker syncing a 150-event chain from an in-process
   coordinator over a real TCP socket (real frames, real signatures):
   the mirror must end byte-identical to the coordinator chain, and a wiped
   tail must resume from the mirror head without gaps or duplicates.

Acceptance mapping (docs/phase2/TASK-BOARD.md E-1):
- 150 events -> 2 batches (<=100) with no gap and no duplicate: TestLedgerPage.
- after_seq beyond the head -> empty page + current head: TestLedgerPage.
- every event verified (sig + prev_hash + amount>0) before append, corrupt
  event not appended and its seq reported: TestLedgerPage + verify_and_append
  unit tests in tests/test_ledger.py.
- mirror byte-identical to the coordinator chain: TestWorkerSyncIntegration.
- resume after a partial mirror: TestWorkerSyncIntegration.
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
from idle_compute.ledger import append_credit, export_page, head_seq, verify_and_append
from idle_compute.models import CreditEventType
from idle_compute.node import Node
from idle_compute.protocol import (
    LEDGER_PAGE_BYTES,
    MAX_EVENT_METADATA,
    MAX_SYNC_PAGE_EVENTS,
    MessageType,
    ProtocolError,
    validate_body,
)
from idle_compute.signing import Ed25519Policy

ROOT = Path(__file__).resolve().parent.parent
NODE_RUN = [sys.executable, "-m", "idle_compute", "node", "run", "--config"]

# The 13 columns export_page/verify_and_append/wire schema agree on.
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


def _canonical(event: dict) -> str:
    """The exact serialization verify_and_append stores in ledger_copy."""
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def _wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
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


def _worker_config(tmp_path: Path, coord_id: str, coord_addr: str, poll: int = 1):
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


def _mirror_rows(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows = conn.execute(
            "SELECT seq, event_id, event_json, verified FROM ledger_copy ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"seq": seq, "event_id": event_id, "event": json.loads(event_json), "event_json": event_json, "verified": verified}
        for seq, event_id, event_json, verified in rows
    ]


def _coordinator_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM credit_events ORDER BY seq")]
    finally:
        conn.close()
    return rows


class LedgerPageTests(unittest.TestCase):
    """Page semantics against a real Ed25519 coordinator chain (no sockets)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.coordinator = NodeIdentity.load_or_create(
            root / "coordinator" / "identity.key", node_name="coordinator"
        )
        # Credit events carry a real node id as peer_id: the wire schema only
        # accepts 12D3KooW... ids, exactly as production credits do.
        self.peer = NodeIdentity.load_or_create(root / "peer" / "identity.key", node_name="peer")
        self.store = Store(
            root / "coord.db", signer=Ed25519Policy.from_identity(self.coordinator)
        )
        self.addCleanup(self.store.close)

    def _seed(self, count: int, metadata: dict | None = None) -> None:
        with self.store.transaction():
            for i in range(count):
                _, created = append_credit(
                    self.store.conn,
                    self.store.signer,
                    self.peer.node_id,
                    f"tsk_{i}",
                    metadata=metadata,
                )
                self.assertTrue(created)

    def _pages(self, limit: int = MAX_SYNC_PAGE_EVENTS) -> list[tuple[list[dict], bool]]:
        """Pull the whole chain page by page, exactly like the worker does."""
        pages: list[tuple[list[dict], bool]] = []
        after = 0
        for _ in range(100):  # bounded loop: a stall fails instead of hanging
            page, more = export_page(self.store.conn, after, limit=limit)
            pages.append((page, more))
            if not more:
                return pages
            after = page[-1]["seq"]
        raise AssertionError("pagination did not terminate")

    def test_pages_partition_the_chain_without_gaps_or_duplicates(self) -> None:
        self._seed(150)
        self.assertEqual(head_seq(self.store.conn), 150)

        pages = self._pages()
        self.assertGreaterEqual(len(pages), 2, "150 signed events cannot fit one page")
        for index, (page, more) in enumerate(pages):
            self.assertTrue(page, "a complete chain never yields an empty page")
            self.assertLessEqual(len(page), MAX_SYNC_PAGE_EVENTS)
            self.assertLessEqual(
                sum(len(_canonical(event)) for event in page),
                LEDGER_PAGE_BYTES,
                "a page must fit the wire body budget",
            )
            self.assertEqual(
                more,
                index < len(pages) - 1,
                "more=True on every page but the last, which ends at the head",
            )
        seqs = [event["seq"] for page, _ in pages for event in page]
        self.assertEqual(seqs, list(range(1, 151)), "pages must tile the chain, in order")
        ids = [event["event_id"] for page, _ in pages for event in page]
        self.assertEqual(len(ids), len(set(ids)), "no event may be exported twice")
        self.assertEqual(export_page(self.store.conn, 150), ([], False))

    def test_beyond_head_returns_empty_page_and_current_head(self) -> None:
        self._seed(7)
        self.assertEqual(export_page(self.store.conn, 7), ([], False))
        self.assertEqual(export_page(self.store.conn, 999), ([], False))
        self.assertEqual(head_seq(self.store.conn), 7)

        empty = Store(Path(self._tmp.name) / "empty.db", signer=Ed25519Policy.from_identity(self.coordinator))
        try:
            self.assertEqual(head_seq(empty.conn), 0)
            self.assertEqual(export_page(empty.conn, 0), ([], False))
        finally:
            empty.close()

    def test_page_is_clamped_by_serialized_bytes(self) -> None:
        # ~1.5 KiB of metadata per event: far fewer than 100 events fit in
        # LEDGER_PAGE_BYTES, so the byte clamp must bite before the count clamp.
        self._seed(40, metadata={"pad": "x" * 1400})
        page, more = export_page(self.store.conn, 0)
        self.assertGreater(len(page), 0)
        self.assertLess(len(page), 40, "byte clamp must stop the page early")
        self.assertTrue(more)
        total = sum(len(_canonical(event)) for event in page)
        self.assertLessEqual(total, LEDGER_PAGE_BYTES)
        # The rest is still reachable, page after page.
        seqs = [event["seq"] for event in page]
        after = page[-1]["seq"]
        while more:
            page, more = export_page(self.store.conn, after)
            seqs.extend(event["seq"] for event in page)
            if page:
                after = page[-1]["seq"]
        self.assertEqual(seqs, list(range(1, 41)))

    def test_limit_is_clamped_to_the_wire_page_cap(self) -> None:
        self._seed(150)
        self.assertEqual(export_page(self.store.conn, 0, limit=-3), ([], False))
        clamped_page, _ = export_page(self.store.conn, 0, limit=1000)
        reference_page, _ = export_page(self.store.conn, 0, limit=MAX_SYNC_PAGE_EVENTS)
        self.assertEqual(clamped_page, reference_page, "an oversized limit is clamped to the page cap")
        self.assertLessEqual(len(clamped_page), MAX_SYNC_PAGE_EVENTS)

    def test_exported_page_is_valid_on_the_wire(self) -> None:
        self._seed(120)
        page, more = export_page(self.store.conn, 0)
        body = {
            "after_seq": 0,
            "head_seq": head_seq(self.store.conn),
            "events": page,
            "more": more,
        }
        validate_body(MessageType.LEDGER_SYNC_RESPONSE, body)  # must not raise
        for event in page:
            self.assertEqual(set(event), set(EVENT_KEYS))
            self.assertEqual(event["sig_algo"], "ed25519")
            self.assertLessEqual(len(event["metadata_json"]), MAX_EVENT_METADATA)

    def test_wire_schema_accepts_the_stored_event_type_and_nothing_else(self) -> None:
        """Regression for the E-1 sync stall: the stored column is the
        canonical enum value ("CREDIT"), and that exact string is signed into
        the credit body. A schema that expects "credit" silently rejects every
        real response, so the worker never syncs. Both directions are pinned
        here: the stored value is accepted, the lowercased one is rejected."""
        self._seed(1)
        page, more = export_page(self.store.conn, 0)
        self.assertEqual(page[0]["event_type"], CreditEventType.CREDIT.value)
        body = {
            "after_seq": 0,
            "head_seq": head_seq(self.store.conn),
            "events": page,
            "more": more,
        }
        validate_body(MessageType.LEDGER_SYNC_RESPONSE, body)  # must not raise
        lowercased = dict(page[0], event_type=CreditEventType.CREDIT.value.lower())
        with self.assertRaises(ProtocolError):
            validate_body(MessageType.LEDGER_SYNC_RESPONSE, dict(body, events=[lowercased]))

    def test_corrupt_event_stops_the_mirror_at_that_seq(self) -> None:
        self._seed(3)
        page, _ = export_page(self.store.conn, 0)
        policy = Ed25519Policy.from_identity(
            self.coordinator, coordinator_node_id=self.coordinator.node_id
        )
        mirror = sqlite3.connect(str(Path(self._tmp.name) / "mirror.db"))
        mirror.row_factory = sqlite3.Row
        self.addCleanup(mirror.close)

        corrupt = dict(page[1])
        corrupt["event_hash"] = "f" * 64
        self.assertTrue(verify_and_append(mirror, page[0], policy)[0])
        ok, reason = verify_and_append(mirror, corrupt, policy)
        self.assertFalse(ok)
        self.assertIn(corrupt["event_id"], reason)
        self.assertIn("event_hash mismatch", reason)
        # Nothing past the corrupt event is accepted either (no skipping).
        ok, reason = verify_and_append(mirror, page[2], policy)
        self.assertFalse(ok)
        self.assertIn("seq gap", reason)
        count = mirror.execute("SELECT COUNT(*) FROM ledger_copy").fetchone()[0]
        self.assertEqual(count, 1, "the mirror must stop, not skip, at the corrupt seq")


class WorkerSyncTests(unittest.TestCase):
    """A real worker subprocess syncing a 150-event chain over a real socket."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ledger-sync-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.coord_tmp = self.root / "coord"
        self.worker_tmp = self.root / "worker"
        self.coord_tmp.mkdir(parents=True, exist_ok=True)
        self.worker_tmp.mkdir(parents=True, exist_ok=True)
        self.coordinator_identity = NodeIdentity.load_or_create(
            self.coord_tmp / "identity.key", node_name="coordinator"
        )
        self.worker_identity = NodeIdentity.load_or_create(
            self.worker_tmp / "identity.key", node_name="worker"
        )
        self.coord_db = self.coord_tmp / "coordinator.db"
        self.worker_db = self.worker_tmp / "ledger_copy.db"

        # Seed 150 signed credit events BEFORE the node opens the DB: this is
        # the coordinator chain the worker has to pull in two pages.
        seeding = Store(self.coord_db, signer=Ed25519Policy.from_identity(self.coordinator_identity))
        try:
            with seeding.transaction():
                for i in range(150):
                    _, created = append_credit(
                        seeding.conn, seeding.signer, self.worker_identity.node_id, f"tsk_{i}"
                    )
                    self.assertTrue(created)
        finally:
            seeding.close()
        self.assertEqual(head_seq_of(self.coord_db), 150)

        config = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(self.coord_tmp),
                "peers": [self.worker_identity.node_id],
                "poll_interval_seconds": 1,
                "log_level": "WARNING",
                "log_file": str(self.coord_tmp / "coordinator.log"),
            }
        )
        self.coordinator = Node(config)
        self.addCleanup(self.coordinator.shutdown)
        self._coord_thread = threading.Thread(
            target=self.coordinator.run, name="ledger-sync-coordinator", daemon=True
        )
        self._coord_thread.start()
        self.addCleanup(self._coord_thread.join, 10.0)
        _wait_for(
            lambda: bool(self.coordinator.transport.advertised_addrs()), 20.0, "coordinator listen"
        )
        self.coord_addr = self.coordinator.transport.advertised_addrs()[0]
        self._worker: subprocess.Popen | None = None

    def _start_worker(self) -> subprocess.Popen:
        config = _worker_config(self.worker_tmp, self.coordinator_identity.node_id, self.coord_addr)
        _write_config(self.worker_tmp / "worker.json", config)
        worker = _spawn_node(self.worker_tmp / "worker.json")
        self.addCleanup(worker.wait, 10.0)
        self.addCleanup(worker.terminate)
        self._worker = worker
        return worker

    def _stop_worker(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - harness safety
                worker.kill()
                worker.wait(timeout=10)
            self._worker = None

    def _assert_mirror_matches_chain(self) -> None:
        chain = _coordinator_rows(self.coord_db)
        mirror = _mirror_rows(self.worker_db)
        self.assertEqual(len(mirror), len(chain), "mirror must have every chain event")
        self.assertEqual([row["seq"] for row in mirror], [row["seq"] for row in chain])
        for chain_row, mirror_row in zip(chain, mirror):
            self.assertEqual(mirror_row["seq"], chain_row["seq"])
            self.assertEqual(mirror_row["event_id"], chain_row["event_id"])
            self.assertEqual(mirror_row["verified"], 1, "mirrored events must be verified")
            # Column-wise identity with the coordinator chain (all 13 fields).
            self.assertEqual(set(mirror_row["event"]), set(EVENT_KEYS))
            for key in EVENT_KEYS:
                self.assertEqual(mirror_row["event"][key], chain_row[key], f"{key} diverged at seq {chain_row['seq']}")
            # And the stored text is the canonical serialization of that row.
            self.assertEqual(mirror_row["event_json"], _canonical(mirror_row["event"]))

    def test_worker_mirror_reaches_head_byte_identical(self) -> None:
        self._start_worker()
        _wait_for(
            lambda: len(_mirror_rows(self.worker_db)) >= 150,
            60.0,
            "worker mirror to reach the 150-event head (two LEDGER_SYNC pages)",
        )
        self._assert_mirror_matches_chain()

    def test_partial_mirror_resumes_from_its_own_head(self) -> None:
        self._start_worker()
        _wait_for(lambda: len(_mirror_rows(self.worker_db)) >= 150, 60.0, "initial sync")
        self._stop_worker()

        # Simulate a mirror that only got 100 of the 150 events (crash or
        # offline window): the next sync round must resume from seq 100.
        conn = sqlite3.connect(str(self.worker_db), timeout=10)
        try:
            conn.execute("DELETE FROM ledger_copy WHERE seq > 100")
            conn.commit()
            remaining = conn.execute("SELECT COUNT(*) FROM ledger_copy").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(remaining, 100)

        self._start_worker()
        _wait_for(lambda: len(_mirror_rows(self.worker_db)) >= 150, 60.0, "resumed sync")
        self._assert_mirror_matches_chain()
        ids = [row["event_id"] for row in _mirror_rows(self.worker_db)]
        self.assertEqual(len(ids), len(set(ids)), "resume must not duplicate events")


def head_seq_of(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return head_seq(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
