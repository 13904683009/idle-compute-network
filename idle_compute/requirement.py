"""Task requirement protocol (D1).

A ``TaskRequirement`` describes what a task actually needs (runtime, minimum
resources, limits, verification policy) instead of naming a device. Only
whitelisted task types are accepted, so a requirement can never introduce
arbitrary execution.

Frozen rules (see TASK-REQUIREMENT-PROTOCOL.md):
- schema_version is required and strict; exact key sets (no smuggled fields);
- ``task_type`` must exist in the whitelist task registry (``tasks.py``) — the
  single dispatch point; no new task type can be introduced by a requirement.
- ``verification_policy.mode`` is ``RECOMPUTE`` in MVP (independent recompute);
- limits are positive ints and are enforced by the matcher/worker, not trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .crypto import content_hash
from .tasks import InvalidTaskInput, get_task_type

SCHEMA_VERSION = 1
MAX_TASK_ID = 128
MAX_RUNTIME = 64
MAX_GPU_VENDOR = 32
MAX_PROFILE = 64
_HEX64_PATTERN = r"[0-9a-f]{64}"

_MIN_RESOURCE_KEYS = frozenset({"cpu_cores", "ram_mb", "gpu_memory_mb"})
_PREFERRED_KEYS = frozenset({"gpu_vendor", "gpu_compute_profile"})
_CONSTRAINT_KEYS = frozenset({"max_execution_seconds", "max_input_bytes", "max_output_bytes"})
_DATA_POLICY_KEYS = frozenset({"requires_private_data", "allow_relay"})
_VERIFICATION_KEYS = frozenset({"mode", "required"})
_REQUIREMENT_KEYS = (
    "schema_version",
    "task_id",
    "task_type",
    "task_version",
    "input_manifest_hash",
    "required_runtime",
    "minimum_resources",
    "preferred_resources",
    "constraints",
    "data_policy",
    "verification_policy",
    "deadline",
)
VERIFICATION_MODES = frozenset({"RECOMPUTE"})


class RequirementError(Exception):
    SCHEMA = "SCHEMA"
    UNKNOWN_TASK_TYPE = "UNKNOWN_TASK_TYPE"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RequirementError(RequirementError.SCHEMA, message)


def _exact(value: Any, keys: tuple[str, ...] | frozenset[str], label: str) -> dict:
    _require(isinstance(value, dict), f"{label} must be an object")
    if set(value) != set(keys):
        raise RequirementError(RequirementError.SCHEMA, f"{label} keys must be exactly {sorted(keys)}")
    return value


def _int_field(value: Any, label: str, minimum: int) -> int:
    _require(_is_int(value) and value >= minimum, f"{label} must be an int >= {minimum}")
    return value


@dataclass(frozen=True)
class TaskRequirement:
    task_id: str
    task_type: str
    task_version: int
    input_manifest_hash: str
    required_runtime: str
    minimum_resources: dict[str, Any]
    preferred_resources: dict[str, Any]
    constraints: dict[str, Any]
    data_policy: dict[str, Any]
    verification_policy: dict[str, Any]
    deadline: int
    schema_version: int = SCHEMA_VERSION

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "task_version": self.task_version,
            "input_manifest_hash": self.input_manifest_hash,
            "required_runtime": self.required_runtime,
            "minimum_resources": dict(self.minimum_resources),
            "preferred_resources": dict(self.preferred_resources),
            "constraints": dict(self.constraints),
            "data_policy": dict(self.data_policy),
            "verification_policy": dict(self.verification_policy),
            "deadline": self.deadline,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.payload()

    @classmethod
    def from_dict(cls, value: Any) -> "TaskRequirement":
        data = _exact(value, _REQUIREMENT_KEYS, "requirement")
        requirement = cls(
            task_id=data["task_id"],
            task_type=data["task_type"],
            task_version=data["task_version"],
            input_manifest_hash=data["input_manifest_hash"],
            required_runtime=data["required_runtime"],
            minimum_resources=dict(data["minimum_resources"]),
            preferred_resources=dict(data["preferred_resources"]),
            constraints=dict(data["constraints"]),
            data_policy=dict(data["data_policy"]),
            verification_policy=dict(data["verification_policy"]),
            deadline=data["deadline"],
            schema_version=data["schema_version"],
        )
        requirement.validate()
        return requirement

    def hash(self) -> str:
        return content_hash(self.payload())

    def validate(self) -> None:
        _require(self.schema_version == SCHEMA_VERSION, "unsupported schema_version")
        _require(
            isinstance(self.task_id, str) and 1 <= len(self.task_id) <= MAX_TASK_ID,
            f"task_id must be 1..{MAX_TASK_ID} chars",
        )
        try:
            get_task_type(self.task_type)
        except InvalidTaskInput as exc:
            raise RequirementError(
                RequirementError.UNKNOWN_TASK_TYPE, f"task_type {self.task_type!r} is not whitelisted"
            ) from exc
        _int_field(self.task_version, "task_version", 1)
        _require(
            self.input_manifest_hash == "" or bool(re.fullmatch(_HEX64_PATTERN, self.input_manifest_hash)),
            "input_manifest_hash must be empty or 64 lowercase hex chars",
        )
        _require(
            isinstance(self.required_runtime, str) and 1 <= len(self.required_runtime) <= MAX_RUNTIME,
            f"required_runtime must be 1..{MAX_RUNTIME} chars",
        )

        resources = _exact(self.minimum_resources, _MIN_RESOURCE_KEYS, "minimum_resources")
        _require(_is_int(resources["cpu_cores"]) and resources["cpu_cores"] >= 1, "minimum_resources.cpu_cores must be >= 1")
        _require(_is_int(resources["ram_mb"]) and resources["ram_mb"] >= 0, "minimum_resources.ram_mb must be >= 0")
        _require(
            _is_int(resources["gpu_memory_mb"]) and resources["gpu_memory_mb"] >= 0,
            "minimum_resources.gpu_memory_mb must be >= 0",
        )

        preferred = self.preferred_resources
        _require(isinstance(preferred, dict), "preferred_resources must be an object")
        _require(set(preferred) <= _PREFERRED_KEYS, f"preferred_resources keys must be a subset of {sorted(_PREFERRED_KEYS)}")
        for key, maximum in (("gpu_vendor", MAX_GPU_VENDOR), ("gpu_compute_profile", MAX_PROFILE)):
            if key in preferred:
                _require(
                    isinstance(preferred[key], str) and 1 <= len(preferred[key]) <= maximum,
                    f"preferred_resources.{key} is invalid",
                )

        constraints = _exact(self.constraints, _CONSTRAINT_KEYS, "constraints")
        _require(_is_int(constraints["max_execution_seconds"]) and constraints["max_execution_seconds"] >= 1, "constraints.max_execution_seconds must be >= 1")
        _require(_is_int(constraints["max_input_bytes"]) and constraints["max_input_bytes"] >= 0, "constraints.max_input_bytes must be >= 0")
        _require(_is_int(constraints["max_output_bytes"]) and constraints["max_output_bytes"] >= 0, "constraints.max_output_bytes must be >= 0")

        policy = _exact(self.data_policy, _DATA_POLICY_KEYS, "data_policy")
        _require(isinstance(policy["requires_private_data"], bool), "data_policy.requires_private_data must be a bool")
        _require(isinstance(policy["allow_relay"], bool), "data_policy.allow_relay must be a bool")

        verification = _exact(self.verification_policy, _VERIFICATION_KEYS, "verification_policy")
        _require(verification["mode"] in VERIFICATION_MODES, f"verification_policy.mode must be one of {sorted(VERIFICATION_MODES)}")
        _require(isinstance(verification["required"], bool), "verification_policy.required must be a bool")

        _int_field(self.deadline, "deadline", 0)

    def is_expired(self, now: int) -> bool:
        return now >= self.deadline
