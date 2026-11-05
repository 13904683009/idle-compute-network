"""Deterministic, explainable capability matching (D3).

Inputs are the D1/D2 models themselves -- ``TaskRequirement``,
``CapabilityManifest``, ``CapabilityAnnouncement`` -- wrapped in a
``Candidate``. Nothing here reads a database, touches the network, spawns a
process, or mutates its inputs; the same arguments and the same ``now`` always
produce the same result.

Pipeline (hard constraints first, scoring only for survivors)::

    requirement valid?
      -> candidate shape / node_id consistency
      -> signatures (optional, pure) and expiry
      -> revoked / unreachable
      -> deadline
      -> task_type, runtime
      -> resources (cpu_cores, ram_mb, gpu_memory_mb) capped by the manifest's
         own resource_limits; a MISSING field never satisfies a requirement
      -> max_execution_seconds vs resource_limits.max_task_duration_seconds
      -> accepting_tasks, relay policy, private-data policy, verification level

Rejection codes (stable strings):

    INVALID_CANDIDATE / NODE_ID_MISMATCH / MALFORMED_CAPABILITY / SIGNATURE_INVALID
    MANIFEST_EXPIRED / ANNOUNCEMENT_EXPIRED / CAPABILITY_REVOKED / NODE_UNREACHABLE
    DEADLINE_PASSED / TASK_TYPE_UNSUPPORTED / RUNTIME_MISMATCH / RESOURCE_UNSPECIFIED
    RESOURCE_INSUFFICIENT / DURATION_LIMIT_EXCEEDED / NOT_ACCEPTING
    RELAY_NOT_ALLOWED / DATA_POLICY_UNPROVEN / VERIFICATION_UNPROVEN

Scoring (only for admitted candidates), total 0..100:

    resource headroom        0..40   min over required dims of available/required,
                                     bonus only above 100% (2x headroom = full 40)
    declared trust level     0..25   SELF_DECLARED=0, BENCHMARKED=15, VERIFIED=25
    idle queue               0..20   0 jobs=20, 1=15, 2=10, >=3=0
    connectivity             0..5    DIRECT=5, RELAY=2, UNKNOWN=0
    gpu_vendor preference    0..10   only when the requirement names one AND the
                                     manifest declares it; absence scores 0

Tie-breaker: highest score first, then ``node_id`` ascending -- never the input
order, never dict iteration order.

Trust honesty: ``SELF_DECLARED`` is never upgraded. Every manifest field and
every announcement field is self-reported; ``verification.level`` is the
node's claim about its own evidence, so a requirement with
``verification_policy.required == true`` only admits BENCHMARKED/VERIFIED
claims (and still labels them as claims).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .capability import CapabilityAnnouncement, CapabilityError, CapabilityManifest
from .requirement import RequirementError, TaskRequirement

MAX_SCORE = 100
WEIGHT_HEADROOM = 40
WEIGHT_TRUST = 25
WEIGHT_IDLE = 20
WEIGHT_CONNECTIVITY = 5
WEIGHT_PREFERENCE = 10

TRUST_POINTS = {"SELF_DECLARED": 0, "BENCHMARKED": 15, "VERIFIED": 25}
CONNECTIVITY_POINTS = {"DIRECT": 5, "RELAY": 2, "UNKNOWN": 0}
IDLE_POINTS = (20, 15, 10)  # 0, 1, 2 queued+running; >=3 -> 0

# Rejection codes -----------------------------------------------------------
INVALID_CANDIDATE = "INVALID_CANDIDATE"
NODE_ID_MISMATCH = "NODE_ID_MISMATCH"
MALFORMED_CAPABILITY = "MALFORMED_CAPABILITY"
SIGNATURE_INVALID = "SIGNATURE_INVALID"
MANIFEST_EXPIRED = "MANIFEST_EXPIRED"
ANNOUNCEMENT_EXPIRED = "ANNOUNCEMENT_EXPIRED"
CAPABILITY_REVOKED = "CAPABILITY_REVOKED"
NODE_UNREACHABLE = "NODE_UNREACHABLE"
DEADLINE_PASSED = "DEADLINE_PASSED"
TASK_TYPE_UNSUPPORTED = "TASK_TYPE_UNSUPPORTED"
RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
RESOURCE_UNSPECIFIED = "RESOURCE_UNSPECIFIED"
RESOURCE_INSUFFICIENT = "RESOURCE_INSUFFICIENT"
DURATION_LIMIT_EXCEEDED = "DURATION_LIMIT_EXCEEDED"
NOT_ACCEPTING = "NOT_ACCEPTING"
RELAY_NOT_ALLOWED = "RELAY_NOT_ALLOWED"
DATA_POLICY_UNPROVEN = "DATA_POLICY_UNPROVEN"
VERIFICATION_UNPROVEN = "VERIFICATION_UNPROVEN"
# D5 registry-first matching: the registry is authoritative when it has a
# record; a missing record falls back to the in-memory announcement cache.
REGISTRY_REVOKED = "REGISTRY_REVOKED"
REGISTRY_EXPIRED = "REGISTRY_EXPIRED"
REGISTRY_NOT_FINAL = "REGISTRY_NOT_FINAL"
REGISTRY_MISMATCH = "REGISTRY_MISMATCH"

#: Fields that come from the candidate's own claim, not from an independent
#: measurement. Reported per evaluation so callers cannot mistake them for
#: verified facts.
SELF_DECLARED_FIELDS = (
    "manifest.hardware",
    "manifest.resource_limits",
    "manifest.network",
    "manifest.verification",
    "announcement.available_resources",
    "announcement.queue",
    "announcement.network_status",
)

_LIMIT_FOR = {
    "cpu_cores": "max_cpu_cores",
    "ram_mb": "max_ram_mb",
    "gpu_memory_mb": "max_gpu_memory_mb",
}


class MatcherError(Exception):
    """The matcher was called with an unusable requirement (fail closed)."""

    INVALID_REQUIREMENT = "INVALID_REQUIREMENT"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Candidate:
    """One peer's signed capability state plus discovery-layer trust flags."""

    node_id: str
    manifest: CapabilityManifest
    announcement: CapabilityAnnouncement
    reachable: bool = True
    revoked: bool = False

    @classmethod
    def from_cache_entry(cls, entry: Any, *, revoked: bool = False) -> "Candidate":
        """Adapt a ``discovery.CachedPeer`` (duck-typed; no DB import)."""
        return cls(
            node_id=entry.node_id,
            manifest=entry.manifest,
            announcement=entry.announcement,
            reachable=bool(entry.reachable),
            revoked=revoked,
        )


