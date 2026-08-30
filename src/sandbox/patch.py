"""Fail-closed patch validation and check planning.

This module deliberately has no Git, Docker, or filesystem implementation.  It
turns a candidate unified diff into an immutable description that a later,
isolated executor may inspect and run through ``git apply --check`` and
``git diff --check``.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal, Mapping, Protocol

from pydantic import Field, field_validator, model_validator

from src.models.contracts import ContractModel
from src.sandbox.contracts import BoundedArgv
from src.sandbox.worktree_contracts import (
    RepositoryEntryKind,
    RepositorySnapshot,
    WorktreePlanningError,
)
from src.sandbox.worktree import WorktreePlan
from src.sandbox.worktree import rehydrate_worktree_plan


_PATCH_MAX_TEXT = 500_000
_PATH_MAX = 4_096
_SHELL_META = re.compile(r"[;&|`$<>\r\n]")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _diff_paths(text: str) -> tuple[str, ...]:
    """Extract declared unified-diff paths without interpreting patch content."""
    found: list[str] = []
    for line in text.splitlines():
        if line.startswith("@@"):
            break
        if line.startswith(("--- ", "+++ ")):
            token = line[4:].split("\t", 1)[0].split(" ", 1)[0]
            if token == "/dev/null":
                continue
            if token.startswith(("a/", "b/")):
                token = token[2:]
            found.append(token)
    return tuple(found)


def _path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _PATH_MAX:
        raise ValueError(f"{field} must be a bounded non-empty path")
    if value.startswith(("/", "~")) or "\\" in value or "\x00" in value or _SHELL_META.search(value):
        raise ValueError(f"{field} must be a safe relative path")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field} must not contain whitespace")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} contains a traversal segment")
    if value == ".git" or value.startswith(".git/") or value == ".gitmodules" or value.startswith(".gitmodules/"):
        raise ValueError(f"{field} may not address Git metadata")
    return value


def _absolute_path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _PATH_MAX:
        raise ValueError(f"{field} must be a bounded absolute path")
    if not value.startswith("/") or value == "/" or _WINDOWS_ABSOLUTE.match(value):
        raise ValueError(f"{field} must be a non-root absolute path")
    if "\\" in value or "\x00" in value or _SHELL_META.search(value):
        raise ValueError(f"{field} contains forbidden path syntax")
    if any(part in {"", ".", ".."} for part in value.split("/")[1:]):
        raise ValueError(f"{field} contains an unsafe path segment")
    return value


def _rehydrate(value, model_type, *, label: str):
    """Round-trip a contract and reject stale hashes or non-canonical objects."""
    try:
        canonical = _canonical(value.model_dump(mode="json", by_alias=True))
        hydrated = model_type.model_validate(json.loads(canonical))
        if _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
            raise ValueError("non-canonical")
        return hydrated
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorktreePlanningError(f"{label} failed integrity rehydration") from exc


class PatchChangeKind(StrEnum):
    ADD = "add"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"
    BINARY = "binary"


class PatchLimits(ContractModel):
    """Explicit resource and feature policy; restrictive defaults are intentional."""

    max_files: int = Field(default=50, ge=1, le=1_000)
    max_bytes: int = Field(default=500_000, ge=1, le=10_000_000)
    max_file_bytes: int = Field(default=100_000, ge=1, le=2_000_000)
    allow_deletes: bool = False
    allow_renames: bool = False
    allow_binary: bool = False
    allow_fuzzy: bool = False


class PatchEntry(ContractModel):
    """One file's unified diff and its explicitly declared change boundary."""

    security_fields = ("path", "old_path", "new_path", "patch")

    path: str = Field(min_length=1, max_length=_PATH_MAX)
    patch: str = Field(min_length=1, max_length=_PATCH_MAX_TEXT)
    kind: PatchChangeKind = PatchChangeKind.MODIFY
    old_path: str | None = Field(default=None, max_length=_PATH_MAX)
    new_path: str | None = Field(default=None, max_length=_PATH_MAX)
    binary: bool = False

    @field_validator("path", "old_path", "new_path")
    @classmethod
    def safe_paths(cls, value: str | None, info) -> str | None:
        return None if value is None else _path(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_shape(self) -> PatchEntry:
        if self.kind is PatchChangeKind.RENAME and (not self.old_path or not self.new_path):
            raise ValueError("rename patches require old_path and new_path")
        if self.kind is PatchChangeKind.DELETE and self.new_path is not None:
            raise ValueError("delete patches cannot provide new_path")
        if self.kind is PatchChangeKind.ADD and self.old_path is not None:
            raise ValueError("add patches cannot provide old_path")
        if self.kind is PatchChangeKind.BINARY or self.binary:
            if not self.binary:
                raise ValueError("binary kind must set binary=true")
        if self.kind is PatchChangeKind.RENAME and self.old_path == self.new_path:
            raise ValueError("rename old_path and new_path must differ")
        declared = _diff_paths(self.patch)
        if self.kind is not PatchChangeKind.BINARY and (
            not any(line.startswith("--- ") for line in self.patch.splitlines())
            or not any(line.startswith("+++ ") for line in self.patch.splitlines())
        ):
            raise ValueError("non-binary patch requires unified diff --- and +++ headers")
        if declared:
            expected = {self.path}
            if self.old_path:
                expected.add(self.old_path)
            if self.new_path:
                expected.add(self.new_path)
            if set(declared) != expected:
                raise ValueError("unified diff paths do not match the patch entry")
        if "GIT binary patch" in self.patch or "Binary files " in self.patch:
            if self.kind is not PatchChangeKind.BINARY and not self.binary:
                raise ValueError("binary patch must be explicitly declared")
        if "rename from " in self.patch or "rename to " in self.patch:
            if self.kind is not PatchChangeKind.RENAME:
                raise ValueError("rename metadata must be explicitly declared")
        if "new file mode " in self.patch and self.kind is not PatchChangeKind.ADD:
            raise ValueError("new file metadata requires an add patch")
        if "deleted file mode " in self.patch and self.kind is not PatchChangeKind.DELETE:
            raise ValueError("deleted file metadata requires a delete patch")
        if not self.patch.strip():
            raise ValueError("patch content must not be empty")
        return self


class PatchBundle(ContractModel):
    """Candidate patch payload bound to one exact clean-base revision."""

    security_fields = ("base_commit", "files")

    schema_: Literal["patch-bundle/v1"] = Field(default="patch-bundle/v1", alias="schema", serialization_alias="schema")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    files: list[PatchEntry] = Field(min_length=1, max_length=1_000)
    fuzzy: bool = False
    patch_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def derive_hash(self) -> PatchBundle:
        material = [{"path": item.path, "patch": item.patch, "kind": item.kind.value,
                     "old_path": item.old_path, "new_path": item.new_path, "binary": item.binary}
                    for item in self.files]
        expected = "sha256:" + hashlib.sha256(_canonical(material).encode()).hexdigest()
        if self.patch_hash is not None and self.patch_hash != expected:
            raise ValueError("patch_hash does not match patch contents")
        self.patch_hash = expected
        return self


class PatchCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    REJECTED = "rejected"


class PatchValidation(ContractModel):
    """Validated patch metadata, including the exact paths and byte counts."""

    schema_: Literal["patch-validation/v1"] = Field(default="patch-validation/v1", alias="schema", serialization_alias="schema")
    patch_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    paths: list[str] = Field(min_length=1, max_length=1_000)
    file_count: int = Field(ge=1)
    byte_count: int = Field(ge=1)
    fuzzy: bool = False


class PatchCheckRequest(ContractModel):
    """Transport request; no operation in this protocol mutates a checkout."""

    schema_: Literal["patch-check-request/v1"] = Field(default="patch-check-request/v1", alias="schema", serialization_alias="schema")
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    patch_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    worktree_path: str = Field(min_length=1, max_length=_PATH_MAX)
    cwd: str = Field(min_length=1, max_length=_PATH_MAX)
    apply_check: BoundedArgv
    diff_check: BoundedArgv
    clean_base: bool = True
    retry_index: int = Field(default=0, ge=0, le=1)
    request_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("worktree_path", "cwd")
    @classmethod
    def safe_absolute_paths(cls, value: str, info) -> str:
        return _absolute_path(value, field=info.field_name)

    @model_validator(mode="after")
    def derive_digest(self) -> PatchCheckRequest:
        material = self.model_dump(mode="json", exclude={"request_digest"})
        expected = "sha256:" + hashlib.sha256(_canonical(material).encode()).hexdigest()
        if self.request_digest is not None and self.request_digest != expected:
            raise ValueError("request_digest does not match request contents")
        self.request_digest = expected
        return self


class PatchCheckReceipt(ContractModel):
    """A checker result supplied by an isolated transport (usually a fixture in 3D)."""

    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: PatchCheckStatus
    apply_exit_code: int | None = Field(default=None, ge=0)
    diff_exit_code: int | None = Field(default=None, ge=0)
    stderr: str = Field(default="", max_length=20_000)
    stdout: str = Field(default="", max_length=20_000)
    reason: str = Field(default="", max_length=2_000)


class PatchPlan(ContractModel):
    """Deterministic validation/check plan; it contains no apply operation."""

    schema_: Literal["patch-plan/v1"] = Field(default="patch-plan/v1", alias="schema", serialization_alias="schema")
    worktree_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    worktree_path: str = Field(min_length=1, max_length=_PATH_MAX)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    validation: PatchValidation
    request: PatchCheckRequest
    limits: PatchLimits
    status: PatchCheckStatus = PatchCheckStatus.REJECTED
    reason: str = Field(default="", max_length=2_000)
    plan_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("worktree_path")
    @classmethod
    def safe_worktree_path(cls, value: str) -> str:
        return _absolute_path(value, field="worktree_path")

    @model_validator(mode="after")
    def derive_hash(self) -> PatchPlan:
        if (
            self.request.plan_hash != self.worktree_plan_hash
            or self.request.base_commit != self.base_commit
            or self.request.worktree_path != self.request.cwd
            or self.request.worktree_path != self.worktree_path
        ):
            raise ValueError("check request is not bound to the worktree plan")
        material = self.model_dump(mode="json", exclude={"plan_hash"})
        expected = "sha256:" + hashlib.sha256(_canonical(material).encode()).hexdigest()
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("plan_hash does not match plan contents")
        self.plan_hash = expected
        return self


class PatchChecker(Protocol):
    def check(self, request: PatchCheckRequest, patch: PatchBundle) -> PatchCheckReceipt: ...


class FakePatchChecker:
    """Deterministic injected checker used by protocol tests; never applies patches."""

    def __init__(self, fixtures: Mapping[str, PatchCheckReceipt] | None = None,
                 *, default: PatchCheckReceipt | None = None) -> None:
        self.fixtures = dict(fixtures or {})
        self.default = default or PatchCheckReceipt(request_digest="sha256:" + "0" * 64,
                                                     status=PatchCheckStatus.PASSED)
        self.requests: list[str] = []

    def check(self, request: PatchCheckRequest, patch: PatchBundle) -> PatchCheckReceipt:
        self.requests.append(request.request_digest or "")
        value = self.fixtures.get(request.request_digest or "", self.default).model_copy(deep=True)
        if value.request_digest != request.request_digest:
            payload = value.model_dump(mode="json")
            payload["request_digest"] = request.request_digest or ""
            value = PatchCheckReceipt.model_validate(payload)
        return value


def _validate_entries(bundle: PatchBundle, worktree_plan: WorktreePlan, snapshot: RepositorySnapshot,
                     limits: PatchLimits) -> PatchValidation:
    if bundle.base_commit != worktree_plan.base_commit or bundle.base_commit != snapshot.base_commit:
        raise WorktreePlanningError("patch base_commit does not match worktree plan and snapshot")
    if snapshot.repository_id != worktree_plan.repository_id or snapshot.source_root != worktree_plan.source_root:
        raise WorktreePlanningError("patch snapshot identity does not match the worktree plan")
    if worktree_plan.plan_hash is None:
        raise WorktreePlanningError("worktree plan is missing its integrity hash")
    effective_max_files = min(limits.max_files, worktree_plan.request.max_files)
    if len(bundle.files) > effective_max_files:
        raise WorktreePlanningError("patch changes too many files")
    allow = worktree_plan.allowed_paths
    inventory = {
        (entry.path if hasattr(entry, "path") else entry["path"]):
        (entry.kind if hasattr(entry, "kind") else RepositoryEntryKind(entry["kind"]))
        for entry in snapshot.entries
    }
    total = 0
    paths: list[str] = []
    for entry in bundle.files:
        changed = [entry.path]
        if entry.kind is PatchChangeKind.RENAME:
            changed = [entry.old_path or "", entry.new_path or ""]
        if entry.kind is PatchChangeKind.DELETE:
            changed = [entry.path, entry.old_path] if entry.old_path else [entry.path]
        if entry.kind is PatchChangeKind.BINARY or entry.binary:
            if not limits.allow_binary:
                raise WorktreePlanningError("binary patches are disabled")
        if entry.kind is PatchChangeKind.RENAME and not limits.allow_renames:
            raise WorktreePlanningError("rename patches are disabled")
        if entry.kind is PatchChangeKind.DELETE and not limits.allow_deletes:
            raise WorktreePlanningError("delete patches are disabled")
        if entry.kind is PatchChangeKind.ADD and entry.path in inventory:
            raise WorktreePlanningError(f"add patch target already exists: {entry.path}")
        if entry.kind is PatchChangeKind.DELETE:
            source = entry.old_path or entry.path
            if inventory.get(source) is not RepositoryEntryKind.FILE:
                raise WorktreePlanningError(f"delete patch source is not a regular file: {source}")
        if entry.kind is PatchChangeKind.RENAME:
            source = entry.old_path or ""
            target = entry.new_path or ""
            if inventory.get(source) is not RepositoryEntryKind.FILE:
                raise WorktreePlanningError(f"rename patch source is not a regular file: {source}")
            if target in inventory:
                raise WorktreePlanningError(f"rename patch target already exists: {target}")
        for changed_path in changed:
            if not changed_path or not any(changed_path == scope or changed_path.startswith(scope + "/") for scope in allow):
                raise WorktreePlanningError(f"patch path is outside the worktree allowlist: {changed_path}")
            kind = inventory.get(changed_path)
            for boundary_path, boundary_kind in inventory.items():
                if boundary_kind in {RepositoryEntryKind.SYMLINK, RepositoryEntryKind.SUBMODULE} and (
                    changed_path == boundary_path or changed_path.startswith(boundary_path + "/")
                ):
                    raise WorktreePlanningError(f"patch crosses a {boundary_kind.value} boundary: {changed_path}")
            if entry.kind is PatchChangeKind.MODIFY and kind is None:
                raise WorktreePlanningError(f"modify patch targets an untracked path: {changed_path}")
            if entry.kind is PatchChangeKind.MODIFY and kind is not RepositoryEntryKind.FILE:
                raise WorktreePlanningError(f"modify patch requires a regular file: {changed_path}")
            if changed_path == ".git" or changed_path.startswith(".git/") or changed_path == ".gitmodules" or changed_path.startswith(".gitmodules/"):
                raise WorktreePlanningError("patch may not modify Git metadata")
            paths.append(changed_path)
        size = len(entry.patch.encode("utf-8"))
        if size > limits.max_file_bytes:
            raise WorktreePlanningError("one patch file exceeds byte limit")
        total += size
    if len(set(paths)) > effective_max_files:
        raise WorktreePlanningError("patch changes too many distinct files")
    if total > limits.max_bytes:
        raise WorktreePlanningError("patch exceeds total byte limit")
    return PatchValidation(patch_hash=bundle.patch_hash or "", base_commit=bundle.base_commit,
                           paths=sorted(set(paths)), file_count=len(set(paths)), byte_count=total,
                           fuzzy=bundle.fuzzy)


def plan_patch(bundle: PatchBundle, worktree_plan: WorktreePlan, snapshot: RepositorySnapshot | None = None,
               limits: PatchLimits | None = None) -> PatchPlan:
    """Validate a patch and create deterministic check operations only."""
    worktree_plan = rehydrate_worktree_plan(worktree_plan)
    bundle = _rehydrate(bundle, PatchBundle, label="patch bundle")
    limits = _rehydrate(limits or PatchLimits(), PatchLimits, label="patch limits")
    if snapshot is None:
        snapshot = worktree_plan.snapshot
    snapshot = _rehydrate(snapshot, RepositorySnapshot, label="repository snapshot")
    if bundle.fuzzy and not limits.allow_fuzzy:
        raise WorktreePlanningError("fuzzy patch replacement is disabled")
    validation = _validate_entries(bundle, worktree_plan, snapshot, limits)
    apply = BoundedArgv(command="git", args=["apply", "--check", "--whitespace=error-all", "-"])
    diff = BoundedArgv(command="git", args=["diff", "--check", "--"])
    request = PatchCheckRequest(plan_hash=worktree_plan.plan_hash or "", patch_hash=validation.patch_hash,
                               base_commit=bundle.base_commit, worktree_path=worktree_plan.worktree_path,
                               cwd=worktree_plan.worktree_path, apply_check=apply, diff_check=diff)
    return PatchPlan(worktree_plan_hash=worktree_plan.plan_hash or "", worktree_path=worktree_plan.worktree_path,
                     base_commit=bundle.base_commit,
                     validation=validation, request=request, limits=limits,
                     status=PatchCheckStatus.PASSED)


def validate_patch(bundle: PatchBundle, worktree_plan: WorktreePlan,
                   snapshot: RepositorySnapshot | None = None,
                   limits: PatchLimits | None = None) -> PatchValidation:
    """Validate and return patch metadata without producing checker operations."""
    worktree_plan = rehydrate_worktree_plan(worktree_plan)
    bundle = _rehydrate(bundle, PatchBundle, label="patch bundle")
    snapshot = _rehydrate(snapshot or worktree_plan.snapshot, RepositorySnapshot, label="repository snapshot")
    limits = _rehydrate(limits or PatchLimits(), PatchLimits, label="patch limits")
    if bundle.fuzzy and not limits.allow_fuzzy:
        raise WorktreePlanningError("fuzzy patch replacement is disabled")
    return _validate_entries(bundle, worktree_plan, snapshot, limits)


def plan_patch_retry(plan: PatchPlan, bundle: PatchBundle, *, retry_index: int = 1) -> PatchPlan:
    """Generate at most one clean-base retry description; never executes apply."""
    plan = _rehydrate(plan, PatchPlan, label="patch plan")
    bundle = _rehydrate(bundle, PatchBundle, label="patch bundle")
    if retry_index != 1 or plan.request.retry_index != 0:
        raise WorktreePlanningError("patch self-repair permits exactly one retry")
    if bundle.base_commit != plan.base_commit or (bundle.patch_hash or "") != plan.validation.patch_hash:
        raise WorktreePlanningError("retry patch does not match the original plan")
    request = PatchCheckRequest(plan_hash=plan.worktree_plan_hash, patch_hash=plan.validation.patch_hash,
                               base_commit=plan.base_commit, worktree_path=plan.worktree_path, cwd=plan.worktree_path,
                               apply_check=plan.request.apply_check,
                               diff_check=plan.request.diff_check, clean_base=True, retry_index=1)
    return PatchPlan(worktree_plan_hash=plan.worktree_plan_hash, worktree_path=plan.worktree_path,
                     base_commit=plan.base_commit,
                     validation=plan.validation, request=request, limits=plan.limits,
                     status=PatchCheckStatus.PASSED, reason="retry from clean base")


def check_patch(plan: PatchPlan, bundle: PatchBundle, checker: PatchChecker) -> PatchPlan:
    """Consume an injected receipt and return a status; checker failures fail closed."""
    plan = _rehydrate(plan, PatchPlan, label="patch plan")
    bundle = _rehydrate(bundle, PatchBundle, label="patch bundle")
    if plan.request.patch_hash != bundle.patch_hash:
        raise WorktreePlanningError("patch payload does not match check plan")
    try:
        raw_receipt = checker.check(plan.request, bundle)
    except Exception:
        return _patch_result(plan, PatchCheckStatus.REJECTED, "checker failure")
    if not isinstance(raw_receipt, PatchCheckReceipt):
        return _patch_result(plan, PatchCheckStatus.REJECTED, "checker returned an invalid receipt")
    try:
        receipt = _rehydrate(raw_receipt, PatchCheckReceipt, label="checker receipt")
    except WorktreePlanningError:
        return _patch_result(plan, PatchCheckStatus.REJECTED, "checker returned an invalid receipt")
    if receipt.request_digest != plan.request.request_digest:
        return _patch_result(plan, PatchCheckStatus.REJECTED, "checker receipt is not bound to the request")
    status = receipt.status
    reason = receipt.reason
    if receipt.apply_exit_code not in (None, 0) or receipt.diff_exit_code not in (None, 0):
        status = PatchCheckStatus.FAILED
        reason = reason or "patch check command failed"
    return _patch_result(plan, status, reason)


def _patch_result(plan: PatchPlan, status: PatchCheckStatus, reason: str) -> PatchPlan:
    payload = plan.model_dump(mode="json", by_alias=True)
    payload.update({"status": status.value, "reason": reason, "plan_hash": None})
    return PatchPlan.model_validate(payload)


__all__ = [
    "FakePatchChecker", "PatchBundle", "PatchChangeKind", "PatchCheckReceipt", "PatchCheckRequest",
    "PatchCheckStatus", "PatchChecker", "PatchEntry", "PatchLimits", "PatchPlan", "PatchValidation",
    "check_patch", "plan_patch", "plan_patch_retry", "validate_patch",
]
