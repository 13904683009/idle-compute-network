"""Whitelisted deterministic tasks. No arbitrary code or shell execution."""

from __future__ import annotations

from typing import Any


TASK_TYPE = "numeric_batch_v1"
MAX_NUMBERS = 4096
MAX_ABS_VALUE = 1_000_000_000


class InvalidTaskInput(ValueError):
    """Raised when a task payload violates the whitelist schema."""


def validate_payload(payload: Any) -> list[int]:
    if not isinstance(payload, dict) or set(payload.keys()) != {"numbers"}:
        raise InvalidTaskInput("payload must be exactly {'numbers': [...]}")
    numbers = payload["numbers"]
    if not isinstance(numbers, list):
        raise InvalidTaskInput("numbers must be a list")
    if not 1 <= len(numbers) <= MAX_NUMBERS:
        raise InvalidTaskInput(f"numbers length must be 1..{MAX_NUMBERS}")
    for value in numbers:
        if type(value) is not int:
            raise InvalidTaskInput("every number must be an int (booleans are not allowed)")
        if abs(value) > MAX_ABS_VALUE:
            raise InvalidTaskInput(f"number exceeds +/-{MAX_ABS_VALUE}")
    return list(numbers)


def run(payload: Any) -> dict[str, Any]:
    """Deterministic whitelisted computation."""
    numbers = validate_payload(payload)
    return {
        "count": len(numbers),
        "sum": sum(numbers),
        "sum_squares": sum(value * value for value in numbers),
    }


def expected_output(payload: Any) -> dict[str, Any]:
    return run(payload)


class NumericBatchTask:
    task_type = TASK_TYPE

    @staticmethod
    def validate(payload: Any) -> list[int]:
        return validate_payload(payload)

    @staticmethod
    def execute(payload: Any) -> dict[str, Any]:
        return run(payload)

    @staticmethod
    def verify(payload: Any, output: Any) -> bool:
        return output == expected_output(payload)


TASK_TYPES = {TASK_TYPE: NumericBatchTask}


def get_task_type(name: str):
    try:
        return TASK_TYPES[name]
    except KeyError as exc:
        raise InvalidTaskInput(f"task type is not whitelisted: {name}") from exc