@dataclass(frozen=True)
class CandidateEvaluation:
    node_id: str
    admitted: bool
    rejection_codes: tuple[str, ...]
    rejection_details: tuple[str, ...]
    score: int
    breakdown: tuple[tuple[str, int], ...]
    trust_level: str
    accepting_tasks: bool
    #: True only when the LocalRegistryAdapter confirmed this exact manifest
    #: (FINAL, same hash/version); False means "cache fallback / unregistered".
    registry_verified: bool = False

    def scores(self) -> dict[str, int]:
        return dict(self.breakdown)


@dataclass(frozen=True)
class MatchResult:
    matched_at: int
    requirement_hash: str
    evaluations: tuple[CandidateEvaluation, ...]
    ranking: tuple[CandidateEvaluation, ...]
    rejected: tuple[CandidateEvaluation, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched_at": self.matched_at,
            "requirement_hash": self.requirement_hash,
            "counts": {
                "candidates": len(self.evaluations),
                "admitted": len(self.ranking),
                "rejected": len(self.rejected),
            },
            "ranking": [
                {
                    "node_id": item.node_id,
                    "score": item.score,
                    "breakdown": dict(item.breakdown),
                    "trust_level": item.trust_level,
                    "registry_verified": item.registry_verified,
                    "self_declared_fields": list(SELF_DECLARED_FIELDS),
                }
                for item in self.ranking
            ],
            "rejected": [
                {
                    "node_id": item.node_id,
                    "codes": list(item.rejection_codes),
                    "details": list(item.rejection_details),
                }
                for item in self.rejected
            ],
        }

    def explain(self, node_id: str) -> CandidateEvaluation | None:
        for item in self.evaluations:
            if item.node_id == node_id:
                return item
        return None


