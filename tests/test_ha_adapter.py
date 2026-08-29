import hashlib
import json
from pathlib import Path

import pytest

from src.ingestion.ha_adapter import HAAdapterError, read_ha_context, read_task_context


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fixture(root: Path, *, people: str = "people: []\n", harness: str = "schema: harness-anything/v1\n") -> None:
    path = root / "harness"
    path.mkdir(parents=True)
    (path / "harness.yaml").write_text(harness, encoding="utf-8")
    (path / "people.yaml").write_text(people, encoding="utf-8")


@pytest.mark.skipif(not (PROJECT_ROOT / "harness/people.yaml").is_file(), reason="local canonical ledger is absent")
def test_current_mi_canonical_yaml_reads_successfully() -> None:
    context = read_ha_context(PROJECT_ROOT)
    assert context.harness["schema"] == "harness-anything/v1"
    assert context.people["schema"] == "harness-people/v1"


def test_minimal_canonical_yaml_is_structured_and_redacted(tmp_path) -> None:
    _fixture(tmp_path, people="credentials: token=secret\n")
    context = read_ha_context(tmp_path)
    assert context.harness["schema"] == "harness-anything/v1"
    assert context.people["credentials"] == "[REDACTED]"


@pytest.mark.parametrize(
    "setup",
    [
        lambda root: None,
        lambda root: _fixture(root, people="people: [\n"),
        lambda root: _fixture(root, people="x: " + ("a" * 101) + "\n"),
    ],
)
def test_missing_invalid_and_oversized_canonical_yaml_fail_closed(tmp_path, setup) -> None:
    setup(tmp_path)
    with pytest.raises(HAAdapterError):
        read_ha_context(tmp_path, max_text=100)


TASK_ID = "task_fixture123"


def _task_fixture(
    root: Path,
    *,
    index: str | None = None,
    contract: str | None = None,
    progress: str | None = None,
    execution: str | None = None,
) -> Path:
    package = root / "harness" / "tasks" / f"{TASK_ID}-fixture"
    (package / "executions").mkdir(parents=True)
    index = index or f'''---
schema: task-package/v2
task_id: {TASK_ID}
title: "Fixture task"
lifecycle:
  status: active
packagePath: tasks/{package.name}
---
# Fixture task
'''
    contract = contract or json.dumps(
        {
            "schema": "task-contract/v1",
            "taskId": TASK_ID,
            "packagePath": f"tasks/{package.name}",
            "title": "Fixture task",
            "status": "active",
        }
    )
    progress = progress or """# Progress

## Entries

### 2026-08-29T00:00:00Z

Fixture progress with token=secret.
Evidence: test:tests/test_ha_adapter.py:fixture
"""
    execution = execution or f"""# Execution exec_fixture

- Task: {TASK_ID}
- State: active
"""
    (package / "INDEX.md").write_text(index, encoding="utf-8")
    (package / "task-contract.json").write_text(contract, encoding="utf-8")
    (package / "progress.md").write_text(progress, encoding="utf-8")
    (package / "executions" / "exec_fixture.md").write_text(execution, encoding="utf-8")
    return package


def test_task_context_reads_minimal_canonical_package_and_redacts(tmp_path) -> None:
    package = _task_fixture(tmp_path)
    context = read_task_context(tmp_path, TASK_ID)

    assert context.task_id == TASK_ID
    assert context.title == "Fixture task"
    assert context.status == "active"
    assert context.package_path == f"tasks/{package.name}"
    assert [(item.execution_id, item.state) for item in context.executions] == [("exec_fixture", "active")]
    assert context.progress is context.progress_entries
    assert context.progress_entries[0].evidence == ["test:tests/test_ha_adapter.py:fixture"]
    assert "[REDACTED]" in context.progress_entries[0].text


@pytest.mark.skipif(not (PROJECT_ROOT / "harness/tasks/task_a471cddf039eafe9c2e3986b83-implement-phase-2a-local-fixture-ingestion-and-diagnostics").is_dir(), reason="local task fixture is absent")
def test_current_mi_task_context_reads_successfully() -> None:
    context = read_task_context(PROJECT_ROOT, "task_a471cddf039eafe9c2e3986b83")

    assert context.title == "Implement Phase 2A local fixture ingestion and diagnostics"
    assert context.status == "active"
    assert context.executions
    assert context.progress_entries
    assert any("ledger service" in entry.text for entry in context.progress_entries)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda package: (package / "INDEX.md").write_text("not markdown", encoding="utf-8"),
        lambda package: (package / "task-contract.json").write_text("{", encoding="utf-8"),
        lambda package: (package / "progress.md").write_text("# Progress\n", encoding="utf-8"),
        lambda package: (package / "executions" / "exec_fixture.md").write_text("# broken\n", encoding="utf-8"),
    ],
)
def test_task_context_rejects_invalid_canonical_documents(tmp_path, mutate) -> None:
    package = _task_fixture(tmp_path)
    mutate(package)
    with pytest.raises(HAAdapterError):
        read_task_context(tmp_path, TASK_ID)


def test_task_context_rejects_missing_and_oversized_documents(tmp_path) -> None:
    package = _task_fixture(tmp_path)
    (package / "progress.md").unlink()
    with pytest.raises(HAAdapterError):
        read_task_context(tmp_path, TASK_ID)

    package = _task_fixture(tmp_path / "large")
    with pytest.raises(HAAdapterError):
        read_task_context(tmp_path / "large", TASK_ID, max_text=100)


def test_task_context_rejects_path_escape_and_symlink(tmp_path) -> None:
    _task_fixture(tmp_path)
    with pytest.raises(HAAdapterError):
        read_task_context(tmp_path, "task_../escape")

    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "harness" / "tasks" / f"{TASK_ID}-escape"
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HAAdapterError):
        read_task_context(tmp_path, TASK_ID)


def test_task_context_is_read_only_and_ignores_projection(tmp_path) -> None:
    package = _task_fixture(tmp_path)
    projection = tmp_path / ".harness" / "tasks" / f"{TASK_ID}-fixture"
    projection.mkdir(parents=True)
    (projection / "INDEX.md").write_text("# projection must not win\n", encoding="utf-8")
    paths = [
        package / "INDEX.md",
        package / "task-contract.json",
        package / "progress.md",
        package / "executions" / "exec_fixture.md",
    ]
    before = [(path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths]
    context = read_task_context(tmp_path, TASK_ID)
    after = [(path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths]

    assert context.title == "Fixture task"
    assert before == after
