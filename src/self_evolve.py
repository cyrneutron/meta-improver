"""Pure offline contracts for the first controlled self-evolve boundary.

This module describes a prompt/rules candidate and its offline evaluation.  It
does not generate prompts, invoke a model, read a dataset, run a command, or
touch a repository.  The only decision it can make is whether already captured
evaluation snapshots satisfy the bounded acceptance rules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from statistics import mean, pvariance
from typing import Any, Literal, TypeAlias

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SAFE_METRIC = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_CONTROL = re.compile(r"[;&|`$< >\r\n\x00]".replace(" ", ""))
_SECRET = re.compile(
    r"(?:\bbearer\s+\S+|(?<![A-Za-z0-9])(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]+|"
    r"(?<![A-Za-z0-9])(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)"
    r"\s*[:=]\s*\S+)",
    re.IGNORECASE,
)

MAX_SCORES = 1_000
MAX_METRICS = 100
MAX_SCORE = 1_000_000_000.0
# A fixed protocol bound makes repeatability deterministic and auditable.
MAX_GAIN_VARIANCE = 0.01

_LEGACY_CANDIDATE_HASH = "sha256:" + "0" * 64
_LEGACY_PROMPT_VERSION = "legacy-prompt-v1"
_LEGACY_RULES_VERSION = "legacy-rules-v1"
_LEGACY_EVALUATOR_VERSION = "legacy-evaluator-v1"
_LEGACY_PARENT_COMMIT = "0" * 40
_LEGACY_ROLLBACK_REF = "refs/heads/self-evolve-rollback"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_version(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not _SAFE_VERSION.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith((".", "/", ":"))
        or _CONTROL.search(value)
        or _SECRET.search(value)
    ):
        raise ValueError(f"{field_name} must be a bounded L1 version")
    return value


def _safe_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not _SAFE_REF.fullmatch(value)
        or value.startswith(("/", "~"))
        or ".." in value
        or "//" in value
        or "\\" in value
        or value.endswith((".", "/"))
        or _CONTROL.search(value)
        or _SECRET.search(value)
    ):
        raise ValueError("rollback_ref must be a bounded safe relative ref")
    return value


def _safe_score(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must contain finite numeric scores")
    numeric = float(value)
    if not math.isfinite(numeric) or abs(numeric) > MAX_SCORE:
        raise ValueError(f"{field_name} must contain finite bounded scores")
    return numeric


class SelfEvolveError(ValueError):
    """Raised when a self-evolve contract cannot be proven safe."""


class _SelfEvolveContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


ScoreVector: TypeAlias = list[float] | dict[str, float]


class PromptCandidate(_SelfEvolveContract):
    """The complete L1-only candidate identity.

    There is intentionally no patch, code path, model configuration, or
    executable field here.  Prompt and rules versions are opaque bounded
    identifiers; the parent and rollback ref make recovery explicit.
    """

    schema_: Literal["prompt-candidate/v1"] = Field(
        default="prompt-candidate/v1", alias="schema", serialization_alias="schema"
    )
    parent_commit: str = Field(pattern=_COMMIT)
    prompt_version: str = Field(min_length=1, max_length=128)
    rules_version: str = Field(min_length=1, max_length=128)
    rollback_ref: str = Field(min_length=1, max_length=256)
    candidate_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("prompt_version", "rules_version")
    @classmethod
    def safe_l1_versions(cls, value: str, info: Any) -> str:
        return _safe_version(value, info.field_name)

    @field_validator("rollback_ref")
    @classmethod
    def safe_rollback_ref(cls, value: str) -> str:
        return _safe_ref(value)

    @model_validator(mode="after")
    def derive_hash(self) -> PromptCandidate:
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"candidate_hash"})
        )
        if self.candidate_hash is not None and self.candidate_hash != expected:
            raise ValueError("candidate_hash does not match canonical contents")
        object.__setattr__(self, "candidate_hash", expected)
        return self


class EvalSnapshot(_SelfEvolveContract):
    """One captured, hash-bound run set over an immutable dataset manifest.

    ``baseline_scores`` and ``holdout_scores`` are repeated scores from the
    reference and holdout split respectively.  A mapping is useful for named
    metrics; a list is useful for a single aggregate metric.  New snapshots
    must identify the split and the exact L1 candidate/evaluator identity.
    The old ``dataset_hash=...`` constructor remains accepted and is upgraded
    to that identity by :func:`plan_self_evolve`.
    """

    schema_: Literal["self-evolve-eval-snapshot/v1"] = Field(
        default="self-evolve-eval-snapshot/v1", alias="schema", serialization_alias="schema"
    )
    dataset_manifest: str = Field(
        pattern=_HASH,
        min_length=71,
        max_length=71,
        validation_alias=AliasChoices(
            "dataset_manifest", "dataset_manifest_hash", "dataset_hash"
        ),
    )
    split: Literal["baseline", "holdout", "both"] = "both"
    candidate_hash: str | None = Field(
        default=None,
        pattern=_HASH,
        validation_alias=AliasChoices("candidate_hash", "candidate"),
    )
    prompt_version: str | None = Field(default=None, min_length=1, max_length=128)
    rules_version: str | None = Field(default=None, min_length=1, max_length=128)
    evaluator_version: str | None = Field(default=None, min_length=1, max_length=128)
    parent_commit: str | None = Field(default=None, pattern=_COMMIT, min_length=7, max_length=64)
    rollback_ref: str | None = Field(default=None, min_length=1, max_length=256)
    baseline_scores: ScoreVector = Field(min_length=1, max_length=MAX_SCORES)
    holdout_scores: ScoreVector = Field(min_length=1, max_length=MAX_SCORES)
    cost_units: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    eval_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="before")
    @classmethod
    def support_legacy_constructor(cls, value: Any) -> Any:
        """Fill only the historical ``dataset_hash`` shape.

        Modern manifest payloads do not receive defaults, so omitted identity
        fields fail closed instead of silently becoming an unbound snapshot.
        """

        if not isinstance(value, Mapping):
            return value
        modern_manifest = any(
            key in value for key in ("dataset_manifest", "dataset_manifest_hash")
        )
        if "dataset_hash" not in value or modern_manifest:
            return value
        upgraded = dict(value)
        upgraded.setdefault("dataset_manifest", upgraded["dataset_hash"])
        upgraded.pop("dataset_hash", None)
        upgraded.setdefault("candidate_hash", _LEGACY_CANDIDATE_HASH)
        upgraded.setdefault("prompt_version", _LEGACY_PROMPT_VERSION)
        upgraded.setdefault("rules_version", _LEGACY_RULES_VERSION)
        upgraded.setdefault("evaluator_version", _LEGACY_EVALUATOR_VERSION)
        upgraded.setdefault("parent_commit", _LEGACY_PARENT_COMMIT)
        upgraded.setdefault("rollback_ref", _LEGACY_ROLLBACK_REF)
        upgraded.setdefault("split", "both")
        return upgraded

    @property
    def dataset_hash(self) -> str:
        """Historical attribute spelling for callers of the original API."""

        return self.dataset_manifest

    @property
    def dataset_manifest_hash(self) -> str:
        return self.dataset_manifest

    @property
    def candidate(self) -> str:
        """Historical-friendly spelling for the candidate digest binding."""

        return self.candidate_hash or ""

    @field_validator("prompt_version", "rules_version", "evaluator_version")
    @classmethod
    def safe_snapshot_versions(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _safe_version(value, info.field_name)

    @field_validator("rollback_ref")
    @classmethod
    def safe_snapshot_rollback_ref(cls, value: str | None) -> str | None:
        return None if value is None else _safe_ref(value)

    @field_validator("candidate_hash", mode="before")
    @classmethod
    def candidate_digest(cls, value: Any) -> Any:
        if isinstance(value, PromptCandidate):
            return value.candidate_hash
        return value

    @field_validator("baseline_scores", "holdout_scores")
    @classmethod
    def validate_scores(cls, value: ScoreVector, info: Any) -> ScoreVector:
        if isinstance(value, list):
            return [_safe_score(item, info.field_name) for item in value]
        if isinstance(value, dict):
            if len(value) > MAX_METRICS:
                raise ValueError(f"{info.field_name} has too many metrics")
            normalized: dict[str, float] = {}
            for name, score in value.items():
                if not isinstance(name, str) or not _SAFE_METRIC.fullmatch(name):
                    raise ValueError(f"{info.field_name} contains an unsafe metric name")
                normalized[name] = _safe_score(score, info.field_name)
            if not normalized:
                raise ValueError(f"{info.field_name} must not be empty")
            return dict(sorted(normalized.items()))
        raise ValueError(f"{info.field_name} must be a score list or metric mapping")

    @model_validator(mode="after")
    def derive_hash(self) -> EvalSnapshot:
        metadata = (
            self.candidate_hash,
            self.prompt_version,
            self.rules_version,
            self.evaluator_version,
            self.parent_commit,
            self.rollback_ref,
        )
        legacy = metadata == (
            _LEGACY_CANDIDATE_HASH,
            _LEGACY_PROMPT_VERSION,
            _LEGACY_RULES_VERSION,
            _LEGACY_EVALUATOR_VERSION,
            _LEGACY_PARENT_COMMIT,
            _LEGACY_ROLLBACK_REF,
        ) and self.split == "both"
        if any(item is None for item in metadata):
            raise ValueError("EvalSnapshot identity fields are required")
        if not legacy and self.split == "both":
            raise ValueError("EvalSnapshot split must be baseline or holdout")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"eval_hash"}))
        if self.eval_hash is not None and self.eval_hash != expected:
            raise ValueError("eval_hash does not match canonical contents")
        object.__setattr__(self, "eval_hash", expected)
        return self


class SelfEvolvePlan(_SelfEvolveContract):
    """A deterministic acceptance calculation over two hash-bound snapshots."""

    schema_: Literal["self-evolve-plan/v1"] = Field(
        default="self-evolve-plan/v1", alias="schema", serialization_alias="schema"
    )
    candidate: PromptCandidate
    baseline_eval: EvalSnapshot
    holdout_eval: EvalSnapshot
    repeat_runs: int = Field(ge=1, le=MAX_SCORES)
    min_gain: float = Field(ge=-MAX_SCORE, le=MAX_SCORE, allow_inf_nan=False)
    max_cost: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    plan_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_dataset_binding_and_hash(self) -> SelfEvolvePlan:
        if self.baseline_eval.dataset_manifest != self.holdout_eval.dataset_manifest:
            raise ValueError("baseline and holdout evaluations must use the same dataset hash/manifest")
        identity_fields = (
            "candidate_hash",
            "prompt_version",
            "rules_version",
            "evaluator_version",
            "parent_commit",
            "rollback_ref",
        )
        for field_name in identity_fields:
            if getattr(self.baseline_eval, field_name) != getattr(self.holdout_eval, field_name):
                raise ValueError(f"baseline and holdout evaluations must share {field_name}")
            candidate_value = getattr(self.candidate, field_name, None)
            if candidate_value is not None and getattr(self.baseline_eval, field_name) != candidate_value:
                raise ValueError(f"evaluation {field_name} does not match candidate")
        if self.baseline_eval.split == "baseline" and self.holdout_eval.split != "holdout":
            raise ValueError("holdout evaluation must use the holdout split")
        if self.holdout_eval.split == "holdout" and self.baseline_eval.split != "baseline":
            raise ValueError("baseline evaluation must use the baseline split")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"plan_hash"}))
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("plan_hash does not match canonical contents")
        object.__setattr__(self, "plan_hash", expected)
        return self


class SelfEvolveStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SelfEvolveReceipt(_SelfEvolveContract):
    """Replayable result of the offline gates; it carries no side effect."""

    schema_: Literal["self-evolve-receipt/v1"] = Field(
        default="self-evolve-receipt/v1", alias="schema", serialization_alias="schema"
    )
    status: SelfEvolveStatus
    plan_hash: str = Field(pattern=_HASH)
    candidate_hash: str = Field(pattern=_HASH)
    parent_commit: str = Field(pattern=_COMMIT)
    rollback_ref: str = Field(min_length=1, max_length=256)
    holdout_gain: float = Field(allow_inf_nan=False)
    baseline_gain: float = Field(allow_inf_nan=False)
    gain_variance: float = Field(ge=0, allow_inf_nan=False)
    total_cost: float = Field(ge=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2_000)
    receipt_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("rollback_ref")
    @classmethod
    def safe_receipt_ref(cls, value: str) -> str:
        return _safe_ref(value)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        if _CONTROL.search(value) or _SECRET.search(value):
            raise ValueError("reason contains unsafe or secret-shaped text")
        return value

    @model_validator(mode="after")
    def derive_hash(self) -> SelfEvolveReceipt:
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"}))
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("receipt_hash does not match canonical contents")
        object.__setattr__(self, "receipt_hash", expected)
        return self


def _rehydrate(value: Any, model: type[_SelfEvolveContract], hash_field: str) -> Any:
    if not isinstance(value, model) or not getattr(value, hash_field, None):
        raise SelfEvolveError(f"unhashed {model.__name__} is rejected")
    existing = getattr(value, hash_field)
    canonical = _canonical(value.model_dump(mode="json", by_alias=True))
    try:
        hydrated = model.model_validate(json.loads(canonical))
    except Exception as exc:
        raise SelfEvolveError(
            f"{model.__name__} failed integrity rehydration for {hash_field}"
        ) from exc
    if getattr(hydrated, hash_field) != existing or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise SelfEvolveError(f"{model.__name__} hash does not match canonical contents")
    return hydrated


def rehydrate_prompt_candidate(value: PromptCandidate) -> PromptCandidate:
    return _rehydrate(value, PromptCandidate, "candidate_hash")


def rehydrate_eval_snapshot(value: EvalSnapshot) -> EvalSnapshot:
    return _rehydrate(value, EvalSnapshot, "eval_hash")


def rehydrate_self_evolve_plan(value: SelfEvolvePlan) -> SelfEvolvePlan:
    return _rehydrate(value, SelfEvolvePlan, "plan_hash")


def rehydrate_self_evolve_receipt(value: SelfEvolveReceipt) -> SelfEvolveReceipt:
    return _rehydrate(value, SelfEvolveReceipt, "receipt_hash")


def _is_legacy_snapshot(value: EvalSnapshot) -> bool:
    return (
        value.split == "both"
        and value.candidate_hash == _LEGACY_CANDIDATE_HASH
        and value.prompt_version == _LEGACY_PROMPT_VERSION
        and value.rules_version == _LEGACY_RULES_VERSION
        and value.evaluator_version == _LEGACY_EVALUATOR_VERSION
        and value.parent_commit == _LEGACY_PARENT_COMMIT
        and value.rollback_ref == _LEGACY_ROLLBACK_REF
    )


def _bind_snapshot(
    value: EvalSnapshot,
    candidate: PromptCandidate,
    split: Literal["baseline", "holdout"],
) -> EvalSnapshot:
    """Bind a legacy snapshot or verify a modern snapshot's full identity."""

    snapshot = rehydrate_eval_snapshot(value)
    candidate_hash = candidate.candidate_hash
    if candidate_hash is None:
        raise SelfEvolveError("candidate is missing candidate_hash")
    if _is_legacy_snapshot(snapshot):
        payload = snapshot.model_dump(mode="json", by_alias=True)
        payload.update(
            {
                "split": split,
                "candidate_hash": candidate_hash,
                "prompt_version": candidate.prompt_version,
                "rules_version": candidate.rules_version,
                "parent_commit": candidate.parent_commit,
                "rollback_ref": candidate.rollback_ref,
                # The first version is the only evaluator available at this
                # boundary; it remains an explicit hash-bound identity.
                "evaluator_version": "evaluator-v1",
                "eval_hash": None,
            }
        )
        try:
            return rehydrate_eval_snapshot(EvalSnapshot.model_validate(payload))
        except Exception as exc:
            raise SelfEvolveError("legacy evaluation snapshot binding failed") from exc
    for field_name in (
        "candidate_hash",
        "prompt_version",
        "rules_version",
        "parent_commit",
        "rollback_ref",
    ):
        if getattr(snapshot, field_name) != getattr(candidate, field_name):
            raise SelfEvolveError(f"evaluation {field_name} does not match candidate")
    if snapshot.split != split:
        raise SelfEvolveError(f"evaluation split must be {split}")
    return snapshot


