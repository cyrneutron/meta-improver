"""Pure admission gate for the Phase 3 sandbox orchestration boundary.

Admission consumes already validated plans and a bounded container request.  It
does not apply patches, invoke Git, start a container, inspect the filesystem,
or read credentials.  Those effects belong to later transports and are kept
outside this module deliberately.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from src.models.contracts import ContractModel
from src.sandbox.patch import PatchCheckStatus, PatchPlan
from src.sandbox.runner import ContainerRunRequest
from src.sandbox.worktree import WorktreePlan, rehydrate_worktree_plan


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class SandboxAdmissionError(ValueError):
    """Raised when an admission request cannot be proven safe and bound."""


class SandboxAdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    REJECTED = "rejected"


class SandboxAdmissionPlan(ContractModel):
    """Deterministic, hash-bound admission plan.

    The embedded contracts are snapshots.  Rehydration immediately before
    admission detects mutation after planning, while the derived hash makes a
    receipt independently auditable.
    """

    schema_: Literal["sandbox-admission-plan/v1"] = Field(
        default="sandbox-admission-plan/v1", alias="schema", serialization_alias="schema"
    )
    worktree_plan: WorktreePlan
    patch_plan: PatchPlan
    container_request: ContainerRunRequest
    worktree_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    patch_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    worktree_path: str = Field(min_length=1, max_length=4_096)
    container_request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bindings(self) -> SandboxAdmissionPlan:
        if self.worktree_plan.plan_hash is None or self.worktree_plan.plan_hash != self.worktree_plan_hash:
            raise ValueError("admission worktree plan hash is not bound")
        if self.patch_plan.plan_hash is None or self.patch_plan.plan_hash != self.patch_plan_hash:
            raise ValueError("admission patch plan hash is not bound")
        if self.patch_plan.status is not PatchCheckStatus.PASSED:
            raise ValueError("only a passed patch check may be admitted")
        if self.patch_plan.worktree_plan_hash != self.worktree_plan_hash:
            raise ValueError("patch plan is not bound to the worktree plan")
        if self.patch_plan.base_commit != self.base_commit or self.worktree_plan.base_commit != self.base_commit:
            raise ValueError("admission base_commit does not match all plans")
        if self.patch_plan.worktree_path != self.worktree_path or self.worktree_plan.worktree_path != self.worktree_path:
            raise ValueError("admission worktree_path does not match all plans")
        if self.patch_plan.validation.base_commit != self.base_commit:
            raise ValueError("patch validation base_commit is not bound")
        if self.patch_plan.request.base_commit != self.base_commit:
            raise ValueError("patch check request base_commit is not bound")
        if self.patch_plan.request.worktree_path != self.worktree_path or self.patch_plan.request.cwd != self.worktree_path:
            raise ValueError("patch check request path is not bound")
        if self.patch_plan.request.plan_hash != self.worktree_plan_hash:
            raise ValueError("patch check request hash is not bound")
        if self.container_request.policy.network_disabled is not True:
            raise ValueError("container network must remain disabled")
        if self.container_request.policy.credentials is not False:
            raise ValueError("container credentials must remain disabled")
        if self.container_request_digest != self.container_request.request_digest:
            raise ValueError("container request digest does not match its contents")
        material = self.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
        expected = _digest(material)
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("admission plan_hash does not match plan contents")
        self.plan_hash = expected
        return self


class SandboxAdmissionReceipt(ContractModel):
    """Stable receipt for the admission decision; it contains no execution output."""

    schema_: Literal["sandbox-admission-receipt/v1"] = Field(
        default="sandbox-admission-receipt/v1", alias="schema", serialization_alias="schema"
    )
    status: SandboxAdmissionStatus
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    worktree_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    patch_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    container_request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=2_000)
    receipt_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def derive_hash(self) -> SandboxAdmissionReceipt:
        material = self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
        expected = _digest(material)
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("admission receipt_hash does not match receipt contents")
        self.receipt_hash = expected
        return self


def _rehydrate_patch_plan(plan: PatchPlan) -> PatchPlan:
    if not isinstance(plan, PatchPlan) or plan.plan_hash is None:
        raise SandboxAdmissionError("cannot admit an unhashed patch plan")
    existing = plan.plan_hash
    canonical = _canonical(plan.model_dump(mode="json", by_alias=True))
    try:
        hydrated = PatchPlan.model_validate(json.loads(canonical))
    except Exception as exc:
        raise SandboxAdmissionError("patch plan failed integrity rehydration") from exc
    if hydrated.plan_hash != existing or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise SandboxAdmissionError("patch plan hash does not match its canonical contents")
    return hydrated


def _rehydrate_request(request: ContainerRunRequest) -> ContainerRunRequest:
    if not isinstance(request, ContainerRunRequest):
        raise SandboxAdmissionError("admission requires a bounded ContainerRunRequest")
    canonical = _canonical(request.model_dump(mode="json", by_alias=True))
    try:
        hydrated = ContainerRunRequest.model_validate(json.loads(canonical))
    except Exception as exc:
        raise SandboxAdmissionError("container request failed integrity rehydration") from exc
    if _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise SandboxAdmissionError("container request is not canonically stable")
    if hydrated.policy.network_disabled is not True or hydrated.policy.credentials is not False:
        raise SandboxAdmissionError("container request overrides deny-by-default policy")
    return hydrated


def rehydrate_sandbox_admission_plan(plan: SandboxAdmissionPlan) -> SandboxAdmissionPlan:
    """Round-trip a plan and verify its existing hash before admission."""

    if not isinstance(plan, SandboxAdmissionPlan) or plan.plan_hash is None:
        raise SandboxAdmissionError("cannot admit an unhashed sandbox admission plan")
    existing = plan.plan_hash
    canonical = _canonical(plan.model_dump(mode="json", by_alias=True))
    try:
        hydrated = SandboxAdmissionPlan.model_validate(json.loads(canonical))
    except Exception as exc:
        raise SandboxAdmissionError("sandbox admission plan failed integrity rehydration") from exc
    if hydrated.plan_hash != existing or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise SandboxAdmissionError("sandbox admission plan hash does not match its contents")
    return hydrated


def plan_sandbox_admission(
    worktree_plan: WorktreePlan,
    patch_plan: PatchPlan,
    container_request: ContainerRunRequest,
) -> SandboxAdmissionPlan:
    """Create a deterministic admission plan without crossing an execution boundary."""

    try:
        worktree = rehydrate_worktree_plan(worktree_plan)
    except Exception as exc:
        raise SandboxAdmissionError("worktree plan failed integrity rehydration") from exc
    patch = _rehydrate_patch_plan(patch_plan)
    request = _rehydrate_request(container_request)
    if patch.status is not PatchCheckStatus.PASSED:
        raise SandboxAdmissionError("patch check did not pass; admission is rejected")
    try:
        return SandboxAdmissionPlan(
            worktree_plan=worktree,
            patch_plan=patch,
            container_request=request,
            worktree_plan_hash=worktree.plan_hash or "",
            patch_plan_hash=patch.plan_hash or "",
            base_commit=worktree.base_commit,
            worktree_path=worktree.worktree_path,
            container_request_digest=request.request_digest,
        )
    except Exception as exc:
        raise SandboxAdmissionError("sandbox admission bindings are inconsistent") from exc


def admit_sandbox(
    plan: SandboxAdmissionPlan | WorktreePlan,
    patch_plan: PatchPlan | None = None,
    container_request: ContainerRunRequest | None = None,
) -> SandboxAdmissionReceipt:
    """Admit a valid plan and issue a deterministic receipt; never execute it."""

    if not isinstance(plan, SandboxAdmissionPlan):
        if patch_plan is None or container_request is None:
            raise SandboxAdmissionError("raw admission requires patch plan and container request")
        plan = plan_sandbox_admission(plan, patch_plan, container_request)
    hydrated = rehydrate_sandbox_admission_plan(plan)
    return SandboxAdmissionReceipt(
        status=SandboxAdmissionStatus.ADMITTED,
        plan_hash=hydrated.plan_hash or "",
        worktree_plan_hash=hydrated.worktree_plan_hash,
        patch_plan_hash=hydrated.patch_plan_hash,
        container_request_digest=hydrated.container_request_digest,
        reason="admission checks passed",
    )


class SandboxAdmissionGate:
    """Small orchestration facade; dependency-free and execution-free by design."""

    def plan(
        self,
        worktree_plan: WorktreePlan,
        patch_plan: PatchPlan,
        container_request: ContainerRunRequest,
    ) -> SandboxAdmissionPlan:
        return plan_sandbox_admission(worktree_plan, patch_plan, container_request)

    def admit(
        self,
        plan: SandboxAdmissionPlan | WorktreePlan,
        patch_plan: PatchPlan | None = None,
        container_request: ContainerRunRequest | None = None,
    ) -> SandboxAdmissionReceipt:
        return admit_sandbox(plan, patch_plan, container_request)


# Short aliases mirror the other sandbox planner entry points.
plan_admission = plan_sandbox_admission
admit = admit_sandbox


__all__ = [
    "SandboxAdmissionError",
    "SandboxAdmissionPlan",
    "SandboxAdmissionReceipt",
    "SandboxAdmissionStatus",
    "SandboxAdmissionGate",
    "admit",
    "admit_sandbox",
    "plan_admission",
    "plan_sandbox_admission",
    "rehydrate_sandbox_admission_plan",
]
