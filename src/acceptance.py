"""Pure, hash-bound acceptance gates for candidate changes.

This module is deliberately an execution-free protocol boundary.  It consumes
already captured Phase 4A evidence and gate evidence, then produces a
deterministic acceptance receipt.  It does not call a model, run tests, touch a
repository, start a process, or inspect the host environment.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator
from pydantic import BaseModel

from src.attribution import (
    AttributionHypothesis,
    BaselineObservation,
    CandidateChangeEvidence,
    rehydrate_baseline,
    rehydrate_candidate,
    rehydrate_hypothesis,
)


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET = re.compile(
    r"(?:\bbearer\s+[A-Za-z0-9._~+/=-]+|(?<![A-Za-z0-9])"
    r"(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]+|(?<![A-Za-z0-9])"
    r"(?:password|passwd|token|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE,
)
_SHELL = re.compile(r"[;&|`$<>\r\n\x00]")

# These are protocol bounds, rather than claims about a particular language
# or test suite.  A caller may report negative complexity deltas, but no
# unbounded metric or cost can reach the acceptance decision.
MAX_COMPLEXITY_DELTA = 1_000.0
MAX_COST_UNITS = 1_000_000.0
MAX_TEST_COUNT = 100_000
MAX_TEXT = 20_000

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _safe_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _safe_branch_identifier(value: str, field_name: str) -> str:
    _safe_identifier(value, field_name)
    if ".." in value or value.endswith(".") or value.casefold().endswith(".lock") or value.startswith(".") or "@{" in value:
        raise ValueError(f"{field_name} is unsafe to derive a Git ref")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_text(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or _SECRET.search(value)
        or _SHELL.search(value)
    ):
        raise ValueError(f"{field_name} contains unsafe or secret-shaped text")
    return value


class AcceptanceError(ValueError):
    """Raised whenever a candidate cannot be proven acceptable."""


class _AcceptanceContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class BaselineGateStatus(StrEnum):
    """The only baseline state that permits a candidate acceptance attempt."""

    FAILED = "failed"


class CandidateAcceptanceStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


# Short names are useful to callers that use the gate as a generic protocol.
AcceptanceStatus = CandidateAcceptanceStatus
BaselineStatus = BaselineGateStatus


class BaselineGateEvidence(_AcceptanceContract):
    """Evidence that the exact baseline reproduced the failure signal."""

    schema_: Literal["baseline-gate-evidence/v1"] = Field(
        default="baseline-gate-evidence/v1", alias="schema", serialization_alias="schema"
    )
    baseline_hash: str = Field(pattern=_HASH)
    status: BaselineGateStatus = BaselineGateStatus.FAILED
    reproduction_command: str = Field(
        min_length=1,
        max_length=MAX_TEXT,
        validation_alias=AliasChoices("reproduction_command", "command"),
    )
    reproduction_evidence: str = Field(
        min_length=1,
        max_length=MAX_TEXT,
        validation_alias=AliasChoices(
            "reproduction_evidence", "reproduction_output", "evidence", "output"
        ),
    )
    evidence_hash: str | None = Field(
        default=None,
        pattern=_HASH,
        validation_alias=AliasChoices("evidence_hash", "gate_hash", "hash"),
    )

    @field_validator("reproduction_command", "reproduction_evidence")
    @classmethod
    def safe_reproduction_text(cls, value: str, info: Any) -> str:
        return _safe_text(value, info.field_name)

    @model_validator(mode="after")
    def derive_hash(self) -> BaselineGateEvidence:
        if self.status is not BaselineGateStatus.FAILED:
            raise ValueError("baseline gate must prove a failed baseline")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"evidence_hash"}))
        if self.evidence_hash is not None and self.evidence_hash != expected:
            raise ValueError("baseline gate evidence_hash does not match canonical contents")
        object.__setattr__(self, "evidence_hash", expected)
        return self

    @property
    def gate_hash(self) -> str:
        return self.evidence_hash or ""


class ValidationGateEvidence(_AcceptanceContract):
    """Evidence that targeted and full regression validation both passed."""

    schema_: Literal["validation-gate-evidence/v1"] = Field(
        default="validation-gate-evidence/v1", alias="schema", serialization_alias="schema"
    )
    candidate_hash: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("candidate_hash", "candidate_evidence_hash"),
    )
    hypothesis_hash: str = Field(pattern=_HASH)
    patch_hash: str = Field(pattern=_HASH)
    targeted_passed: Literal[True] = True
    regression_passed: Literal[True] = True
    targeted_test_count: int = Field(
        ge=1,
        le=MAX_TEST_COUNT,
        validation_alias=AliasChoices("targeted_test_count", "targeted_count", "targeted_tests_count"),
    )
    regression_test_count: int = Field(
        ge=1,
        le=MAX_TEST_COUNT,
        validation_alias=AliasChoices(
            "regression_test_count", "regression_count", "regression_tests_count", "full_test_count"
        ),
    )
    targeted_evidence: str = Field(
        min_length=1,
        max_length=MAX_TEXT,
        validation_alias=AliasChoices("targeted_evidence", "targeted_test_evidence", "targeted_output"),
    )
    regression_evidence: str = Field(
        min_length=1,
        max_length=MAX_TEXT,
        validation_alias=AliasChoices(
            "regression_evidence", "regression_test_evidence", "regression_output", "full_evidence"
        ),
    )
    evidence_hash: str | None = Field(
        default=None,
        pattern=_HASH,
        validation_alias=AliasChoices("evidence_hash", "gate_hash", "hash"),
    )

    @field_validator("targeted_evidence", "regression_evidence")
    @classmethod
    def safe_validation_text(cls, value: str, info: Any) -> str:
        return _safe_text(value, info.field_name)

    @model_validator(mode="after")
    def derive_hash(self) -> ValidationGateEvidence:
        if self.targeted_passed is not True or self.regression_passed is not True:
            raise ValueError("targeted and regression validation must pass")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"evidence_hash"}))
        if self.evidence_hash is not None and self.evidence_hash != expected:
            raise ValueError("validation gate evidence_hash does not match canonical contents")
        object.__setattr__(self, "evidence_hash", expected)
        return self

    @property
    def gate_hash(self) -> str:
        return self.evidence_hash or ""


class QualityGateEvidence(_AcceptanceContract):
    """Bounded quality and security evidence for a candidate change."""

    schema_: Literal["quality-gate-evidence/v1"] = Field(
        default="quality-gate-evidence/v1", alias="schema", serialization_alias="schema"
    )
    complexity_delta: float = Field(ge=-MAX_COMPLEXITY_DELTA, le=MAX_COMPLEXITY_DELTA, allow_inf_nan=False)
    cost_units: float = Field(ge=0, le=MAX_COST_UNITS, allow_inf_nan=False)
    security_passed: Literal[True] = True
    quality_evidence: str = Field(
        min_length=1,
        max_length=MAX_TEXT,
        validation_alias=AliasChoices("quality_evidence", "evidence", "security_evidence", "output"),
    )
    evidence_hash: str | None = Field(
        default=None,
        pattern=_HASH,
        validation_alias=AliasChoices("evidence_hash", "gate_hash", "hash"),
    )

    @field_validator("quality_evidence")
    @classmethod
    def safe_quality_text(cls, value: str) -> str:
        return _safe_text(value, "quality_evidence")

    @model_validator(mode="after")
    def derive_hash(self) -> QualityGateEvidence:
        if self.security_passed is not True:
            raise ValueError("quality gate security check must pass")
        if not math.isfinite(self.complexity_delta) or not math.isfinite(self.cost_units):
            raise ValueError("quality gate metrics must be finite")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"evidence_hash"}))
        if self.evidence_hash is not None and self.evidence_hash != expected:
            raise ValueError("quality gate evidence_hash does not match canonical contents")
        object.__setattr__(self, "evidence_hash", expected)
        return self

    @property
    def gate_hash(self) -> str:
        return self.evidence_hash or ""


def _rehydrate_gate(value: Any, model_type: type[_AcceptanceContract], label: str) -> Any:
    if not isinstance(value, model_type):
        raise AcceptanceError(f"{label} has an unknown contract type")
    existing_hash = getattr(value, "evidence_hash", None)
    if not existing_hash:
        raise AcceptanceError(f"{label} is unhashed")
    try:
        canonical = _canonical(value.model_dump(mode="json", by_alias=True))
        hydrated = model_type.model_validate(json.loads(canonical))
    except Exception as exc:
        raise AcceptanceError(f"{label} failed integrity rehydration") from exc
    if (
        getattr(hydrated, "evidence_hash", None) != existing_hash
        or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical
    ):
        raise AcceptanceError(f"{label} hash does not match canonical contents")
    return hydrated


def _rehydrate_candidate_for_plan(
    candidate: CandidateChangeEvidence,
    baseline: BaselineObservation,
    hypothesis: AttributionHypothesis | None,
) -> CandidateChangeEvidence:
    """Rehydrate candidate evidence, using the strongest available binding."""

    try:
        if hypothesis is not None:
            return rehydrate_candidate(candidate, baseline, hypothesis, candidate.patch_hash)
        canonical = _canonical(candidate.model_dump(mode="json"))
        hydrated = CandidateChangeEvidence.model_validate(json.loads(canonical))
        if (
            hydrated.evidence_hash != candidate.evidence_hash
            or _canonical(hydrated.model_dump(mode="json")) != canonical
            or hydrated.baseline_hash != baseline.observation_hash
        ):
            raise ValueError("candidate evidence binding does not match baseline")
        return hydrated
    except Exception as exc:
        raise AcceptanceError("candidate attribution evidence failed integrity rehydration") from exc


def _rehydrate_hypothesis_if_present(
    hypothesis: AttributionHypothesis | None, baseline: BaselineObservation
) -> AttributionHypothesis | None:
    if hypothesis is None:
        return None
    try:
        return rehydrate_hypothesis(hypothesis, baseline)
    except Exception as exc:
        raise AcceptanceError("attribution hypothesis failed integrity rehydration") from exc


def _rehydrate_plan_components(
    baseline: BaselineObservation,
    candidate: CandidateChangeEvidence,
    hypothesis: AttributionHypothesis | None,
    baseline_gate: BaselineGateEvidence,
    validation_gate: ValidationGateEvidence,
    quality_gate: QualityGateEvidence,
) -> tuple[
    BaselineObservation,
    CandidateChangeEvidence,
    AttributionHypothesis | None,
    BaselineGateEvidence,
    ValidationGateEvidence,
    QualityGateEvidence,
]:
    """Rehydrate each input separately so failures retain their boundary."""

    try:
        hydrated_baseline = rehydrate_baseline(baseline)
    except Exception as exc:
        raise AcceptanceError("baseline evidence failed integrity rehydration") from exc
    if hydrated_baseline.passed is not False:
        raise AcceptanceError("acceptance requires a failed baseline observation")

    hydrated_hypothesis = _rehydrate_hypothesis_if_present(hypothesis, hydrated_baseline)
    hydrated_candidate = _rehydrate_candidate_for_plan(
        candidate, hydrated_baseline, hydrated_hypothesis
    )
    try:
        hydrated_baseline_gate = _rehydrate_gate(
            baseline_gate, BaselineGateEvidence, "baseline gate"
        )
    except AcceptanceError:
        raise
    try:
        hydrated_validation_gate = _rehydrate_gate(
            validation_gate, ValidationGateEvidence, "validation gate"
        )
    except AcceptanceError:
        raise
    try:
        hydrated_quality_gate = _rehydrate_gate(
            quality_gate, QualityGateEvidence, "quality gate"
        )
    except AcceptanceError:
        raise

    if hydrated_baseline_gate.baseline_hash != hydrated_baseline.observation_hash:
        raise AcceptanceError(
            "candidate acceptance bindings: baseline gate is not bound to the baseline observation"
        )
    if hydrated_validation_gate.candidate_hash != hydrated_candidate.evidence_hash:
        raise AcceptanceError(
            "candidate acceptance bindings: validation gate is not bound to candidate evidence"
        )
    if hydrated_validation_gate.hypothesis_hash != hydrated_candidate.hypothesis_hash:
        raise AcceptanceError(
            "candidate acceptance bindings: validation gate hypothesis hash does not match candidate evidence"
        )
    if hydrated_validation_gate.patch_hash != hydrated_candidate.patch_hash:
        raise AcceptanceError(
            "candidate acceptance bindings: validation gate patch hash does not match candidate evidence"
        )
    if (
        hydrated_hypothesis is not None
        and hydrated_candidate.hypothesis_hash != hydrated_hypothesis.hypothesis_hash
    ):
        raise AcceptanceError(
            "candidate acceptance bindings: candidate evidence is not bound to the attribution hypothesis"
        )
    return (
        hydrated_baseline,
        hydrated_candidate,
        hydrated_hypothesis,
        hydrated_baseline_gate,
        hydrated_validation_gate,
        hydrated_quality_gate,
    )


class CandidateAcceptancePlan(_AcceptanceContract):
    """Hash-bound composition of Phase 4A evidence and all three gates."""

    schema_: Literal["candidate-acceptance-plan/v1"] = Field(
        default="candidate-acceptance-plan/v1", alias="schema", serialization_alias="schema"
    )
    baseline: BaselineObservation = Field(
        validation_alias=AliasChoices("baseline", "baseline_observation")
    )
    candidate: CandidateChangeEvidence = Field(
        validation_alias=AliasChoices(
            "candidate", "candidate_evidence", "attribution_candidate", "attribution_candidate_evidence"
        )
    )
    # The candidate already carries the hypothesis hash.  Keeping the full
    # hypothesis optional preserves the compact Phase 4A handoff while letting
    # callers opt into an exact object-identity check.
    hypothesis: AttributionHypothesis | None = None
    baseline_gate: BaselineGateEvidence = Field(
        validation_alias=AliasChoices("baseline_gate", "baseline_gate_evidence")
    )
    validation_gate: ValidationGateEvidence = Field(
        validation_alias=AliasChoices("validation_gate", "validation_gate_evidence")
    )
    quality_gate: QualityGateEvidence = Field(
        validation_alias=AliasChoices("quality_gate", "quality_gate_evidence")
    )
    plan_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> CandidateAcceptancePlan:
        (
            baseline,
            candidate,
            hypothesis,
            baseline_gate,
            validation_gate,
            quality_gate,
        ) = _rehydrate_plan_components(
            self.baseline,
            self.candidate,
            self.hypothesis,
            self.baseline_gate,
            self.validation_gate,
            self.quality_gate,
        )

        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "hypothesis", hypothesis)
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "baseline_gate", baseline_gate)
        object.__setattr__(self, "validation_gate", validation_gate)
        object.__setattr__(self, "quality_gate", quality_gate)

        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"plan_hash"}))
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("candidate acceptance plan_hash does not match canonical contents")
        object.__setattr__(self, "plan_hash", expected)
        return self


def rehydrate_candidate_acceptance_plan(plan: CandidateAcceptancePlan) -> CandidateAcceptancePlan:
    """Round-trip an acceptance plan and verify every nested hash and binding."""

    if not isinstance(plan, CandidateAcceptancePlan) or plan.plan_hash is None:
        raise AcceptanceError("cannot use an unhashed candidate acceptance plan")
    existing_hash = plan.plan_hash
    _rehydrate_plan_components(
        plan.baseline,
        plan.candidate,
        plan.hypothesis,
        plan.baseline_gate,
        plan.validation_gate,
        plan.quality_gate,
    )
    try:
        canonical = _canonical(plan.model_dump(mode="json", by_alias=True))
        payload = json.loads(canonical)
        material = dict(payload)
        material.pop("plan_hash", None)
        expected_hash = _digest(material)
        if existing_hash != expected_hash:
            raise AcceptanceError(
                "candidate acceptance plan hash does not match canonical contents"
            )
        hydrated = CandidateAcceptancePlan.model_validate(payload)
    except AcceptanceError:
        raise
    except Exception as exc:
        raise AcceptanceError("candidate acceptance plan failed integrity rehydration") from exc
    if hydrated.plan_hash != existing_hash or _canonical(
        hydrated.model_dump(mode="json", by_alias=True)
    ) != canonical:
        raise AcceptanceError("candidate acceptance plan hash does not match canonical contents")
    return hydrated


def _assert_acceptance_gates(plan: CandidateAcceptancePlan) -> None:
    """Apply the final fail-closed decision checks after rehydration."""

    if plan.baseline.passed is not False or plan.baseline_gate.status is not BaselineGateStatus.FAILED:
        raise AcceptanceError("baseline gate did not prove a failed baseline")
    if plan.validation_gate.targeted_passed is not True:
        raise AcceptanceError("targeted validation gate did not pass")
    if plan.validation_gate.regression_passed is not True:
        raise AcceptanceError("full regression validation gate did not pass")
    if not (-MAX_COMPLEXITY_DELTA <= plan.quality_gate.complexity_delta <= MAX_COMPLEXITY_DELTA):
        raise AcceptanceError("quality gate complexity delta is outside the allowed bound")
    if not (0 <= plan.quality_gate.cost_units <= MAX_COST_UNITS):
        raise AcceptanceError("quality gate cost is outside the allowed bound")
    if plan.quality_gate.security_passed is not True:
        raise AcceptanceError("quality gate security check did not pass")


class CandidateAcceptanceReceipt(_AcceptanceContract):
    """A deterministic receipt proving that all acceptance gates passed."""

    schema_: Literal["candidate-acceptance-receipt/v1"] = Field(
        default="candidate-acceptance-receipt/v1", alias="schema", serialization_alias="schema"
    )
    acceptance_plan: CandidateAcceptancePlan = Field(
        validation_alias=AliasChoices("acceptance_plan", "plan")
    )
    status: CandidateAcceptanceStatus = CandidateAcceptanceStatus.ACCEPTED
    plan_hash: str = Field(pattern=_HASH)
    reason: str = Field(default="acceptance gates passed", min_length=1, max_length=2_000)
    receipt_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        return _safe_text(value, "reason")

    @model_validator(mode="after")
    def validate_receipt(self) -> CandidateAcceptanceReceipt:
        try:
            plan = rehydrate_candidate_acceptance_plan(self.acceptance_plan)
        except AcceptanceError as exc:
            raise ValueError("acceptance receipt contains an invalid plan") from exc
        if self.status is not CandidateAcceptanceStatus.ACCEPTED:
            raise ValueError("acceptance receipt status must be accepted")
        if self.plan_hash != plan.plan_hash:
            raise ValueError("acceptance receipt is not bound to the acceptance plan")
        _assert_acceptance_gates(plan)
        object.__setattr__(self, "acceptance_plan", plan)
        material = self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
        expected = _digest(material)
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("candidate acceptance receipt_hash does not match canonical contents")
        object.__setattr__(self, "receipt_hash", expected)
        return self

    @property
    def accepted(self) -> bool:
        return self.status is CandidateAcceptanceStatus.ACCEPTED

    @property
    def acceptance_plan_hash(self) -> str:
        return self.plan_hash


class CandidateAcceptanceAdmission(_AcceptanceContract):
    """Typed handoff from a locally accepted candidate to target publication."""

    schema_: Literal["candidate-acceptance-admission/v1"] = Field(
        default="candidate-acceptance-admission/v1", alias="schema", serialization_alias="schema"
    )
    acceptance_receipt: CandidateAcceptanceReceipt
    repository: str
    target_task_id: str
    accepted_execution_id: str
    accepted_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_root: Path
    expected_remote: str
    admission_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("repository")
    @classmethod
    def repository_is_safe(cls, value: str) -> str:
        if _REPOSITORY.fullmatch(value) is None:
            raise ValueError("repository must be an owner/name identifier")
        return value

    @field_validator("target_task_id")
    @classmethod
    def target_task_is_safe(cls, value: str) -> str:
        return _safe_branch_identifier(value, "target_task_id")

    @field_validator("accepted_execution_id")
    @classmethod
    def execution_is_safe(cls, value: str) -> str:
        return _safe_identifier(value, "accepted_execution_id")

    @field_validator("target_root")
    @classmethod
    def root_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("target_root must be an absolute path")
        return value

    @field_validator("expected_remote")
    @classmethod
    def remote_is_safe(cls, value: str) -> str:
        if (
            not isinstance(value, str)
            or not value.startswith("https://github.com/")
            or not value.endswith(".git")
            or _SECRET.search(value)
        ):
            raise ValueError("expected_remote must be a GitHub HTTPS git remote")
        return value

    @model_validator(mode="after")
    def validate_admission(self) -> "CandidateAcceptanceAdmission":
        try:
            receipt = rehydrate_candidate_acceptance_receipt(self.acceptance_receipt)
        except Exception as exc:
            raise ValueError("acceptance admission contains an invalid receipt") from exc
        if not receipt.accepted:
            raise ValueError("acceptance admission requires an accepted receipt")
        if self.expected_remote != f"https://github.com/{self.repository}.git":
            raise ValueError("expected_remote is not bound to repository")
        object.__setattr__(self, "acceptance_receipt", receipt)
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"admission_hash"}))
        if self.admission_hash is not None and self.admission_hash != expected:
            raise ValueError("admission_hash does not match canonical contents")
        object.__setattr__(self, "admission_hash", expected)
        return self


def rehydrate_candidate_acceptance_admission(
    admission: CandidateAcceptanceAdmission,
) -> CandidateAcceptanceAdmission:
    """Round-trip an admission before it crosses into publication."""

    if not isinstance(admission, CandidateAcceptanceAdmission) or admission.admission_hash is None:
        raise AcceptanceError("cannot use an unhashed candidate acceptance admission")
    existing_hash = admission.admission_hash
    canonical = _canonical(admission.model_dump(mode="json", by_alias=True))
    try:
        hydrated = CandidateAcceptanceAdmission.model_validate(json.loads(canonical))
    except Exception as exc:
        raise AcceptanceError("candidate acceptance admission failed integrity rehydration") from exc
    if (
        hydrated.admission_hash != existing_hash
        or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical
    ):
        raise AcceptanceError("candidate acceptance admission hash does not match canonical contents")
    return hydrated


CandidateAcceptanceAdmission.model_rebuild()


def rehydrate_candidate_acceptance_receipt(
    receipt: CandidateAcceptanceReceipt,
) -> CandidateAcceptanceReceipt:
    """Round-trip a receipt and verify its plan, gate, and receipt hashes."""

    if not isinstance(receipt, CandidateAcceptanceReceipt) or receipt.receipt_hash is None:
        raise AcceptanceError("cannot use an unhashed candidate acceptance receipt")
    existing_hash = receipt.receipt_hash
    try:
        canonical = _canonical(receipt.model_dump(mode="json", by_alias=True))
        hydrated = CandidateAcceptanceReceipt.model_validate(json.loads(canonical))
    except AcceptanceError:
        raise
    except Exception as exc:
        raise AcceptanceError("candidate acceptance receipt failed integrity rehydration") from exc
    if (
        hydrated.receipt_hash != existing_hash
        or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical
    ):
        raise AcceptanceError("candidate acceptance receipt hash does not match canonical contents")
    return hydrated


def plan_candidate_acceptance(
    baseline: BaselineObservation,
    candidate: CandidateChangeEvidence,
    baseline_gate: BaselineGateEvidence,
    validation_gate: ValidationGateEvidence,
    quality_gate: QualityGateEvidence,
    hypothesis: AttributionHypothesis | None = None,
) -> CandidateAcceptancePlan:
    """Compose a deterministic acceptance plan without executing any gate."""

    try:
        (
            baseline,
            candidate,
            hypothesis,
            baseline_gate,
            validation_gate,
            quality_gate,
        ) = _rehydrate_plan_components(
            baseline,
            candidate,
            hypothesis,
            baseline_gate,
            validation_gate,
            quality_gate,
        )
        return CandidateAcceptancePlan(
            baseline=baseline,
            candidate=candidate,
            hypothesis=hypothesis,
            baseline_gate=baseline_gate,
            validation_gate=validation_gate,
            quality_gate=quality_gate,
        )
    except AcceptanceError:
        raise
    except Exception as exc:
        raise AcceptanceError("candidate acceptance bindings were rejected") from exc


def accept_candidate(plan: CandidateAcceptancePlan) -> CandidateAcceptanceReceipt:
    """Issue an acceptance receipt only when all three gates are proven."""

    hydrated = rehydrate_candidate_acceptance_plan(plan)
    _assert_acceptance_gates(hydrated)
    try:
        return CandidateAcceptanceReceipt(
            acceptance_plan=hydrated,
            status=CandidateAcceptanceStatus.ACCEPTED,
            plan_hash=hydrated.plan_hash or "",
        )
    except AcceptanceError:
        raise
    except Exception as exc:
        raise AcceptanceError("candidate acceptance receipt was rejected") from exc


class CandidateAcceptanceGate:
    """Small execution-free facade for callers that prefer an object API."""

    def plan(
        self,
        baseline: BaselineObservation,
        candidate: CandidateChangeEvidence,
        baseline_gate: BaselineGateEvidence,
        validation_gate: ValidationGateEvidence,
        quality_gate: QualityGateEvidence,
        hypothesis: AttributionHypothesis | None = None,
    ) -> CandidateAcceptancePlan:
        return plan_candidate_acceptance(
            baseline, candidate, baseline_gate, validation_gate, quality_gate, hypothesis
        )

    def accept(self, plan: CandidateAcceptancePlan) -> CandidateAcceptanceReceipt:
        return accept_candidate(plan)


# Keep naming parallel with the earlier planner/orchestrator modules.
plan_acceptance = plan_candidate_acceptance
accept = accept_candidate
accept_candidate_change = accept_candidate
rehydrate_acceptance_plan = rehydrate_candidate_acceptance_plan
rehydrate_acceptance_receipt = rehydrate_candidate_acceptance_receipt


__all__ = [
    "AcceptanceError",
    "AcceptanceStatus",
    "BaselineGateEvidence",
    "BaselineGateStatus",
    "BaselineStatus",
    "CandidateAcceptanceGate",
    "CandidateAcceptanceAdmission",
    "CandidateAcceptancePlan",
    "CandidateAcceptanceReceipt",
    "CandidateAcceptanceStatus",
    "MAX_COMPLEXITY_DELTA",
    "MAX_COST_UNITS",
    "MAX_TEST_COUNT",
    "QualityGateEvidence",
    "ValidationGateEvidence",
    "accept",
    "accept_candidate",
    "accept_candidate_change",
    "plan_acceptance",
    "plan_candidate_acceptance",
    "rehydrate_acceptance_plan",
    "rehydrate_acceptance_receipt",
    "rehydrate_candidate_acceptance_plan",
    "rehydrate_candidate_acceptance_receipt",
    "rehydrate_candidate_acceptance_admission",
]
