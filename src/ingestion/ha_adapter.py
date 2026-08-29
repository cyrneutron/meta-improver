"""Minimal read-only access to canonical Harness Anything configuration."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import Field

from src.models.contracts import ContractModel, MAX_TEXT


class HAAdapterError(ValueError):
    """Raised when canonical Harness Anything input is missing or invalid."""


class HAContext(ContractModel):
    """The bounded, redacted YAML documents read from the canonical ledger root."""

    harness: dict[str, Any] = Field(default_factory=dict)
    people: dict[str, Any] = Field(default_factory=dict)


def _canonical_file(repo_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts) or "\\" in relative:
        raise HAAdapterError(f"unsafe canonical path: {relative}")
    harness = repo_root / "harness"
    if harness.is_symlink() or not harness.is_dir():
        raise HAAdapterError(f"canonical harness directory is missing: {harness}")
    path = repo_root.joinpath(*pure.parts)
    try:
        path.resolve(strict=False).relative_to(harness.resolve(strict=False))
    except ValueError as exc:
        raise HAAdapterError(f"canonical path escapes harness: {relative}") from exc
    for index, _part in enumerate(pure.parts, start=1):
        current = repo_root / Path(*pure.parts[:index])
        if current.is_symlink():
            raise HAAdapterError(f"symlink is not allowed in canonical path: {relative}")
    if path.is_symlink() or not path.is_file():
        raise HAAdapterError(f"canonical file is missing or not regular: {path}")
    return path


def _read_yaml(path: Path, max_text: int) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HAAdapterError(f"cannot read canonical file: {path}") from exc
    if len(text) > max_text:
        raise HAAdapterError(f"canonical file exceeds max_text={max_text}: {path}")
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HAAdapterError(f"invalid YAML: {path}") from exc
    if not isinstance(value, dict):
        raise HAAdapterError(f"canonical YAML must be a mapping: {path}")
    return value


def read_ha_context(repo_root: str | Path, *, max_text: int = MAX_TEXT) -> HAContext:
    """Read only ``harness/harness.yaml`` and ``harness/people.yaml``."""
    if max_text < 1 or max_text > MAX_TEXT:
        raise ValueError(f"max_text must be between 1 and {MAX_TEXT}")
    supplied = Path(repo_root)
    if not supplied.is_dir():
        raise HAAdapterError(f"repository root is not a directory: {supplied}")
    root = supplied.resolve()
    harness = _canonical_file(root, "harness/harness.yaml")
    people = _canonical_file(root, "harness/people.yaml")
    return HAContext(harness=_read_yaml(harness, max_text), people=_read_yaml(people, max_text))


__all__ = ["HAAdapterError", "HAContext", "read_ha_context"]
