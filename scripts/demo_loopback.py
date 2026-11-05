"""Single-machine loopback demo for the CREDIT-for-compute flow.

This is a LIBRARY-LEVEL demo: no network sockets, no second device, no D6.
It shows the same Store/node semantics the coordinator uses:
  free path (task_cost=0) and paid path (coordinator config task_cost=1).

Run from the repo root:
    python scripts/demo_loopback.py --db var/demo/idle.db --reset
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from idle_compute.config import from_dict  # noqa: E402
from idle_compute.db import InsufficientCredit, Store  # noqa: E402
from idle_compute.worker import Worker  # noqa: E402


class Demo:
    def __init__(self, db: Path, key: Path) -> None:
        self.db = db
        self.key = key
        self.store = Store(db, key)

    def reopen(self) -> None:
        self.store = Store(self.db, self.key)

    def show(self, peer: str, label: str) -> None:
        """Close the DB, print a CLI `credit` snapshot, then reopen it."""
        self.store.close()
        print(f"\n--- {label} ---")
        print(f"$ python -m idle_compute credit --db {self.db} --peer {peer}")
        subprocess.run(
            [sys.executable, "-m", "idle_compute", "credit", "--db", str(self.db), "--peer", peer],
            cwd=str(REPO),
            check=True,
        )
        self.reopen()

    def complete(self, task_id: str, peer: str) -> None:
        lease = self.store.claim_task(peer, task_id)
        if lease is None:
            raise SystemExit(f"task {task_id} is not claimable")
        self.store.start_task(lease.lease_id, peer)
        worker = Worker(peer, self.store.signer)
        output, digest, signature = worker.execute(self.store.get_task(task_id), lease)
        result = self.store.submit_result(lease.lease_id, peer, output, digest, signature)
        validation = self.store.validate_result(result.result_id, "demo-validator")
        if validation.status.value != "VALID":
            raise SystemExit(f"validation failed: {validation.reason}")

    def earn(self, peer: str, numbers: list[int]) -> str:
        task = self.store.create_task("numeric_batch_v1", {"numbers": numbers})
        self.complete(task.task_id, peer)
        return task.task_id


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="loopback CREDIT demo (single machine)")
    parser.add_argument("--db", default="var/demo/idle.db", help="SQLite database path")
    parser.add_argument("--key", default=None, help="signing key (default: sibling ledger.key)")
    parser.add_argument("--reset", action="store_true", help="delete an existing demo DB first")
    args = parser.parse_args()

    db = Path(args.db)
    key = Path(args.key) if args.key else db.with_name("ledger.key")
    db.parent.mkdir(parents=True, exist_ok=True)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            for path in (Path(str(db) + suffix), Path(str(key) + suffix)):
                if path.exists():
                    path.unlink()

    print("LOOPBACK DEMO (single machine, no network sockets, NOT cross-device)")

    demo = Demo(db, key)
    try:
        # ------------------------------------------------------- free path
        print("\n[1] Free path: task_cost=0 (legacy default)")
        free_task = demo.store.create_task("numeric_batch_v1", {"numbers": [1, 2, 3]})
        demo.complete(free_task.task_id, "demo-worker")
        demo.show("demo-worker", "free path: worker earned CREDIT, no hold")

        # ------------------------------------------------------- paid path
        print("\n[2] Paid path: coordinator config credits.task_cost = 1")
        cfg = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(db.parent),
                "peers": [],
                "credits": {"task_cost": 1},
            }
        )
        cost = cfg.credits.task_cost
        print(f"    coordinator credits.task_cost={cost} (TASK_CREATE charges this)")

        # The requester must have earned CREDIT first (demo-buyer earns 1).
        demo.earn("demo-buyer", [10, 20])
        paid = demo.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [4, 5]}, requester_id="demo-buyer", cost=cost
        )
        print(f"    paid task {paid.task_id} created; cost={cost} is HELD")
        demo.show("demo-buyer", "paid path: available 0, held 1")

        demo.complete(paid.task_id, "demo-worker")
        print("    paid task completed and validated -> hold becomes SPENT")
        demo.show("demo-buyer", "paid path: held 0, spent 1")

        # --------------------------------------------------- insufficient
        print("\n[3] Insufficient credit: same config, buyer has 0 available")
        try:
            demo.store.create_task_paid(
                "numeric_batch_v1", {"numbers": [6]}, requester_id="demo-buyer", cost=1
            )
        except InsufficientCredit as exc:
            print(f"    rejected: code={exc.code} cost={exc.cost} available={exc.available}")

        # ------------------------------------------------------- refund
        print("\n[4] Cancel path: the HELD credit is refunded")
        demo.earn("demo-buyer", [30, 40])
        refunded = demo.store.create_task_paid(
            "numeric_batch_v1", {"numbers": [7]}, requester_id="demo-buyer", cost=cost
        )
        demo.store.cancel_task(refunded.task_id, "demo cancel")
        demo.show("demo-buyer", "cancel path: refunded 1, available 1")
    finally:
        demo.store.close()

    print("\nDemo finished. This was loopback/library-only; D6 (real dual-device) was NOT run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
