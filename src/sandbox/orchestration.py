"""Pure protocol for composing sandbox admission and a container result.

Phase 3F deliberately stops at an in-memory protocol boundary.  The fake
orchestrator accepts a result supplied by a caller and never invokes a runner,
transport, process, container runtime, or filesystem operation.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from src.models.contracts import ContractModel
from src.sandbox.admission import (
    SandboxAdmissionPlan,
    SandboxAdmissionReceipt,
    SandboxAdmissionStatus,
    rehydrate_sandbox_admission_plan,
)
from src.sandbox.runner import ContainerRunResult, ContainerRunStatus


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class OrchestrationError(ValueError):
    """Raised when a sandbox orchestration cannot prove successful execution."""


class SandboxOrchestrationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


# Keep the shorter name available to callers that treat this as a generic
# orchestration protocol.
OrchestrationStatus = SandboxOrchestrationStatus


def _rehydrate_admission_receipt(receipt: SandboxAdmissionReceipt) -> SandboxAdmissionReceipt:
    if not isinstance(receipt, SandboxAdmissionReceipt) or receipt.receipt_hash is None:
        raise OrchestrationError("cannot orchestrate an unhashed admission receipt")
    existing_hash = receipt.receipt_hash
    canonical = _canonical(receipt.model_dump(mode="json", by_alias=True))
    try:
        hydrated = SandboxAdmissionReceipt.model_validate(json.loads(canonical))
    except Exception as exc:
        raise OrchestrationError("admission receipt failed integrity rehydration") from exc
    if hydrated.receipt_hash != existing_hash or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise OrchestrationError("admission receipt hash does not match its canonical contents")
    return hydrated


def _rehydrate_result(result: ContainerRunResult) -> ContainerRunResult:
    if not isinstance(result, ContainerRunResult):
        raise OrchestrationError("orchestration requires a ContainerRunResult")
    canonical = _canonical(result.model_dump(mode="json", by_alias=True))
    try:
        hydrated = ContainerRunResult.model_validate(json.loads(canonical))
    except Exception as exc:
        raise OrchestrationError("container result failed integrity rehydration") from exc
    if _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise OrchestrationError("container result is not canonically stable")
    return hydrated


class SandboxOrchestrationPlan(ContractModel):
    """Hash-bound composition of an admitted sandbox and its receipt."""

    schema_: Literal["sandbox-orchestration-plan/v1"] = Field(
        default="sandbox-orchestration-plan/v1", alias="schema", serialization_alias="schema"
    )
    admission_plan: SandboxAdmissionPlan
    admission_receipt: SandboxAdmissionReceipt
    plan_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> SandboxOrchestrationPlan:
        try:
            admission_plan = rehydrate_sandbox_admission_plan(self.admission_plan)
            admission_receipt = _rehydrate_admission_receipt(self.admission_receipt)
        except OrchestrationError:
            raise
        except Exception as exc:
            raise ValueError("admission inputs failed integrity rehydration") from exc
        if admission_receipt.status is not SandboxAdmissionStatus.ADMITTED:
            raise ValueError("sandbox orchestration requires an admitted receipt")
        if admission_receipt.plan_hash != admission_plan.plan_hash:
            raise ValueError("admission receipt is not bound to the admission plan")
        self.admission_plan = admission_plan
        self.admission_receipt = admission_receipt
        material = self.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
        expected = _digest(material)
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("orchestration plan_hash does not match plan contents")
        self.plan_hash = expected
        return self


class SandboxOrchestrationReceipt(ContractModel):
    """Hash-bound result of a successful fake orchestration."""

    schema_: Literal["sandbox-orchestration-receipt/v1"] = Field(
        default="sandbox-orchestration-receipt/v1", alias="schema", serialization_alias="schema"
    )
    orchestration_plan: SandboxOrchestrationPlan
    run_result: ContainerRunResult
    status: SandboxOrchestrationStatus
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=2_000)
    receipt_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def admission_receipt(self) -> SandboxAdmissionReceipt:
        return self.orchestration_plan.admission_receipt

    @property
    def success(self) -> bool:
        return (
            self.status is SandboxOrchestrationStatus.SUCCEEDED
            and self.admission_receipt.status is SandboxAdmissionStatus.ADMITTED
            and self.run_result.status is ContainerRunStatus.SUCCEEDED
        )

    @model_validator(mode="after")
    def validate_result_and_hash(self) -> SandboxOrchestrationReceipt:
        try:
            plan = rehydrate_sandbox_orchestration_plan(self.orchestration_plan)
            result = _rehydrate_result(self.run_result)
        except OrchestrationError:
            raise
        if self.result_hash != _digest(result.model_dump(mode="json", by_alias=True)):
            raise ValueError("orchestration result_hash does not match result contents")
        if result.request_digest != plan.admission_plan.container_request.request_digest:
            raise ValueError("container result is not bound to the admitted request")
        if self.status is SandboxOrchestrationStatus.SUCCEEDED and result.status is not ContainerRunStatus.SUCCEEDED:
            raise ValueError("successful orchestration requires a succeeded container result")
        self.orchestration_plan = plan
        self.run_result = result
        material = self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
        expected = _digest(material)
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("orchestration receipt_hash does not match receipt contents")
        self.receipt_hash = expected
        return self


def rehydrate_sandbox_orchestration_plan(plan: SandboxOrchestrationPlan) -> SandboxOrchestrationPlan:
    """Round-trip an orchestration plan and verify its existing hash."""

    if not isinstance(plan, SandboxOrchestrationPlan) or plan.plan_hash is None:
        raise OrchestrationError("cannot orchestrate an unhashed orchestration plan")
    existing_hash = plan.plan_hash
    canonical = _canonical(plan.model_dump(mode="json", by_alias=True))
    try:
        hydrated = SandboxOrchestrationPlan.model_validate(json.loads(canonical))
    except OrchestrationError:
        raise
    except Exception as exc:
        raise OrchestrationError("orchestration plan failed integrity rehydration") from exc
    if hydrated.plan_hash != existing_hash or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise OrchestrationError("orchestration plan hash does not match its canonical contents")
    return hydrated


def rehydrate_sandbox_orchestration_receipt(
    receipt: SandboxOrchestrationReceipt,
) -> SandboxOrchestrationReceipt:
    """Round-trip a receipt and verify plan, result, and receipt hashes."""

    if not isinstance(receipt, SandboxOrchestrationReceipt) or receipt.receipt_hash is None:
        raise OrchestrationError("cannot use an unhashed orchestration receipt")
    existing_hash = receipt.receipt_hash
    canonical = _canonical(receipt.model_dump(mode="json", by_alias=True))
    try:
        hydrated = SandboxOrchestrationReceipt.model_validate(json.loads(canonical))
    except OrchestrationError:
        raise
    except Exception as exc:
        raise OrchestrationError("orchestration receipt failed integrity rehydration") from exc
    if hydrated.receipt_hash != existing_hash or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise OrchestrationError("orchestration receipt hash does not match its canonical contents")
    return hydrated


def plan_sandbox_orchestration(
    admission_plan: SandboxAdmissionPlan,
    admission_receipt: SandboxAdmissionReceipt,
) -> SandboxOrchestrationPlan:
    """Build an execution-free orchestration plan from an admitted sandbox."""

    try:
        return SandboxOrchestrationPlan(
            admission_plan=admission_plan,
            admission_receipt=admission_receipt,
        )
    except OrchestrationError:
        raise
    except Exception as exc:
        raise OrchestrationError("sandbox orchestration plan was rejected") from exc


def complete_sandbox_orchestration(
    plan: SandboxOrchestrationPlan,
    result: ContainerRunResult,
) -> SandboxOrchestrationReceipt:
    """Compose a receipt, accepting only ADMITTED plus SUCCEEDED."""

    hydrated_plan = rehydrate_sandbox_orchestration_plan(plan)
    hydrated_result = _rehydrate_result(result)
    if hydrated_plan.admission_receipt.status is not SandboxAdmissionStatus.ADMITTED:
        raise OrchestrationError("orchestration requires an admitted sandbox")
    if hydrated_result.status is not ContainerRunStatus.SUCCEEDED:
        raise OrchestrationError("orchestration requires a succeeded container result")
    if hydrated_result.request_digest != hydrated_plan.admission_plan.container_request.request_digest:
        raise OrchestrationError("container result is not bound to the admitted request")
    try:
        return SandboxOrchestrationReceipt(
            orchestration_plan=hydrated_plan,
            run_result=hydrated_result,
            status=SandboxOrchestrationStatus.SUCCEEDED,
            result_hash=_digest(hydrated_result.model_dump(mode="json", by_alias=True)),
            reason="sandbox orchestration succeeded",
        )
    except Exception as exc:
        raise OrchestrationError("sandbox orchestration receipt was rejected") from exc


orchestrate_sandbox = complete_sandbox_orchestration
plan_orchestration = plan_sandbox_orchestration


class FakeSandboxOrchestrator:
    """Deterministic fixture composer; it performs no sandbox execution."""

    def __init__(self, result: ContainerRunResult | None = None) -> None:
        self.result = result

    def plan(
        self,
        admission_plan: SandboxAdmissionPlan,
        admission_receipt: SandboxAdmissionReceipt,
    ) -> SandboxOrchestrationPlan:
        return plan_sandbox_orchestration(admission_plan, admission_receipt)

    def run(
        self,
        plan: SandboxOrchestrationPlan,
        result: ContainerRunResult | None = None,
    ) -> SandboxOrchestrationReceipt:
        selected = result if result is not None else self.result
        if selected is None:
            raise OrchestrationError("fake orchestrator requires a supplied result fixture")
        return complete_sandbox_orchestration(plan, selected)

    complete = run
    orchestrate = run


__all__ = [
    "FakeSandboxOrchestrator",
    "OrchestrationError",
    "OrchestrationStatus",
    "SandboxOrchestrationPlan",
    "SandboxOrchestrationReceipt",
    "SandboxOrchestrationStatus",
    "complete_sandbox_orchestration",
    "orchestrate_sandbox",
    "plan_orchestration",
    "plan_sandbox_orchestration",
    "rehydrate_sandbox_orchestration_plan",
    "rehydrate_sandbox_orchestration_receipt",
]
