"""Bounded task state machine."""

from __future__ import annotations

from .models import TaskStatus


class InvalidTransition(Exception):
    """Raised when a task transition is not allowed."""


ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.LEASED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.LEASED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.QUEUED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUBMITTED,
            TaskStatus.QUEUED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        }
    ),
    TaskStatus.SUBMITTED: frozenset(
        {
            TaskStatus.VALIDATED,
            TaskStatus.REJECTED,
            TaskStatus.QUEUED,
            TaskStatus.EXPIRED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.VALIDATED: frozenset({TaskStatus.COMPLETED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.EXPIRED: frozenset(),
}

TERMINAL = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
    }
)


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED.get(current, frozenset())


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"{current.value} -> {target.value} is not allowed")


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL
