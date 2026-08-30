"""Foundational contracts for the pure Phase 3 worktree planner."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from src.models.contracts import ContractModel
from src.sandbox.contracts import WorktreeRequest


_FULL_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHELL_META = re.compile(r"[;&|`$<>\r\n]")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")
_MAX_ENTRIES = 100_000
_MAX_OPERATIONS = 16
_MAX_PATH_LENGTH = 4_096
_MAX_REASON_LENGTH = 1_000


class WorktreePlanningError(ValueError):
    """Raised when a worktree request cannot be safely planned."""


class RepositoryEntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"


class WorktreeOperationKind(StrEnum):
    VERIFY_BASE_COMMIT = "verify_base_commit"
    INSPECT_WORKTREES = "inspect_worktrees"
    CREATE_WORKTREE = "create_worktree"
    REMOVE_WORKTREE = "remove_worktree"


def _safe_identity(value: str, *, field_name: str, allow_repository_path: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\x00" in value or "\\" in value or _SHELL_META.search(value):
        raise ValueError(f"{field_name} contains forbidden shell syntax")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name} must be one identity token")
    if "/" in value and not allow_repository_path:
        raise ValueError(f"{field_name} must be one identity token")
    if allow_repository_path and any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError(f"{field_name} contains an unsafe segment")
    if len(value) > 200:
        raise ValueError(f"{field_name} is too long")
    return value


def _safe_absolute_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > _MAX_PATH_LENGTH:
        raise ValueError(f"{field_name} is too long")
    if "\x00" in value or "\\" in value or _SHELL_META.search(value):
        raise ValueError(f"{field_name} contains forbidden path syntax")
    if not value.startswith("/") or value == "/" or _WINDOWS_ABSOLUTE.match(value):
        raise ValueError(f"{field_name} must be a non-root POSIX absolute path")
    parts = value.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} contains an unsafe path segment")
    return value


def _safe_relative_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > _MAX_PATH_LENGTH:
        raise ValueError(f"{field_name} is too long")
    if "\x00" in value or "\\" in value or _SHELL_META.search(value):
        raise ValueError(f"{field_name} contains forbidden path syntax")
    if value.startswith(("/", "~")) or _WINDOWS_ABSOLUTE.match(value):
        raise ValueError(f"{field_name} must be relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} contains an unsafe path segment")
    return value


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _scope_contains(scope: str, path: str) -> bool:
    return path == scope or path.startswith(scope + "/")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _resource_concurrency_key(repository_id: str, source_root: str, worktree_path: str) -> str:
    """Lock the destination resource, independent of request audit metadata."""

    material = {
        "repository_id": repository_id,
        "source_root": source_root,
        "worktree_path": worktree_path,
    }
    return "worktree:sha256:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


class RepositoryEntry(ContractModel):
    """One tracked path from a caller-supplied repository inventory."""

    path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    kind: RepositoryEntryKind

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _safe_relative_path(value, field_name="repository entry path")


class AbsolutePathEntry(ContractModel):
    """One no-follow observation in an absolute path chain."""

    security_fields = ("path",)

    path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    kind: Literal["directory", "file", "symlink", "submodule"]

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _safe_absolute_path(value, field_name="absolute chain path")


def _absolute_path_chain(path: str) -> list[str]:
    """Return every non-root component from /srv through ``path``."""

    current = ""
    chain: list[str] = []
    for component in path.split("/")[1:]:
        current += "/" + component
        chain.append(current)
    return chain


def _validate_absolute_chain(
    entries: list[AbsolutePathEntry],
    path: str,
    *,
    label: str,
) -> None:
    expected = _absolute_path_chain(path)
    actual = [entry.path for entry in entries]
    if actual != expected:
        raise WorktreePlanningError(f"{label} attestation is not the exact root-to-path chain")
    if any(entry.kind != "directory" for entry in entries):
        raise WorktreePlanningError(f"{label} attestation crosses a non-directory boundary")


class RepositorySnapshot(ContractModel):
    """Read-only attestation for one exact repository revision and path chain."""

    security_fields = ("repository_id", "source_root", "source_root_chain", "base_commit", "entries")

    attestation: Literal["repository-snapshot/v1"] = "repository-snapshot/v1"
    repository_id: str = Field(min_length=1, max_length=200)
    source_root: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    source_root_chain: list[AbsolutePathEntry] = Field(min_length=1, max_length=100)
    base_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    entries: list[RepositoryEntry] = Field(default_factory=list, max_length=_MAX_ENTRIES)
    base_commit_verified: Literal[True] = True
    inventory_complete: bool = True
    root_kind: Literal["directory"] = "directory"

    @field_validator("repository_id")
    @classmethod
    def safe_repository_id(cls, value: str) -> str:
        return _safe_identity(value, field_name="repository_id", allow_repository_path=True)

    @field_validator("source_root")
    @classmethod
    def safe_source_root(cls, value: str) -> str:
        return _safe_absolute_path(value, field_name="source_root")

    @field_validator("entries")
    @classmethod
    def unique_entries(cls, values: list[RepositoryEntry]) -> list[RepositoryEntry]:
        paths = [entry.path for entry in values]
        if len(paths) != len(set(paths)):
            raise ValueError("repository inventory contains duplicate paths")
        return values

    @model_validator(mode="after")
    def inventory_boundaries(self) -> RepositorySnapshot:
        _validate_absolute_chain(self.source_root_chain, self.source_root, label="source root")
        if not self.inventory_complete:
            return self
        entries = sorted(self.entries, key=lambda entry: entry.path)
        for index, entry in enumerate(entries):
            if entry.kind not in {RepositoryEntryKind.SYMLINK, RepositoryEntryKind.SUBMODULE}:
                continue
            prefix = entry.path + "/"
            if any(candidate.path.startswith(prefix) for candidate in entries[index + 1 :]):
                raise ValueError(f"repository inventory crosses {entry.kind.value} boundary at {entry.path}")
        return self


class RepositoryBoundary(ContractModel):
    """One explicitly allowed repository identity and source root."""

    security_fields = ("repository_id", "source_root")

    repository_id: str = Field(min_length=1, max_length=200)
    source_root: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)

    @field_validator("repository_id")
    @classmethod
    def safe_repository_id(cls, value: str) -> str:
        return _safe_identity(value, field_name="repository_id", allow_repository_path=True)

    @field_validator("source_root")
    @classmethod
    def safe_source_root(cls, value: str) -> str:
        return _safe_absolute_path(value, field_name="source_root")


class WorktreePolicy(ContractModel):
    """Default-deny repository and destination boundaries for the planner."""

    security_fields = ("worktree_root", "repository_allowlist")

    repository_allowlist: list[RepositoryBoundary] = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("repository_allowlist", "allowed_repositories", "repositories"),
    )
    worktree_root: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)

    @field_validator("worktree_root")
    @classmethod
    def safe_worktree_root(cls, value: str) -> str:
        return _safe_absolute_path(value, field_name="worktree_root")

    @model_validator(mode="after")
    def unique_repositories(self) -> WorktreePolicy:
        repository_ids = [item.repository_id for item in self.repository_allowlist]
        if len(repository_ids) != len(set(repository_ids)):
            raise ValueError("repository allowlist contains duplicate identities")
        return self


class WorktreeDestination(ContractModel):
    """Destination plus complete no-follow parent-chain planning evidence.

    The Phase 3C executor must re-inspect the chain with no-follow ``lstat``
    immediately before every operation; this object is not mutation authority.
    """

    security_fields = ("path", "parent_chain")

    attestation: Literal["worktree-destination/v1"] = "worktree-destination/v1"
    path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    kind: Literal["absent", "directory", "file", "symlink", "submodule"] = "absent"
    parent_chain: list[AbsolutePathEntry] = Field(min_length=1, max_length=100)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _safe_absolute_path(value, field_name="destination path")

    @field_validator("parent_chain")
    @classmethod
    def unique_parent_paths(cls, values: list[AbsolutePathEntry]) -> list[AbsolutePathEntry]:
        paths = [entry.path for entry in values]
        if len(paths) != len(set(paths)):
            raise ValueError("destination parent chain contains duplicate paths")
        return values


def _expected_parent_chain(worktree_root: str, destination_path: str) -> list[str]:
    """Return every absolute path component through destination's parent."""

    parent = destination_path.rsplit("/", 1)[0]
    return _absolute_path_chain(parent) if _is_under(parent, worktree_root) else []


def _validate_destination(
    destination: WorktreeDestination,
    *,
    worktree_root: str,
    worktree_path: str,
) -> None:
    """Validate exact no-follow parent evidence before planning any Git op."""

    if destination.path != worktree_path:
        raise WorktreePlanningError("destination attestation path does not match the request")
    if destination.kind != "absent":
        raise WorktreePlanningError("worktree destination must be absent before creation")
    expected_paths = _expected_parent_chain(worktree_root, worktree_path)
    if not expected_paths:
        raise WorktreePlanningError("worktree path is not under the allowed worktree root")
    _validate_absolute_chain(destination.parent_chain, worktree_path.rsplit("/", 1)[0], label="destination parent")


class WorktreeRemovalPrecondition(ContractModel):
    """Expected receipt-backed facts required before one worktree remove."""

    security_fields = (
        "repository_id",
        "source_root",
        "worktree_path",
        "expected_base_commit",
        "expected_head",
        "request_id",
        "concurrency_key",
        "parent_plan_hash",
    )

    repository_id: str = Field(min_length=1, max_length=200)
    source_root: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    worktree_path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    expected_base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    expected_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    request_id: str = Field(min_length=1, max_length=200)
    concurrency_key: str = Field(pattern=r"^worktree:sha256:[0-9a-f]{64}$")
    parent_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("repository_id", "request_id")
    @classmethod
    def safe_identity(cls, value: str, info) -> str:
        return _safe_identity(
            value,
            field_name=info.field_name,
            allow_repository_path=info.field_name == "repository_id",
        )

    @field_validator("source_root", "worktree_path")
    @classmethod
    def safe_path(cls, value: str, info) -> str:
        return _safe_absolute_path(value, field_name=info.field_name)


class WorktreeCreationReceipt(ContractModel):
    """Receipt emitted after a create operation succeeds."""

    security_fields = (
        "parent_plan_hash",
        "create_operation_id",
        "repository_id",
        "source_root",
        "common_git_dir",
        "worktree_gitdir",
        "worktree_path",
        "base_commit",
        "head_commit",
        "receipt_digest",
    )

    schema_: Literal["worktree-creation-receipt/v1"] = Field(
        default="worktree-creation-receipt/v1", alias="schema", serialization_alias="schema"
    )
    parent_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    create_operation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    repository_id: str = Field(min_length=1, max_length=200)
    source_root: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    common_git_dir: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    worktree_gitdir: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    worktree_path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    success: Literal[True] = True
    receipt_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("repository_id")
    @classmethod
    def safe_repository_id(cls, value: str) -> str:
        return _safe_identity(value, field_name="repository_id", allow_repository_path=True)

    @field_validator("source_root", "common_git_dir", "worktree_gitdir", "worktree_path")
    @classmethod
    def safe_paths(cls, value: str, info) -> str:
        return _safe_absolute_path(value, field_name=info.field_name)

    @model_validator(mode="after")
    def derive_digest(self) -> WorktreeCreationReceipt:
        material = self.model_dump(mode="json", exclude={"receipt_digest"})
        expected = "sha256:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
        if self.receipt_digest is not None and self.receipt_digest != expected:
            raise ValueError("receipt_digest does not match receipt contents")
        self.receipt_digest = expected
        return self


class WorktreePorcelainAttestation(ContractModel):
    """Transport-backed observation of the worktree's current state.

    The transport digest and operation id must come from an authorized fresh
    observation.  There is intentionally no caller-controlled ``fresh=True``
    field; Phase 3C must obtain fresh porcelain and no-follow path evidence.
    """

    security_fields = (
        "parent_plan_hash",
        "create_operation_id",
        "repository_id",
        "source_root",
        "common_git_dir",
        "worktree_gitdir",
        "worktree_path",
        "base_commit",
        "head_commit",
        "request_id",
        "concurrency_key",
        "transport_receipt_digest",
        "transport_operation_id",
    )

    schema_: Literal["worktree-porcelain-attestation/v2"] = Field(
        default="worktree-porcelain-attestation/v2", alias="schema", serialization_alias="schema"
    )
    parent_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    create_operation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    repository_id: str = Field(min_length=1, max_length=200)
    source_root: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    common_git_dir: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    worktree_gitdir: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    worktree_path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    request_id: str = Field(min_length=1, max_length=200)
    concurrency_key: str = Field(pattern=r"^worktree:sha256:[0-9a-f]{64}$")
    transport_receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    transport_operation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("repository_id", "request_id")
    @classmethod
    def safe_identity(cls, value: str, info) -> str:
        return _safe_identity(
            value,
            field_name=info.field_name,
            allow_repository_path=info.field_name == "repository_id",
        )

    @field_validator("source_root", "common_git_dir", "worktree_gitdir", "worktree_path")
    @classmethod
    def safe_paths(cls, value: str, info) -> str:
        return _safe_absolute_path(value, field_name=info.field_name)


class GitOperation(ContractModel):
    """A bounded, shell-free Git argv plus its trusted path operands."""

    security_fields = ("cwd", "argv", "approved_paths", "precondition")

    kind: WorktreeOperationKind
    cwd: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    argv: list[str] = Field(min_length=2, max_length=32)
    approved_paths: list[str] = Field(default_factory=list, max_length=8)
    precondition: WorktreeRemovalPrecondition | None = None
    requires_confirmation: bool = False
    operation_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("cwd")
    @classmethod
    def safe_cwd(cls, value: str) -> str:
        return _safe_absolute_path(value, field_name="operation cwd")

    @field_validator("approved_paths")
    @classmethod
    def safe_approved_paths(cls, values: list[str]) -> list[str]:
        normalized = [_safe_absolute_path(value, field_name="approved path") for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("approved paths must not contain duplicates")
        return sorted(normalized)

    @model_validator(mode="after")
    def validate_argv_and_derive_id(self) -> GitOperation:
        if self.argv[0] != "git":
            raise ValueError("Git operation must invoke the git executable token")
        approved = set(self.approved_paths)
        for token in self.argv:
            if not isinstance(token, str) or not token or "\x00" in token or "\\" in token:
                raise ValueError("Git argv contains an invalid token")
            if len(token) > _MAX_PATH_LENGTH:
                raise ValueError("Git argv token is too long")
            if _SHELL_META.search(token):
                raise ValueError("Git argv contains shell syntax")
            if any(part in {".", ".."} for part in token.split("/")):
                raise ValueError("Git argv contains a traversal segment")
            if token.startswith("/") or _WINDOWS_ABSOLUTE.match(token):
                if _safe_absolute_path(token, field_name="Git argv path") not in approved:
                    raise ValueError("absolute Git argv paths must be explicitly approved")
        if self.kind is WorktreeOperationKind.REMOVE_WORKTREE:
            if not self.requires_confirmation:
                raise ValueError(f"{self.kind.value} requires explicit confirmation")
        if self.kind is WorktreeOperationKind.CREATE_WORKTREE and not self.approved_paths:
            raise ValueError("create operation requires an approved destination path")
        if self.kind is WorktreeOperationKind.REMOVE_WORKTREE:
            if self.precondition is None:
                raise ValueError("remove operation requires a receipt-backed precondition")
        elif self.precondition is not None:
            raise ValueError("only remove operations may carry a removal precondition")
        self._validate_command_shape(approved)
        material = {
            "kind": self.kind.value,
            "cwd": self.cwd,
            "argv": self.argv,
            "approved_paths": self.approved_paths,
            "precondition": self.precondition.model_dump(mode="json") if self.precondition else None,
            "requires_confirmation": self.requires_confirmation,
        }
        expected = "sha256:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
        if self.operation_id is not None and self.operation_id != expected:
            raise ValueError("operation_id does not match operation contents")
        self.operation_id = expected
        return self

    def _validate_command_shape(self, approved: set[str]) -> None:
        """Keep the protocol closed to the four modeled worktree commands."""

        if self.kind is WorktreeOperationKind.VERIFY_BASE_COMMIT:
            if len(self.argv) != 5 or self.argv[1:4] != ["rev-parse", "--verify", "--end-of-options"]:
                raise ValueError("verify operation has an invalid Git argv")
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\^\{commit\}", self.argv[4]):
                raise ValueError("verify operation must address one full base commit")
            if approved:
                raise ValueError("verify operation cannot have approved paths")
        elif self.kind is WorktreeOperationKind.INSPECT_WORKTREES:
            if self.argv != ["git", "worktree", "list", "--porcelain"] or approved:
                raise ValueError("inspect operation has an invalid Git argv")
        elif self.kind is WorktreeOperationKind.CREATE_WORKTREE:
            if (
                len(self.argv) != 6
                or self.argv[1:4] != ["worktree", "add", "--detach"]
                or len(approved) != 1
                or self.argv[4] not in approved
                or not _FULL_COMMIT_PATTERN.fullmatch(self.argv[5])
            ):
                raise ValueError("create operation has an invalid Git argv")
        elif self.kind is WorktreeOperationKind.REMOVE_WORKTREE:
            if len(self.argv) != 5 or self.argv[1:4] != ["worktree", "remove", "--"] or self.argv[4] not in approved:
                raise ValueError("remove operation has an invalid Git argv")
            if len(approved) != 1:
                raise ValueError("remove operation must have one approved path")


__all__ = [
    "AbsolutePathEntry",
    "GitOperation",
    "RepositoryBoundary",
    "RepositoryEntry",
    "RepositoryEntryKind",
    "RepositorySnapshot",
    "WorktreeCreationReceipt",
    "WorktreeDestination",
    "WorktreeOperationKind",
    "WorktreePlanningError",
    "WorktreePolicy",
    "WorktreePorcelainAttestation",
    "WorktreeRemovalPrecondition",
]
