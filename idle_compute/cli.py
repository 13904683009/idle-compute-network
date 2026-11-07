"""Local CLI for the idle-compute MVP.

The 11 legacy commands (init/create-task/claim/run/status/events/cancel/
expire/verify-ledger/rebuild-ledger/balances) are the Phase 1 single-process
path (HMAC, --db/--key) and stay byte-compatible. The new network commands
(identity, peers) take --config only — they never touch --db/--key and never
print key material.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sqlite3
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .coordinator import Coordinator
from .config import from_dict, load_config
from .db import (
    DuplicateSubmission,
    DuplicateValidation,
    InviteError,
    LeaseExpired,
    Store,
    StoreError,
    TaskUnavailable,
)
from .identity import NodeIdentity, is_valid_node_id
from .ledger import LedgerError, ensure_ledger_copy, mirror_status
from .net_transport import TransportError
from .node import Node, NodeError, NodeRemoteError
from .state_machine import InvalidTransition
from .tasks import InvalidTaskInput
from .transport_tcp import fetch_server_certificate


class JoinError(ValueError):
    """Actionable join failure (invite state/connection/certificate)."""


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parse_numbers(text: str) -> list[int]:
    values = [item.strip() for item in text.split(",") if item.strip()]
    if not values:
        raise InvalidTaskInput("--numbers must contain at least one integer")
    return [int(item) for item in values]


def _coordinator(args: argparse.Namespace) -> Coordinator:
    return Coordinator(Path(args.db), Path(args.key) if args.key else None)


def _read_config(path: Path) -> dict[str, Any]:
    """Read a node config JSON; a missing file is an empty config."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed config JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"config is not a JSON object: {path}")
    return value


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_peers_shape(config: dict[str, Any]) -> bool:
    """Rewrite ``config`` in place to peers=list + directory (D2, ADR-D0-13).

    Returns True when the legacy object form was migrated (roles/addrs are
    preserved; nothing is dropped). The caller writes the new shape.
    """
    peers_raw = config.get("peers", [])
    if isinstance(peers_raw, dict):
        directory = dict(peers_raw)
        if config.get("directory") is not None:
            # One source of truth: explicit new-format values win, but legacy
            # entries are merged in rather than dropped.
            merged = dict(directory)
            merged.update(config["directory"])
            directory = merged
        config["peers"] = list(peers_raw)
        config["directory"] = directory
        return True
    if isinstance(peers_raw, list):
        config.setdefault("directory", {})
        return False
    raise ValueError(
        f"peers must be a list (or the legacy object form), got {type(peers_raw).__name__}"
    )


def _identity_path(config: dict[str, Any], config_path: Path) -> Path:
    return Path(config.get("identity_key", config_path.parent / "identity.key"))


def cmd_identity(args: argparse.Namespace) -> int:
    """Create/load the node identity and print ONLY the node_id."""
    config_path = Path(args.config)
    config = _read_config(config_path)
    identity = NodeIdentity.load_or_create(
        _identity_path(config, config_path), node_name=config_path.parent.name
    )
    _print({"node_id": identity.node_id})
    return 0


