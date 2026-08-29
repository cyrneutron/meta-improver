import hashlib
import json
from pathlib import Path

import pytest

from src.ingestion import (
    HAAdapterError,
    HAContext,
    HAExecution,
    HAProgressEntry,
    HATaskContext,
    HAFact,
    HADecision,
    read_decision_context,
    read_fact_context,
    read_ha_context,
    read_task_context,
)


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


def test_task_adapter_symbols_are_exported_from_ingestion_package() -> None:
    assert HAAdapterError.__name__ == "HAAdapterError"
    assert HAContext.__name__ == "HAContext"
    assert HAExecution.__name__ == "HAExecution"
    assert HAProgressEntry.__name__ == "HAProgressEntry"
    assert HATaskContext.__name__ == "HATaskContext"
    assert HAFact.__name__ == "HAFact"
    assert HADecision.__name__ == "HADecision"
    assert callable(read_ha_context)
    assert callable(read_fact_context)
    assert callable(read_decision_context)
    assert callable(read_task_context)


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


@pytest.mark.parametrize(
    "index_or_contract",
    [
        lambda package: (package / "INDEX.md").write_text(
            (package / "INDEX.md").read_text(encoding="utf-8").replace("schema: task-package/v2", "schema: task-package/v1"),
            encoding="utf-8",
        ),
        lambda package: (package / "task-contract.json").write_text(
            (package / "task-contract.json").read_text(encoding="utf-8").replace('"task-contract/v1"', '"task-contract/v2"'),
            encoding="utf-8",
        ),
    ],
)
def test_task_context_rejects_unsupported_document_schema(tmp_path, index_or_contract) -> None:
    package = _task_fixture(tmp_path)
    index_or_contract(package)
    with pytest.raises(HAAdapterError):
        read_task_context(tmp_path, TASK_ID)


def test_task_context_rejects_execution_filename_header_mismatch(tmp_path) -> None:
    package = _task_fixture(tmp_path)
    (package / "executions" / "exec_fixture.md").rename(package / "executions" / "other.md")
    with pytest.raises(HAAdapterError):
        read_task_context(tmp_path, TASK_ID)


def test_task_context_rejects_unknown_execution_state(tmp_path) -> None:
    package = _task_fixture(tmp_path, execution="# Execution exec_fixture\n\n- State: unknown\n")
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


FACT_ID = "F-AAAA1111"
DECISION_ID = "dec_fixture123"


def _fact_fixture(root: Path, *, content: str | None = None, filename: str | None = None) -> Path:
    directory = root / "harness" / "facts"
    directory.mkdir(parents=True)
    path = directory / (filename or f"{FACT_ID}.md")
    content = content or f"""# Facts

## Records

### {FACT_ID}

- Statement: Fixture fact with token=secret.
- Evidence source: fixture test
- Observed at: 2026-08-29T00:00:00Z
- Confidence: high
- State: standing
"""
    path.write_text(content, encoding="utf-8")
    return path


def _decision_fixture(root: Path, *, content: str | None = None, directory_name: str | None = None) -> Path:
    directory = root / "harness" / "decisions" / (directory_name or f"decision-{DECISION_ID}")
    directory.mkdir(parents=True)
    path = directory / "decision.md"
    content = content or f'''---
schema: decision-package/v1
decision_id: {DECISION_ID}
title: "Fixture decision"
state: proposed
question: "Should the fixture remain bounded?"
chosen: [{{"id": "CH1", "text": "Keep it bounded", "rationale": "Auditability"}}]
rejected: [{{"id": "RJ1", "text": "Use unbounded input", "whyNot": "Unsafe"}}]
claims: [{{"id": "C1", "text": "The boundary is load-bearing", "loadBearing": true}}]
relations: [{{"type": "evidenced-by", "target": "fact/{FACT_ID}"}}]
---

# Fixture decision
'''
    path.write_text(content, encoding="utf-8")
    return path


def test_fact_context_reads_and_redacts_canonical_records(tmp_path) -> None:
    path = _fact_fixture(tmp_path)
    records = read_fact_context(tmp_path)

    assert len(records) == 1
    assert isinstance(records[0], HAFact)
    assert records[0].fact_id == FACT_ID
    assert "[REDACTED]" in records[0].statement
    assert records[0].confidence == "high"
    assert records[0].state == "standing"
    assert path.exists()


