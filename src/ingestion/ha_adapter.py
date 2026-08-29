"""Read-only access to bounded canonical Harness Anything documents."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import Field

from src.models.contracts import ContractModel, MAX_TEXT, redact


class HAAdapterError(ValueError):
    """Raised when canonical Harness Anything input is missing or invalid."""


class HAContext(ContractModel):
    """The bounded, redacted YAML documents read from the canonical ledger root."""

    harness: dict[str, Any] = Field(default_factory=dict)
    people: dict[str, Any] = Field(default_factory=dict)


class HAExecution(ContractModel):
    """The stable execution identity and state exposed by an execution file."""

    execution_id: str = Field(min_length=1, max_length=200)
    state: str = Field(min_length=1, max_length=100)


class HAProgressEntry(ContractModel):
    """One append-only progress entry and its evidence references."""

    timestamp: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    evidence: list[str] = Field(default_factory=list, max_length=100)


class HATaskContext(ContractModel):
    """Bounded task-package context read from canonical Markdown and JSON."""

    task_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    status: str = Field(min_length=1, max_length=100)
    package_path: str = Field(min_length=1, max_length=500)
    executions: list[HAExecution] = Field(default_factory=list, max_length=100)
    progress_entries: list[HAProgressEntry] = Field(default_factory=list, max_length=1_000)

    @property
    def progress(self) -> list[HAProgressEntry]:
        """Compatibility alias for callers that use the shorter field name."""
        return self.progress_entries


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


def _read_text(path: Path, max_text: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HAAdapterError(f"cannot read canonical file: {path}") from exc
    if len(text) > max_text:
        raise HAAdapterError(f"canonical file exceeds max_text={max_text}: {path}")
    return text


def _read_json(path: Path, max_text: int) -> dict[str, Any]:
    text = _read_text(path, max_text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HAAdapterError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise HAAdapterError(f"canonical JSON must be an object: {path}")
    return value


def _read_index_frontmatter(path: Path, max_text: int) -> tuple[dict[str, Any], str]:
    text = _read_text(path, max_text)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise HAAdapterError(f"task INDEX has no YAML frontmatter: {path}")
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise HAAdapterError(f"unterminated task INDEX frontmatter: {path}") from exc
    try:
        value = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        raise HAAdapterError(f"invalid INDEX YAML frontmatter: {path}") from exc
    if not isinstance(value, dict):
        raise HAAdapterError(f"task INDEX frontmatter must be a mapping: {path}")
    body = "\n".join(lines[closing + 1 :])
    if not re.search(r"(?m)^#\s+\S", body):
        raise HAAdapterError(f"task INDEX has invalid Markdown heading: {path}")
    return value, body


def _read_progress(path: Path, max_text: int) -> list[HAProgressEntry]:
    text = _read_text(path, max_text)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "# Progress":
        raise HAAdapterError(f"invalid progress Markdown heading: {path}")
    try:
        entries_start = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "## Entries")
    except StopIteration as exc:
        raise HAAdapterError(f"progress Markdown has no Entries section: {path}") from exc

    entries: list[HAProgressEntry] = []
    current_timestamp: str | None = None
    current_lines: list[str] = []

    def finish() -> None:
        nonlocal current_timestamp, current_lines
        if current_timestamp is None:
            return
        content = "\n".join(current_lines).strip()
        if not content:
            raise HAAdapterError(f"progress entry is empty: {path}")
        evidence: list[str] = []
        prose: list[str] = []
        for line in content.splitlines():
            if line.startswith("Evidence:"):
                reference = line.removeprefix("Evidence:").strip()
                if not reference:
                    raise HAAdapterError(f"progress evidence is empty: {path}")
                evidence.append(reference)
            else:
                prose.append(line)
        entries.append(
            HAProgressEntry(
                timestamp=current_timestamp,
                text="\n".join(prose).strip() or "evidence-only progress entry",
                evidence=evidence,
            )
        )
        current_timestamp = None
        current_lines = []

    for line in lines[entries_start + 1 :]:
        match = re.fullmatch(r"###\s+(\S.+?)\s*", line)
        if match:
            finish()
            current_timestamp = match.group(1)
            continue
        if current_timestamp is not None:
            if line.startswith("#") and not line.startswith("###"):
                raise HAAdapterError(f"unexpected progress Markdown heading: {path}")
            current_lines.append(line)
        elif line.strip() and not line.startswith("<!--"):
            raise HAAdapterError(f"content outside progress entry: {path}")
    finish()
    return entries


def _task_package(repo_root: Path, task_id: str) -> Path:
    if not re.fullmatch(r"task_[A-Za-z0-9]+", task_id):
        raise HAAdapterError(f"unsafe task id: {task_id}")
    tasks_root = repo_root / "harness" / "tasks"
    if not tasks_root.is_dir() or tasks_root.is_symlink():
        raise HAAdapterError(f"canonical tasks directory is missing: {tasks_root}")
    candidates = [
        child
        for child in tasks_root.iterdir()
        if child.name == task_id or child.name.startswith(task_id + "-")
    ]
    if len(candidates) != 1:
        raise HAAdapterError(f"task package is missing or ambiguous: {task_id}")
    package = candidates[0]
    if package.is_symlink() or not package.is_dir():
        raise HAAdapterError(f"task package is not a regular directory: {package}")
    try:
        package.resolve(strict=True).relative_to(tasks_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise HAAdapterError(f"task package escapes canonical tasks root: {task_id}") from exc
    return package


def read_task_context(
    repo_root: str | Path,
    task_id: str,
    *,
    max_text: int = MAX_TEXT,
) -> HATaskContext:
    """Read one task package's INDEX, contract, executions, and progress only."""
    if max_text < 1 or max_text > MAX_TEXT:
        raise ValueError(f"max_text must be between 1 and {MAX_TEXT}")
    supplied = Path(repo_root)
    if not supplied.is_dir():
        raise HAAdapterError(f"repository root is not a directory: {supplied}")
    root = supplied.resolve()
    package = _task_package(root, task_id)
    index_path = package / "INDEX.md"
    contract_path = package / "task-contract.json"
    progress_path = package / "progress.md"
    for path in (index_path, contract_path, progress_path):
        try:
            path.resolve(strict=True).relative_to(package.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise HAAdapterError(f"task document escapes package: {path}") from exc
        if path.is_symlink() or not path.is_file():
            raise HAAdapterError(f"task document is missing or not regular: {path}")

    index, _body = _read_index_frontmatter(index_path, max_text)
    contract = _read_json(contract_path, max_text)
    if contract.get("taskId") != task_id or index.get("task_id") != task_id:
        raise HAAdapterError(f"task id mismatch in package: {package}")
    package_path = contract.get("packagePath")
    expected_package_path = f"tasks/{package.name}"
    if package_path != expected_package_path:
        raise HAAdapterError(f"task package path mismatch: {package}")
    if index.get("packagePath") != expected_package_path:
        raise HAAdapterError(f"task INDEX package path mismatch: {package}")
    if index.get("title") != contract.get("title"):
        raise HAAdapterError(f"task title mismatch: {package}")
    lifecycle = index.get("lifecycle")
    if not isinstance(lifecycle, dict) or not isinstance(lifecycle.get("status"), str):
        raise HAAdapterError(f"task status mismatch: {package}")
    status = lifecycle["status"]

    executions: list[HAExecution] = []
    execution_dir = package / "executions"
    if execution_dir.exists():
        if execution_dir.is_symlink() or not execution_dir.is_dir():
            raise HAAdapterError(f"task executions directory is not regular: {execution_dir}")
        for execution_path in sorted(execution_dir.glob("*.md")):
            if execution_path.is_symlink() or not execution_path.is_file():
                raise HAAdapterError(f"task execution document is not regular: {execution_path}")
            execution_text = _read_text(execution_path, max_text)
            id_match = re.search(r"(?m)^#\s+Execution\s+(\S+)\s*$", execution_text)
            state_match = re.search(r"(?m)^-\s+State:\s*(\S+)\s*$", execution_text)
            if not id_match or not state_match:
                raise HAAdapterError(f"invalid execution Markdown: {execution_path}")
            executions.append(HAExecution(execution_id=id_match.group(1), state=state_match.group(1)))

    result = HATaskContext(
        task_id=task_id,
        title=str(contract["title"]),
        status=status,
        package_path=expected_package_path,
        executions=redact(executions),
        progress_entries=redact(_read_progress(progress_path, max_text)),
    )
    return result


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


__all__ = [
    "HAAdapterError",
    "HAContext",
    "HAExecution",
    "HAProgressEntry",
    "HATaskContext",
    "read_ha_context",
    "read_task_context",
]
