"""D6 preflight: local, single-machine readiness checks (D6 is NOT executed).

This script never connects to another device and never claims D6. It verifies
the local conditions that must hold before a two-device run:

- clean committed revision (unless --allow-dirty for development);
- Python/runtime and CLI command availability;
- source/documentation gates and the critical lease/credit regression subset;
- DB schema (lease_negotiations + credit_holds) and capability signing;
- valid coordinator/worker/client configs, including the public-listen guard;
- local interface/port bindability (not a firewall test);
- clock snapshot and confirmation that cross-device skew is still unmeasured.

Usage:
    python scripts/d6_preflight.py --json
    python scripts/d6_preflight.py --out var/d6/preflight.json
    python scripts/d6_preflight.py --full      # also runs the whole suite (slow)

Exit code 0 only if no FAIL checks remain. WARN is expected for items that can
only be measured in D6 (e.g. cross-device clock skew, firewall).
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

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


@dataclass
class Check:
    id: str
    status: str  # PASS / WARN / FAIL
    detail: str

    def as_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "detail": self.detail}


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)
    commit: str | None = None
    dirty: bool = False

    def add(self, check_id: str, status: str, detail: str) -> None:
        self.checks.append(Check(check_id, status, detail))

    def run_process(self, args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", *args],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def check_environment(pf: Preflight) -> None:
    version = platform.python_version()
    ok = sys.version_info >= (3, 11)
    pf.add(
        "python_version",
        "PASS" if ok else "FAIL",
        f"{version} on {platform.platform()}",
    )
    for args, check_id in (
        (["idle_compute", "--help"], "cli_help"),
        (["idle_compute", "node", "--help"], "cli_node_help"),
        (["idle_compute", "credit", "--help"], "cli_credit_help"),
    ):
        try:
            result = pf.run_process(args)
            pf.add(
                check_id,
                "PASS" if result.returncode == 0 else "FAIL",
                f"exit={result.returncode}" if result.returncode else "command available",
            )
        except Exception as exc:  # pragma: no cover - defensive
            pf.add(check_id, "FAIL", f"{type(exc).__name__}: {exc}")


def check_git(pf: Preflight, allow_dirty: bool) -> None:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True
        )
        if head.returncode != 0:
            pf.add("git_revision", "WARN", head.stderr.strip()[:200] or "not a git checkout")
            return
        pf.commit = head.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        )
        dirty = bool(status.stdout.strip())
        pf.dirty = dirty
        if not dirty:
            pf.add("git_revision", "PASS", f"HEAD={pf.commit[:12]} clean")
        elif allow_dirty:
            pf.add("git_revision", "WARN", f"HEAD={pf.commit[:12]} has tracked changes (--allow-dirty)")
        else:
            pf.add("git_revision", "FAIL", "tracked files are dirty; commit before D6")
    except FileNotFoundError:
        pf.add("git_revision", "WARN", "git executable not found")


def check_gates(pf: Preflight, full: bool) -> None:
    try:
        result = pf.run_process(["unittest", "tests.test_source_safety", "tests.test_docs"], timeout=120)
        pf.add(
            "source_doc_gates",
            "PASS" if result.returncode == 0 else "FAIL",
            "source safety + docs gate" if result.returncode == 0 else (result.stderr.strip()[-300:] or "gate failed"),
        )
    except Exception as exc:  # pragma: no cover
        pf.add("source_doc_gates", "FAIL", f"{type(exc).__name__}: {exc}")

    try:
        result = pf.run_process(
            ["unittest", "tests.test_c5_restart", "tests.test_credit_cli", "tests.test_credit_exchange"],
            timeout=180,
        )
        pf.add(
            "critical_regressions",
            "PASS" if result.returncode == 0 else "FAIL",
            "C5 + credit exchange + CLI" if result.returncode == 0 else "critical regressions failed",
        )
    except Exception as exc:  # pragma: no cover
        pf.add("critical_regressions", "FAIL", f"{type(exc).__name__}: {exc}")

    if full:
        try:
            result = pf.run_process(["unittest", "discover", "-s", "tests", "-q"], timeout=900)
            pf.add(
                "full_suite",
                "PASS" if result.returncode == 0 else "FAIL",
                "full suite green" if result.returncode == 0 else "full suite failed",
            )
        except Exception as exc:  # pragma: no cover
            pf.add("full_suite", "FAIL", f"{type(exc).__name__}: {exc}")


def check_schema(pf: Preflight) -> None:
    from idle_compute.db import Store

    with tempfile.TemporaryDirectory(prefix="d6-preflight-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        try:
            store = Store(root / "idle.db", root / "ledger.key")
            try:
                tables = {
                    row[0]
                    for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                required = {"tasks", "leases", "lease_negotiations", "credit_holds", "credit_events"}
                missing = required - tables
                pf.add(
                    "db_schema",
                    "PASS" if not missing else "FAIL",
                    "tasks/leases/lease_negotiations/credit_holds/credit_events present"
                    if not missing
                    else f"missing tables: {sorted(missing)}",
                )
            finally:
                store.close()
        except Exception as exc:
            pf.add("db_schema", "FAIL", f"{type(exc).__name__}: {exc}")


def check_config_and_crypto(pf: Preflight) -> None:
    from idle_compute.capability import CapabilityError
    from idle_compute.config import ValidationError, from_dict
    from idle_compute.discovery import build_manifest
    from idle_compute.identity import NodeIdentity

    with tempfile.TemporaryDirectory(prefix="d6-preflight-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        try:
            coord = NodeIdentity.load_or_create(root / "c" / "identity.key", node_name="c")
            worker = NodeIdentity.load_or_create(root / "w" / "identity.key", node_name="w")
            client = NodeIdentity.load_or_create(root / "cl" / "identity.key", node_name="cl")
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(root / "coord"),
                    "peers": [worker.node_id, client.node_id],
                    "credits": {"task_cost": 1},
                }
            )
            from_dict(
                {
                    "role": "worker",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(root / "worker"),
                    "peers": [coord.node_id],
                    "coordinator_node_id": coord.node_id,
                    "coordinator_addr": "127.0.0.1:1",
                    "capability": CAPABILITY,
                }
            )
            from_dict(
                {
                    "role": "client",
                    "transport": "tcp",
                    "listen_host": "127.0.0.1",
                    "listen_port": 0,
                    "data_dir": str(root / "client"),
                    "peers": [coord.node_id],
                    "coordinator_node_id": coord.node_id,
                    "coordinator_addr": "127.0.0.1:1",
                }
            )
            pf.add("config_examples", "PASS", "coordinator/worker/client parse")

            # A LAN/public bind must be explicit; this guard must stay enabled.
            try:
                from_dict(
                    {
                        "role": "coordinator",
                        "transport": "tcp",
                        "listen_host": "0.0.0.0",
                        "listen_port": 7000,
                        "data_dir": str(root / "bad"),
                        "peers": [],
                    }
                )
            except ValidationError:
                pf.add("public_listen_guard", "PASS", "non-loopback bind requires explicit opt-in")
            else:
                pf.add("public_listen_guard", "FAIL", "0.0.0.0 accepted without allow_public_listen")

            manifest = build_manifest(CAPABILITY, coord, now=int(time.time()))
            try:
                manifest.verify(int(time.time()))
                pf.add("capability_signing", "PASS", "manifest signs and verifies")
            except CapabilityError as exc:
                pf.add("capability_signing", "FAIL", f"{exc.code}: {exc.message}")
        except Exception as exc:
            pf.add("config_examples", "FAIL", f"{type(exc).__name__}: {exc}")


def check_network_and_clock(pf: Preflight) -> None:
    addresses: list[str] = []
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        addresses = sorted({ip for ip in ips if not ip.startswith("127.")})
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        pf.add(
            "port_bind",
            "PASS",
            f"loopback bind OK (ephemeral port {port}); LAN IPs: {addresses or 'none detected'}",
        )
        if not addresses:
            pf.add("lan_interface", "WARN", "no non-loopback IPv4 detected; two-device test needs one")
        else:
            pf.add("lan_interface", "PASS", f"candidate LAN address(es): {', '.join(addresses)}")
    except OSError as exc:
        pf.add("port_bind", "FAIL", f"cannot bind loopback: {exc}")

    # Firewall/NAT/reachability cannot be checked from one machine.
    pf.add("firewall_reachability", "WARN", "must be measured between the two devices in D6")

    now = datetime.now(timezone.utc)
    offset = datetime.now().astimezone().utcoffset()
    pf.add(
        "clock_snapshot",
        "PASS",
        f"UTC={now.isoformat(timespec='seconds')} local_offset={offset}",
    )
    pf.add(
        "cross_device_clock_skew",
        "WARN",
        "NOT TESTED: measure A/B UTC offset before and after D6 (tolerance to be recorded)",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="D6 local preflight (does not run D6)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--out", default=None, help="also write the JSON report to this path")
    parser.add_argument("--allow-dirty", action="store_true", help="warn instead of failing on tracked changes")
    parser.add_argument("--full", action="store_true", help="also run the full test suite (slow)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    pf = Preflight()
    check_environment(pf)
    check_git(pf, args.allow_dirty)
    check_gates(pf, args.full)
    check_schema(pf)
    check_config_and_crypto(pf)
    check_network_and_clock(pf)

    failed = [check for check in pf.checks if check.status == "FAIL"]
    report = {
        "d6_status": "NOT TESTED",
        "preflight_only": True,
        "message": "local preparation only; no cross-device test was executed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": pf.commit,
        "dirty_worktree": pf.dirty,
        "checks": [check.as_dict() for check in pf.checks],
        "summary": {
            "pass": sum(1 for check in pf.checks if check.status == "PASS"),
            "warn": sum(1 for check in pf.checks if check.status == "WARN"),
            "fail": sum(1 for check in pf.checks if check.status == "FAIL"),
            "total": len(pf.checks),
        },
        "evidence_required_in_d6": [
            "both device commit/Python/OS",
            "sanitized config snapshot",
            "preflight JSON (this file) on both devices",
            "UTC-timestamped HELLO/TASK_CREATE/LEASE_PROPOSE/ACCEPT/COMMIT logs",
            "DB lease_negotiations transitions + terms_hash",
            "results/validation VALID and exactly one credit_events row",
            "credit CLI output before/after (held -> spent)",
            "clock offset measurement on both devices",
            "negative checks (no commit/expired/cancel/misaddressed -> no execution)",
        ],
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("D6 PREFLIGHT — D6 仍为 NOT TESTED，本材料仅为准备")
        for check in pf.checks:
            print(f"[{check.status}] {check.id}: {check.detail}")
        print(
            f"summary: pass={report['summary']['pass']} warn={report['summary']['warn']} "
            f"fail={report['summary']['fail']} total={report['summary']['total']}"
        )
        print("D6 was NOT executed; cross-device clock/firewall/reachability remain UNMEASURED.")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
