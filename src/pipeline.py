"""Deterministic facade for the attribution-to-acceptance pipeline.

The facade composes already captured Phase 4A attribution evidence and the
Phase 4B acceptance decision.  It has no model, repository, process, container,
socket, or environment execution boundary. Callers may explicitly supply the
local experience ledger to persist the otherwise pure validation lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
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
from src.models import (
    Attempt,
    AttemptStage,
    AttemptStatus,
    InputSnapshot,
    TestEvidence,
)
from src.models.contracts import TestStatus, utc_now
from src.storage.ledger_db import Ledger, LedgerConflictError
from src.target_publication import TargetPublicationReceipt, TargetPublicationStatus, rehydrate_target_publication


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class PipelineError(ValueError):
    """Raised when the pipeline cannot prove a hash-bound successful input."""

    def __init__(self, message: str, *, stage: AttemptStage = AttemptStage.CAPTURED) -> None:
        super().__init__(message)
        self.stage = stage


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
    publication_receipt: TargetPublicationReceipt | None = None
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
    publication_receipt_hash: str | None = Field(default=None, pattern=_HASH)
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
        if (value.publication_receipt.receipt_hash if value.publication_receipt is not None else None) != self.publication_receipt_hash:
            raise ValueError("pipeline publication receipt binding does not match")
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
    publication_receipt_hash: str | None = Field(default=None, pattern=_HASH)
    reason: str = Field(default="acceptance receipt proven", min_length=1, max_length=2_000)
    receipt_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> PipelineReceipt:
        if self.status is not PipelineStatus.SUCCEEDED:
            raise ValueError("pipeline receipt must be succeeded")
        if self.acceptance_receipt_hash != self.pipeline_plan.acceptance_receipt_hash:
            raise ValueError("pipeline receipt is not bound to acceptance receipt")
        if self.publication_receipt_hash != self.pipeline_plan.publication_receipt_hash:
            raise ValueError("pipeline receipt is not bound to publication receipt")
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
        )
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("pipeline receipt_hash does not match canonical contents")
        object.__setattr__(self, "receipt_hash", expected)
        return self


_StageCallback = Callable[[AttemptStage, dict[str, Any]], None]


def _validation_test_evidence(value: PipelineInput) -> list[TestEvidence]:
    gate = value.acceptance_plan.validation_gate
    observed_at = value.baseline.observed_at
    evidence = [
        TestEvidence(
            command=command,
            status=TestStatus.PASSED,
            output=gate.targeted_evidence,
            ran_at=observed_at,
        )
        for command in value.candidate.targeted_tests
    ]
    evidence.extend(
        TestEvidence(
            command=command,
            status=TestStatus.PASSED,
            output=gate.regression_evidence,
            ran_at=observed_at,
        )
        for command in value.candidate.regression_tests
    )
    return evidence


class _AttemptRecorder:
    def __init__(self, ledger: Ledger, attempt: Attempt) -> None:
        self.ledger = ledger
        self.attempt = attempt

    def advance(self, stage: AttemptStage, changes: dict[str, Any]) -> None:
        if self.attempt.status in {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.REJECTED,
        }:
            return
        current_stage = list(AttemptStage).index(self.attempt.stage)
        target_stage = list(AttemptStage).index(stage)
        if target_stage <= current_stage:
            if any(getattr(self.attempt, key) != value for key, value in changes.items()):
                raise LedgerConflictError(f"attempt stage {stage.value} has conflicting evidence")
            return
        if target_stage != current_stage + 1:
            raise LedgerConflictError(
                f"attempt stage cannot skip from {self.attempt.stage.value} to {stage.value}"
            )
        updated = Attempt.model_validate(
            {
                **self.attempt.model_dump(),
                **changes,
                "status": AttemptStatus.RUNNING,
                "stage": stage,
                "updated_at": utc_now(),
            }
        )
        self.attempt = self.ledger.transition_attempt(updated)

    def reject(self, stage: AttemptStage, reason: str) -> None:
        if self.attempt.status is AttemptStatus.REJECTED:
            if self.attempt.stage is stage and self.attempt.failure_reason == reason:
                return
            raise LedgerConflictError("replayed rejected attempt has conflicting outcome")
        if self.attempt.status in {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED}:
            raise LedgerConflictError("attempt already has a different terminal outcome")
        # A rejected snapshot names the last stage with complete evidence. The
        # PipelineError stage identifies the gate that rejected the input.
        stage = self.attempt.stage
        updated = Attempt.model_validate(
            {
                **self.attempt.model_dump(),
                "status": AttemptStatus.REJECTED,
                "stage": stage,
                "failure_reason": reason,
                "updated_at": utc_now(),
            }
        )
        self.attempt = self.ledger.transition_attempt(updated)

    def succeed(self, receipt: PipelineReceipt) -> None:
        changes = {"pipeline_receipt_hash": receipt.receipt_hash}
        if self.attempt.status is AttemptStatus.SUCCEEDED:
            if self.attempt.stage is AttemptStage.COMPLETED and all(
                getattr(self.attempt, key) == value for key, value in changes.items()
            ):
                return
            raise LedgerConflictError("replayed succeeded attempt has conflicting outcome")
        if self.attempt.status in {AttemptStatus.FAILED, AttemptStatus.REJECTED}:
            raise LedgerConflictError("attempt already has a different terminal outcome")
        updated = Attempt.model_validate(
            {
                **self.attempt.model_dump(),
                **changes,
                "status": AttemptStatus.SUCCEEDED,
                "stage": AttemptStage.COMPLETED,
                "updated_at": utc_now(),
            }
        )
        self.attempt = self.ledger.transition_attempt(updated)


def _attempt_components(
    value: PipelineInput | BaselineObservation | PipelinePlan,
    hypothesis: AttributionHypothesis | None,
) -> tuple[BaselineObservation, AttributionHypothesis]:
    if isinstance(value, PipelinePlan):
        return value.pipeline_input.baseline, value.pipeline_input.hypothesis
    if isinstance(value, PipelineInput):
        return value.baseline, value.hypothesis
    if not isinstance(value, BaselineObservation) or not isinstance(hypothesis, AttributionHypothesis):
        raise PipelineError("persisted pipeline requires baseline and hypothesis identities")
    return value, hypothesis


def _prepare_attempt_recorder(
    value: PipelineInput | BaselineObservation | PipelinePlan,
    hypothesis: AttributionHypothesis | None,
    *,
    ledger: Ledger | None,
    input_snapshot: InputSnapshot | None,
    strategy_version: str | None,
) -> _AttemptRecorder | None:
    supplied = (ledger is not None, input_snapshot is not None, strategy_version is not None)
    if not any(supplied):
        return None
    if not all(supplied):
        raise PipelineError("ledger, input_snapshot, and strategy_version must be supplied together")
    assert ledger is not None and input_snapshot is not None and strategy_version is not None
    baseline, diagnosis = _attempt_components(value, hypothesis)
    key = _digest(
        {
            "signal": baseline.signal_signature,
            "base_commit": baseline.base_commit,
            "strategy_version": strategy_version,
        }
    )
    initial = Attempt(
        attempt_id=baseline.attempt_id,
        idempotency_key=key,
        signal=baseline.signal_signature,
        base_commit=baseline.base_commit,
        strategy_version=strategy_version,
        input_snapshot=input_snapshot,
        model_version=diagnosis.model_version,
        prompt_version=diagnosis.prompt_version,
        created_at=input_snapshot.captured_at,
        updated_at=input_snapshot.captured_at,
    )
    existing = ledger.find_by_idempotency_key(key)
    if existing is None:
        existing = ledger.record_attempt(initial)
    immutable = (
        "attempt_id",
        "idempotency_key",
        "signal",
        "base_commit",
        "strategy_version",
        "input_snapshot",
        "model_version",
        "prompt_version",
        "created_at",
    )
    if any(getattr(existing, field) != getattr(initial, field) for field in immutable):
        raise LedgerConflictError("idempotency key belongs to a different pipeline attempt")
    return _AttemptRecorder(ledger, existing)


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
    publication_receipt: TargetPublicationReceipt | None,
) -> tuple[
    BaselineObservation,
    AttributionHypothesis,
    CandidateChangeEvidence,
    CandidateAcceptancePlan,
    CandidateAcceptanceReceipt,
    TargetPublicationReceipt | None,
    str | None,
]:
    if isinstance(value, PipelineInput):
        if any(item is not None for item in (hypothesis, candidate, acceptance_plan, acceptance_receipt, publication_receipt)):
            raise PipelineError("pipeline input cannot be combined with component overrides")
        return (
            value.baseline,
            value.hypothesis,
            value.candidate,
            value.acceptance_plan,
            value.acceptance_receipt,
            value.publication_receipt,
            value.input_hash,
        )
    if not isinstance(value, BaselineObservation):
        raise PipelineError("pipeline requires a BaselineObservation")
    if any(item is None for item in (hypothesis, candidate, acceptance_plan, acceptance_receipt)):
        raise PipelineError("pipeline requires baseline, hypothesis, candidate, acceptance plan, and receipt")
    return value, hypothesis, candidate, acceptance_plan, acceptance_receipt, publication_receipt, None  # type: ignore[return-value]


def _validate_in_order(
    value: PipelineInput | BaselineObservation,
    hypothesis: AttributionHypothesis | None = None,
    candidate: CandidateChangeEvidence | None = None,
    acceptance_plan: CandidateAcceptancePlan | None = None,
    acceptance_receipt: CandidateAcceptanceReceipt | None = None,
    publication_receipt: TargetPublicationReceipt | None = None,
    *,
    stage_callback: _StageCallback | None = None,
) -> PipelineInput:
    """Validate in dependency order, preserving the fail-closed boundary."""

    raw_baseline, raw_hypothesis, raw_candidate, raw_plan, raw_receipt, raw_publication, input_hash = _components_from(
        value, hypothesis, candidate, acceptance_plan, acceptance_receipt, publication_receipt
    )

    # Baseline is intentionally the first semantic gate.
    try:
        baseline = rehydrate_baseline(raw_baseline)
    except Exception as exc:
        raise PipelineError(
            "baseline failed integrity validation",
            stage=AttemptStage.BASELINE_EVALUATED,
        ) from exc
    if stage_callback is not None:
        stage_callback(
            AttemptStage.BASELINE_EVALUATED,
            {"baseline_hash": baseline.observation_hash},
        )
    if baseline.passed is not False:
        raise PipelineError(
            "pipeline requires a failed baseline",
            stage=AttemptStage.BASELINE_EVALUATED,
        )

    # Attribution bindings are the second gate and do not trust acceptance yet.
    try:
        hydrated_hypothesis = rehydrate_hypothesis(raw_hypothesis, baseline)
    except Exception as exc:
        raise PipelineError(
            "hypothesis/candidate bindings failed validation",
            stage=AttemptStage.DIAGNOSED,
        ) from exc
    if stage_callback is not None:
        stage_callback(
            AttemptStage.DIAGNOSED,
            {"diagnosis_hash": hydrated_hypothesis.hypothesis_hash},
        )
    try:
        hydrated_candidate = rehydrate_candidate(
            raw_candidate,
            baseline,
            hydrated_hypothesis,
            raw_candidate.patch_hash,
        )
    except Exception as exc:
        raise PipelineError(
            "hypothesis/candidate bindings failed validation",
            stage=AttemptStage.PATCH_VALIDATED,
        ) from exc
    if stage_callback is not None:
        stage_callback(
            AttemptStage.PATCH_VALIDATED,
            {"patch_hash": hydrated_candidate.patch_hash},
        )

    # Acceptance plan and receipt are checked only after attribution is sound.
    try:
        plan = rehydrate_candidate_acceptance_plan(raw_plan)
    except Exception as exc:
        raise PipelineError(
            "acceptance plan failed integrity validation",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        ) from exc
    if plan.baseline.observation_hash != baseline.observation_hash:
        raise PipelineError(
            "acceptance plan baseline binding does not match pipeline baseline",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        )
    if plan.candidate.evidence_hash != hydrated_candidate.evidence_hash:
        raise PipelineError(
            "acceptance plan candidate binding does not match pipeline candidate",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        )
    if plan.hypothesis is not None and plan.hypothesis.hypothesis_hash != hydrated_hypothesis.hypothesis_hash:
        raise PipelineError(
            "acceptance plan hypothesis binding does not match pipeline hypothesis",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        )
    try:
        receipt = rehydrate_candidate_acceptance_receipt(raw_receipt)
    except Exception as exc:
        raise PipelineError(
            "acceptance receipt failed integrity validation",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        ) from exc
    if receipt.acceptance_plan.plan_hash != plan.plan_hash:
        raise PipelineError(
            "acceptance receipt is not bound to the acceptance plan",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        )

    publication = None
    if raw_publication is not None:
        try:
            publication = rehydrate_target_publication(raw_publication)
        except Exception as exc:
            raise PipelineError(
                "publication receipt failed integrity validation",
                stage=AttemptStage.ACCEPTANCE_EVALUATED,
            ) from exc
        if publication.status is not TargetPublicationStatus.SUCCEEDED:
            raise PipelineError(
                "pipeline cannot consume a non-successful publication receipt",
                stage=AttemptStage.ACCEPTANCE_EVALUATED,
            )
        if publication.acceptance_plan_hash != plan.plan_hash:
            raise PipelineError(
                "publication receipt is not bound to the acceptance plan",
                stage=AttemptStage.ACCEPTANCE_EVALUATED,
            )

    try:
        normalized = PipelineInput(
            baseline=baseline,
            hypothesis=hydrated_hypothesis,
            candidate=hydrated_candidate,
            acceptance_plan=plan,
            acceptance_receipt=receipt,
            publication_receipt=publication,
        )
    except Exception as exc:
        raise PipelineError(
            "pipeline input composition failed",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        ) from exc
    if input_hash is not None and normalized.input_hash != input_hash:
        raise PipelineError(
            "pipeline input hash does not match validated contents",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        )
    if stage_callback is not None:
        stage_callback(
            AttemptStage.ACCEPTANCE_EVALUATED,
            {
                "acceptance_receipt_hash": normalized.acceptance_receipt.receipt_hash,
                "test_evidence": _validation_test_evidence(normalized),
            },
        )
    return normalized


def _build_pipeline_plan(
    value: PipelineInput | BaselineObservation,
    hypothesis: AttributionHypothesis | None = None,
    candidate: CandidateChangeEvidence | None = None,
    acceptance_plan: CandidateAcceptancePlan | None = None,
    acceptance_receipt: CandidateAcceptanceReceipt | None = None,
    publication_receipt: TargetPublicationReceipt | None = None,
    *,
    stage_callback: _StageCallback | None = None,
) -> PipelinePlan:
    normalized = _validate_in_order(
        value,
        hypothesis,
        candidate,
        acceptance_plan,
        acceptance_receipt,
        publication_receipt,
        stage_callback=stage_callback,
    )
    try:
        return PipelinePlan(
            pipeline_input=normalized,
            baseline_hash=normalized.baseline.observation_hash or "",
            hypothesis_hash=normalized.hypothesis.hypothesis_hash or "",
            candidate_hash=normalized.candidate.evidence_hash or "",
            acceptance_plan_hash=normalized.acceptance_plan.plan_hash or "",
            acceptance_receipt_hash=normalized.acceptance_receipt.receipt_hash or "",
            publication_receipt_hash=(normalized.publication_receipt.receipt_hash if normalized.publication_receipt else None),
        )
    except Exception as exc:
        raise PipelineError(
            "pipeline plan bindings were rejected",
            stage=AttemptStage.ACCEPTANCE_EVALUATED,
        ) from exc


def plan_pipeline(
    value: PipelineInput | BaselineObservation,
    hypothesis: AttributionHypothesis | None = None,
    candidate: CandidateChangeEvidence | None = None,
    acceptance_plan: CandidateAcceptancePlan | None = None,
    acceptance_receipt: CandidateAcceptanceReceipt | None = None,
    publication_receipt: TargetPublicationReceipt | None = None,
) -> PipelinePlan:
    """Validate and compose a deterministic pipeline plan without execution."""

    return _build_pipeline_plan(
        value, hypothesis, candidate, acceptance_plan, acceptance_receipt, publication_receipt
    )


def run_pipeline(
    value: PipelineInput | BaselineObservation | PipelinePlan,
    hypothesis: AttributionHypothesis | None = None,
    candidate: CandidateChangeEvidence | None = None,
    acceptance_plan: CandidateAcceptancePlan | None = None,
    acceptance_receipt: CandidateAcceptanceReceipt | None = None,
    publication_receipt: TargetPublicationReceipt | None = None,
    *,
    ledger: Ledger | None = None,
    input_snapshot: InputSnapshot | None = None,
    strategy_version: str | None = None,
) -> PipelineReceipt:
    """Validate in order and optionally persist one replayable attempt lifecycle."""

    recorder = _prepare_attempt_recorder(
        value,
        hypothesis,
        ledger=ledger,
        input_snapshot=input_snapshot,
        strategy_version=strategy_version,
    )
    try:
        if isinstance(value, PipelinePlan):
            if value.plan_hash is None:
                raise PipelineError("cannot use an unhashed pipeline plan")
            # A plan is still a snapshot; re-run all semantic gates before issuing
            # the receipt so mutating a nested object cannot bypass ordering.
            normalized = _validate_in_order(
                value.pipeline_input,
                stage_callback=recorder.advance if recorder is not None else None,
            )
            if normalized.input_hash != value.pipeline_input.input_hash:
                raise PipelineError(
                    "pipeline plan input changed after planning",
                    stage=AttemptStage.ACCEPTANCE_EVALUATED,
                )
            plan = rehydrate_pipeline_plan(value)
            plan = rehydrate_pipeline_plan(
                PipelinePlan(
                    pipeline_input=normalized,
                    baseline_hash=plan.baseline_hash,
                    hypothesis_hash=plan.hypothesis_hash,
                    candidate_hash=plan.candidate_hash,
                    acceptance_plan_hash=plan.acceptance_plan_hash,
                    acceptance_receipt_hash=plan.acceptance_receipt_hash,
                    publication_receipt_hash=plan.publication_receipt_hash,
                )
            )
        else:
            plan = _build_pipeline_plan(
                value,
                hypothesis,
                candidate,
                acceptance_plan,
                acceptance_receipt,
                publication_receipt,
                stage_callback=recorder.advance if recorder is not None else None,
            )
        try:
            receipt = PipelineReceipt(
                pipeline_plan=plan,
                status=PipelineStatus.SUCCEEDED,
                acceptance_receipt_hash=plan.acceptance_receipt_hash,
                publication_receipt_hash=plan.publication_receipt_hash,
            )
        except Exception as exc:
            raise PipelineError(
                "pipeline receipt was rejected",
                stage=AttemptStage.COMPLETED,
            ) from exc
    except PipelineError as exc:
        if recorder is not None:
            recorder.reject(exc.stage, str(exc))
        raise
    if recorder is not None:
        recorder.succeed(receipt)
    return receipt


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
