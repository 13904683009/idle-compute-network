"""Local CLI for the idle-compute MVP.

The 11 legacy commands (init/create-task/claim/run/status/events/cancel/
expire/verify-ledger/rebuild-ledger/balances) are the Phase 1 single-process
path (HMAC, --db/--key) and stay byte-compatible. The new network commands
(identity, peers) take --config only — they never touch --db/--key and never
print key material.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .coordinator import Coordinator
from .config import load_config
from .db import DuplicateSubmission, DuplicateValidation, LeaseExpired, StoreError, TaskUnavailable
from .identity import NodeIdentity, is_valid_node_id
from .ledger import LedgerError, ensure_ledger_copy, mirror_status
from .node import Node, NodeError
from .state_machine import InvalidTransition
from .tasks import InvalidTaskInput


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
