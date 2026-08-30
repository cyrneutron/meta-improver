"""Fail-closed contracts for future isolated target execution."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from src.models.contracts import ContractModel


_BASE_COMMIT_PATTERN = r"^[0-9a-f]{7,64}$"
_MAX_ARG_LENGTH = 1_000
_MAX_ARG_COUNT = 64
_MAX_ALLOWLIST_COUNT = 100
_MAX_FILES = 1_000
_SHELL_META = re.compile(r"[;&|`$<>\r\n]")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")


def _validate_token(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\x00" in value or "\\" in value:
        raise ValueError(f"{field_name} contains a forbidden NUL or backslash")
    if _SHELL_META.search(value):
        raise ValueError(f"{field_name} contains shell syntax")
    return value


def _validate_relative_path(value: str, *, field_name: str) -> str:
    _validate_token(value, field_name=field_name)
    if value.startswith(("/", "~")) or _WINDOWS_ABSOLUTE.match(value):
        raise ValueError(f"{field_name} must be relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} contains an unsafe path segment")
    return value


class BoundedArgv(ContractModel):
    """A command and bounded argv list that cannot invoke a shell."""

    command: str = Field(min_length=1, max_length=_MAX_ARG_LENGTH)
    args: list[str] = Field(default_factory=list, max_length=_MAX_ARG_COUNT)

    @field_validator("command")
    @classmethod
    def safe_command(cls, value: str) -> str:
        value = _validate_token(value, field_name="command")
        if any(character.isspace() for character in value):
            raise ValueError("command must be one executable token, not a shell string")
        if value.startswith(("/", "~")) or _WINDOWS_ABSOLUTE.match(value):
            raise ValueError("command path must not be absolute")
        if "/" in value and any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("command path contains an unsafe segment")
        return value

    @field_validator("args")
    @classmethod
    def safe_args(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) > _MAX_ARG_LENGTH:
                raise ValueError(f"argv arguments must be at most {_MAX_ARG_LENGTH} characters")
            _validate_token(value, field_name="argv argument")
            candidate = value.partition("=")[2] if "=" in value else value
            if candidate.startswith(("/", "~")) or _WINDOWS_ABSOLUTE.match(candidate):
                raise ValueError("argv path must not be absolute")
            if "/" in candidate and any(part in {"", ".", ".."} for part in candidate.split("/")):
                raise ValueError("argv path contains an unsafe segment")
        return values


class WorktreeRequest(ContractModel):
    """The immutable source and bounded path scope for a future worktree."""

    base_commit: str = Field(pattern=_BASE_COMMIT_PATTERN)
    path_allowlist: list[str] = Field(min_length=1, max_length=_MAX_ALLOWLIST_COUNT)
    max_files: int = Field(ge=1, le=_MAX_FILES)

    @field_validator("path_allowlist")
    @classmethod
    def safe_allowlist(cls, values: list[str]) -> list[str]:
        normalized = [_validate_relative_path(value, field_name="path allowlist entry") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("path allowlist must not contain duplicates")
        return normalized


class ContainerPolicy(ContractModel):
    """Bounded default-deny policy reserved for a future container runner."""

    network_disabled: Literal[True] = True
    credentials: Literal[False] = False
    timeout_seconds: float = Field(ge=1, le=3_600)
    memory_mb: int = Field(ge=16, le=16_384)


__all__ = ["BoundedArgv", "ContainerPolicy", "WorktreeRequest"]
