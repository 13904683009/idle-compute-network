from __future__ import annotations

import unittest

from idle_compute.crypto import canonical_json, content_hash, result_signing_payload
from idle_compute.models import new_id, utc_now
from idle_compute.state_machine import (
    InvalidTransition,
    can_transition,
    ensure_transition,
    is_terminal,
)
from idle_compute.models import TaskStatus


class ModelTests(unittest.TestCase):
    def test_canonical_json_is_stable(self) -> None:
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')
        self.assertEqual(content_hash({"b": 2, "a": 1}), content_hash({"a": 1, "b": 2}))

    def test_ids_and_timestamps(self) -> None:
        first, second = new_id("t_"), new_id("t_")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("t_"))
        self.assertIn("+00:00", utc_now())

    def test_signing_payload_is_domain_separated(self) -> None:
        payload = result_signing_payload("t1", "l1", "p1", "h" * 64)
        self.assertIn("idle-compute-result-v1", payload)
        self.assertNotEqual(payload, result_signing_payload("t1", "l1", "p1", "x" * 64))


class StateMachineTests(unittest.TestCase):
    def test_allowed_happy_path(self) -> None:
        path = [
            TaskStatus.CREATED,
            TaskStatus.QUEUED,
            TaskStatus.LEASED,
            TaskStatus.RUNNING,
            TaskStatus.SUBMITTED,
            TaskStatus.VALIDATED,
            TaskStatus.COMPLETED,
        ]
        for current, target in zip(path, path[1:]):
            ensure_transition(current, target)

    def test_invalid_transition_rejected(self) -> None:
        self.assertFalse(can_transition(TaskStatus.COMPLETED, TaskStatus.QUEUED))
        with self.assertRaises(InvalidTransition):
            ensure_transition(TaskStatus.COMPLETED, TaskStatus.QUEUED)

    def test_terminal_states(self) -> None:
        self.assertTrue(is_terminal(TaskStatus.COMPLETED))
        self.assertTrue(is_terminal(TaskStatus.REJECTED))
        self.assertFalse(is_terminal(TaskStatus.SUBMITTED))


if __name__ == "__main__":
    unittest.main()
