"""Pure, deterministic facade for the attribution-to-acceptance pipeline.

The facade composes already captured Phase 4A attribution evidence and the
Phase 4B acceptance decision.  It deliberately has no execution boundary:
there are no model, repository, process, container, filesystem, socket, or
environment integrations here.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.acceptance import (
    AcceptanceError,
    CandidateAcceptancePlan,
    CandidateAcceptanceReceipt,
    rehydrate_candidate_acceptance_plan,
    rehydrate_candidate_acceptance_receipt,
)
from src.attribution import (
    AttributionHypothesis,
    BaselineObservation,
    CandidateChangeEvidence,
    rehydrate_baseline,
    rehydrate_candidate,
    rehydrate_hypothesis,
)


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class PipelineError(ValueError):
    """Raised when the pipeline cannot prove a hash-bound successful input."""


class _PipelineContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class PipelineInput(_PipelineContract):
    """A snapshot of all contracts consumed by the pure pipeline facade."""

    schema_: Literal["meta-improver-pipeline-input/v1"] = Field(
        default="meta-improver-pipeline-input/v1",
        alias="schema",
        serialization_alias="schema",
    )
    baseline: BaselineObservation
    hypothesis: AttributionHypothesis
    candidate: CandidateChangeEvidence
    acceptance_plan: CandidateAcceptancePlan
    acceptance_receipt: CandidateAcceptanceReceipt
    input_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def derive_hash(self) -> PipelineInput:
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"input_hash"})
        )
        if self.input_hash is not None and self.input_hash != expected:
            raise ValueError("pipeline input_hash does not match canonical contents")
        object.__setattr__(self, "input_hash", expected)
        return self


class PipelinePlan(_PipelineContract):
    """Hash-bound, execution-free plan for a complete improvement attempt."""

    schema_: Literal["meta-improver-pipeline-plan/v1"] = Field(
        default="meta-improver-pipeline-plan/v1",
        alias="schema",
        serialization_alias="schema",
    )
    pipeline_input: PipelineInput = Field(validation_alias="pipeline_input")
    baseline_hash: str = Field(pattern=_HASH)
    hypothesis_hash: str = Field(pattern=_HASH)
    candidate_hash: str = Field(pattern=_HASH)
    acceptance_plan_hash: str = Field(pattern=_HASH)
    acceptance_receipt_hash: str = Field(pattern=_HASH)
    plan_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> PipelinePlan:
        value = self.pipeline_input
        if value.baseline.observation_hash != self.baseline_hash:
            raise ValueError("pipeline baseline binding does not match")
        if value.hypothesis.hypothesis_hash != self.hypothesis_hash:
            raise ValueError("pipeline hypothesis binding does not match")
        if value.candidate.evidence_hash != self.candidate_hash:
            raise ValueError("pipeline candidate binding does not match")
        if value.acceptance_plan.plan_hash != self.acceptance_plan_hash:
            raise ValueError("pipeline acceptance plan binding does not match")
        if value.acceptance_receipt.receipt_hash != self.acceptance_receipt_hash:
            raise ValueError("pipeline acceptance receipt binding does not match")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"plan_hash"}))
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("pipeline plan_hash does not match canonical contents")
        object.__setattr__(self, "plan_hash", expected)
        return self


class PipelineStatus(StrEnum):
    SUCCEEDED = "succeeded"


class PipelineReceipt(_PipelineContract):
    """The only successful output of the facade, bound to every input hash."""

    schema_: Literal["meta-improver-pipeline-receipt/v1"] = Field(
        default="meta-improver-pipeline-receipt/v1",
        alias="schema",
        serialization_alias="schema",
    )
    pipeline_plan: PipelinePlan
    status: PipelineStatus = PipelineStatus.SUCCEEDED
    acceptance_receipt_hash: str = Field(pattern=_HASH)
    reason: str = Field(default="acceptance receipt proven", min_length=1, max_length=2_000)
    receipt_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> PipelineReceipt:
        if self.status is not PipelineStatus.SUCCEEDED:
            raise ValueError("pipeline receipt must be succeeded")
        if self.acceptance_receipt_hash != self.pipeline_plan.acceptance_receipt_hash:
            raise ValueError("pipeline receipt is not bound to acceptance receipt")
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
        )
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("pipeline receipt_hash does not match canonical contents")
        object.__setattr__(self, "receipt_hash", expected)
        return self


def _rehydrate_input(value: PipelineInput) -> PipelineInput:
    if not isinstance(value, PipelineInput) or value.input_hash is None:
        raise PipelineError("cannot use an unhashed pipeline input")
    existing = value.input_hash
    canonical = _canonical(value.model_dump(mode="json", by_alias=True))
    try:
        hydrated = PipelineInput.model_validate(json.loads(canonical))
    except Exception as exc:
        raise PipelineError("pipeline input failed integrity rehydration") from exc
    if (
        hydrated.input_hash != existing
        or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical
    ):
        raise PipelineError("pipeline input hash does not match canonical contents")
    return hydrated


def rehydrate_pipeline_input(value: PipelineInput) -> PipelineInput:
    """Round-trip a pipeline input and verify its existing hash."""

    return _rehydrate_input(value)


def rehydrate_pipeline_plan(value: PipelinePlan) -> PipelinePlan:
    """Round-trip a plan and verify all nested and derived hashes."""

    if not isinstance(value, PipelinePlan) or value.plan_hash is None:
        raise PipelineError("cannot use an unhashed pipeline plan")
    existing = value.plan_hash
    canonical = _canonical(value.model_dump(mode="json", by_alias=True))
    try:
        hydrated = PipelinePlan.model_validate(json.loads(canonical))
    except Exception as exc:
        raise PipelineError("pipeline plan failed integrity rehydration") from exc
    if (
        hydrated.plan_hash != existing
        or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical
    ):
        raise PipelineError("pipeline plan hash does not match canonical contents")
    return hydrated


def rehydrate_pipeline_receipt(value: PipelineReceipt) -> PipelineReceipt:
    """Round-trip a successful receipt and verify its plan and receipt hash."""

    if not isinstance(value, PipelineReceipt) or value.receipt_hash is None:
        raise PipelineError("cannot use an unhashed pipeline receipt")
    existing = value.receipt_hash
    canonical = _canonical(value.model_dump(mode="json", by_alias=True))
    try:
        hydrated = PipelineReceipt.model_validate(json.loads(canonical))
    except Exception as exc:
        raise PipelineError("pipeline receipt failed integrity rehydration") from exc
    if (
        hydrated.receipt_hash != existing
        or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical
    ):
        raise PipelineError("pipeline receipt hash does not match canonical contents")
    return hydrated


def _components_from(
    value: PipelineInput | BaselineObservation,
    hypothesis: AttributionHypothesis | None,
    candidate: CandidateChangeEvidence | None,
    acceptance_plan: CandidateAcceptancePlan | None,
    acceptance_receipt: CandidateAcceptanceReceipt | None,
) -> tuple[
    BaselineObservation,
    AttributionHypothesis,
    CandidateChangeEvidence,
    CandidateAcceptancePlan,
    CandidateAcceptanceReceipt,
    str | None,
]:
    if isinstance(value, PipelineInput):
        if any(item is not None for item in (hypothesis, candidate, acceptance_plan, acceptance_receipt)):
            raise PipelineError("pipeline input cannot be combined with component overrides")
        return (
            value.baseline,
            value.hypothesis,
            value.candidate,
            value.acceptance_plan,
            value.acceptance_receipt,
            value.input_hash,
        )
    if not isinstance(value, BaselineObservation):
        raise PipelineError("pipeline requires a BaselineObservation")
    if any(item is None for item in (hypothesis, candidate, acceptance_plan, acceptance_receipt)):
        raise PipelineError("pipeline requires baseline, hypothesis, candidate, acceptance plan, and receipt")
    return value, hypothesis, candidate, acceptance_plan, acceptance_receipt, None  # type: ignore[return-value]


def _validate_in_order(
    value: PipelineInput | BaselineObservation,
    hypothesis: AttributionHypothesis | None = None,
    candidate: CandidateChangeEvidence | None = None,
    acceptance_plan: CandidateAcceptancePlan | None = None,
    acceptance_receipt: CandidateAcceptanceReceipt | None = None,
) -> PipelineInput:
    """Validate in dependency order, preserving the fail-closed boundary."""

    raw_baseline, raw_hypothesis, raw_candidate, raw_plan, raw_receipt, input_hash = _components_from(
        value, hypothesis, candidate, acceptance_plan, acceptance_receipt
    )

    # Baseline is intentionally the first semantic gate.
    try:
        baseline = rehydrate_baseline(raw_baseline)
    except Exception as exc:
        raise PipelineError("baseline failed integrity validation") from exc
    if baseline.passed is not False:
        raise PipelineError("pipeline requires a failed baseline")

    # Attribution bindings are the second gate and do not trust acceptance yet.
    try:
        hydrated_hypothesis = rehydrate_hypothesis(raw_hypothesis, baseline)
        hydrated_candidate = rehydrate_candidate(
            raw_candidate,
            baseline,
            hydrated_hypothesis,
            raw_candidate.patch_hash,
        )
    except Exception as exc:
        raise PipelineError("hypothesis/candidate bindings failed validation") from exc

    # Acceptance plan and receipt are checked only after attribution is sound.
    try:
        plan = rehydrate_candidate_acceptance_plan(raw_plan)
    except Exception as exc:
        raise PipelineError("acceptance plan failed integrity validation") from exc
    if plan.baseline.observation_hash != baseline.observation_hash:
        raise PipelineError("acceptance plan baseline binding does not match pipeline baseline")
    if plan.candidate.evidence_hash != hydrated_candidate.evidence_hash:
        raise PipelineError("acceptance plan candidate binding does not match pipeline candidate")
    if plan.hypothesis is not None and plan.hypothesis.hypothesis_hash != hydrated_hypothesis.hypothesis_hash:
        raise PipelineError("acceptance plan hypothesis binding does not match pipeline hypothesis")
    try:
        receipt = rehydrate_candidate_acceptance_receipt(raw_receipt)
    except Exception as exc:
        raise PipelineError("acceptance receipt failed integrity validation") from exc
    if receipt.acceptance_plan.plan_hash != plan.plan_hash:
        raise PipelineError("acceptance receipt is not bound to the acceptance plan")

    try:
        normalized = PipelineInput(
            baseline=baseline,
            hypothesis=hydrated_hypothesis,
            candidate=hydrated_candidate,
            acceptance_plan=plan,
            acceptance_receipt=receipt,
        )
    except Exception as exc:
        raise PipelineError("pipeline input composition failed") from exc
    if input_hash is not None and normalized.input_hash != input_hash:
        raise PipelineError("pipeline input hash does not match validated contents")
    return normalized


def plan_pipeline(
    value: PipelineInput | BaselineObservation,
    hypothesis: AttributionHypothesis | None = None,
    candidate: CandidateChangeEvidence | None = None,
    acceptance_plan: CandidateAcceptancePlan | None = None,
    acceptance_receipt: CandidateAcceptanceReceipt | None = None,
) -> PipelinePlan:
    """Validate and compose a deterministic pipeline plan without execution."""

    normalized = _validate_in_order(
        value, hypothesis, candidate, acceptance_plan, acceptance_receipt
    )
    try:
        return PipelinePlan(
            pipeline_input=normalized,
            baseline_hash=normalized.baseline.observation_hash or "",
            hypothesis_hash=normalized.hypothesis.hypothesis_hash or "",
            candidate_hash=normalized.candidate.evidence_hash or "",
            acceptance_plan_hash=normalized.acceptance_plan.plan_hash or "",
            acceptance_receipt_hash=normalized.acceptance_receipt.receipt_hash or "",
        )
    except Exception as exc:
        raise PipelineError("pipeline plan bindings were rejected") from exc


def run_pipeline(
    value: PipelineInput | BaselineObservation | PipelinePlan,
    hypothesis: AttributionHypothesis | None = None,
    candidate: CandidateChangeEvidence | None = None,
    acceptance_plan: CandidateAcceptancePlan | None = None,
    acceptance_receipt: CandidateAcceptanceReceipt | None = None,
) -> PipelineReceipt:
    """Return a receipt only after ordered validation and plan rehydration."""

    if isinstance(value, PipelinePlan):
        if value.plan_hash is None:
            raise PipelineError("cannot use an unhashed pipeline plan")
        # A plan is still a snapshot; re-run all semantic gates before issuing
        # the receipt so mutating a nested object cannot bypass ordering.
        normalized = _validate_in_order(value.pipeline_input)
        if normalized.input_hash != value.pipeline_input.input_hash:
            raise PipelineError("pipeline plan input changed after planning")
        plan = rehydrate_pipeline_plan(value)
        plan = rehydrate_pipeline_plan(
            PipelinePlan(
                pipeline_input=normalized,
                baseline_hash=plan.baseline_hash,
                hypothesis_hash=plan.hypothesis_hash,
                candidate_hash=plan.candidate_hash,
                acceptance_plan_hash=plan.acceptance_plan_hash,
                acceptance_receipt_hash=plan.acceptance_receipt_hash,
            )
        )
    else:
        plan = plan_pipeline(value, hypothesis, candidate, acceptance_plan, acceptance_receipt)
    try:
        return PipelineReceipt(
            pipeline_plan=plan,
            status=PipelineStatus.SUCCEEDED,
            acceptance_receipt_hash=plan.acceptance_receipt_hash,
        )
    except Exception as exc:
        raise PipelineError("pipeline receipt was rejected") from exc


replay_pipeline = run_pipeline
pipeline = run_pipeline
rehydrate_pipeline_plan_hash = rehydrate_pipeline_plan
rehydrate_pipeline_receipt_hash = rehydrate_pipeline_receipt


class PipelineFacade:
    """Object facade useful for deterministic fixture replay callers."""

    def plan(self, *args: Any, **kwargs: Any) -> PipelinePlan:
        return plan_pipeline(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> PipelineReceipt:
        return run_pipeline(*args, **kwargs)

    replay = run


__all__ = [
    "PipelineError",
    "PipelineFacade",
    "PipelineInput",
    "PipelinePlan",
    "PipelineReceipt",
    "PipelineStatus",
    "pipeline",
    "plan_pipeline",
    "replay_pipeline",
    "rehydrate_pipeline_input",
    "rehydrate_pipeline_plan",
    "rehydrate_pipeline_plan_hash",
    "rehydrate_pipeline_receipt",
    "rehydrate_pipeline_receipt_hash",
    "run_pipeline",
]