def _values(scores: ScoreVector) -> list[float]:
    return list(scores) if isinstance(scores, list) else list(scores.values())


def _mean(scores: ScoreVector) -> float:
    return mean(_values(scores))


def _run_gains(reference: ScoreVector, candidate: ScoreVector, repeat_runs: int) -> list[float]:
    if isinstance(reference, dict) or isinstance(candidate, dict):
        if not isinstance(reference, dict) or not isinstance(candidate, dict):
            raise SelfEvolveError("score vector representations must match")
        if repeat_runs != 1:
            raise SelfEvolveError("mapping score vectors require repeat_runs=1")
        if set(reference) != set(candidate):
            raise SelfEvolveError("score metric sets do not match")
        # Named metrics act as independent observations when there is only
        # one repeated run, so their gains also contribute to variance.
        return [candidate[name] - reference[name] for name in sorted(reference)]
    reference_values = _values(reference)
    candidate_values = _values(candidate)
    if len(reference_values) != repeat_runs or len(candidate_values) != repeat_runs:
        raise SelfEvolveError("evaluation score count must equal repeat_runs")
    return [candidate_values[index] - reference_values[index] for index in range(repeat_runs)]


def plan_self_evolve(
    candidate: PromptCandidate,
    baseline_eval: EvalSnapshot,
    holdout_eval: EvalSnapshot,
    *,
    repeat_runs: int = 1,
    min_gain: float = 0.0,
    max_cost: float = 1_000_000.0,
) -> SelfEvolvePlan:
    """Build a pure self-evolve plan after rehydrating every input."""

    try:
        bound_candidate = rehydrate_prompt_candidate(candidate)
        return SelfEvolvePlan(
            candidate=bound_candidate,
            baseline_eval=_bind_snapshot(baseline_eval, bound_candidate, "baseline"),
            holdout_eval=_bind_snapshot(holdout_eval, bound_candidate, "holdout"),
            repeat_runs=repeat_runs,
            min_gain=min_gain,
            max_cost=max_cost,
        )
    except SelfEvolveError:
        raise
    except Exception as exc:
        detail = str(exc) or "validation error"
        raise SelfEvolveError(f"self-evolve inputs were rejected: {detail}") from exc


