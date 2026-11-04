"""Capability protocol (D1): manifest, announcement, revocation.

Frozen rules (see CAPABILITY-PROTOCOL.md):

- Canonical form: ``crypto.canonical_json`` (sorted keys, no whitespace, UTF-8);
  hash = sha256 of that canonical form; signature = Ed25519 over
  ``canonical_json({"domain": <domain>, **payload})`` with the key embedded in
  ``node_id``. The ``signature`` field is never part of its own payload.
- ``capability_version`` is monotonic per node: a lower version is a rollback,
  the same version with a different hash is a conflict (never a silent
  overwrite), a higher version must carry a valid signature.
- ``expires_at`` is authoritative: expired manifests never participate.
- Revocation is a signed, version-scoped statement; a revoked version stays
  revoked even if the manifest is still unexpired.
- Announcements are short-lived and monotonic per node: an announcement whose
  ``sequence`` is not greater than the last seen one is dropped.

This module is pure data + crypto. It does NOT import libp2p, asyncio or
socket: transport concerns belong to net_transport.py / discovery.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .crypto import canonical_json, content_hash
from .identity import NodeIdentity, is_valid_node_id

SCHEMA_VERSION = 1
CAPABILITY_DOMAIN = "idle-compute-capability-v1"
ANNOUNCEMENT_DOMAIN = "idle-compute-announcement-v1"
REVOCATION_DOMAIN = "idle-compute-capability-revoke-v1"

CLOCK_SKEW_SECONDS = 300
MAX_TASK_TYPE_ENTRIES = 32
MAX_RUNTIMES = 32
MAX_TRANSPORTS = 8
MAX_REASON = 200

CPU_ARCHES = frozenset({"x86_64", "aarch64", "other"})
TRANSPORTS = frozenset({"tcp", "libp2p", "quic"})
VERIFICATION_LEVELS = frozenset({"SELF_DECLARED", "BENCHMARKED", "VERIFIED"})
CONNECTIVITY = frozenset({"DIRECT", "RELAY", "UNKNOWN"})

_MANIFEST_KEYS = (
    "schema_version",
    "node_id",
    "capability_version",
    "issued_at",
    "expires_at",
    "supported_task_types",
    "supported_runtimes",
    "hardware",
    "resource_limits",
    "network",
    "verification",
)
_HARDWARE_KEYS = frozenset({"cpu_arch", "cpu_cores", "ram_mb", "gpu_vendor", "gpu_memory_mb"})
_LIMIT_KEYS = frozenset(
    {"max_cpu_cores", "max_ram_mb", "max_gpu_memory_mb", "max_task_duration_seconds"}
)
_NETWORK_KEYS = frozenset({"supported_transports", "supports_relay"})
_VERIFICATION_KEYS = frozenset({"level", "benchmark_profile"})
_ANNOUNCEMENT_KEYS = (
    "schema_version",
    "node_id",
    "capability_version",
    "sequence",
    "observed_at",
    "expires_at",
    "available_resources",
    "queue",
    "network_status",
    "accepting_tasks",
)
_AVAILABLE_KEYS = frozenset({"cpu_cores", "ram_mb", "gpu_memory_mb"})
_QUEUE_KEYS = frozenset({"running_tasks", "queued_tasks"})
_NETWORK_STATUS_KEYS = frozenset({"connectivity", "supported_transports"})
_REVOCATION_KEYS = (
    "schema_version",
    "node_id",
    "capability_version",
    "issued_at",
    "reason",
)


class CapabilityError(Exception):
    """Structured capability rejection; ``code`` is stable for tests/registry."""

    SCHEMA = "SCHEMA"
    SIGNATURE = "SIGNATURE"
    EXPIRED = "EXPIRED"
    VERSION_ROLLBACK = "VERSION_ROLLBACK"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    REVOKED = "REVOKED"
    SEQ_ROLLBACK = "SEQ_ROLLBACK"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# small validators


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapabilityError(CapabilityError.SCHEMA, message)


def _exact_keys(value: Any, keys: tuple[str, ...], label: str) -> dict:
    _require(isinstance(value, dict), f"{label} must be an object")
    if set(value) != set(keys):
        raise CapabilityError(CapabilityError.SCHEMA, f"{label} keys must be exactly {sorted(keys)}")
    return value


def _str_list(value: Any, label: str, maximum: int) -> tuple[str, ...]:
    _require(isinstance(value, (list, tuple)), f"{label} must be a list")
    items = tuple(value)
    _require(len(items) <= maximum, f"{label} has more than {maximum} entries")
    for item in items:
        _require(isinstance(item, str) and 1 <= len(item) <= 64, f"{label} entries must be short strings")
    return items


def _int_field(value: Any, label: str, minimum: int = 0) -> int:
    _require(_is_int(value) and value >= minimum, f"{label} must be an int >= {minimum}")
    return value


def _signed(identity: NodeIdentity, payload: dict, domain: str) -> str:
    return identity.sign(canonical_json({"domain": domain, **payload}))


def _verify(node_id: str, payload: dict, domain: str, signature: str) -> None:
    if not isinstance(signature, str) or len(signature) != 128:
        raise CapabilityError(CapabilityError.SIGNATURE, "signature must be 128 hex chars")
    if not NodeIdentity.verify_signature(node_id, canonical_json({"domain": domain, **payload}), signature):
        raise CapabilityError(CapabilityError.SIGNATURE, "signature does not match node_id/payload")


# ---------------------------------------------------------------------------
# CapabilityManifest


@dataclass(frozen=True)
class CapabilityManifest:
    node_id: str
    capability_version: int
    issued_at: int
    expires_at: int
    supported_task_types: tuple[str, ...]
    supported_runtimes: tuple[str, ...]
    hardware: dict[str, Any]
    resource_limits: dict[str, Any]
    network: dict[str, Any]
    verification: dict[str, Any]
    schema_version: int = SCHEMA_VERSION
    signature: str = ""

    # -- serialization ------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "capability_version": self.capability_version,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "supported_task_types": list(self.supported_task_types),
            "supported_runtimes": list(self.supported_runtimes),
            "hardware": dict(self.hardware),
            "resource_limits": dict(self.resource_limits),
            "network": dict(self.network),
            "verification": dict(self.verification),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityManifest":
        data = _exact_keys(value, _MANIFEST_KEYS + ("signature",), "manifest")
        manifest = cls(
            node_id=data["node_id"],
            capability_version=data["capability_version"],
            issued_at=data["issued_at"],
            expires_at=data["expires_at"],
            supported_task_types=tuple(data["supported_task_types"]),
            supported_runtimes=tuple(data["supported_runtimes"]),
            hardware=dict(data["hardware"]),
            resource_limits=dict(data["resource_limits"]),
            network=dict(data["network"]),
            verification=dict(data["verification"]),
            schema_version=data["schema_version"],
            signature=data["signature"],
        )
        _validate_manifest(manifest)
        return manifest

    # -- signing ------------------------------------------------------------

    def hash(self) -> str:
        return content_hash(self.payload())

    def sign(self, identity: NodeIdentity) -> "CapabilityManifest":
        if identity.node_id != self.node_id:
            raise CapabilityError(CapabilityError.SCHEMA, "signer node_id does not own this manifest")
        return replace(self, signature=_signed(identity, self.payload(), CAPABILITY_DOMAIN))

    def verify(self, now: int | None = None, *, check_time: bool = True) -> None:
        _validate_manifest(self)
        _verify(self.node_id, self.payload(), CAPABILITY_DOMAIN, self.signature)
        if check_time and now is not None:
            check_manifest_time(self, now)


def check_manifest_time(manifest: CapabilityManifest, now: int) -> None:
    _require(_is_int(now), "now must be an int")
    if manifest.issued_at > now + CLOCK_SKEW_SECONDS:
        raise CapabilityError(CapabilityError.SCHEMA, "issued_at is in the future")
    if manifest.expires_at <= now:
        raise CapabilityError(CapabilityError.EXPIRED, "manifest has expired")


def _validate_manifest(manifest: CapabilityManifest) -> None:
    _require(manifest.schema_version == SCHEMA_VERSION, f"unsupported schema_version {manifest.schema_version!r}")
    _require(is_valid_node_id(manifest.node_id), "node_id is not a valid libp2p Ed25519 id")
    _int_field(manifest.capability_version, "capability_version", 1)
    _int_field(manifest.issued_at, "issued_at", 0)
    _int_field(manifest.expires_at, "expires_at", 1)
    _require(manifest.expires_at > manifest.issued_at, "expires_at must be after issued_at")

    task_types = _str_list(manifest.supported_task_types, "supported_task_types", MAX_TASK_TYPE_ENTRIES)
    _require(len(task_types) >= 1, "supported_task_types must not be empty")
    _str_list(manifest.supported_runtimes, "supported_runtimes", MAX_RUNTIMES)

    hardware = manifest.hardware
    _require(isinstance(hardware, dict), "hardware must be an object")
    _require(set(hardware) <= _HARDWARE_KEYS, f"hardware keys must be a subset of {sorted(_HARDWARE_KEYS)}")
    _require(hardware.get("cpu_arch") in CPU_ARCHES, "hardware.cpu_arch is invalid")
    for key in ("cpu_cores", "ram_mb", "gpu_memory_mb"):
        if key in hardware:
            _int_field(hardware[key], f"hardware.{key}", 1 if key != "gpu_memory_mb" else 0)

    limits = manifest.resource_limits
    _exact_keys(limits, tuple(_LIMIT_KEYS), "resource_limits")
    for key in _LIMIT_KEYS:
        _int_field(limits[key], f"resource_limits.{key}", 0)
    _require(limits["max_cpu_cores"] >= 1, "resource_limits.max_cpu_cores must be >= 1")
    _require(limits["max_task_duration_seconds"] >= 1, "resource_limits.max_task_duration_seconds must be >= 1")

    network = manifest.network
    _exact_keys(network, tuple(_NETWORK_KEYS), "network")
    transports = _str_list(network["supported_transports"], "network.supported_transports", MAX_TRANSPORTS)
    _require(set(transports) <= TRANSPORTS, "network.supported_transports has an unknown transport")
    _require(isinstance(network["supports_relay"], bool), "network.supports_relay must be a bool")

    verification = manifest.verification
    _exact_keys(verification, tuple(_VERIFICATION_KEYS), "verification")
    _require(verification["level"] in VERIFICATION_LEVELS, "verification.level is invalid")
    profile = verification["benchmark_profile"]
    _require(profile is None or (isinstance(profile, str) and len(profile) <= 64), "verification.benchmark_profile is invalid")


def check_manifest_update(
    record: dict[str, Any] | None, incoming: CapabilityManifest, now: int
) -> str:
    """Version semantics for a registry/announcement store.

    Returns ``ACCEPT`` / ``IDEMPOTENT`` / ``CONFLICT`` / ``ROLLBACK`` /
    ``EXPIRED`` / ``REVOKED``. The caller maps these to its own result type;
    nothing here mutates state.
    """
    incoming.verify(now)
    if record is None:
        return "ACCEPT"
    if record.get("revoked"):
        return "REVOKED"
    stored_version = int(record.get("capability_version", 0))
    if incoming.capability_version < stored_version:
        return "ROLLBACK"
    if incoming.capability_version == stored_version:
        same = record.get("manifest_hash") == incoming.hash()
        return "IDEMPOTENT" if same else "CONFLICT"
    return "ACCEPT"


# ---------------------------------------------------------------------------
# CapabilityAnnouncement


@dataclass(frozen=True)
class CapabilityAnnouncement:
    node_id: str
    capability_version: int
    sequence: int
    observed_at: int
    expires_at: int
    available_resources: dict[str, Any]
    queue: dict[str, Any]
    network_status: dict[str, Any]
    accepting_tasks: bool
    schema_version: int = SCHEMA_VERSION
    signature: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "capability_version": self.capability_version,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "available_resources": dict(self.available_resources),
            "queue": dict(self.queue),
            "network_status": dict(self.network_status),
            "accepting_tasks": self.accepting_tasks,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityAnnouncement":
        data = _exact_keys(value, _ANNOUNCEMENT_KEYS + ("signature",), "announcement")
        announcement = cls(
            node_id=data["node_id"],
            capability_version=data["capability_version"],
            sequence=data["sequence"],
            observed_at=data["observed_at"],
            expires_at=data["expires_at"],
            available_resources=dict(data["available_resources"]),
            queue=dict(data["queue"]),
            network_status=dict(data["network_status"]),
            accepting_tasks=data["accepting_tasks"],
            schema_version=data["schema_version"],
            signature=data["signature"],
        )
        _validate_announcement(announcement)
        return announcement

    def sign(self, identity: NodeIdentity) -> "CapabilityAnnouncement":
        if identity.node_id != self.node_id:
            raise CapabilityError(CapabilityError.SCHEMA, "signer node_id does not own this announcement")
        return replace(self, signature=_signed(identity, self.payload(), ANNOUNCEMENT_DOMAIN))

    def verify(
        self,
        manifest: CapabilityManifest,
        now: int,
        last_sequence: int | None = None,
    ) -> None:
        _validate_announcement(self)
        _verify(self.node_id, self.payload(), ANNOUNCEMENT_DOMAIN, self.signature)
        if self.node_id != manifest.node_id:
            raise CapabilityError(CapabilityError.SCHEMA, "announcement node_id != manifest node_id")
        if self.capability_version != manifest.capability_version:
            raise CapabilityError(CapabilityError.SCHEMA, "announcement capability_version != manifest version")
        if self.observed_at > now + CLOCK_SKEW_SECONDS:
            raise CapabilityError(CapabilityError.SCHEMA, "observed_at is in the future")
        if self.expires_at <= now:
            raise CapabilityError(CapabilityError.EXPIRED, "announcement has expired")
        if last_sequence is not None and self.sequence <= last_sequence:
            raise CapabilityError(
                CapabilityError.SEQ_ROLLBACK,
                f"announcement sequence {self.sequence} <= last seen {last_sequence}",
            )


def _validate_announcement(announcement: CapabilityAnnouncement) -> None:
    _require(announcement.schema_version == SCHEMA_VERSION, "unsupported schema_version")
    _require(is_valid_node_id(announcement.node_id), "node_id is not a valid node id")
    _int_field(announcement.capability_version, "capability_version", 1)
    _int_field(announcement.sequence, "sequence", 1)
    _int_field(announcement.observed_at, "observed_at", 0)
    _int_field(announcement.expires_at, "expires_at", 1)
    _require(announcement.expires_at > announcement.observed_at, "expires_at must be after observed_at")
    _require(isinstance(announcement.accepting_tasks, bool), "accepting_tasks must be a bool")

    resources = _exact_keys(announcement.available_resources, tuple(_AVAILABLE_KEYS), "available_resources")
    for key in _AVAILABLE_KEYS:
        _int_field(resources[key], f"available_resources.{key}", 0)

    queue = _exact_keys(announcement.queue, tuple(_QUEUE_KEYS), "queue")
    for key in _QUEUE_KEYS:
        _int_field(queue[key], f"queue.{key}", 0)

    status = _exact_keys(announcement.network_status, tuple(_NETWORK_STATUS_KEYS), "network_status")
    _require(status["connectivity"] in CONNECTIVITY, "network_status.connectivity is invalid")
    transports = _str_list(status["supported_transports"], "network_status.supported_transports", MAX_TRANSPORTS)
    _require(set(transports) <= TRANSPORTS, "network_status.supported_transports has an unknown transport")


# ---------------------------------------------------------------------------
# CapabilityRevocation


@dataclass(frozen=True)
class CapabilityRevocation:
    node_id: str
    capability_version: int
    issued_at: int
    reason: str
    schema_version: int = SCHEMA_VERSION
    signature: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "capability_version": self.capability_version,
            "issued_at": self.issued_at,
            "reason": self.reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityRevocation":
        data = _exact_keys(value, _REVOCATION_KEYS + ("signature",), "revocation")
        revocation = cls(
            node_id=data["node_id"],
            capability_version=data["capability_version"],
            issued_at=data["issued_at"],
            reason=data["reason"],
            schema_version=data["schema_version"],
            signature=data["signature"],
        )
        _validate_revocation(revocation)
        return revocation

    def sign(self, identity: NodeIdentity) -> "CapabilityRevocation":
        if identity.node_id != self.node_id:
            raise CapabilityError(CapabilityError.SCHEMA, "signer node_id does not own this revocation")
        return replace(self, signature=_signed(identity, self.payload(), REVOCATION_DOMAIN))

    def verify(self, now: int | None = None) -> None:
        _validate_revocation(self)
        _verify(self.node_id, self.payload(), REVOCATION_DOMAIN, self.signature)
        if now is not None and self.issued_at > now + CLOCK_SKEW_SECONDS:
            raise CapabilityError(CapabilityError.SCHEMA, "revocation issued_at is in the future")


def _validate_revocation(revocation: CapabilityRevocation) -> None:
    _require(revocation.schema_version == SCHEMA_VERSION, "unsupported schema_version")
    _require(is_valid_node_id(revocation.node_id), "node_id is not a valid node id")
    _int_field(revocation.capability_version, "capability_version", 1)
    _int_field(revocation.issued_at, "issued_at", 0)
    _require(
        isinstance(revocation.reason, str) and 1 <= len(revocation.reason) <= MAX_REASON,
        f"reason must be a string of 1..{MAX_REASON} chars",
    )