def test_decision_context_reads_structured_frontmatter_and_redacts(tmp_path) -> None:
    path = _decision_fixture(tmp_path)
    records = read_decision_context(tmp_path)

    assert len(records) == 1
    assert isinstance(records[0], HADecision)
    assert records[0].decision_id == DECISION_ID
    assert records[0].state == "proposed"
    assert records[0].chosen[0]["id"] == "CH1"
    assert records[0].relations[0]["target"] == f"fact/{FACT_ID}"
    assert path.exists()


@pytest.mark.skipif(not (PROJECT_ROOT / "harness/facts").is_dir(), reason="local facts ledger is absent")
def test_current_mi_facts_and_decisions_read_successfully() -> None:
    facts = read_fact_context(PROJECT_ROOT)
    decisions = read_decision_context(PROJECT_ROOT)

    assert len(facts) >= 1
    assert len(decisions) >= 1
    assert all(item.fact_id.startswith("F-") for item in facts)
    assert all(item.decision_id.startswith("dec_") for item in decisions)


@pytest.mark.parametrize(
    "kind,mutate",
    [
        ("fact", lambda root: (root / "harness/facts/F-AAAA1111.md").write_text("# broken\n", encoding="utf-8")),
        ("decision", lambda root: (root / "harness/decisions/decision-dec_fixture123/decision.md").write_text("---\nschema: wrong\n---\n# broken\n", encoding="utf-8")),
    ],
)
def test_fact_and_decision_context_reject_invalid_documents(tmp_path, kind, mutate) -> None:
    _fact_fixture(tmp_path)
    _decision_fixture(tmp_path)
    mutate(tmp_path)
    reader = read_fact_context if kind == "fact" else read_decision_context
    with pytest.raises(HAAdapterError):
        reader(tmp_path)


def test_fact_and_decision_context_reject_missing_and_oversized_documents(tmp_path) -> None:
    _fact_fixture(tmp_path)
    (tmp_path / "harness/facts/F-AAAA1111.md").unlink()
    with pytest.raises(HAAdapterError):
        read_fact_context(tmp_path)

    large_root = tmp_path / "large"
    _decision_fixture(large_root)
    decision_path = large_root / "harness/decisions/decision-dec_fixture123/decision.md"
    decision_path.write_text(decision_path.read_text(encoding="utf-8") + ("x" * 200), encoding="utf-8")
    with pytest.raises(HAAdapterError):
        read_decision_context(large_root, max_text=100)


def test_fact_and_decision_context_reject_path_escape_and_symlink(tmp_path) -> None:
    _fact_fixture(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Facts\n", encoding="utf-8")
    (tmp_path / "harness/facts/F-BBBB2222.md").symlink_to(outside)
    with pytest.raises(HAAdapterError):
        read_fact_context(tmp_path)

    decision_root = tmp_path / "decision"
    _decision_fixture(decision_root)
    outside_dir = decision_root / "outside"
    outside_dir.mkdir()
    (decision_root / "harness/decisions/decision-dec_escape").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(HAAdapterError):
        read_decision_context(decision_root)


def test_fact_and_decision_context_reject_duplicate_ids(tmp_path) -> None:
    _fact_fixture(tmp_path)
    duplicate = tmp_path / "harness/facts/F-BBBB2222.md"
    duplicate.write_text((tmp_path / "harness/facts/F-AAAA1111.md").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(HAAdapterError):
        read_fact_context(tmp_path)

    decision_root = tmp_path / "decision"
    _decision_fixture(decision_root)
    duplicate_dir = decision_root / "harness/decisions/decision-dec_duplicate"
    duplicate_dir.mkdir()
    duplicate_path = duplicate_dir / "decision.md"
    duplicate_path.write_text(
        (decision_root / "harness/decisions/decision-dec_fixture123/decision.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(HAAdapterError):
        read_decision_context(decision_root)


def test_fact_and_decision_context_is_read_only_and_ignores_projection(tmp_path) -> None:
    fact_path = _fact_fixture(tmp_path)
    decision_path = _decision_fixture(tmp_path)
    projection = tmp_path / ".harness"
    (projection / "facts").mkdir(parents=True)
    (projection / "facts" / fact_path.name).write_text("projection", encoding="utf-8")
    before = [(path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in (fact_path, decision_path)]

    facts = read_fact_context(tmp_path)
    decisions = read_decision_context(tmp_path)

    after = [(path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in (fact_path, decision_path)]
    assert facts[0].fact_id == FACT_ID
    assert decisions[0].decision_id == DECISION_ID
    assert before == after
