from __future__ import annotations

import unittest

from idle_compute.tasks import (
    MAX_NUMBERS,
    InvalidTaskInput,
    NumericBatchTask,
    get_task_type,
    run,
    validate_payload,
)


class NumericTaskTests(unittest.TestCase):
    def test_deterministic_output(self) -> None:
        payload = {"numbers": [1, 2, 3, -4]}
        expected = {"count": 4, "sum": 2, "sum_squares": 30}
        self.assertEqual(run(payload), expected)
        self.assertEqual(run(payload), expected)
        self.assertTrue(NumericBatchTask.verify(payload, expected))
        self.assertFalse(NumericBatchTask.verify(payload, {"count": 4}))

    def test_rejects_bool(self) -> None:
        with self.assertRaises(InvalidTaskInput):
            validate_payload({"numbers": [True]})

    def test_rejects_non_integer(self) -> None:
        with self.assertRaises(InvalidTaskInput):
            validate_payload({"numbers": [1.5]})
        with self.assertRaises(InvalidTaskInput):
            validate_payload({"numbers": ["1"]})

    def test_rejects_empty_and_oversized(self) -> None:
        with self.assertRaises(InvalidTaskInput):
            validate_payload({"numbers": []})
        with self.assertRaises(InvalidTaskInput):
            validate_payload({"numbers": [1] * (MAX_NUMBERS + 1)})

    def test_rejects_extra_keys_and_bad_shape(self) -> None:
        with self.assertRaises(InvalidTaskInput):
            validate_payload({"numbers": [1], "code": "print(1)"})
        with self.assertRaises(InvalidTaskInput):
            validate_payload([1, 2])

    def test_rejects_out_of_range(self) -> None:
        with self.assertRaises(InvalidTaskInput):
            validate_payload({"numbers": [1_000_000_001]})

    def test_only_whitelisted_type(self) -> None:
        with self.assertRaises(InvalidTaskInput):
            get_task_type("arbitrary_python")
        self.assertIs(get_task_type("numeric_batch_v1"), NumericBatchTask)


if __name__ == "__main__":
    unittest.main()