def match(
    requirement: TaskRequirement,
    candidates: Iterable[Any],
    *,
    now: int,
    verify_signatures: bool = True,
    registry: Any | None = None,
) -> MatchResult:
    """Filter, score and rank ``candidates`` for ``requirement`` at ``now``.

    Raises ``MatcherError(INVALID_REQUIREMENT)`` when the requirement itself is
    invalid -- there is no "everything rejected" answer for a malformed
    requirement. Per-candidate problems never raise; they become rejection
    codes so one bad peer cannot hide the rest.
    """
    _require_int(now)
    _validate_requirement(requirement)

    evaluations: list[CandidateEvaluation] = []
    for raw in candidates:
        evaluations.append(
            _evaluate(
                requirement,
                raw,
                now=now,
                verify_signatures=verify_signatures,
                registry=registry,
            )
        )

    evaluations.sort(key=lambda item: item.node_id)
    ranking = sorted(
        (item for item in evaluations if item.admitted),
        key=lambda item: (-item.score, item.node_id),
    )
    rejected = [item for item in evaluations if not item.admitted]
    return MatchResult(
        matched_at=now,
        requirement_hash=requirement.hash(),
        evaluations=tuple(evaluations),
        ranking=tuple(ranking),
        rejected=tuple(rejected),
    )


# ---------------------------------------------------------------------------
# Internals


