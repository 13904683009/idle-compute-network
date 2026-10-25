"""Local CLI for the idle-compute MVP."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .coordinator import Coordinator
from .db import DuplicateSubmission, DuplicateValidation, LeaseExpired, StoreError, TaskUnavailable
from .ledger import LedgerError
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    # TypeError and sqlite3.Error cover malformed persisted rows (e.g. NULL
    # tampering) and database-level failures (corrupt/locked DB) so they
    # surface as structured JSON errors instead of raw tracebacks.
    except (InvalidTaskInput, StoreError, LedgerError, InvalidTransition, DuplicateSubmission, DuplicateValidation, LeaseExpired, TaskUnavailable, ValueError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
