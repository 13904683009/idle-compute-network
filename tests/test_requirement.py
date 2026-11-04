"""D1: TaskRequirement protocol tests (whitelist, strict schema, limits)."""

from __future__ import annotations

import unittest

from idle_compute.requirement import RequirementError, TaskRequirement

BASE = {
    "schema_version": 1,
    "task_id": "tsk_req_1",
    "task_type": "numeric_batch_v1",
    "task_version": 1,
    "input_manifest_hash": "",
    "required_runtime": "python_sandbox_v1",
    "minimum_resources": {"cpu_cores": 2, "ram_mb": 4096, "gpu_memory_mb": 0},
    "preferred_resources": {"gpu_vendor": "NVIDIA"},
    "constraints": {"max_execution_seconds": 600, "max_input_bytes": 1_048_576, "max_output_bytes": 65_536},
    "data_policy": {"requires_private_data": False, "allow_relay": True},
    "verification_policy": {"mode": "RECOMPUTE", "required": True},
    "deadline": 1_800_000_600,
}


def requirement(**overrides) -> TaskRequirement:
    data = {**BASE, **overrides}
    return TaskRequirement.from_dict(data)


class RequirementTests(unittest.TestCase):
    def test_valid_requirement_roundtrips_with_stable_hash(self) -> None:
        req = requirement()
        self.assertEqual(req.hash(), TaskRequirement.from_dict(req.to_dict()).hash())
        self.assertEqual(req.hash(), requirement().hash())

    def test_unknown_task_type_is_rejected(self) -> None:
        with self.assertRaises(RequirementError) as ctx:
            requirement(task_type="arbitrary_code_v1")
        self.assertEqual(ctx.exception.code, RequirementError.UNKNOWN_TASK_TYPE)

    def test_extra_key_is_rejected(self) -> None:
        with self.assertRaises(RequirementError) as ctx:
            TaskRequirement.from_dict({**BASE, "shell": "rm -rf /"})
        self.assertEqual(ctx.exception.code, RequirementError.SCHEMA)

    def test_bool_is_not_an_int(self) -> None:
        with self.assertRaises(RequirementError):
            requirement(minimum_resources={"cpu_cores": True, "ram_mb": 1, "gpu_memory_mb": 0})

    def test_negative_gpu_memory_is_rejected(self) -> None:
        with self.assertRaises(RequirementError):
            requirement(minimum_resources={"cpu_cores": 1, "ram_mb": 1, "gpu_memory_mb": -1})

    def test_unknown_verification_mode_is_rejected(self) -> None:
        with self.assertRaises(RequirementError):
            requirement(verification_policy={"mode": "TRUST_ME", "required": True})

    def test_bad_input_hash_is_rejected(self) -> None:
        with self.assertRaises(RequirementError):
            requirement(input_manifest_hash="not-hex")

    def test_deadline_expiry(self) -> None:
        req = requirement(deadline=1000)
        self.assertFalse(req.is_expired(999))
        self.assertTrue(req.is_expired(1000))

    def test_preferred_resources_are_optional_but_strict(self) -> None:
        self.assertTrue(requirement(preferred_resources={}).preferred_resources == {})
        with self.assertRaises(RequirementError):
            requirement(preferred_resources={"cpu_model": "i9"})


if __name__ == "__main__":
    unittest.main()