def _require_int(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MatcherError(MatcherError.INVALID_REQUIREMENT, f"now must be an int, got {type(value).__name__}")


def _validate_requirement(requirement: TaskRequirement) -> None:
    if not isinstance(requirement, TaskRequirement):
        raise MatcherError(
            MatcherError.INVALID_REQUIREMENT,
            f"requirement must be a TaskRequirement, got {type(requirement).__name__}",
        )
    try:
        requirement.validate()
    except RequirementError as exc:
        raise MatcherError(MatcherError.INVALID_REQUIREMENT, f"{exc.code}: {exc.message}") from exc


def _evaluate(
    requirement: TaskRequirement,
    raw: Any,
    *,
    now: int,
    verify_signatures: bool,
    registry: Any | None = None,
) -> CandidateEvaluation:
    node_id = getattr(raw, "node_id", "<unknown>")
    codes: list[str] = []
    details: list[str] = []

    def reject(code: str, detail: str = "") -> None:
        if code not in codes:
            codes.append(code)
            details.append(f"{code}: {detail}" if detail else code)

    manifest = getattr(raw, "manifest", None)
    announcement = getattr(raw, "announcement", None)
    if not isinstance(manifest, CapabilityManifest) or not isinstance(
        announcement, CapabilityAnnouncement
    ):
        reject(INVALID_CANDIDATE, "candidate must expose CapabilityManifest and CapabilityAnnouncement")
        return _evaluation(str(node_id), codes, details, score=0, breakdown=(), trust="UNKNOWN", accepting=False)

    if manifest.node_id != announcement.node_id or str(node_id) != manifest.node_id:
        reject(
            NODE_ID_MISMATCH,
            f"candidate={node_id} manifest={manifest.node_id} announcement={announcement.node_id}",
        )

    # Signature + expiry ------------------------------------------------------
    manifest_ok = True
    if verify_signatures:
        try:
            manifest.verify(now)
        except CapabilityError as exc:
            manifest_ok = False
            if exc.code == CapabilityError.EXPIRED:
                reject(MANIFEST_EXPIRED, "manifest has expired")
            elif exc.code == CapabilityError.SIGNATURE:
                reject(SIGNATURE_INVALID, f"manifest: {exc.message}")
            else:
                reject(MALFORMED_CAPABILITY, f"manifest: {exc.message}")
    elif manifest.expires_at <= now:
        manifest_ok = False
        reject(MANIFEST_EXPIRED, f"expires_at={manifest.expires_at} <= now={now}")

    if verify_signatures and manifest_ok:
        try:
            announcement.verify(manifest, now)
        except CapabilityError as exc:
            if exc.code == CapabilityError.EXPIRED:
                reject(ANNOUNCEMENT_EXPIRED, "announcement has expired")
            elif exc.code == CapabilityError.SIGNATURE:
                reject(SIGNATURE_INVALID, f"announcement: {exc.message}")
            else:
                reject(MALFORMED_CAPABILITY, f"announcement: {exc.message}")
    elif not verify_signatures and announcement.expires_at <= now:
        reject(ANNOUNCEMENT_EXPIRED, f"expires_at={announcement.expires_at} <= now={now}")

    # D5 registry-first: a FINAL registry record for exactly this manifest
    # wins; a missing record falls back to the announcement cache. A record
    # that exists but is revoked/expired/pending/mismatched is authoritative
    # and rejects the candidate (cache cannot override the registry).
    registry_verified = False
    if registry is not None:
        try:
            record = registry.get(manifest.node_id, now=now)
        except Exception:
            record = None  # registry unavailable -> cache fallback (fail open to cache)
        if record is not None:
            if record.status == "REVOKED":
                reject(REGISTRY_REVOKED, "registry says this capability version is revoked")
            elif record.status == "EXPIRED":
                reject(REGISTRY_EXPIRED, "registry record has expired")
            elif record.status == "PENDING":
                reject(REGISTRY_NOT_FINAL, "registry record is PENDING, not final")
            elif (
                record.manifest_hash != manifest.hash()
                or record.capability_version != manifest.capability_version
            ):
                reject(REGISTRY_MISMATCH, "registry holds a different manifest/version")
            else:
                registry_verified = True

    # Trust flags from the discovery layer -----------------------------------
    if bool(getattr(raw, "revoked", False)):
        reject(CAPABILITY_REVOKED, "capability version is revoked")
    if not bool(getattr(raw, "reachable", False)):
        reject(NODE_UNREACHABLE, "no live frame within the discovery liveness window")

    # Deadline ---------------------------------------------------------------
    if requirement.deadline <= now:
        reject(DEADLINE_PASSED, f"deadline={requirement.deadline} <= now={now}")

    # Capability matches -----------------------------------------------------
    if requirement.task_type not in manifest.supported_task_types:
        reject(
            TASK_TYPE_UNSUPPORTED,
            f"{requirement.task_type!r} not in {list(manifest.supported_task_types)}",
        )
    if requirement.required_runtime not in manifest.supported_runtimes:
        reject(
            RUNTIME_MISMATCH,
            f"{requirement.required_runtime!r} not in {list(manifest.supported_runtimes)}",
        )

    # Resource headroom ------------------------------------------------------
    available: dict[str, int] = {}
    for key in ("cpu_cores", "ram_mb", "gpu_memory_mb"):
        required = requirement.minimum_resources[key]
        if key not in announcement.available_resources:
            reject(RESOURCE_UNSPECIFIED, f"announcement.available_resources.{key} is missing")
            continue
        raw_available = announcement.available_resources[key]
        limit = manifest.resource_limits[_LIMIT_FOR[key]]
        effective = min(int(raw_available), int(limit))
        available[key] = effective
        if effective < required:
            reject(
                RESOURCE_INSUFFICIENT,
                f"{key}: available {effective} (min(announced {raw_available}, limit {limit})) < required {required}",
            )

    duration_required = requirement.constraints["max_execution_seconds"]
    duration_limit = manifest.resource_limits["max_task_duration_seconds"]
    if duration_required > duration_limit:
        reject(
            DURATION_LIMIT_EXCEEDED,
            f"max_execution_seconds {duration_required} > max_task_duration_seconds {duration_limit}",
        )

    if not announcement.accepting_tasks:
        reject(NOT_ACCEPTING, "announcement.accepting_tasks is false")

    connectivity = announcement.network_status.get("connectivity", "UNKNOWN")
    if not requirement.data_policy["allow_relay"] and connectivity == "RELAY":
        reject(RELAY_NOT_ALLOWED, "requirement forbids relay but the peer reports RELAY")

    if requirement.data_policy["requires_private_data"]:
        # No manifest/announcement field can prove private-data handling, so
        # this fails closed instead of silently admitting a peer.
        reject(DATA_POLICY_UNPROVEN, "no capability field can prove private-data handling")

    trust_level = manifest.verification.get("level", "UNKNOWN")
    if requirement.verification_policy["required"] and trust_level == "SELF_DECLARED":
        reject(
            VERIFICATION_UNPROVEN,
            "verification required but the peer only claims SELF_DECLARED",
        )

    if codes:
        return _evaluation(
            manifest.node_id,
            codes,
            details,
            score=0,
            breakdown=(),
            trust=trust_level,
            accepting=bool(announcement.accepting_tasks),
        )

    breakdown, total = _score(requirement, manifest, announcement, available)
    return _evaluation(
        manifest.node_id,
        codes,
        details,
        score=total,
        breakdown=breakdown,
        trust=trust_level,
        accepting=bool(announcement.accepting_tasks),
        registry_verified=registry_verified,
    )


def _evaluation(
    node_id: str,
    codes: Sequence[str],
    details: Sequence[str],
    *,
    score: int,
    breakdown: Sequence[tuple[str, int]],
    trust: str,
    accepting: bool,
    registry_verified: bool = False,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        node_id=node_id,
        admitted=not codes,
        rejection_codes=tuple(codes),
        rejection_details=tuple(details),
        score=int(score),
        breakdown=tuple(breakdown),
        trust_level=trust,
        accepting_tasks=accepting,
        registry_verified=registry_verified,
    )


def _score(
    requirement: TaskRequirement,
    manifest: CapabilityManifest,
    announcement: CapabilityAnnouncement,
    available: Mapping[str, int],
) -> tuple[tuple[tuple[str, int], ...], int]:
    required = requirement.minimum_resources

    # Integer-only headroom: ratio in percent of the tightest required dim.
    ratios = [
        available[key] * 100 // required[key]
        for key in ("cpu_cores", "ram_mb", "gpu_memory_mb")
        if required[key] > 0
    ]
    tightest = min(ratios) if ratios else 100
    headroom = min(WEIGHT_HEADROOM, max(0, (tightest - 100) * WEIGHT_HEADROOM // 100))

    trust = WEIGHT_TRUST * TRUST_POINTS.get(manifest.verification.get("level", ""), 0) // 25

    jobs = int(announcement.queue.get("running_tasks", 0)) + int(
        announcement.queue.get("queued_tasks", 0)
    )
    idle = IDLE_POINTS[jobs] if jobs < len(IDLE_POINTS) else 0

    connectivity = WEIGHT_CONNECTIVITY * CONNECTIVITY_POINTS.get(
        announcement.network_status.get("connectivity", "UNKNOWN"), 0
    ) // 5

    preference = 0
    wanted_vendor = requirement.preferred_resources.get("gpu_vendor")
    if wanted_vendor is not None and manifest.hardware.get("gpu_vendor") == wanted_vendor:
        preference = WEIGHT_PREFERENCE

    breakdown = (
        ("headroom", headroom),
        ("trust", trust),
        ("idle", idle),
        ("connectivity", connectivity),
        ("preference", preference),
    )
    total = sum(points for _, points in breakdown)
    if not 0 <= total <= MAX_SCORE:  # pragma: no cover - guards future edits
        raise MatcherError(MatcherError.INVALID_REQUIREMENT, f"score {total} outside 0..{MAX_SCORE}")
    return breakdown, total
