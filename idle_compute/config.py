"""Strict validated node configuration (Phase C, C-3).

``NodeConfig`` is a plain dataclass; construction goes through ``from_dict``
(or ``load_config`` for a JSON file), which enforces every rule below and
rejects anything unexpected — unknown keys (typo protection), wrong types,
and cross-field conflicts — with a ``ValidationError`` whose message names
the offending field. A missing/unreadable/malformed config FILE raises
``ConfigError`` instead, so file-level and value-level failures stay
distinct.

The node-id shape check is implemented inline: identity.py is one of the two
modules allowed to import libp2p and config.py must not, so the rule — a str
of exactly 52 chars starting with "12D3KooW", every char in the base58
alphabet — is spelled out here (kept identical to identity.is_valid_node_id).

Peers default-deny: ``peers`` is the static inbound allowlist of node ids.
An EMPTY list means default-deny — nobody may talk to this node until a
HELLO arrives from a listed peer id; there is no implicit trust of unknown
callers.

Peer metadata (D2, ADR-D0-13): ``directory`` optionally maps a node id to
``{role, addrs}``. It is metadata only — the trust decision stays ``peers``.
Every directory key must also be in ``peers`` so the file cannot describe a
peer it does not allow. The legacy object form of ``peers`` (written by
``peers add`` before D2) is still READ, migrated in memory (no role/addr is
dropped) and reported through ``NodeConfig.migration_hint``; the writer always
emits the list + directory shape.

Public-listen authorization: a listen_host that is not loopback (127.0.0.1,
anything in 127.0.0.0/8, ::1, localhost) requires ``allow_public_listen`` to
be true. There is no silent default that would expose a node on a public
address.

DB location exclusivity: the ledger lives either under ``data_dir`` (default
naming) or at an explicit ``db_path`` — never both. A non-None ``db_path``
together with a non-empty ``data_dir`` is ambiguous and rejected; a
``db_path`` is only accepted when ``data_dir`` is "" (unset).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ConfigError",
    "ValidationError",
    "NodeConfig",
    "PeerDirectoryEntry",
    "DiscoveryConfig",
    "from_dict",
    "load_config",
    "is_valid_node_id",
]

# Base58 alphabet (no 0, O, I, l), spelled out so the node-id shape check
# needs nothing beyond the standard library.
_B58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_NODE_ID_PREFIX = "12D3KooW"
_NODE_ID_LEN = 52

_ROLES = ("coordinator", "worker", "client")
_TRANSPORTS = ("libp2p", "tcp")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_LOOPBACK_EXACT = ("127.0.0.1", "::1", "localhost")

_DEFAULTS = {
    "node_id": None,
    "transport": "libp2p",
    "listen_host": "127.0.0.1",
    "listen_port": 0,
    "allow_public_listen": False,
    "db_path": None,
    "peers": [],
    "directory": {},
    "capability": None,
    "discovery": {},
    "credits": {},
    "lease": {},
    "tls": None,
    "chain_registry": None,
    "coordinator_node_id": None,
    "coordinator_addr": None,
    "poll_interval_seconds": 5,
    "backoff_min": 1.0,
    "backoff_max": 8.0,
    "log_level": "INFO",
    "log_file": None,
}
_FIELDS = frozenset(_DEFAULTS) | {"role", "data_dir"}

# D2 (ADR-D0-13): ``peers`` is the allowlist (list of node ids); ``directory``
# is optional metadata keyed by node id. The legacy object form of ``peers``
# ({node_id: {role, addrs}}) is still READ (and migrated in memory with an
# actionable hint) but never written again.
_DIRECTORY_KEYS = frozenset({"role", "addrs"})
_CAPABILITY_TEMPLATE_KEYS = frozenset(
    {
        "capability_version",
        "ttl_seconds",
        "supported_task_types",
        "supported_runtimes",
        "hardware",
        "resource_limits",
        "network",
        "verification",
        "available_resources",
        "accepting_tasks",
    }
)
_DISCOVERY_KEYS = frozenset(
    {
        "announce_interval_seconds",
        "max_entries",
        "max_announcement_ttl_seconds",
        "offline_after_seconds",
        "query_timeout_seconds",
    }
)
# D4 final: nodes ONLY support the dual-signed lease mode. The historic
# "legacy" coordinator-grant mode is rejected at config load so a node can
# never be configured (by file, typo, or default) into unsigned execution.
LEASE_MODES = ("dual_signed",)
_LEASE_KEYS = frozenset({"mode", "proposal_timeout_seconds"})


@dataclass(frozen=True)
class LeaseConfig:
    mode: str = "dual_signed"
    proposal_timeout_seconds: int = 15


@dataclass(frozen=True)
class CreditsConfig:
    """CREDIT-for-compute price per task; 0 keeps the historic free path."""

    task_cost: int = 0


@dataclass(frozen=True)
class TlsConfig:
    """Optional TLS settings for the TCP transport.

    - Server (coordinator): ``cert_file`` + ``key_file`` are required.
    - Client (worker/client): ``ca_file`` is optional (system roots otherwise),
      ``server_hostname`` overrides certificate hostname verification.
    - Omitting the whole section keeps plaintext (backward compatible).
    """

    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    server_hostname: str | None = None


_TLS_KEYS = frozenset({"cert_file", "key_file", "ca_file", "server_hostname"})


def _parse_tls(value: Any) -> TlsConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"tls must be an object or null, got {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in _TLS_KEYS)
    if unknown:
        raise ValidationError(f"tls has unknown key(s): {', '.join(unknown)}")

    def optional_str(key: str) -> str | None:
        raw_value = value.get(key)
        if raw_value is None:
            return None
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValidationError(f"tls.{key} must be a non-empty string or null")
        return raw_value

    cert_file = optional_str("cert_file")
    key_file = optional_str("key_file")
    ca_file = optional_str("ca_file")
    server_hostname = optional_str("server_hostname")
    if bool(cert_file) != bool(key_file):
        raise ValidationError("tls.cert_file and tls.key_file must be set together")
    if not any((cert_file, key_file, ca_file, server_hostname)):
        raise ValidationError("tls section is empty; omit it for plaintext")
    return TlsConfig(
        cert_file=cert_file,
        key_file=key_file,
        ca_file=ca_file,
        server_hostname=server_hostname,
    )


@dataclass(frozen=True)
class ChainRegistryConfig:
    """Read-only EVM NodeRegistry admission (Phase 1 prototype)."""

    enabled: bool = False
    rpc_url: str = "http://127.0.0.1:8545"
    registry_address: str = ""
    refresh_seconds: int = 30
    timeout_seconds: int = 5


_CHAIN_KEYS = frozenset(
    {"enabled", "rpc_url", "registry_address", "refresh_seconds", "timeout_seconds"}
)


def _parse_chain_registry(value: Any) -> ChainRegistryConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(
            f"chain_registry must be an object or null, got {type(value).__name__}"
        )
    unknown = sorted(str(key) for key in value if key not in _CHAIN_KEYS)
    if unknown:
        raise ValidationError(f"chain_registry has unknown key(s): {', '.join(unknown)}")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValidationError("chain_registry.enabled must be a bool")
    rpc_url = value.get("rpc_url", "http://127.0.0.1:8545")
    if not isinstance(rpc_url, str) or not rpc_url.startswith(("http://", "https://")):
        raise ValidationError("chain_registry.rpc_url must be an http(s) URL")
    registry_address = value.get("registry_address", "")
    if not isinstance(registry_address, str):
        raise ValidationError("chain_registry.registry_address must be a string")
    refresh = value.get("refresh_seconds", 30)
    if isinstance(refresh, bool) or not isinstance(refresh, int) or not 1 <= refresh <= 3600:
        raise ValidationError("chain_registry.refresh_seconds must be an int in 1..3600")
    timeout = value.get("timeout_seconds", 5)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise ValidationError("chain_registry.timeout_seconds must be an int in 1..60")
    if enabled:
        if not registry_address.startswith("0x") or len(registry_address) != 42:
            raise ValidationError(
                "chain_registry.registry_address must be 0x + 40 hex when enabled"
            )
        int(registry_address[2:], 16)
    return ChainRegistryConfig(
        enabled=enabled,
        rpc_url=rpc_url,
        registry_address=registry_address,
        refresh_seconds=refresh,
        timeout_seconds=timeout,
    )


_CREDITS_KEYS = frozenset({"task_cost"})


def _parse_credits(value: Any) -> CreditsConfig:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValidationError(f"credits must be an object, got {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in _CREDITS_KEYS)
    if unknown:
        raise ValidationError(f"credits has unknown key(s): {', '.join(unknown)}")
    cost = value.get("task_cost", 0)
    if isinstance(cost, bool) or not isinstance(cost, int) or not 0 <= cost <= 1000:
        raise ValidationError("credits.task_cost must be an int in 0..1000")
    return CreditsConfig(task_cost=cost)



@dataclass(frozen=True)
class PeerDirectoryEntry:
    """Metadata for one known peer (never the trust decision — that is peers)."""

    role: str
    addrs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryConfig:
    """Bounded discovery tunables (D2); defaults are the documented values."""

    announce_interval_seconds: int = 20
    max_entries: int = 1024
    max_announcement_ttl_seconds: int = 120
    offline_after_seconds: int = 90
    query_timeout_seconds: int = 10



class ConfigError(Exception):
    """The config file could not be loaded (missing, unreadable, bad JSON)."""


class ValidationError(ValueError):
    """A config value violated a rule; the message names the field."""


def is_valid_node_id(value: object) -> bool:
    """True when ``value`` is a structurally valid libp2p Ed25519 node id.

    Shape rule (inline copy of identity.is_valid_node_id so this module
    never has to import libp2p): str of exactly 52 chars, starting with
    "12D3KooW", every char in the base58 alphabet.
    """
    return (
        isinstance(value, str)
        and len(value) == _NODE_ID_LEN
        and value.startswith(_NODE_ID_PREFIX)
        and all(ch in _B58_ALPHABET for ch in value)
    )


def _node_id_error(field: str, value: object) -> ValidationError:
    if not isinstance(value, str):
        return ValidationError(f"{field} must be a string, got {type(value).__name__}")
    return ValidationError(
        f"{field} is not a valid node id: must be {_NODE_ID_LEN} chars "
        f"starting with {_NODE_ID_PREFIX!r} and base58-alphabet characters only"
    )


def _is_loopback(host: str) -> bool:
    """True for 127.0.0.1, anything in 127.0.0.0/8, ::1, localhost."""
    lowered = host.lower()
    return lowered in _LOOPBACK_EXACT or lowered.startswith("127.")


def _require_str(raw: dict[str, Any], key: str, default: object = ...) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _require_int(
    raw: dict[str, Any], key: str, low: int, high: int, default: object = ...
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"{key} must be an int in {low}..{high}, got {type(value).__name__}"
        )
    if not low <= value <= high:
        raise ValidationError(f"{key} must be in {low}..{high}, got {value}")
    return value


def _parse_capability_template(value: Any) -> dict[str, Any] | None:
    """Validate the optional announcement template (D2).

    Deep validation of the manifest fields happens when the node builds and
    SIGNS a manifest (``discovery``/``capability`` modules); here we reject
    unknown keys and the obviously broken shapes so a typo fails at startup.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"capability must be an object or null, got {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in _CAPABILITY_TEMPLATE_KEYS)
    if unknown:
        raise ValidationError(f"capability has unknown key(s): {', '.join(unknown)}")
    template = dict(value)
    version = template.get("capability_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("capability.capability_version must be an int >= 1")
    ttl = template.get("ttl_seconds", 120)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 10 <= ttl <= 3600:
        raise ValidationError("capability.ttl_seconds must be an int in 10..3600")
    task_types = template.get("supported_task_types")
    if not isinstance(task_types, list) or not task_types or not all(
        isinstance(item, str) and 0 < len(item) <= 64 for item in task_types
    ):
        raise ValidationError("capability.supported_task_types must be a non-empty list of strings")
    runtimes = template.get("supported_runtimes", [])
    if not isinstance(runtimes, list) or not all(
        isinstance(item, str) and 0 < len(item) <= 64 for item in runtimes
    ):
        raise ValidationError("capability.supported_runtimes must be a list of strings")
    resources = template.get("available_resources", {"cpu_cores": 0, "ram_mb": 0, "gpu_memory_mb": 0})
    if not isinstance(resources, dict) or set(resources) != {"cpu_cores", "ram_mb", "gpu_memory_mb"}:
        raise ValidationError(
            "capability.available_resources must have exactly cpu_cores, ram_mb, gpu_memory_mb"
        )
    for key, item in resources.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValidationError(f"capability.available_resources.{key} must be an int >= 0")
    if "accepting_tasks" in template and not isinstance(template["accepting_tasks"], bool):
        raise ValidationError("capability.accepting_tasks must be a bool")
    return template


def _parse_discovery(value: Any) -> DiscoveryConfig:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValidationError(f"discovery must be an object, got {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in _DISCOVERY_KEYS)
    if unknown:
        raise ValidationError(f"discovery has unknown key(s): {', '.join(unknown)}")

    def _int(key: str, low: int, high: int, default: int) -> int:
        item = value.get(key, default)
        if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
            raise ValidationError(f"discovery.{key} must be an int in {low}..{high}")
        return item

    config = DiscoveryConfig(
        announce_interval_seconds=_int("announce_interval_seconds", 5, 3600, 20),
        max_entries=_int("max_entries", 1, 100_000, 1024),
        max_announcement_ttl_seconds=_int("max_announcement_ttl_seconds", 10, 3600, 120),
        offline_after_seconds=_int("offline_after_seconds", 10, 3600, 90),
        query_timeout_seconds=_int("query_timeout_seconds", 1, 60, 10),
    )
    if config.offline_after_seconds <= config.announce_interval_seconds:
        raise ValidationError(
            "discovery.offline_after_seconds must be greater than announce_interval_seconds"
        )
    return config


def _parse_lease(value: Any) -> LeaseConfig:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValidationError(f"lease must be an object, got {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in _LEASE_KEYS)
    if unknown:
        raise ValidationError(f"lease has unknown key(s): {', '.join(unknown)}")
    mode = value.get("mode", "dual_signed")
    if mode not in LEASE_MODES:
        raise ValidationError(f"lease.mode must be one of {', '.join(LEASE_MODES)}, got {mode!r}")
    timeout = value.get("proposal_timeout_seconds", 15)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise ValidationError("lease.proposal_timeout_seconds must be an int in 1..120")
    return LeaseConfig(mode=mode, proposal_timeout_seconds=timeout)


def from_dict(raw: dict[str, Any]) -> NodeConfig:
    """Validate ``raw`` and build a ``NodeConfig`` (see module docstring).

    Raises ValidationError on unknown keys, wrong types, and every rule
    violation; the message names the field.
    """
    if not isinstance(raw, dict):
        raise ValidationError(f"config must be a JSON object, got {type(raw).__name__}")
    unknown = sorted(str(key) for key in raw if key not in _FIELDS)
    if unknown:
        raise ValidationError(f"unknown key(s): {', '.join(unknown)}")

    if "role" not in raw:
        raise ValidationError("role is required")
    role = _require_str(raw, "role")
    if role not in _ROLES:
        raise ValidationError(f"role must be one of {', '.join(_ROLES)}, got {role!r}")

    # node_id: optional; shape-checked when present. Absent/null = a fresh
    # identity will be created at startup.
    node_id = raw.get("node_id")
    if node_id is not None and not is_valid_node_id(node_id):
        raise _node_id_error("node_id", node_id)

    transport = _require_str(raw, "transport", _DEFAULTS["transport"])
    if transport not in _TRANSPORTS:
        raise ValidationError(
            f"transport must be one of {', '.join(_TRANSPORTS)}, got {transport!r}"
        )

    listen_host = _require_str(raw, "listen_host", _DEFAULTS["listen_host"])
    if not listen_host or any(ch.isspace() for ch in listen_host):
        raise ValidationError("listen_host must be a non-empty hostname or IP address")

    allow_public_listen = raw.get("allow_public_listen", _DEFAULTS["allow_public_listen"])
    if not isinstance(allow_public_listen, bool):
        raise ValidationError(
            f"allow_public_listen must be a bool, got {type(allow_public_listen).__name__}"
        )
    if not _is_loopback(listen_host) and not allow_public_listen:
        raise ValidationError(
            f"listen_host {listen_host!r} is not loopback: public listen requires "
            "explicit authorization, set allow_public_listen=true"
        )

    listen_port = _require_int(raw, "listen_port", 0, 65535, _DEFAULTS["listen_port"])

    if "data_dir" not in raw:
        raise ValidationError("data_dir is required")
    data_dir = _require_str(raw, "data_dir")

    db_path = raw.get("db_path")
    if db_path is not None and not isinstance(db_path, str):
        raise ValidationError(f"db_path must be a string or null, got {type(db_path).__name__}")
    if db_path is not None and data_dir:
        raise ValidationError(
            "db_path and data_dir are both set explicitly: the ledger location is "
            "ambiguous (db_path is only allowed when data_dir is empty)"
        )

    # ---- peers + directory (D2, ADR-D0-13) --------------------------------
    peers_raw = raw.get("peers", _DEFAULTS["peers"])
    directory_raw = raw.get("directory")
    migration_hint: str | None = None
    if isinstance(peers_raw, dict):
        # Legacy form written by `peers add` before D2. Read it, keep every
        # role/addr, and tell the operator how to migrate. Never written again.
        if directory_raw is not None:
            raise ValidationError(
                "peers is the legacy object form AND directory is set: keep exactly one "
                "source of peer metadata (rewrite peers as a list of node ids)"
            )
        directory_raw = peers_raw
        peers_raw = list(peers_raw)
        migration_hint = (
            "legacy 'peers' object migrated to a node-id list plus 'directory'; "
            "rewrite the file to the new shape (any `peers` or `identity` command does it)"
        )
    if not isinstance(peers_raw, list):
        raise ValidationError(
            f"peers must be a list of node ids (or the legacy object form), "
            f"got {type(peers_raw).__name__}"
        )
    peers: list[str] = []
    for index, peer in enumerate(peers_raw):
        if not is_valid_node_id(peer):
            raise _node_id_error(f"peers[{index}]", peer)
        if peer in peers:
            raise ValidationError(f"peers[{index}] duplicates an earlier node id")
        peers.append(peer)

    directory: dict[str, PeerDirectoryEntry] = {}
    if directory_raw is not None:
        if not isinstance(directory_raw, dict):
            raise ValidationError(
                f"directory must be an object keyed by node id, got {type(directory_raw).__name__}"
            )
        for node_id, entry in directory_raw.items():
            if not is_valid_node_id(node_id):
                raise _node_id_error(f"directory key {node_id!r}", node_id)
            if node_id not in peers:
                raise ValidationError(
                    f"directory[{node_id}] is not listed in peers: metadata must not "
                    "describe a peer that is not in the allowlist"
                )
            if not isinstance(entry, dict):
                raise ValidationError(
                    f"directory[{node_id}] must be an object, got {type(entry).__name__}"
                )
            unknown_entry = sorted(str(key) for key in entry if key not in _DIRECTORY_KEYS)
            if unknown_entry:
                raise ValidationError(
                    f"directory[{node_id}] has unknown key(s): {', '.join(unknown_entry)}"
                )
            entry_role = entry.get("role")
            if entry_role not in _ROLES:
                raise ValidationError(
                    f"directory[{node_id}].role must be one of {', '.join(_ROLES)}, got {entry_role!r}"
                )
            addrs_raw = entry.get("addrs", [])
            if not isinstance(addrs_raw, list) or not all(
                isinstance(addr, str) and 0 < len(addr) <= 200 and not any(ch.isspace() for ch in addr)
                for addr in addrs_raw
            ):
                raise ValidationError(
                    f"directory[{node_id}].addrs must be a list of short address strings"
                )
            if len(addrs_raw) > 8:
                raise ValidationError(f"directory[{node_id}].addrs accepts at most 8 entries")
            directory[node_id] = PeerDirectoryEntry(role=entry_role, addrs=tuple(addrs_raw))


    coordinator_node_id = raw.get("coordinator_node_id")
    if coordinator_node_id is not None and not is_valid_node_id(coordinator_node_id):
        raise _node_id_error("coordinator_node_id", coordinator_node_id)
    if role in ("worker", "client") and coordinator_node_id is None:
        raise ValidationError(f"coordinator_node_id is required for role {role!r}")
    if role == "coordinator" and coordinator_node_id is not None:
        raise ValidationError("coordinator_node_id must be absent for role 'coordinator'")
    # Symmetric default-deny (audit finding, node.py:363): a worker/client
    # whose peers list omits the coordinator would silently kill its OWN
    # dialed stream during the mutual HELLO and retry forever. Fail fast at
    # config load instead of shipping a never-connecting node.
    if role in ("worker", "client") and coordinator_node_id not in peers:
        raise ValidationError(
            "coordinator_node_id must be listed in peers for role "
            f"{role!r} (symmetric default-deny)"
        )

    coordinator_addr = raw.get("coordinator_addr")
    if coordinator_addr is not None and not isinstance(coordinator_addr, str):
        raise ValidationError(
            f"coordinator_addr must be a string or null, got {type(coordinator_addr).__name__}"
        )

    poll_interval_seconds = _require_int(
        raw, "poll_interval_seconds", 1, 60, _DEFAULTS["poll_interval_seconds"]
    )

    backoff_min = raw.get("backoff_min", _DEFAULTS["backoff_min"])
    backoff_max = raw.get("backoff_max", _DEFAULTS["backoff_max"])
    for key, value in (("backoff_min", backoff_min), ("backoff_max", backoff_max)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{key} must be a number, got {type(value).__name__}")
    if not backoff_min > 0:
        raise ValidationError(f"backoff_min must be > 0, got {backoff_min}")
    if backoff_min > backoff_max:
        raise ValidationError(
            f"backoff_min must be <= backoff_max, got {backoff_min} > {backoff_max}"
        )

    log_level = _require_str(raw, "log_level", _DEFAULTS["log_level"])
    if log_level not in _LOG_LEVELS:
        raise ValidationError(
            f"log_level must be one of {', '.join(_LOG_LEVELS)}, got {log_level!r}"
        )

    log_file = raw.get("log_file")
    if log_file is not None and not isinstance(log_file, str):
        raise ValidationError(f"log_file must be a string or null, got {type(log_file).__name__}")

    capability = _parse_capability_template(raw.get("capability", _DEFAULTS["capability"]))
    discovery = _parse_discovery(raw.get("discovery", _DEFAULTS["discovery"]))
    lease = _parse_lease(raw.get("lease", _DEFAULTS["lease"]))
    credits = _parse_credits(raw.get("credits", _DEFAULTS["credits"]))
    chain_registry = _parse_chain_registry(raw.get("chain_registry"))
    tls = _parse_tls(raw.get("tls"))
    if tls is not None:
        if transport != "tcp":
            raise ValidationError("tls is only supported with transport='tcp'")
        if role == "coordinator" and not (tls.cert_file and tls.key_file):
            raise ValidationError("coordinator tls requires cert_file and key_file")

    return NodeConfig(
        node_id=node_id,
        role=role,
        transport=transport,
        listen_host=listen_host,
        listen_port=listen_port,
        allow_public_listen=allow_public_listen,
        data_dir=data_dir,
        db_path=db_path,
        peers=peers,
        directory=directory,
        capability=capability,
        discovery=discovery,
        lease=lease,
        credits=credits,
        tls=tls,
        chain_registry=chain_registry,
        coordinator_node_id=coordinator_node_id,
        coordinator_addr=coordinator_addr,
        poll_interval_seconds=poll_interval_seconds,
        backoff_min=backoff_min,
        backoff_max=backoff_max,
        log_level=log_level,
        log_file=log_file,
        migration_hint=migration_hint,
    )


def load_config(path: str | os.PathLike[str]) -> NodeConfig:
    """Read a JSON config file and return a validated ``NodeConfig``.

    Raises ConfigError when the file is missing, unreadable, or not valid
    JSON (file-level problems); raises ValidationError when the contents
    violate a rule (value-level problems).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.loads(handle.read())
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"config file {path} must contain a JSON object")
    return from_dict(raw)


@dataclass
class NodeConfig:
    """Validated node configuration; build via ``from_dict``/``load_config``.

    ``peers`` default-deny: an empty list means nobody may talk to this node
    until a HELLO arrives from a listed peer id. For worker/client roles the
    coordinator node id must itself be listed (the dialed stream is bound by
    the same allowlist).
    """

    role: str
    data_dir: str
    node_id: str | None = None
    transport: str = "libp2p"
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    allow_public_listen: bool = False
    db_path: str | None = None
    peers: list[str] = field(default_factory=list)
    directory: dict[str, PeerDirectoryEntry] = field(default_factory=dict)
    capability: dict[str, Any] | None = None
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    lease: LeaseConfig = field(default_factory=LeaseConfig)
    credits: CreditsConfig = field(default_factory=CreditsConfig)
    tls: TlsConfig | None = None
    chain_registry: ChainRegistryConfig | None = None
    coordinator_node_id: str | None = None
    coordinator_addr: str | None = None
    poll_interval_seconds: int = 5
    backoff_min: float = 1.0
    backoff_max: float = 8.0
    log_level: str = "INFO"
    log_file: str | None = None
    # Set when a legacy `peers` object was migrated in memory. Not serialized:
    # writing the new shape is what clears it.
    migration_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to schema form; ``from_dict`` roundtrips it."""
        data = {
            "node_id": self.node_id,
            "role": self.role,
            "transport": self.transport,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "allow_public_listen": self.allow_public_listen,
            "data_dir": self.data_dir,
            "db_path": self.db_path,
            "peers": list(self.peers),
            "directory": {
                node_id: {"role": entry.role, "addrs": list(entry.addrs)}
                for node_id, entry in self.directory.items()
            },
            "capability": dict(self.capability) if self.capability is not None else None,
            "discovery": {
                "announce_interval_seconds": self.discovery.announce_interval_seconds,
                "max_entries": self.discovery.max_entries,
                "max_announcement_ttl_seconds": self.discovery.max_announcement_ttl_seconds,
                "offline_after_seconds": self.discovery.offline_after_seconds,
                "query_timeout_seconds": self.discovery.query_timeout_seconds,
            },
            "lease": {
                "mode": self.lease.mode,
                "proposal_timeout_seconds": self.lease.proposal_timeout_seconds,
            },
            "credits": {
                "task_cost": self.credits.task_cost,
            },
            "coordinator_node_id": self.coordinator_node_id,
            "coordinator_addr": self.coordinator_addr,
            "poll_interval_seconds": self.poll_interval_seconds,
            "backoff_min": self.backoff_min,
            "backoff_max": self.backoff_max,
            "log_level": self.log_level,
            "log_file": self.log_file,
        }
        if self.tls is not None:
            data["tls"] = {
                "cert_file": self.tls.cert_file,
                "key_file": self.tls.key_file,
                "ca_file": self.tls.ca_file,
                "server_hostname": self.tls.server_hostname,
            }
        if self.chain_registry is not None:
            data["chain_registry"] = {
                "enabled": self.chain_registry.enabled,
                "rpc_url": self.chain_registry.rpc_url,
                "registry_address": self.chain_registry.registry_address,
                "refresh_seconds": self.chain_registry.refresh_seconds,
                "timeout_seconds": self.chain_registry.timeout_seconds,
            }
        return data
