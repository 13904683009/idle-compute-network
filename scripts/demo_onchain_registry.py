"""End-to-end prototype: on-chain NodeRegistry + Coordinator chain admission.

Requires a local Anvil RPC and a deployed NodeRegistry (see
docs/ONCHAIN-REGISTRY-DEMO.md). Registers a worker and a client, starts a real
Coordinator/worker/client over loopback, and completes one numeric task.

This is a prototype only. No token/escrow; no production claim.
"""

from __future__ import annotations

import argparse
import http.client
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Crypto.Hash import keccak  # noqa: E402
from idle_compute.chain_registry import ChainRegistryClient, node_key  # noqa: E402
from idle_compute.identity import NodeIdentity  # noqa: E402

CAPABILITY = {
    "capability_version": 1,
    "ttl_seconds": 3600,
    "supported_task_types": ["numeric_batch_v1"],
    "supported_runtimes": ["python_sandbox_v1"],
    "hardware": {"cpu_arch": "x86_64", "cpu_cores": 8, "ram_mb": 16384},
    "resource_limits": {"max_cpu_cores": 4, "max_ram_mb": 8192, "max_gpu_memory_mb": 0,
                        "max_task_duration_seconds": 600},
    "network": {"supported_transports": ["tcp"], "supports_relay": False},
    "verification": {"level": "SELF_DECLARED", "benchmark_profile": None},
    "available_resources": {"cpu_cores": 4, "ram_mb": 8192, "gpu_memory_mb": 0},
    "accepting_tasks": True,
}


def rpc(url: str, method: str, params: list[Any]) -> Any:
    scheme, _, rest = url.partition("://")
    host_port, _, path = rest.partition("/")
    host, _, port = host_port.partition(":")
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = cls(host, int(port), timeout=10)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    try:
        conn.request("POST", "/" + path if path else "/", body=body,
                     headers={"Content-Type": "application/json"})
        payload = json.loads(conn.getresponse().read().decode())
    finally:
        conn.close()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload.get("result")


def _selector(signature: str) -> str:
    return "0x" + keccak.new(digest_bits=256, data=signature.encode()).hexdigest()[:8]


REGISTER_SELECTOR = _selector("register(bytes32,uint8,bytes32,bytes32,bytes32)")


def register_node(rpc_url: str, registry: str, sender: str, node_id: str, role: int) -> str:
    key = node_key(node_id)[2:]
    data = REGISTER_SELECTOR + key + f"{role:064x}" + "0" * 64 * 3
    tx = rpc(rpc_url, "eth_sendTransaction",
             [{"from": sender, "to": registry, "data": data, "gas": "0x2dc6c0"}])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        receipt = rpc(rpc_url, "eth_getTransactionReceipt", [tx])
        if receipt:
            if int(receipt.get("status", "0x0"), 16) != 1:
                raise RuntimeError(f"registration reverted: {receipt}")
            return tx
        time.sleep(0.2)
    raise RuntimeError("registration receipt timeout")


def wait_port(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {host}:{port} not listening")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description="on-chain registry loopback prototype")
    parser.add_argument("--rpc", default="http://127.0.0.1:8545")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--dir", default="var/onchain-registry-demo")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    if args.reset and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    coord_id = NodeIdentity.load_or_create(root / "coord" / "identity.key", node_name="coord")
    worker_id = NodeIdentity.load_or_create(root / "worker" / "identity.key", node_name="worker")
    client_id = NodeIdentity.load_or_create(root / "client" / "identity.key", node_name="client")
    accounts = rpc(args.rpc, "eth_accounts", [])
    if len(accounts) < 3:
        raise SystemExit("Anvil must expose at least 3 accounts")
    register_node(args.rpc, args.registry, accounts[1], worker_id.node_id, 1)
    register_node(args.rpc, args.registry, accounts[2], client_id.node_id, 2)
    print("registered worker/client on chain")

    port = free_port()
    coord_cfg = {
        "role": "coordinator", "transport": "tcp", "listen_host": "127.0.0.1",
        "listen_port": port, "data_dir": str(root / "coord"), "peers": [],
        "poll_interval_seconds": 1, "log_level": "INFO",
        "log_file": str(root / "coord" / "coordinator.log"),
        "chain_registry": {
            "enabled": True, "rpc_url": args.rpc, "registry_address": args.registry,
            "refresh_seconds": 2, "timeout_seconds": 5,
        },
    }
    worker_cfg = {
        "role": "worker", "transport": "tcp", "listen_host": "127.0.0.1", "listen_port": 0,
        "data_dir": str(root / "worker"), "peers": [coord_id.node_id],
        "coordinator_node_id": coord_id.node_id,
        "coordinator_addr": f"127.0.0.1:{port}",
        "capability": CAPABILITY, "poll_interval_seconds": 1,
        "log_level": "INFO", "log_file": str(root / "worker" / "worker.log"),
    }
    client_cfg = {
        "role": "client", "transport": "tcp", "listen_host": "127.0.0.1", "listen_port": 0,
        "data_dir": str(root / "client"), "peers": [coord_id.node_id],
        "coordinator_node_id": coord_id.node_id,
        "coordinator_addr": f"127.0.0.1:{port}", "log_level": "INFO",
    }
    cfg_paths = {}
    for name, cfg in (("coordinator", coord_cfg), ("worker", worker_cfg), ("client", client_cfg)):
        path = root / f"{name}.json"
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        cfg_paths[name] = path

    procs: list[subprocess.Popen] = []
    try:
        for name in ("coordinator", "worker"):
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "idle_compute", "node", "run", "--config", str(cfg_paths[name])],
                cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        wait_port("127.0.0.1", port)
        time.sleep(2)
        create = subprocess.run(
            [sys.executable, "-m", "idle_compute", "node", "create-task",
             "--config", str(cfg_paths["client"]), "--numbers", "1,2,3"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        if create.returncode != 0:
            raise RuntimeError(create.stderr[-1000:])
        task_id = json.loads(create.stdout)["task_id"]
        print("task", task_id, "created; waiting for completion")
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline:
            probe = subprocess.run(
                [sys.executable, "-m", "idle_compute", "node", "status",
                 "--config", str(cfg_paths["client"]), "--task-id", task_id],
                cwd=str(ROOT), capture_output=True, text=True, timeout=30,
            )
            if probe.returncode == 0:
                status = json.loads(probe.stdout)
                if status.get("status") == "COMPLETED":
                    break
            time.sleep(0.5)
        if not status or status.get("status") != "COMPLETED":
            raise RuntimeError(f"task did not complete: {status}")
        reader = ChainRegistryClient(args.rpc, args.registry, refresh_seconds=1)
        count = reader.refresh(force=True)
        print(json.dumps({
            "contract": args.registry,
            "active_nodes": count,
            "task_id": task_id,
            "status": status,
            "worker_registered": reader.is_active(worker_id.node_id, "worker"),
            "client_registered": reader.is_active(client_id.node_id, "client"),
        }, indent=2))
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
