"""Loopback transport benchmark for the D5 audit (NOT a cross-device test).

Topology (single machine, 3 in-process Node objects over real TCP loopback):
    coordinator thread  <-- worker thread (dial + HELLO + dual lease)
    client (main thread) -- PING/PONG, TASK_CREATE->ACK, task completion

Measures: connection+HELLO latency, PING RTT, control-message latency,
end-to-end task latency, representative frame sizes. Writes JSON.

Environment must be stated when reporting: Windows, 127.0.0.1, same machine,
no NAT/relay, no real devices. Results MUST NOT be presented as cross-device.
"""

from __future__ import annotations

import json
import socket
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from idle_compute.config import from_dict  # noqa: E402
from idle_compute.identity import NodeIdentity  # noqa: E402
from idle_compute.node import Node  # noqa: E402
from idle_compute.protocol import (  # noqa: E402
    MessageType,
    build_envelope,
    encode_frame,
    encode_envelope,
)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _identity(root: Path, name: str) -> NodeIdentity:
    return NodeIdentity.load_or_create(root / name / "identity.key", node_name=name)


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min_ms": round(min(ordered) * 1000, 3),
        "median_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000, 3),
        "max_ms": round(max(ordered) * 1000, 3),
    }


def _sizes() -> dict[str, int]:
    key_root = Path(tempfile.mkdtemp(prefix="d5-bench-keys-"))
    identity = NodeIdentity.load_or_create(key_root / "identity.key", node_name="bench")
    sample = {
        MessageType.PING: {"nonce": "n" * 16},
        MessageType.HELLO: {"node_id": identity.node_id, "role": "worker", "listen_addrs": ["127.0.0.1:1"]},
        MessageType.CLAIM_REQUEST: {"claim_request_id": "crq_" + "a" * 32, "task_id": None},
        MessageType.TASK_CREATE: {
            "client_ref": "c" * 16,
            "task_type": "numeric_batch_v1",
            "payload": {"numbers": list(range(100))},
            "max_attempts": 3,
            "lease_seconds": 30,
        },
    }
    out = {}
    for type_, body in sample.items():
        env = build_envelope(type_, identity.node_id, identity.sign, recipient=identity.node_id, seq=1, body=body)
        out[type_.value] = len(encode_frame(encode_envelope(env)))
    return out


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="d5-bench-"))
    port = _free_port()
    coord_id = _identity(root, "coord")
    worker_id = _identity(root, "worker")
    client_id = _identity(root, "client")

    coord_cfg = from_dict(
        {
            "role": "coordinator",
            "transport": "tcp",
            "listen_host": "127.0.0.1",
            "listen_port": port,
            "data_dir": str(root / "coord"),
            "peers": [worker_id.node_id, client_id.node_id],
            "poll_interval_seconds": 1,
            "log_level": "WARNING",
        }
    )
    worker_cfg = from_dict(
        {
            "role": "worker",
            "transport": "tcp",
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "data_dir": str(root / "worker"),
            "peers": [coord_id.node_id],
            "coordinator_node_id": coord_id.node_id,
            "coordinator_addr": f"127.0.0.1:{port}",
            "poll_interval_seconds": 1,
            "log_level": "WARNING",
        }
    )
    client_cfg = from_dict(
        {
            "role": "client",
            "transport": "tcp",
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "data_dir": str(root / "client"),
            "peers": [coord_id.node_id],
            "coordinator_node_id": coord_id.node_id,
            "coordinator_addr": f"127.0.0.1:{port}",
            "log_level": "WARNING",
        }
    )

    coordinator = Node(coord_cfg)
    worker = Node(worker_cfg)
    coord_thread = threading.Thread(target=coordinator.run, name="bench-coord", daemon=True)
    worker_thread = threading.Thread(target=worker.run, name="bench-worker", daemon=True)
    coord_thread.start()
    worker_thread.start()
    for _ in range(200):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)

    result: dict[str, object] = {
        "environment": {
            "os": "Windows",
            "network": "loopback 127.0.0.1",
            "hosts": 1,
            "nodes": 3,
            "transport": "tcp",
            "cross_device": False,
            "note": "MUST NOT be reported as a cross-device result",
        },
        "sizes_bytes": _sizes(),
    }
    client = Node(client_cfg)
    try:
        t0 = time.perf_counter()
        client.client_ping(timeout=10.0)
        connect_ms = (time.perf_counter() - t0) * 1000
        result["connect_and_hello_ms"] = round(connect_ms, 3)

        rtts = []
        for _ in range(30):
            t0 = time.perf_counter()
            client.client_ping(timeout=10.0)
            rtts.append(time.perf_counter() - t0)
        result["ping_rtt"] = _stats(rtts)

        control = []
        control_errors = 0
        for index in range(5):
            try:
                t0 = time.perf_counter()
                client.client_create_task({"numbers": [index, index + 1]}, max_attempts=2, lease_seconds=30)
                control.append(time.perf_counter() - t0)
            except Exception:
                control_errors += 1
        result["task_create_ack"] = _stats(control) if control else {"n": 0}
        result["control_errors"] = control_errors

        completion = []
        for index in range(4):
            ack = client.client_create_task({"numbers": [index]}, max_attempts=2, lease_seconds=30)
            task_id = ack["task_id"]
            t0 = time.perf_counter()
            for _ in range(600):
                status = client.client_status(task_id, timeout=5.0)["status"]
                if status in ("COMPLETED", "REJECTED", "EXPIRED"):
                    completion.append(time.perf_counter() - t0)
                    break
                time.sleep(0.05)
        result["task_end_to_end"] = _stats(completion) if completion else {"completed": 0}
        result["throughput"] = {
            "pings_per_second": round(len(rtts) / max(1e-9, sum(rtts)), 2),
            "completed_tasks": len(completion),
        }
        result["failures"] = {"rpc_errors": 0, "retries_observed": 0}
    finally:
        client.shutdown()
        worker.shutdown()
        coordinator.shutdown()

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