def cmd_peers_add(args: argparse.Namespace) -> int:
    if not is_valid_node_id(args.node_id):
        raise ValueError(
            f"malformed node_id: {args.node_id!r} (expected a 52-char libp2p "
            "Ed25519 peer id starting with 12D3KooW)"
        )
    config_path = Path(args.config)
    config = _read_config(config_path)
    migrated = _normalize_peers_shape(config)
    peers = config.setdefault("peers", [])
    if args.node_id not in peers:
        peers.append(args.node_id)
    directory = config.setdefault("directory", {})
    entry = directory.get(args.node_id)
    if not isinstance(entry, dict):
        entry = {"role": args.role, "addrs": []}
    directory[args.node_id] = entry
    entry["role"] = args.role
    addrs = entry.get("addrs")
    if not isinstance(addrs, list):
        addrs = []
    entry["addrs"] = addrs
    if args.addr and args.addr not in addrs:
        addrs.append(args.addr)
    _write_config(config_path, config)
    if migrated:
        print(
            json.dumps(
                {
                    "migration": "legacy 'peers' object rewritten as a node-id list + "
                    "'directory' (roles and addrs preserved)"
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    _print({"peers": peers, "directory": directory})
    return 0


def cmd_peers_list(args: argparse.Namespace) -> int:
    config = _read_config(Path(args.config))
    _normalize_peers_shape(config)
    _print({"peers": config.get("peers", []), "directory": config.get("directory", {})})
    return 0


# ---- node commands (--config only, never --db/--key) ----


def cmd_node_run(args: argparse.Namespace) -> int:
    """Run one configured network node (coordinator/worker/client serve)."""
    node = Node(load_config(args.config))
    return node.run()


def cmd_node_create_task(args: argparse.Namespace) -> int:
    node = Node(load_config(args.config))
    try:
        ack = node.client_create_task(
            {"numbers": _parse_numbers(args.numbers)},
            max_attempts=args.max_attempts,
            lease_seconds=args.lease_seconds,
        )
        _print(ack)
        return 0
    finally:
        node.shutdown()


def cmd_node_status(args: argparse.Namespace) -> int:
    node = Node(load_config(args.config))
    try:
        _print(node.client_status(args.task_id))
        return 0
    finally:
        node.shutdown()


def cmd_node_ping(args: argparse.Namespace) -> int:
    node = Node(load_config(args.config))
    try:
        _print(node.client_ping())
        return 0
    finally:
        node.shutdown()


def cmd_worker_ledger_status(args: argparse.Namespace) -> int:
    """Report this worker's local mirror and any latched divergence (E-2)."""
    config = load_config(args.config)
    if config.role != "worker":
        raise NodeError(
            f"worker ledger status needs a worker config, got role={config.role!r}"
        )
    db_path = Path(config.data_dir) / "ledger_copy.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            ensure_ledger_copy(conn)
            mirror = mirror_status(conn)
        finally:
            conn.close()
    else:
        mirror = {"head": 0, "events": 0, "verified": 0, "conflict": None}
    conflict = mirror["conflict"]
    _print(
        {
            "coordinator_node_id": config.coordinator_node_id,
            "mirror_head": mirror["head"],
            "events": mirror["events"],
            "verified": mirror["verified"],
            "conflict": conflict,
            # The worker skips claiming while a conflict is latched; reporting
            # a boolean keeps scripts from re-deriving that rule.
            "claim_blocked": conflict is not None,
        }
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    coordinator.close()
    _print({"initialized": True, "db": str(Path(args.db))})
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        task = coordinator.create_numeric_task(
            _parse_numbers(args.numbers),
            max_attempts=args.max_attempts,
            lease_seconds=args.lease_seconds,
        )
        _print(task)
        return 0
    finally:
        coordinator.close()


def cmd_claim(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        lease = coordinator.claim(args.peer, args.task_id)
        _print({"lease": lease})
        return 0
    finally:
        coordinator.close()


def cmd_run(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        _print(coordinator.run_one(args.peer, args.task_id))
        return 0
    finally:
        coordinator.close()


def cmd_status(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        if args.task_id:
            status = coordinator.status(args.task_id)
            if status is None:
                _print({"error": "task not found"})
                return 2
            _print(status)
        else:
            _print({"tasks": coordinator.list_tasks()})
        return 0
    finally:
        coordinator.close()


def cmd_events(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        events, errors = coordinator.events(args.peer)
        _print({"events": events, "errors": errors})
        return 0
    finally:
        coordinator.close()


def cmd_cancel(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        _print(coordinator.cancel(args.task_id, args.reason))
        return 0
    finally:
        coordinator.close()


def cmd_expire(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        _print({"expired_leases": coordinator.expire()})
        return 0
    finally:
        coordinator.close()


def cmd_verify(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        _print(coordinator.verify_ledger())
        return 0
    finally:
        coordinator.close()


def cmd_rebuild(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        _print(coordinator.rebuild_ledger())
        return 0
    finally:
        coordinator.close()


def cmd_balances(args: argparse.Namespace) -> int:
    coordinator = _coordinator(args)
    try:
        _print({"balances": coordinator.balances()})
        return 0
    finally:
        coordinator.close()


def cmd_client_tasks(args: argparse.Namespace) -> int:
    """List tasks a client peer created, with the CREDIT hold state per task."""
    coordinator = _coordinator(args)
    try:
        payload = coordinator.store.client_tasks(args.peer, limit=args.limit)
    finally:
        coordinator.close()
    _print(payload)
    return 0


def cmd_worker_tasks(args: argparse.Namespace) -> int:
    """List tasks a worker peer submitted, with the CREDIT earned per task."""
    coordinator = _coordinator(args)
    try:
        payload = coordinator.store.worker_tasks(args.peer, limit=args.limit)
    finally:
        coordinator.close()
    _print(payload)
    return 0


def cmd_credit(args: argparse.Namespace) -> int:
    """Show earned/held/spent/refunded/available CREDIT per peer."""
    coordinator = _coordinator(args)
    try:
        summary = coordinator.store.credit_summary()
    finally:
        coordinator.close()
    if args.peer:
        summary = {
            args.peer: summary.get(
                args.peer,
                {"earned": 0, "held": 0, "spent": 0, "refunded": 0, "available": 0},
            )
        }
    _print({"credit": summary})
    return 0


def _invite_code(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _invite_payload(code: str) -> dict[str, Any]:
    padded = code + "=" * (-len(code) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise ValueError(f"malformed invite code: {exc}") from None
    if not isinstance(value, dict) or value.get("v") != 1:
        raise ValueError("malformed invite code: unsupported version")
    for key in ("secret", "coordinator_node_id", "addr", "role"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"invite code missing {key}")
    return value


def _cert_fingerprint(path: str) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(Path(path).read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()


def _public_addr(cfg: Any) -> str:
    """Best-effort public address for an invite (cert SAN preferred)."""
    port = int(cfg.listen_port)
    if cfg.tls is not None and cfg.tls.cert_file:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(Path(cfg.tls.cert_file).read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        ips = san.get_values_for_type(x509.IPAddress)
        names = san.get_values_for_type(x509.DNSName)
        host = str(ips[0]) if ips else (names[0] if names else "")
        if host:
            return f"{host}:{port}"
    if cfg.listen_host in ("0.0.0.0", "::", ""):
        raise ValueError("cannot infer public address; pass --addr <host:port>")
    return f"{cfg.listen_host}:{port}"


def _fetch_peer_cert(addr: str, timeout: float = 10.0) -> bytes:
    return fetch_server_certificate(addr, timeout=timeout)


def cmd_invite_create(args: argparse.Namespace) -> int:
    if not 60 <= args.ttl <= 86400:
        raise ValueError("--ttl must be in 60..86400 seconds")
    cfg = load_config(args.config)
    data_dir = Path(cfg.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    identity = NodeIdentity.load_or_create(data_dir / "identity.key", node_name=data_dir.name)
    addr = args.addr or _public_addr(cfg)
    cert_sha = (
        _cert_fingerprint(cfg.tls.cert_file)
        if cfg.tls is not None and cfg.tls.cert_file
        else ""
    )
    secret = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=args.ttl)).isoformat()
    db_path = Path(cfg.db_path) if cfg.db_path else data_dir / "coordinator.db"
    store = Store(db_path, data_dir / "ledger.key")
    try:
        store.create_invite(
            hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            args.role,
            expires_at,
        )
    finally:
        store.close()
    payload = {
        "v": 1,
        "secret": secret,
        "coordinator_node_id": identity.node_id,
        "addr": addr,
        "role": args.role,
        "cert_sha256": cert_sha,
        "expires_at": expires_at,
    }
    _print(
        {
            "invite": _invite_code(payload),
            "role": args.role,
            "addr": addr,
            "coordinator_node_id": identity.node_id,
            "expires_at": expires_at,
            "cert_sha256": cert_sha,
        }
    )
    return 0


_DEFAULT_CAPABILITY = {
    "capability_version": 1,
    "ttl_seconds": 3600,
    "supported_task_types": ["numeric_batch_v1"],
    "supported_runtimes": ["python_sandbox_v1"],
    "hardware": {"cpu_arch": "x86_64", "cpu_cores": 4, "ram_mb": 8192},
    "resource_limits": {
        "max_cpu_cores": 2,
        "max_ram_mb": 4096,
        "max_gpu_memory_mb": 0,
        "max_task_duration_seconds": 600,
    },
    "network": {"supported_transports": ["tcp"], "supports_relay": False},
    "verification": {"level": "SELF_DECLARED", "benchmark_profile": None},
    "available_resources": {"cpu_cores": 2, "ram_mb": 4096, "gpu_memory_mb": 0},
    "accepting_tasks": True,
}


def cmd_join(args: argparse.Namespace) -> int:
    code = _invite_payload(args.invite)
    role = code["role"]
    if role == "any":
        if not args.role:
            raise ValueError("invite is role-agnostic; pass --role worker|client")
        role = args.role
    elif args.role and args.role != role:
        raise ValueError(f"invite is for role {role!r}, not {args.role!r}")
    data_dir = Path(args.data_dir or f"var/remote/{role}")
    config_out = Path(args.config_out) if args.config_out else data_dir / "node.json"
    config: dict[str, Any] = {
        "role": role,
        "transport": "tcp",
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "data_dir": str(data_dir),
        "peers": [code["coordinator_node_id"]],
        "coordinator_node_id": code["coordinator_node_id"],
        "coordinator_addr": code["addr"],
        "poll_interval_seconds": 1,
        "log_level": "INFO",
        "log_file": str(data_dir / f"{role}.log"),
    }
    expected = code.get("cert_sha256") or ""
    if expected:
        try:
            der = _fetch_peer_cert(code["addr"])
        except Exception as exc:
            raise JoinError(
                f"无法连接 Coordinator {code['addr']} 获取证书：{exc}。"
                "请检查公网地址/端口、安全组/防火墙和网络。"
            ) from exc
        actual = hashlib.sha256(der).hexdigest()
        if actual != expected:
            raise JoinError(
                "证书指纹不符，已拒绝连接。可能连接到了错误服务器或邀请码被篡改；"
                "请核对公网地址，并向 Coordinator 重新申请邀请码。"
            )
        ca_path = Path(args.ca_file or "certs/server.crt")
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_text(ssl.DER_cert_to_PEM_cert(der), encoding="ascii")
        config["tls"] = {
            "ca_file": str(ca_path),
            "server_hostname": code["addr"].rsplit(":", 1)[0].strip("[]"),
        }
    if role == "worker":
        config["capability"] = _DEFAULT_CAPABILITY
        config["discovery"] = {"announce_interval_seconds": 5}
        config["poll_interval_seconds"] = 1
    node = Node(from_dict(config))
    try:
        node.join_with_invite(code["secret"], timeout=args.timeout)
        local_node_id = node.identity.node_id
    except NodeRemoteError as exc:
        text = str(exc)
        reason = text.split("invite rejected:", 1)[-1].strip() if "invite rejected:" in text else ""
        messages = {
            "expired": "邀请码已过期。请向 Coordinator 重新申请一次性邀请码。",
            "used": "邀请码已被使用。每个邀请码只能加入一次，请申请新的邀请码。",
            "role_mismatch": "邀请码角色不匹配。Worker/Client 请使用对应角色的邀请码。",
            "invalid": "邀请码无效（不存在、格式错误或已失效）。请核对完整邀请码。",
        }
        raise JoinError(messages.get(reason, f"加入被 Coordinator 拒绝：{text}")) from exc
    except (TransportError, NodeError) as exc:
        raise JoinError(
            f"无法连接 Coordinator {code['addr']}：{exc}。"
            "请检查公网地址/端口、安全组/防火墙、TLS 证书和网络。"
        ) from exc
    finally:
        node.shutdown()
    config_out.parent.mkdir(parents=True, exist_ok=True)
    config_out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if role == "worker":
        next_steps = [
            f"python -m idle_compute node run --config {config_out}",
            "在另一终端用 Client 的邀请码 join 并创建任务",
        ]
    else:
        next_steps = [
            "先确保配套 Worker 已启动",
            f"python -m idle_compute node create-task --config {config_out} --numbers 1,2,3",
            f"python -m idle_compute node status --config {config_out} --task-id <TASK_ID>",
        ]
    _print(
        {
            "joined": True,
            "node_id": local_node_id,
            "coordinator_node_id": code["coordinator_node_id"],
            "coordinator_addr": code["addr"],
            "config": str(config_out),
            "cert_sha256": expected or None,
            "next": next_steps,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="var/idle.db", help="SQLite database path")
    common.add_argument("--key", default=None, help="signing key path (default: sibling ledger.key)")

    parser = argparse.ArgumentParser(description="Idle Compute Network local MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", parents=[common], help="initialize database and local signing key")
    init.set_defaults(func=cmd_init)

    create = sub.add_parser("create-task", parents=[common], help="create a whitelisted numeric task")
    create.add_argument("--numbers", required=True, help="comma-separated integers")
    create.add_argument("--max-attempts", type=int, default=3)
    create.add_argument("--lease-seconds", type=int, default=30)
    create.set_defaults(func=cmd_create)

    claim = sub.add_parser("claim", parents=[common], help="claim a queued task")
    claim.add_argument("--peer", required=True)
    claim.add_argument("--task-id", default=None)
    claim.set_defaults(func=cmd_claim)

    run = sub.add_parser("run", parents=[common], help="claim, execute, validate, and credit one task")
    run.add_argument("--peer", required=True)
    run.add_argument("--task-id", default=None)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", parents=[common], help="show task status or list tasks")
    status.add_argument("--task-id", default=None)
    status.set_defaults(func=cmd_status)

    events = sub.add_parser("events", parents=[common], help="list credit events")
    events.add_argument("--peer", default=None)
    events.set_defaults(func=cmd_events)

    cancel = sub.add_parser("cancel", parents=[common], help="cancel a task")
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--reason", default="cancelled by user")
    cancel.set_defaults(func=cmd_cancel)

    expire = sub.add_parser("expire", parents=[common], help="expire overdue leases")
    expire.set_defaults(func=cmd_expire)

    verify = sub.add_parser("verify-ledger", parents=[common], help="verify the credit event chain")
    verify.set_defaults(func=cmd_verify)

    rebuild = sub.add_parser("rebuild-ledger", parents=[common], help="rebuild balances from events")
    rebuild.set_defaults(func=cmd_rebuild)

    balances = sub.add_parser("balances", parents=[common], help="show ledger balances")
    balances.set_defaults(func=cmd_balances)

    credit = sub.add_parser(
        "credit",
        parents=[common],
        help="show earned/held/spent/refunded/available CREDIT per peer",
    )
    credit.add_argument("--peer", default=None, help="only show this peer (zeros if unknown)")
    credit.set_defaults(func=cmd_credit)

    worker_tasks = sub.add_parser(
        "worker-tasks",
        parents=[common],
        help="list tasks a worker peer submitted, with CREDIT earned",
    )
    worker_tasks.add_argument("--peer", required=True, help="worker node_id")
    worker_tasks.add_argument("--limit", type=int, default=50, help="max tasks (1..1000)")
    worker_tasks.set_defaults(func=cmd_worker_tasks)

    invite = sub.add_parser("invite", help="create one-time enrollment invites")
    invite_sub = invite.add_subparsers(dest="invite_command", required=True)
    invite_create = invite_sub.add_parser("create", help="create a one-time invite code")
    invite_create.add_argument("--config", required=True, help="coordinator config path")
    invite_create.add_argument("--role", choices=["worker", "client", "any"], default="worker")
    invite_create.add_argument("--ttl", type=int, default=3600, help="validity seconds (60..86400)")
    invite_create.add_argument("--addr", default=None, help="public host:port (default: cert SAN/listen)")
    invite_create.set_defaults(func=cmd_invite_create)

    join = sub.add_parser("join", help="join a coordinator with a one-time invite code")
    join.add_argument("--invite", required=True, help="invite code from 'invite create'")
    join.add_argument("--role", choices=["worker", "client"], default=None)
    join.add_argument("--data-dir", default=None)
    join.add_argument("--config-out", default=None)
    join.add_argument("--ca-file", default=None, help="where to save the pinned coordinator cert")
    join.add_argument("--timeout", type=float, default=30.0)
    join.set_defaults(func=cmd_join)

    client_tasks = sub.add_parser(
        "client-tasks",
        parents=[common],
        help="list tasks a client peer created, with CREDIT hold state",
    )
    client_tasks.add_argument("--peer", required=True, help="requester/client node_id")
    client_tasks.add_argument("--limit", type=int, default=50, help="max tasks (1..1000)")
    client_tasks.set_defaults(func=cmd_client_tasks)

    # ---- network commands (--config only, never --db/--key) ----

    identity = sub.add_parser(
        "identity", help="create/load the node identity and print its node_id only"
    )
    identity.add_argument(
        "--config", default="var/node/node.json", help="node config JSON path"
    )
    identity.set_defaults(func=cmd_identity)

    peers = sub.add_parser("peers", help="manage static peer entries in the node config")
    peers_sub = peers.add_subparsers(dest="peers_command", required=True)
    peers_add = peers_sub.add_parser("add", help="add or update a peer entry")
    peers_add.add_argument("--config", default="var/node/node.json", help="node config JSON path")
    peers_add.add_argument("node_id")
    peers_add.add_argument("--role", required=True, choices=["coordinator", "worker", "client"])
    peers_add.add_argument("--addr", default=None, help="multiaddr to append to the peer entry")
    peers_add.set_defaults(func=cmd_peers_add)
    peers_list = peers_sub.add_parser("list", help="list configured peers")
    peers_list.add_argument("--config", default="var/node/node.json", help="node config JSON path")
    peers_list.set_defaults(func=cmd_peers_list)

    node = sub.add_parser("node", help="run or drive a real network node (Phase C)")
    node_sub = node.add_subparsers(dest="node_command", required=True)

    node_run = node_sub.add_parser("run", help="run this node's role until SIGINT")
    node_run.add_argument("--config", default="var/node/node.json", help="node config JSON path")
    node_run.set_defaults(func=cmd_node_run)

    node_create = node_sub.add_parser("create-task", help="create one whitelisted task on the coordinator")
    node_create.add_argument("--config", default="var/node/node.json", help="node config JSON path")
    node_create.add_argument("--numbers", required=True, help="comma-separated integers")
    node_create.add_argument("--max-attempts", type=int, default=3)
    node_create.add_argument("--lease-seconds", type=int, default=30)
    node_create.set_defaults(func=cmd_node_create_task)

    node_status = node_sub.add_parser("status", help="query one task's status on the coordinator")
    node_status.add_argument("--config", default="var/node/node.json", help="node config JSON path")
    node_status.add_argument("--task-id", required=True)
    node_status.set_defaults(func=cmd_node_status)

    node_ping = node_sub.add_parser("ping", help="PING/PONG the coordinator")
    node_ping.add_argument("--config", default="var/node/node.json", help="node config JSON path")
    node_ping.set_defaults(func=cmd_node_ping)

    worker = sub.add_parser("worker", help="inspect this worker's local state (Phase E)")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_ledger = worker_sub.add_parser("ledger", help="worker ledger mirror operations")
    worker_ledger_sub = worker_ledger.add_subparsers(
        dest="worker_ledger_command", required=True
    )
    worker_ledger_status = worker_ledger_sub.add_parser(
        "status", help="report mirror head/verified counts and any latched conflict"
    )
    worker_ledger_status.add_argument(
        "--config", default="var/node/node.json", help="node config JSON path"
    )
    worker_ledger_status.set_defaults(func=cmd_worker_ledger_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    # TypeError and sqlite3.Error cover malformed persisted rows (e.g. NULL
    # tampering) and database-level failures (corrupt/locked DB) so they
    # surface as structured JSON errors instead of raw tracebacks.
    except (InvalidTaskInput, StoreError, LedgerError, InvalidTransition, DuplicateSubmission, DuplicateValidation, LeaseExpired, TaskUnavailable, NodeError, ValueError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
