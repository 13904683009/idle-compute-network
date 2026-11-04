"""D3: deterministic, explainable capability matching.

Pure unit tests: no sockets, no database, no subprocesses. Each candidate is a
signed D1 manifest + D2 announcement built from a template, so the matcher is
exercised against the real models rather than a parallel fixture type.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from idle_compute.capability import CapabilityAnnouncement
from idle_compute.discovery import build_announcement, build_manifest
from idle_compute.identity import NodeIdentity
from idle_compute.matcher import (
    ANNOUNCEMENT_EXPIRED,
    CAPABILITY_REVOKED,
    DATA_POLICY_UNPROVEN,
    DEADLINE_PASSED,
    DURATION_LIMIT_EXCEEDED,
    MANIFEST_EXPIRED,
    MAX_SCORE,
    NODE_UNREACHABLE,
    NOT_ACCEPTING,
    RELAY_NOT_ALLOWED,
    RESOURCE_INSUFFICIENT,
    RESOURCE_UNSPECIFIED,
    RUNTIME_MISMATCH,
    TASK_TYPE_UNSUPPORTED,
    VERIFICATION_UNPROVEN,
    Candidate,
    MatcherError,
    match,
)
from idle_compute.requirement import TaskRequirement

NOW = 1_800_000_000

BASE_TEMPLATE = {
    "capability_version": 1,
    "ttl_seconds": 120,
    "supported_task_types": ["numeric_batch_v1"],
    "supported_runtimes": ["python_sandbox_v1"],
    "hardware": {"cpu_arch": "x86_64", "cpu_cores": 8, "ram_mb": 16384, "gpu_memory_mb": 0},
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


def requirement(**overrides) -> TaskRequirement:
    data = {
        "task_id": "task-1",
        "task_type": "numeric_batch_v1",
        "task_version": 1,
        "input_manifest_hash": "",
        "required_runtime": "python_sandbox_v1",
        "minimum_resources": {"cpu_cores": 1, "ram_mb": 512, "gpu_memory_mb": 0},
        "preferred_resources": {},
        "constraints": {
            "max_execution_seconds": 60,
            "max_input_bytes": 1024,
            "max_output_bytes": 1024,
        },
        "data_policy": {"requires_private_data": False, "allow_relay": True},
        "verification_policy": {"mode": "RECOMPUTE", "required": False},
        "deadline": NOW + 600,
    }
    data.update(overrides)
    return TaskRequirement(**data)


class MatcherTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="matcher-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.identities = {
            name: NodeIdentity.load_or_create(root / name / "identity.key", node_name=name)
            for name in ("a", "b", "c")
        }

    def candidate(
        self,
        name="a",
        template=None,
        *,
        seq=1,
        now=NOW,
        reachable=True,
        revoked=False,
        announcement_ttl=None,
    ):
        identity = self.identities[name]
        values = dict(BASE_TEMPLATE)
        values.update(template or {})
        manifest = build_manifest(values, identity, now=now)
        announcement = build_announcement(
            manifest,
            values,
            identity,
            now=now,
            sequence=seq,
            ttl_seconds=values["ttl_seconds"] if announcement_ttl is None else announcement_ttl,
        )
        return Candidate(
            node_id=identity.node_id,
            manifest=manifest,
            announcement=announcement,
            reachable=reachable,
            revoked=revoked,
        )

    def run_match(self, req=None, candidates=None, *, now=NOW, verify=True):
        return match(
            req or requirement(),
            candidates if candidates is not None else [self.candidate()],
            now=now,
            verify_signatures=verify,
        )


class HardConstraintTests(MatcherTestBase):
    def test_fully_satisfied_candidate_is_ranked(self) -> None:
        result = self.run_match()
        self.assertEqual(len(result.ranking), 1)
        self.assertEqual(result.ranking[0].node_id, self.identities["a"].node_id)
        self.assertEqual(result.rejected, ())
        self.assertTrue(0 <= result.ranking[0].score <= MAX_SCORE)

    def test_unsupported_task_type_is_rejected(self) -> None:
        # The requirement must name a whitelisted task type (requirement.py);
        # the CANDIDATE may declare something else, which must not match.
        result = self.run_match(
            candidates=[self.candidate(template={"supported_task_types": ["onnx_inference_v1"]})]
        )
        self.assertEqual(result.ranking, ())
        self.assertIn(TASK_TYPE_UNSUPPORTED, result.rejected[0].rejection_codes)

    def test_runtime_mismatch_is_rejected(self) -> None:
        result = self.run_match(requirement(required_runtime="wasm_v1"))
        self.assertEqual(result.ranking, ())
        self.assertIn(RUNTIME_MISMATCH, result.rejected[0].rejection_codes)

    def test_resource_shortage_is_rejected(self) -> None:
        result = self.run_match(requirement(minimum_resources={"cpu_cores": 8, "ram_mb": 512, "gpu_memory_mb": 0}))
        self.assertEqual(result.ranking, ())
        codes = result.rejected[0].rejection_codes
        self.assertIn(RESOURCE_INSUFFICIENT, codes)
        detail = " ".join(result.rejected[0].rejection_details)
        self.assertIn("cpu_cores", detail)

    def test_manifest_limit_caps_the_announced_resources(self) -> None:
        # Announces 100 cores but its own manifest limits it to 4 -> requirement 8 fails.
        result = self.run_match(
            requirement(minimum_resources={"cpu_cores": 8, "ram_mb": 512, "gpu_memory_mb": 0}),
            [self.candidate(template={"available_resources": {"cpu_cores": 100, "ram_mb": 8192, "gpu_memory_mb": 0}})],
        )
        self.assertEqual(result.ranking, ())
        self.assertIn(RESOURCE_INSUFFICIENT, result.rejected[0].rejection_codes)

    def test_missing_resource_field_does_not_satisfy_requirement(self) -> None:
        candidate = self.candidate()
        broken = replace(
            candidate,
            announcement=replace(candidate.announcement, available_resources={"cpu_cores": 4}),
        )
        result = self.run_match(candidates=[broken])
        self.assertEqual(result.ranking, ())
        self.assertIn(RESOURCE_UNSPECIFIED, result.rejected[0].rejection_codes)

    def test_expired_announcement_is_rejected(self) -> None:
        # Manifest stays valid; only the announcement's short TTL runs out.
        result = self.run_match(now=NOW + 11, candidates=[self.candidate(announcement_ttl=10)])
        self.assertEqual(result.ranking, ())
        self.assertIn(ANNOUNCEMENT_EXPIRED, result.rejected[0].rejection_codes)

    def test_expired_manifest_is_rejected(self) -> None:
        expired = self.candidate(now=NOW - 200, template={"ttl_seconds": 120, "capability_version": 1})
        result = self.run_match(now=NOW, candidates=[expired])
        self.assertEqual(result.ranking, ())
        self.assertIn(MANIFEST_EXPIRED, result.rejected[0].rejection_codes)

    def test_revoked_candidate_is_rejected(self) -> None:
        result = self.run_match(candidates=[self.candidate(revoked=True)])
        self.assertEqual(result.ranking, ())
        self.assertIn(CAPABILITY_REVOKED, result.rejected[0].rejection_codes)

    def test_unreachable_candidate_is_not_treated_as_available(self) -> None:
        result = self.run_match(candidates=[self.candidate(reachable=False)])
        self.assertEqual(result.ranking, ())
        self.assertIn(NODE_UNREACHABLE, result.rejected[0].rejection_codes)

    def test_duration_limit_is_rejected(self) -> None:
        result = self.run_match(
            requirement(
                constraints={"max_execution_seconds": 900, "max_input_bytes": 1024, "max_output_bytes": 1024}
            )
        )
        self.assertEqual(result.ranking, ())
        self.assertIn(DURATION_LIMIT_EXCEEDED, result.rejected[0].rejection_codes)

    def test_not_accepting_is_rejected(self) -> None:
        result = self.run_match(candidates=[self.candidate(template={"accepting_tasks": False})])
        self.assertEqual(result.ranking, ())
        self.assertIn(NOT_ACCEPTING, result.rejected[0].rejection_codes)

    def test_private_data_fails_closed(self) -> None:
        result = self.run_match(
            requirement(data_policy={"requires_private_data": True, "allow_relay": True})
        )
        self.assertEqual(result.ranking, ())
        self.assertIn(DATA_POLICY_UNPROVEN, result.rejected[0].rejection_codes)

    def test_relay_is_refused_when_the_requirement_forbids_it(self) -> None:
        candidate = self.candidate()
        relayed = replace(
            candidate,
            announcement=replace(
                candidate.announcement,
                network_status={"connectivity": "RELAY", "supported_transports": ["tcp"]},
            ),
        )
        result = self.run_match(
            requirement(data_policy={"requires_private_data": False, "allow_relay": False}),
            [relayed],
        )
        self.assertEqual(result.ranking, ())
        self.assertIn(RELAY_NOT_ALLOWED, result.rejected[0].rejection_codes)

    def test_self_declared_is_not_upgraded_to_verified(self) -> None:
        result = self.run_match(requirement(verification_policy={"mode": "RECOMPUTE", "required": True}))
        self.assertEqual(result.ranking, ())
        self.assertIn(VERIFICATION_UNPROVEN, result.rejected[0].rejection_codes)
        self.assertEqual(result.rejected[0].trust_level, "SELF_DECLARED")

    def test_deadline_boundary(self) -> None:
        at_deadline = self.run_match(requirement(deadline=NOW))
        self.assertEqual(at_deadline.ranking, ())
        self.assertIn(DEADLINE_PASSED, at_deadline.rejected[0].rejection_codes)

        just_before = self.run_match(requirement(deadline=NOW + 1))
        self.assertEqual(len(just_before.ranking), 1)


class ScoringAndExplanationTests(MatcherTestBase):
    def test_score_range_and_breakdown_sum(self) -> None:
        result = self.run_match(
            candidates=[
                self.candidate("a"),
                self.candidate(
                    "b",
                    {
                        "available_resources": {"cpu_cores": 4, "ram_mb": 16384, "gpu_memory_mb": 0},
                        "verification": {"level": "VERIFIED", "benchmark_profile": "v1"},
                    },
                    seq=2,
                ),
            ]
        )
        self.assertEqual(len(result.ranking), 2)
        for item in result.ranking:
            self.assertTrue(0 <= item.score <= MAX_SCORE)
            self.assertEqual(sum(item.scores().values()), item.score)
            self.assertEqual(
                set(item.scores()),
                {"headroom", "trust", "idle", "connectivity", "preference"},
            )

    def test_scores_are_explainable(self) -> None:
        admitted = self.run_match().ranking[0]
        self.assertTrue(admitted.breakdown, "an admitted candidate must explain its score")
        self.assertTrue(admitted.admitted)
        self.assertEqual(admitted.rejection_codes, ())

        rejected = self.run_match(
            candidates=[self.candidate(template={"supported_task_types": ["onnx_inference_v1"]})]
        ).rejected[0]
        self.assertFalse(rejected.admitted)
        self.assertTrue(rejected.rejection_codes)
        self.assertTrue(rejected.rejection_details)
        self.assertEqual(rejected.score, 0)

    def test_verified_claim_scores_higher_than_self_declared(self) -> None:
        plain = self.run_match(candidates=[self.candidate("a")]).ranking[0]
        verified = self.run_match(
            candidates=[
                self.candidate(
                    "b", {"verification": {"level": "VERIFIED", "benchmark_profile": "v1"}}
                )
            ]
        ).ranking[0]
        self.assertEqual(plain.scores()["trust"], 0)
        self.assertGreater(verified.scores()["trust"], 0)
        self.assertGreater(verified.score, plain.score)


class DeterminismTests(MatcherTestBase):
    def _three(self):
        return [
            self.candidate("a"),
            self.candidate(
                "b",
                {
                    "available_resources": {"cpu_cores": 4, "ram_mb": 16384, "gpu_memory_mb": 0},
                    "verification": {"level": "BENCHMARKED", "benchmark_profile": "v1"},
                },
                seq=2,
            ),
            self.candidate("c", {"available_resources": {"cpu_cores": 2, "ram_mb": 2048, "gpu_memory_mb": 0}}, seq=3),
        ]

    def test_repeated_run_is_identical(self) -> None:
        first = self.run_match(candidates=self._three())
        second = self.run_match(candidates=self._three())
        self.assertEqual(first, second)

    def test_input_order_does_not_change_ranking(self) -> None:
        forward = self.run_match(candidates=self._three())
        backward = self.run_match(candidates=list(reversed(self._three())))
        self.assertEqual([item.node_id for item in forward.ranking], [item.node_id for item in backward.ranking])
        self.assertEqual([item.score for item in forward.ranking], [item.score for item in backward.ranking])
        self.assertEqual([item.node_id for item in forward.evaluations], [item.node_id for item in backward.evaluations])

    def test_tie_breaker_is_node_id(self) -> None:
        # Two identical candidates: different identity, same scores -> node_id order.
        a = self.candidate("a")
        b = self.candidate("b")
        first = self.run_match(candidates=[b, a])
        expected = sorted([a.node_id, b.node_id])
        self.assertEqual([item.node_id for item in first.ranking], expected)
        self.assertEqual(first.ranking[0].score, first.ranking[1].score)

    def test_does_not_mutate_inputs(self) -> None:
        req = requirement()
        candidates = self._three()
        req_before = copy.deepcopy(req)
        cand_before = [(c.node_id, c.manifest, c.announcement, c.reachable) for c in candidates]
        self.run_match(req, candidates)
        self.assertEqual(req, req_before)
        self.assertEqual(
            [(c.node_id, c.manifest, c.announcement, c.reachable) for c in candidates],
            cand_before,
        )


class EdgeCaseTests(MatcherTestBase):
    def test_empty_candidate_set(self) -> None:
        result = self.run_match(candidates=[])
        self.assertEqual(result.ranking, ())
        self.assertEqual(result.rejected, ())
        self.assertEqual(result.as_dict()["counts"], {"candidates": 0, "admitted": 0, "rejected": 0})

    def test_all_rejected_have_reasons(self) -> None:
        result = self.run_match(
            candidates=[
                self.candidate("a", {"supported_task_types": ["onnx_inference_v1"]}),
                self.candidate("b", {"supported_task_types": ["onnx_inference_v1"]}, seq=2),
            ]
        )
        self.assertEqual(len(result.rejected), 2)
        for item in result.rejected:
            self.assertTrue(item.rejection_codes)
            self.assertTrue(item.rejection_details)

    def test_invalid_requirement_raises(self) -> None:
        with self.assertRaises(MatcherError) as ctx:
            match(requirement(task_type="not_whitelisted"), [], now=NOW)
        self.assertEqual(ctx.exception.code, MatcherError.INVALID_REQUIREMENT)

        with self.assertRaises(MatcherError):
            match(requirement(minimum_resources={"cpu_cores": 0, "ram_mb": 1, "gpu_memory_mb": 0}), [], now=NOW)

    def test_now_must_be_an_int(self) -> None:
        with self.assertRaises(MatcherError):
            match(requirement(), [], now=True)  # type: ignore[arg-type]

    def test_candidate_with_wrong_types_is_rejected_not_raised(self) -> None:
        result = self.run_match(candidates=[object()])
        self.assertEqual(result.ranking, ())
        self.assertTrue(result.rejected[0].rejection_codes)

    def test_verify_signatures_false_still_checks_expiry(self) -> None:
        expired = self.candidate()
        result = match(requirement(), [expired], now=NOW + 121, verify_signatures=False)
        self.assertEqual(result.ranking, ())
        self.assertIn(ANNOUNCEMENT_EXPIRED, result.rejected[0].rejection_codes)

    def test_from_cache_entry_adapter(self) -> None:
        candidate = self.candidate()

        class Entry:
            node_id = candidate.node_id
            manifest = candidate.manifest
            announcement = candidate.announcement
            reachable = True

        adapted = Candidate.from_cache_entry(Entry())
        self.assertEqual(adapted.node_id, candidate.node_id)
        self.assertEqual(self.run_match(candidates=[adapted]).ranking[0].node_id, candidate.node_id)

    def test_explain_lookup(self) -> None:
        result = self.run_match()
        node_id = self.identities["a"].node_id
        self.assertIsNotNone(result.explain(node_id))
        self.assertIsNone(result.explain(self.identities["c"].node_id))


if __name__ == "__main__":
    unittest.main()
