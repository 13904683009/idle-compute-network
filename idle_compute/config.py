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
    "coordinator_node_id": None,
    "coordinator_addr": None,
    "poll_interval_seconds": 5,
    "backoff_min": 1.0,
    "backoff_max": 8.0,
    "log_level": "INFO",
    "log_file": None,
}
_FIELDS = frozenset(_DEFAULTS) | {"role", "data_dir"}


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

    peers_raw = raw.get("peers", _DEFAULTS["peers"])
    if not isinstance(peers_raw, list):
        raise ValidationError(f"peers must be a list of node ids, got {type(peers_raw).__name__}")
    peers: list[str] = []
    for index, peer in enumerate(peers_raw):
        if not is_valid_node_id(peer):
            raise _node_id_error(f"peers[{index}]", peer)
        peers.append(peer)

    coordinator_node_id = raw.get("coordinator_node_id")
    if coordinator_node_id is not None and not is_valid_node_id(coordinator_node_id):
        raise _node_id_error("coordinator_node_id", coordinator_node_id)
    if role in ("worker", "client") and coordinator_node_id is None:
        raise ValidationError(f"coordinator_node_id is required for role {role!r}")
    if role == "coordinator" and coordinator_node_id is not None:
        raise ValidationError("coordinator_node_id must be absent for role 'coordinator'")

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
        coordinator_node_id=coordinator_node_id,
        coordinator_addr=coordinator_addr,
        poll_interval_seconds=poll_interval_seconds,
        backoff_min=backoff_min,
        backoff_max=backoff_max,
        log_level=log_level,
        log_file=log_file,
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
    until a HELLO arrives from a listed peer id.
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
    coordinator_node_id: str | None = None
    coordinator_addr: str | None = None
    poll_interval_seconds: int = 5
    backoff_min: float = 1.0
    backoff_max: float = 8.0
    log_level: str = "INFO"
    log_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to schema form; ``from_dict`` roundtrips it."""
        return {
            "node_id": self.node_id,
            "role": self.role,
            "transport": self.transport,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "allow_public_listen": self.allow_public_listen,
            "data_dir": self.data_dir,
            "db_path": self.db_path,
            "peers": list(self.peers),
            "coordinator_node_id": self.coordinator_node_id,
            "coordinator_addr": self.coordinator_addr,
            "poll_interval_seconds": self.poll_interval_seconds,
            "backoff_min": self.backoff_min,
            "backoff_max": self.backoff_max,
            "log_level": self.log_level,
            "log_file": self.log_file,
        }
