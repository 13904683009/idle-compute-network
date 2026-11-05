"""Three-process loopback network demo: coordinator + worker + client.

Single machine only. This is NOT D6 and NOT a cross-device validation: all
processes share 127.0.0.1 and the same disk. It exercises the real wire path:

  TASK_CREATE -> CLAIM_REQUEST -> LEASE_PROPOSE -> LEASE_ACCEPT ->
  LEASE_COMMIT -> execute -> SUBMIT -> validate -> CREDIT

Phase 1 (free):   coordinator task_cost=0, worker W, client C
Phase 2 (paid):   coordinator task_cost=1 (same DB), client W (the worker
                  that earned 1 CREDIT in phase 1), new worker W2

Run: python scripts/demo_network_loopback.py --dir var/demo-network --reset
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from idle_compute.identity import NodeIdentity  # noqa: E402

CAPABILITY = {
    "capability_version": 1,
    "ttl_seconds": 3600,
    "supported_task_types": ["numeric_batch_v1"],
    "supported_runtimes": ["python_sandbox_v1"],
    "hardware": {"cpu_arch": "x86_64", "cpu_cores": 8, "ram_mb": 16384},
    "resource_limits": {
        "max_cpu_cores": 4,
        "max_ram_mb": 8192,
        "max_gpu_memory_mb": 0,
        "max_task_duration_seconds": 600,
    },
    "network": {"supported_transports": ["tcp"], "supports_relay": False},
    "verification": {"level": "SELF_DECLARED", "benchmark_profile": None},
    "available_resources": {"cpu_cores": 4, "ram_mb": 8192, "gpu_memory_mb": 0},
    "accepting_tasks": True,
}


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def wait_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"coordinator did not listen on 127.0.0.1:{port}")


class Demo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.procs: list[subprocess.Popen] = []
        self.coord = NodeIdentity.load_or_create(root / "coord" / "identity.key", node_name="coord")
        self.worker = NodeIdentity.load_or_create(root / "w" / "identity.key", node_name="w")
        self.client = NodeIdentity.load_or_create(root / "client" / "identity.key", node_name="client")
        self.worker2 = NodeIdentity.load_or_create(root / "w2" / "identity.key", node_name="w2")

    # ------------------------------------------------------------- processes
    def start(self, name: str, config_path: Path) -> subprocess.Popen:
        log = self.root / f"{name}.log"
        handle = log.open("ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "idle_compute", "node", "run", "--config", str(config_path)],
            cwd=str(REPO),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        self.procs.append(proc)
        print(f"    started {name} (pid {proc.pid}) -> {log.name}")
        return proc

    def stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if proc in self.procs:
            self.procs.remove(proc)

    def stop_all(self) -> None:
        for proc in list(self.procs):
            self.stop(proc)

    # ------------------------------------------------------------------- cli
    def run_cli(self, args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "idle_compute", *args],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def create_task(self, client_config: Path, numbers: str) -> str:
        result = self.run_cli(
            ["node", "create-task", "--config", str(client_config), "--numbers", numbers]
        )
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            raise SystemExit(f"create-task failed (exit {result.returncode})")
        payload = json.loads(result.stdout)
        print(f"    client: TASK_CREATE -> {payload['task_id']}")
        return str(payload["task_id"])

    def wait_completed(self, client_config: Path, task_id: str, timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            result = self.run_cli(
                ["node", "status", "--config", str(client_config), "--task-id", task_id]
            )
            if result.returncode == 0:
                last = json.loads(result.stdout)
                status = last.get("status") or last.get("task", {}).get("status")
                if status == "COMPLETED":
                    print(f"    client: task {task_id} -> COMPLETED")
                    return last
            time.sleep(0.5)
        raise SystemExit(f"task {task_id} did not complete: {last}")

    def show_credit(self, peer: str, label: str) -> None:
        db = self.root / "coord" / "coordinator.db"
        print(f"\n--- {label} ---")
        print(f"$ python -m idle_compute credit --db {db} --peer {peer}")
        result = self.run_cli(["credit", "--db", str(db), "--peer", peer])
        print(result.stdout.rstrip())
        if result.returncode != 0:
            raise SystemExit("credit command failed")

    # ---------------------------------------------------------------- config
    def write_config(self, name: str, data: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def coordinator_config(
        self, port: int, peers: list[str], cost: int, name: str
    ) -> Path:
        return self.write_config(
            name,
            {
                "role": "coordinator",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "data_dir": str(self.root / "coord"),
                "peers": peers,
                "credits": {"task_cost": cost},
                "poll_interval_seconds": 1,
                "log_level": "DEBUG",
                "log_file": str(self.root / f"coord-{name}.log"),
            },
        )

    def worker_config(self, name: str, data_dir: Path, coord_port: int) -> Path:
        return self.write_config(
            name,
            {
                "role": "worker",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(data_dir),
                "peers": [self.coord.node_id],
                "coordinator_node_id": self.coord.node_id,
                "coordinator_addr": f"127.0.0.1:{coord_port}",
                "capability": CAPABILITY,
                "discovery": {"announce_interval_seconds": 5, "query_timeout_seconds": 5},
                "poll_interval_seconds": 1,
                "log_level": "DEBUG",
                "log_file": str(self.root / f"{name}.log"),
            },
        )

    def client_config(self, name: str, data_dir: Path, coord_port: int) -> Path:
        return self.write_config(
            name,
            {
                "role": "client",
                "transport": "tcp",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "data_dir": str(data_dir),
                "peers": [self.coord.node_id],
                "coordinator_node_id": self.coord.node_id,
                "coordinator_addr": f"127.0.0.1:{coord_port}",
                "log_level": "INFO",
            },
        )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="three-process loopback network demo")
    parser.add_argument("--dir", default="var/demo-network", help="working directory")
    parser.add_argument("--reset", action="store_true", help="delete the working directory first")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    if args.reset and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    print("NETWORK LOOPBACK DEMO (single machine, 127.0.0.1; NOT cross-device, NOT D6)")
    demo = Demo(root)
    try:
        # ------------------------------------------------------------ free
        print("\n[1] Free path (coordinator credits.task_cost=0)")
        free_port_num = free_port()
        free_coord = demo.start(
            "coord-free",
            demo.coordinator_config(free_port_num, [demo.worker.node_id, demo.client.node_id], 0, "free.json"),
        )
        wait_port(free_port_num)
        demo.start("worker-free", demo.worker_config("worker-free.json", root / "w", free_port_num))
        time.sleep(2)
        free_client_cfg = demo.client_config("client-free.json", root / "client", free_port_num)
        free_task = demo.create_task(free_client_cfg, "1,2,3")
        demo.wait_completed(free_client_cfg, free_task)
        demo.show_credit(demo.worker.node_id, "free path: worker earned CREDIT (no hold)")
        demo.stop_all()

        # ------------------------------------------------------------ paid
        print("\n[2] Paid path (same DB restarted with credits.task_cost=1)")
        paid_port_num = free_port()
        demo.start(
            "coord-paid",
            demo.coordinator_config(
                paid_port_num, [demo.worker.node_id, demo.worker2.node_id], 1, "paid.json"
            ),
        )
        wait_port(paid_port_num)
        # The client in this phase reuses the worker identity that earned the
        # credit above; its data_dir/seq continues, so replay protection holds.
        paid_client_cfg = demo.client_config("client-paid.json", root / "w", paid_port_num)
        # Create the paid task BEFORE starting the worker, so the HELD state is
        # observable; the worker then claims it through the normal network path.
        paid_task = demo.create_task(paid_client_cfg, "4,5")
        demo.show_credit(demo.worker.node_id, "paid path: available 0, held 1 before execution")
        demo.start("worker-paid", demo.worker_config("worker-paid.json", root / "w2", paid_port_num))
        demo.wait_completed(paid_client_cfg, paid_task)
        demo.show_credit(demo.worker.node_id, "paid path: held 0, spent 1 after completion")
        db = root / "coord" / "coordinator.db"
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT lease_id, state FROM lease_negotiations ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM tasks), (SELECT COUNT(*) FROM results), "
                "(SELECT COUNT(*) FROM credit_events)"
            ).fetchone()
        finally:
            conn.close()
        print(f"    coordinator DB: lease {row[0]} final negotiation state={row[1]}")
        print(f"    tasks={counts[0]} results={counts[1]} credit_events={counts[2]}")

        # ------------------------------------------------------ insufficient
        print("\n[3] Paid path: second task with 0 available credit")
        result = demo.run_cli(
            ["node", "create-task", "--config", str(paid_client_cfg), "--numbers", "6"]
        )
        combined = (result.stdout + result.stderr).strip()
        if "INSUFFICIENT_CREDIT" not in combined:
            raise SystemExit(f"expected INSUFFICIENT_CREDIT, got exit={result.returncode}: {combined[-500:]}")
        print("    rejected with INSUFFICIENT_CREDIT (wire code 17); no task created")
    finally:
        demo.stop_all()

    print("\nDemo finished: loopback only; D6 / real dual-device was NOT run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