def evaluate_self_evolve(plan: SelfEvolvePlan) -> SelfEvolveReceipt:
    """Apply offline gates and return ACCEPTED or REJECTED deterministically."""

    try:
        hydrated = rehydrate_self_evolve_plan(plan)
        candidate = rehydrate_prompt_candidate(hydrated.candidate)
        baseline = rehydrate_eval_snapshot(hydrated.baseline_eval)
        holdout = rehydrate_eval_snapshot(hydrated.holdout_eval)
        if baseline.dataset_manifest != holdout.dataset_manifest:
            raise SelfEvolveError("baseline and holdout dataset hashes/manifests do not match")
        baseline_gains = _run_gains(baseline.baseline_scores, holdout.baseline_scores, hydrated.repeat_runs)
        holdout_gains = _run_gains(baseline.holdout_scores, holdout.holdout_scores, hydrated.repeat_runs)
        baseline_gain = mean(baseline_gains)
        holdout_gain = mean(holdout_gains)
        baseline_variance = pvariance(baseline_gains) if len(baseline_gains) > 1 else 0.0
        holdout_variance = pvariance(holdout_gains) if len(holdout_gains) > 1 else 0.0
        gain_variance = max(baseline_variance, holdout_variance)
        total_cost = baseline.cost_units + holdout.cost_units
        checks = [
            (all(gain >= 0.0 for gain in baseline_gains), "baseline regression detected"),
            (holdout_gain >= hydrated.min_gain, "holdout gain is below min_gain"),
            (gain_variance <= MAX_GAIN_VARIANCE, "repeatability variance is unbounded"),
            (total_cost <= hydrated.max_cost, "evaluation cost exceeds max_cost"),
        ]
        accepted = all(result for result, _ in checks)
        reason = "all self-evolve gates passed" if accepted else next(message for result, message in checks if not result)
        return SelfEvolveReceipt(
            status=SelfEvolveStatus.ACCEPTED if accepted else SelfEvolveStatus.REJECTED,
            plan_hash=hydrated.plan_hash or "",
            candidate_hash=candidate.candidate_hash or "",
            parent_commit=candidate.parent_commit,
            rollback_ref=candidate.rollback_ref,
            holdout_gain=holdout_gain,
            baseline_gain=baseline_gain,
            gain_variance=gain_variance,
            total_cost=total_cost,
            reason=reason,
        )
    except SelfEvolveError:
        raise
    except Exception as exc:
        raise SelfEvolveError("self-evolve evaluation failed closed") from exc


# Naming aliases make the boundary consistent with the other planner APIs.
assess_self_evolve = evaluate_self_evolve
plan_evolution = plan_self_evolve
rehydrate_candidate = rehydrate_prompt_candidate
rehydrate_plan = rehydrate_self_evolve_plan
rehydrate_receipt = rehydrate_self_evolve_receipt


__all__ = [
    "EvalSnapshot",
    "MAX_GAIN_VARIANCE",
    "PromptCandidate",
    "SelfEvolveError",
    "SelfEvolvePlan",
    "SelfEvolveReceipt",
    "SelfEvolveStatus",
    "assess_self_evolve",
    "evaluate_self_evolve",
    "plan_evolution",
    "plan_self_evolve",
    "rehydrate_candidate",
    "rehydrate_eval_snapshot",
    "rehydrate_plan",
    "rehydrate_prompt_candidate",
    "rehydrate_receipt",
    "rehydrate_self_evolve_plan",
    "rehydrate_self_evolve_receipt",
]
