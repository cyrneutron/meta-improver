"""Pure worktree planning and receipt-backed lifecycle factories."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from src.models.contracts import ContractModel
from src.sandbox.contracts import WorktreeRequest
from .worktree_contracts import (
    AbsolutePathEntry,
    GitOperation,
    RepositoryBoundary,
    RepositoryEntry,
    RepositoryEntryKind,
    RepositorySnapshot,
    WorktreeCreationReceipt,
    WorktreeDestination,
    WorktreeOperationKind,
    WorktreePlanningError,
    WorktreePolicy,
    WorktreePorcelainAttestation,
    WorktreeRemovalPrecondition,
    _FULL_COMMIT_PATTERN,
    _MAX_OPERATIONS,
    _MAX_PATH_LENGTH,
    _MAX_REASON_LENGTH,
    _canonical_json,
    _is_under,
    _resource_concurrency_key,
    _scope_contains,
    _safe_absolute_path,
    _safe_identity,
    _validate_absolute_chain,
    _validate_destination,
)


def _rehydrate_receipt(receipt: WorktreeCreationReceipt) -> WorktreeCreationReceipt:
    """Round-trip and verify the receipt's existing digest before use."""

    if not isinstance(receipt, WorktreeCreationReceipt) or receipt.receipt_digest is None:
        raise WorktreePlanningError("creation receipt is missing its integrity digest")
    canonical = _canonical_json(receipt.model_dump(mode="json", by_alias=True))
    try:
        hydrated = WorktreeCreationReceipt.model_validate(json.loads(canonical))
    except (TypeError, ValueError) as exc:
        raise WorktreePlanningError("creation receipt failed integrity rehydration") from exc
    if hydrated.receipt_digest != receipt.receipt_digest:
        raise WorktreePlanningError("creation receipt digest does not match its contents")
    if _canonical_json(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise WorktreePlanningError("creation receipt is not canonically stable")
    return hydrated


def _rehydrate_observation(observation: WorktreePorcelainAttestation) -> WorktreePorcelainAttestation:
    """Round-trip an observation before comparing its transport bindings."""

    if not isinstance(observation, WorktreePorcelainAttestation):
        raise WorktreePlanningError("porcelain observation has an invalid contract type")
    canonical = _canonical_json(observation.model_dump(mode="json", by_alias=True))
    try:
        hydrated = WorktreePorcelainAttestation.model_validate(json.loads(canonical))
    except (TypeError, ValueError) as exc:
        raise WorktreePlanningError("porcelain observation failed integrity rehydration") from exc
    if _canonical_json(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise WorktreePlanningError("porcelain observation is not canonically stable")
    return hydrated


class WorktreePlan(ContractModel):
    """Deterministic create plan; lifecycle actions are derived separately."""

    security_fields = (
        "repository_id",
        "source_root",
        "worktree_path",
        "base_commit",
        "request_id",
        "concurrency_key",
        "allowed_paths",
        "operations",
    )

    schema_: Literal["worktree-plan/v1"] = Field(
        default="worktree-plan/v1", alias="schema", serialization_alias="schema"
    )
    request: WorktreeRequest
    snapshot: RepositorySnapshot
    policy: WorktreePolicy
    destination: WorktreeDestination
    repository_id: str
    source_root: str
    worktree_path: str
    base_commit: str
    request_id: str
    concurrency_key: str = Field(pattern=r"^worktree:sha256:[0-9a-f]{64}$")
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    operations: list[GitOperation] = Field(min_length=1, max_length=_MAX_OPERATIONS)
    plan_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_and_derive_hash(self) -> WorktreePlan:
        _validate_scope(self.request, self.snapshot, self.policy)
        _validate_destination(
            self.destination,
            worktree_root=self.policy.worktree_root,
            worktree_path=self.worktree_path,
        )
        if (
            self.request.repository_id != self.repository_id
            or self.request.source_root != self.source_root
            or self.request.worktree_path != self.worktree_path
            or self.request.base_commit != self.base_commit
            or self.snapshot.repository_id != self.repository_id
            or self.snapshot.source_root != self.source_root
            or self.snapshot.base_commit != self.base_commit
        ):
            raise ValueError("plan fields do not match the embedded worktree request")
        request_id = self.request.concurrency_id or self.request.request_id
        if request_id != self.request_id:
            raise ValueError("plan request_id does not match the embedded worktree request")
        if self.allowed_paths != sorted(self.request.path_allowlist):
            raise ValueError("plan allowlist does not match the embedded worktree request")
        expected_concurrency_key = _resource_concurrency_key(
            self.repository_id,
            self.source_root,
            self.worktree_path,
        )
        if self.concurrency_key != expected_concurrency_key:
            raise ValueError("concurrency_key does not match the plan identity")
        expected_operations = [
            WorktreeOperationKind.VERIFY_BASE_COMMIT,
            WorktreeOperationKind.INSPECT_WORKTREES,
            WorktreeOperationKind.CREATE_WORKTREE,
        ]
        if [operation.kind for operation in self.operations] != expected_operations:
            raise ValueError("create plan must contain verify, inspect, and create operations in order")
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("worktree operations must have unique identities")
        for operation in self.operations:
            if operation.cwd != self.source_root:
                raise ValueError("all worktree operations must use the source repository as cwd")
        if self.operations[0].argv[4] != f"{self.base_commit}^{{commit}}":
            raise ValueError("verify operation base does not match the plan")
        if self.operations[2].argv[4] != self.worktree_path:
            raise ValueError("create operation destination does not match the plan")
        if self.operations[2].argv[5] != self.base_commit:
            raise ValueError("create operation base does not match the plan")
        material = self.model_dump(mode="json", exclude={"plan_hash"})
        expected = "sha256:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("plan_hash does not match plan contents")
        self.plan_hash = expected
        return self


def _rehydrate_plan(plan: WorktreePlan) -> WorktreePlan:
    """Round-trip a plan and verify its already-issued canonical hash."""

    if not isinstance(plan, WorktreePlan) or plan.plan_hash is None:
        raise WorktreePlanningError("cannot derive an action from an unhashed worktree plan")
    existing_hash = plan.plan_hash
    canonical = _canonical_json(plan.model_dump(mode="json", by_alias=True))
    try:
        hydrated = WorktreePlan.model_validate(json.loads(canonical))
    except (TypeError, ValueError) as exc:
        raise WorktreePlanningError("worktree plan failed integrity rehydration") from exc
    if hydrated.plan_hash != existing_hash:
        raise WorktreePlanningError("worktree plan hash does not match its canonical contents")
    if _canonical_json(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise WorktreePlanningError("worktree plan is not canonically stable")
    return hydrated


def rehydrate_worktree_plan(plan: WorktreePlan) -> WorktreePlan:
    """Public integrity boundary for consumers deriving plans from a worktree plan."""

    return _rehydrate_plan(plan)


def _validate_receipt_observation_for_plan(
    plan: WorktreePlan,
    receipt: WorktreeCreationReceipt,
    observation: WorktreePorcelainAttestation,
) -> tuple[WorktreeCreationReceipt, WorktreePorcelainAttestation]:
    """Bind a creation receipt and transport observation to one plan."""

    receipt = _rehydrate_receipt(receipt)
    observation = _rehydrate_observation(observation)
    create_operations = [
        operation
        for operation in plan.operations
        if operation.kind is WorktreeOperationKind.CREATE_WORKTREE
    ]
    if len(create_operations) != 1 or create_operations[0].operation_id is None:
        raise WorktreePlanningError("worktree plan has no uniquely identifiable create operation")
    create_operation_id = create_operations[0].operation_id
    expected_receipt = {
        "parent_plan_hash": plan.plan_hash,
        "create_operation_id": create_operation_id,
        "repository_id": plan.repository_id,
        "source_root": plan.source_root,
        "worktree_path": plan.worktree_path,
    }
    for field, expected in expected_receipt.items():
        if getattr(receipt, field) != expected:
            raise WorktreePlanningError(f"creation receipt {field} does not match the worktree plan")
    if receipt.base_commit != plan.base_commit or receipt.head_commit != plan.base_commit:
        raise WorktreePlanningError("creation receipt commits do not match the detached base")
    observation_fields = (
        "parent_plan_hash",
        "create_operation_id",
        "repository_id",
        "source_root",
        "common_git_dir",
        "worktree_gitdir",
        "worktree_path",
        "base_commit",
        "head_commit",
    )
    for field in observation_fields:
        if getattr(observation, field) != getattr(receipt, field):
            raise WorktreePlanningError(f"porcelain observation {field} does not match the creation receipt")
    if observation.request_id != plan.request_id:
        raise WorktreePlanningError("porcelain observation request_id does not match the worktree plan")
    if observation.concurrency_key != plan.concurrency_key:
        raise WorktreePlanningError("porcelain observation concurrency_key does not match the worktree plan")
    if observation.transport_receipt_digest != receipt.receipt_digest:
        raise WorktreePlanningError("porcelain observation is not bound to the creation receipt digest")
    return receipt, observation


def _validate_action_evidence(action: WorktreeActionPlan) -> None:
    """Close every evidence and precondition field on a remove-capable action."""

    assert action.creation_receipt is not None
    assert action.porcelain_observation is not None
    assert action.remove_precondition is not None
    receipt = _rehydrate_receipt(action.creation_receipt)
    observation = _rehydrate_observation(action.porcelain_observation)
    if receipt.parent_plan_hash != action.parent_plan_hash:
        raise ValueError("creation receipt is not bound to the parent plan")
    for field in ("repository_id", "source_root", "worktree_path", "base_commit"):
        if getattr(receipt, field) != getattr(action, field):
            raise ValueError(f"creation receipt {field} does not match the action plan")
    if receipt.head_commit != action.base_commit:
        raise ValueError("creation receipt head_commit does not match the action plan")
    if observation.parent_plan_hash != action.parent_plan_hash:
        raise ValueError("porcelain observation is not bound to the parent plan")
    for field in (
        "create_operation_id",
        "repository_id",
        "source_root",
        "common_git_dir",
        "worktree_gitdir",
        "worktree_path",
        "base_commit",
        "head_commit",
    ):
        if getattr(observation, field) != getattr(receipt, field):
            raise ValueError(f"porcelain observation {field} does not match the receipt")
    if observation.request_id != action.request_id:
        raise ValueError("porcelain observation request_id does not match the action plan")
    if observation.concurrency_key != action.concurrency_key:
        raise ValueError("porcelain observation concurrency_key does not match the action plan")
    if observation.transport_receipt_digest != receipt.receipt_digest:
        raise ValueError("porcelain observation is not bound to the receipt digest")
    precondition = action.remove_precondition
    if (
        precondition.parent_plan_hash != action.parent_plan_hash
        or precondition.repository_id != action.repository_id
        or precondition.source_root != action.source_root
        or precondition.worktree_path != action.worktree_path
        or precondition.expected_base_commit != action.base_commit
        or precondition.expected_head != receipt.head_commit
        or precondition.request_id != action.request_id
        or precondition.concurrency_key != action.concurrency_key
    ):
        raise ValueError("remove precondition fields do not match the action plan")


class WorktreeActionPlan(ContractModel):
    """Deterministic cleanup or crash-recovery operations."""

    security_fields = (
        "parent_plan_hash",
        "repository_id",
        "source_root",
        "worktree_path",
        "base_commit",
        "request_id",
        "concurrency_key",
        "disposition",
        "creation_receipt",
        "porcelain_observation",
        "remove_precondition",
        "operations",
    )

    schema_: Literal["worktree-action-plan/v1"] = Field(
        default="worktree-action-plan/v1", alias="schema", serialization_alias="schema"
    )
    action: Literal["cleanup", "recovery"]
    disposition: Literal["remove_ready", "manual_review_required"]
    parent_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    repository_id: str = Field(min_length=1, max_length=200)
    concurrency_key: str = Field(pattern=r"^worktree:sha256:[0-9a-f]{64}$")
    source_root: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    worktree_path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    request_id: str = Field(min_length=1, max_length=200)
    creation_receipt: WorktreeCreationReceipt | None = None
    porcelain_observation: WorktreePorcelainAttestation | None = None
    remove_precondition: WorktreeRemovalPrecondition | None = None
    operations: list[GitOperation] = Field(min_length=1, max_length=_MAX_OPERATIONS)
    reason: str = Field(default="", max_length=_MAX_REASON_LENGTH)
    plan_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("source_root", "worktree_path")
    @classmethod
    def safe_paths(cls, value: str, info) -> str:
        return _safe_absolute_path(value, field_name=info.field_name)

    @field_validator("repository_id", "request_id")
    @classmethod
    def safe_identity(cls, value: str, info) -> str:
        return _safe_identity(
            value,
            field_name=info.field_name,
            allow_repository_path=info.field_name == "repository_id",
        )

    @model_validator(mode="after")
    def derive_hash(self) -> WorktreeActionPlan:
        expected_operations = [WorktreeOperationKind.INSPECT_WORKTREES]
        if self.disposition == "remove_ready":
            expected_operations.append(WorktreeOperationKind.REMOVE_WORKTREE)
        if [operation.kind for operation in self.operations] != expected_operations:
            raise ValueError("action operations do not match the action disposition")
        for operation in self.operations:
            if operation.cwd != self.source_root:
                raise ValueError("action operations must use the source repository as cwd")
        if self.disposition == "manual_review_required":
            if self.creation_receipt is not None or self.porcelain_observation is not None:
                raise ValueError("manual-review recovery cannot carry removal evidence")
            if self.remove_precondition is not None:
                raise ValueError("manual-review recovery cannot carry a remove precondition")
        else:
            if (
                self.creation_receipt is None
                or self.porcelain_observation is None
                or self.remove_precondition is None
            ):
                raise ValueError("remove-ready action requires creation and porcelain evidence")
            _validate_action_evidence(self)
            remove = self.operations[1]
            if remove.argv[4] != self.worktree_path:
                raise ValueError("action operation destination does not match the plan")
            if remove.approved_paths != [self.worktree_path]:
                raise ValueError("remove operation approval does not match the action plan")
            if remove.precondition != self.remove_precondition:
                raise ValueError("remove operation precondition does not match the action plan")
        material = self.model_dump(mode="json", exclude={"plan_hash"})
        expected = "sha256:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("action plan_hash does not match plan contents")
        self.plan_hash = expected
        return self


def rehydrate_action_plan(action: WorktreeActionPlan) -> WorktreeActionPlan:
    """Round-trip an action and verify its existing hash before execution."""

    if not isinstance(action, WorktreeActionPlan) or action.plan_hash is None:
        raise WorktreePlanningError("cannot execute an unhashed action plan")
    existing_hash = action.plan_hash
    canonical = _canonical_json(action.model_dump(mode="json", by_alias=True))
    try:
        hydrated = WorktreeActionPlan.model_validate(json.loads(canonical))
    except (TypeError, ValueError) as exc:
        raise WorktreePlanningError("action plan failed integrity rehydration") from exc
    if hydrated.plan_hash != existing_hash:
        raise WorktreePlanningError("action plan hash does not match its canonical contents")
    if _canonical_json(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise WorktreePlanningError("action plan is not canonically stable")
    return hydrated


class WorktreeManager(Protocol):
    """Planning-only manager boundary; implementations must not execute Git."""

    def plan(
        self,
        request: WorktreeRequest,
        snapshot: RepositorySnapshot,
        policy: WorktreePolicy,
        destination: WorktreeDestination,
    ) -> WorktreePlan:
        """Return a validated create plan."""

    def cleanup(
        self,
        plan: WorktreePlan,
        receipt: WorktreeCreationReceipt,
        observation: WorktreePorcelainAttestation,
        *,
        reason: str = "planned worktree cleanup",
    ) -> WorktreeActionPlan:
        """Return a receipt-backed confirmation-gated cleanup plan."""

    def recovery(
        self,
        plan: WorktreePlan,
        receipt: WorktreeCreationReceipt | None = None,
        observation: WorktreePorcelainAttestation | None = None,
        *,
        reason: str = "recover an interrupted worktree operation",
    ) -> WorktreeActionPlan:
        """Return remove-ready or inspect-only recovery."""


def _require_request_fields(request: WorktreeRequest) -> tuple[str, str, str, str]:
    missing = [
        name
        for name, value in (
            ("repository_id", request.repository_id),
            ("source_root", request.source_root),
            ("worktree_path", request.worktree_path),
        )
        if not value
    ]
    request_id = request.concurrency_id or request.request_id
    if not request_id:
        missing.append("concurrency_id or request_id")
    if missing:
        raise WorktreePlanningError("worktree request is missing " + ", ".join(missing))
    return request.repository_id, request.source_root, request.worktree_path, request_id


def _validate_scope(request: WorktreeRequest, snapshot: RepositorySnapshot, policy: WorktreePolicy) -> list[str]:
    repository_id, source_root, worktree_path, _ = _require_request_fields(request)
    _validate_absolute_chain(snapshot.source_root_chain, snapshot.source_root, label="source root")
    if snapshot.root_kind != "directory":
        raise WorktreePlanningError("repository source root must be a real directory")
    if snapshot.base_commit_verified is not True:
        raise WorktreePlanningError("repository base commit lacks a verified attestation")
    if not snapshot.inventory_complete:
        raise WorktreePlanningError("repository inventory is incomplete")
    if snapshot.repository_id != repository_id:
        raise WorktreePlanningError("request repository identity does not match repository snapshot")
    if snapshot.source_root != source_root:
        raise WorktreePlanningError("request source root does not match repository snapshot")
    matching = [item for item in policy.repository_allowlist if item.repository_id == repository_id]
    if not matching:
        raise WorktreePlanningError("repository identity is not in the explicit allowlist")
    if all(item.source_root != source_root for item in matching):
        raise WorktreePlanningError("repository source root is not in the explicit allowlist")
    if not _FULL_COMMIT_PATTERN.fullmatch(request.base_commit):
        raise WorktreePlanningError("base_commit must be a full lowercase SHA-1 or SHA-256")
    if snapshot.base_commit != request.base_commit:
        raise WorktreePlanningError("requested base_commit does not match the inspected repository revision")
    if not _is_under(worktree_path, policy.worktree_root) or worktree_path == policy.worktree_root:
        raise WorktreePlanningError("worktree path is outside the allowed worktree root")
    if _is_under(worktree_path, source_root) or _is_under(source_root, worktree_path):
        raise WorktreePlanningError("source and worktree paths must not overlap")
    paths = sorted(request.path_allowlist)
    for path in paths:
        if path == ".git" or path.startswith(".git/"):
            raise WorktreePlanningError(".git paths are never allowed in a worktree scope")
        if path == ".gitmodules" or path.startswith(".gitmodules/"):
            raise WorktreePlanningError(".gitmodules paths are never allowed in a worktree scope")
    for previous, current in zip(paths, paths[1:]):
        if current.startswith(previous + "/"):
            raise WorktreePlanningError("path allowlist contains redundant nested scopes")
    boundary_entries = [
        entry
        for entry in snapshot.entries
        if entry.kind in {RepositoryEntryKind.SYMLINK, RepositoryEntryKind.SUBMODULE}
        and any(
            _scope_contains(scope, entry.path) or _scope_contains(entry.path, scope)
            for scope in paths
        )
    ]
    if boundary_entries:
        detail = ", ".join(f"{entry.kind.value}:{entry.path}" for entry in boundary_entries)
        raise WorktreePlanningError("path allowlist crosses a symlink/submodule boundary: " + detail)
    file_count = sum(
        entry.kind is RepositoryEntryKind.FILE
        and any(_scope_contains(scope, entry.path) for scope in paths)
        for entry in snapshot.entries
    )
    if file_count > request.max_files:
        raise WorktreePlanningError(f"path allowlist selects {file_count} files, above max_files")
    return paths


def _make_operation(
    kind: WorktreeOperationKind,
    source_root: str,
    argv: list[str],
    *,
    approved_paths: list[str] | None = None,
    precondition: WorktreeRemovalPrecondition | None = None,
    requires_confirmation: bool = False,
) -> GitOperation:
    return GitOperation(
        kind=kind,
        cwd=source_root,
        argv=argv,
        approved_paths=approved_paths or [],
        precondition=precondition,
        requires_confirmation=requires_confirmation,
    )


def _action_plan(
    plan: WorktreePlan,
    action: Literal["cleanup", "recovery"],
    reason: str,
    *,
    receipt: WorktreeCreationReceipt | None,
    observation: WorktreePorcelainAttestation | None,
) -> WorktreeActionPlan:
    plan = _rehydrate_plan(plan)
    inspect = _make_operation(
        WorktreeOperationKind.INSPECT_WORKTREES,
        plan.source_root,
        ["git", "worktree", "list", "--porcelain"],
    )
    if receipt is None or observation is None:
        if action == "cleanup":
            raise WorktreePlanningError("cleanup requires a creation receipt and fresh porcelain observation")
        return WorktreeActionPlan(
            action=action,
            disposition="manual_review_required",
            parent_plan_hash=plan.plan_hash,
            repository_id=plan.repository_id,
            concurrency_key=plan.concurrency_key,
            source_root=plan.source_root,
            worktree_path=plan.worktree_path,
            base_commit=plan.base_commit,
            request_id=plan.request_id,
            operations=[inspect],
            reason=(reason + "; manual_review_required: complete creation receipt and fresh porcelain are required")[
                :_MAX_REASON_LENGTH
            ],
        )
    receipt, observation = _validate_receipt_observation_for_plan(plan, receipt, observation)
    precondition = WorktreeRemovalPrecondition(
        repository_id=plan.repository_id,
        source_root=plan.source_root,
        worktree_path=plan.worktree_path,
        expected_base_commit=plan.base_commit,
        expected_head=receipt.head_commit,
        request_id=plan.request_id,
        concurrency_key=plan.concurrency_key,
        parent_plan_hash=plan.plan_hash,
    )
    remove = _make_operation(
        WorktreeOperationKind.REMOVE_WORKTREE,
        plan.source_root,
        ["git", "worktree", "remove", "--", plan.worktree_path],
        approved_paths=[plan.worktree_path],
        requires_confirmation=True,
        precondition=precondition,
    )
    return WorktreeActionPlan(
        action=action,
        disposition="remove_ready",
        parent_plan_hash=plan.plan_hash,
        repository_id=plan.repository_id,
        concurrency_key=plan.concurrency_key,
        source_root=plan.source_root,
        worktree_path=plan.worktree_path,
        base_commit=plan.base_commit,
        request_id=plan.request_id,
        creation_receipt=receipt,
        porcelain_observation=observation,
        remove_precondition=precondition,
        operations=[inspect, remove],
        reason=reason,
    )


def plan_worktree(
    request: WorktreeRequest,
    snapshot: RepositorySnapshot,
    policy: WorktreePolicy,
    destination: WorktreeDestination,
) -> WorktreePlan:
    """Validate a request and return deterministic create operations."""

    repository_id, source_root, worktree_path, request_id = _require_request_fields(request)
    paths = _validate_scope(request, snapshot, policy)
    _validate_destination(destination, worktree_root=policy.worktree_root, worktree_path=worktree_path)
    concurrency_key = _resource_concurrency_key(repository_id, source_root, worktree_path)
    return WorktreePlan(
        request=request,
        snapshot=snapshot,
        policy=policy,
        destination=destination,
        repository_id=repository_id,
        source_root=source_root,
        worktree_path=worktree_path,
        base_commit=request.base_commit,
        request_id=request_id,
        concurrency_key=concurrency_key,
        allowed_paths=paths,
        operations=[
            _make_operation(
                WorktreeOperationKind.VERIFY_BASE_COMMIT,
                source_root,
                ["git", "rev-parse", "--verify", "--end-of-options", f"{request.base_commit}^{{commit}}"],
            ),
            _make_operation(
                WorktreeOperationKind.INSPECT_WORKTREES,
                source_root,
                ["git", "worktree", "list", "--porcelain"],
            ),
            _make_operation(
                WorktreeOperationKind.CREATE_WORKTREE,
                source_root,
                ["git", "worktree", "add", "--detach", worktree_path, request.base_commit],
                approved_paths=[worktree_path],
            ),
        ],
    )


def plan_cleanup(
    plan: WorktreePlan,
    receipt: WorktreeCreationReceipt,
    observation: WorktreePorcelainAttestation,
    *,
    reason: str = "planned worktree cleanup",
) -> WorktreeActionPlan:
    """Return a remove plan only after receipt-backed observation checks."""

    return _action_plan(plan, "cleanup", reason, receipt=receipt, observation=observation)


def plan_recovery(
    plan: WorktreePlan,
    receipt: WorktreeCreationReceipt | None = None,
    observation: WorktreePorcelainAttestation | None = None,
    *,
    reason: str = "recover an interrupted worktree operation",
) -> WorktreeActionPlan:
    """Return inspect-only recovery unless both evidence objects exist."""

    return _action_plan(plan, "recovery", reason, receipt=receipt, observation=observation)


__all__ = [
    "AbsolutePathEntry",
    "GitOperation",
    "RepositoryBoundary",
    "RepositoryEntry",
    "RepositoryEntryKind",
    "RepositorySnapshot",
    "WorktreeActionPlan",
    "WorktreeCreationReceipt",
    "WorktreeDestination",
    "WorktreeManager",
    "WorktreeOperationKind",
    "WorktreePlan",
    "WorktreePlanningError",
    "WorktreePolicy",
    "WorktreePorcelainAttestation",
    "WorktreeRemovalPrecondition",
    "plan_cleanup",
    "plan_recovery",
    "plan_worktree",
    "rehydrate_worktree_plan",
    "rehydrate_action_plan",
]
