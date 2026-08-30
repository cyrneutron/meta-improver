import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingestion import (
    DiagnosticError,
    HADiagnosticReport,
    IngestionService,
    SignalEvent,
    build_diagnostic_summary,
    build_ledger_diagnostic_summary,
    replay_fixture_signals,
    read_diagnostic_summary,
)
from src.ingestion.models import IssueSignal
from src.storage import Ledger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "task_diag123"
FACT_ID = "F-AAAA1111"
DECISION_ID = "dec_diag123"


def _diagnostic_fixture(root: Path, *, malformed_harness: bool = False) -> None:
    task = root / "harness" / "tasks" / f"{TASK_ID}-fixture"
    (task / "executions").mkdir(parents=True)
    (root / "harness" / "facts").mkdir(parents=True)
    (root / "harness" / "decisions" / f"decision-{DECISION_ID}").mkdir(parents=True)
    (root / ".harness").mkdir()
    (root / "harness" / "harness.yaml").write_text(
        "not: [\n" if malformed_harness else "schema: harness-anything/v1\n", encoding="utf-8"
    )
    (root / "harness" / "people.yaml").write_text("schema: harness-people/v1\n", encoding="utf-8")
    (task / "INDEX.md").write_text(
        f'''---
schema: task-package/v2
task_id: {TASK_ID}
title: "Diagnostic fixture"
lifecycle:
  status: active
packagePath: tasks/{task.name}
---
# Diagnostic fixture
''',
        encoding="utf-8",
    )
    (task / "task-contract.json").write_text(
        json.dumps(
            {
                "schema": "task-contract/v1",
                "taskId": TASK_ID,
                "packagePath": f"tasks/{task.name}",
                "title": "Diagnostic fixture",
            }
        ),
        encoding="utf-8",
    )
    (task / "progress.md").write_text(
        """# Progress

## Entries

### 2026-08-29T00:00:00Z

Fixture diagnostic progress.
Evidence: test:tests/test_diagnostics.py:fixture
""",
        encoding="utf-8",
    )
    (task / "executions" / "exec_diag.md").write_text(
        f"# Execution exec_diag\n\n- Task: {TASK_ID}\n- State: active\n", encoding="utf-8"
    )
    (root / "harness" / "facts" / f"{FACT_ID}.md").write_text(
        f"""# Facts

## Records

### {FACT_ID}

- Statement: Diagnostic fixture fact token=secret.
- Evidence source: test fixture
- Observed at: 2026-08-29T00:00:00Z
- Confidence: high
- State: standing
""",
        encoding="utf-8",
    )
    (root / "harness" / "decisions" / f"decision-{DECISION_ID}" / "decision.md").write_text(
        f'''---
schema: decision-package/v1
decision_id: {DECISION_ID}
title: "Diagnostic fixture decision"
state: proposed
question: "Should the fixture be read?"
chosen: []
rejected: []
claims: []
relations: []
---
# Diagnostic fixture decision
''',
        encoding="utf-8",
    )


@pytest.mark.skipif(not (PROJECT_ROOT / "harness/facts").is_dir(), reason="local canonical ledger is absent")
def test_current_mi_diagnostic_summary_is_structured_and_serializable() -> None:
    report = build_diagnostic_summary(PROJECT_ROOT, "task_a471cddf039eafe9c2e3986b83")

    assert isinstance(report, HADiagnosticReport)
    assert report.task.status == "active"
    assert report.counts.facts == len(report.facts) >= 1
    assert report.counts.decisions == len(report.decisions) >= 1
    assert report.counts.executions == len(report.task.executions)
    assert report.schema_ == "diagnostic-summary/v1"
    assert report.task.progress_entries
    assert "harness/harness.yaml" in report.source_refs
    payload = json.loads(report.to_json())
    assert payload["schema"] == "diagnostic-summary/v1"
    assert payload == report.model_dump(mode="json", by_alias=True)


def test_diagnostic_summary_is_deterministic_and_alias_matches(tmp_path) -> None:
    _diagnostic_fixture(tmp_path)
    projection_facts = tmp_path / ".harness" / "facts"
    projection_facts.mkdir()
    (projection_facts / "F-PROJECT1.md").write_text("projection-token=must-not-be-read", encoding="utf-8")
    canonical_files = [
        tmp_path / "harness/harness.yaml",
        tmp_path / "harness/people.yaml",
        tmp_path / "harness/tasks/task_diag123-fixture/INDEX.md",
        tmp_path / "harness/tasks/task_diag123-fixture/task-contract.json",
        tmp_path / "harness/tasks/task_diag123-fixture/progress.md",
        tmp_path / "harness/tasks/task_diag123-fixture/executions/exec_diag.md",
        tmp_path / "harness/facts/F-AAAA1111.md",
        tmp_path / "harness/decisions/decision-dec_diag123/decision.md",
    ]
    before = [(path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in canonical_files]
    first = build_diagnostic_summary(tmp_path, TASK_ID)
    second = read_diagnostic_summary(tmp_path, TASK_ID)
    after = [(path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in canonical_files]

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.counts.model_dump() == {
        "harness_documents": 2,
        "task_documents": 4,
        "executions": 1,
        "progress_entries": 1,
        "progress_evidence": 1,
        "facts": 1,
        "decisions": 1,
    }
    assert "harness/decisions/decision-dec_diag123/decision.md" in first.source_refs
    assert before == after
    assert all("secret" not in json.dumps(item.model_dump()) for item in first.facts)
    assert "must-not-be-read" not in first.to_json()
    assert first.source is None
    assert first.event_signature is None
    assert first.attempt_id is None


def test_diagnostic_summary_links_signal_event_and_attempt(tmp_path) -> None:
    _diagnostic_fixture(tmp_path)
    observed = datetime(2026, 8, 29, tzinfo=timezone.utc)
    event = SignalEvent(source="issue", external_id="issue-1", content="failure", observed_at=observed)
    attempt = IngestionService(
        Ledger(tmp_path / "history.db"),
        base_commit="a" * 40,
        strategy_version="strategy-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
    ).ingest(event)

    report = build_diagnostic_summary(tmp_path, TASK_ID, signal_event=event, attempt=attempt)

    assert report.source == "issue"
    assert report.event_signature == event.signature
    assert report.attempt_id == attempt.attempt_id
    assert report.attempt_status == "proposed"
    assert report.base_commit == "a" * 40
    assert report.strategy_version == "strategy-v1"
    assert json.loads(report.to_json())["event_signature"] == event.signature


def test_diagnostic_summary_rejects_mismatched_event_and_attempt(tmp_path) -> None:
    _diagnostic_fixture(tmp_path)
    observed = datetime(2026, 8, 29, tzinfo=timezone.utc)
    event = SignalEvent(source="issue", external_id="issue-1", content="failure", observed_at=observed)
    other_event = SignalEvent(source="issue", external_id="issue-2", content="failure", observed_at=observed)
    attempt = IngestionService(
        Ledger(tmp_path / "history.db"),
        base_commit="a" * 40,
        strategy_version="strategy-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
    ).ingest(event)

    with pytest.raises(DiagnosticError):
        build_diagnostic_summary(tmp_path, TASK_ID, signal_event=other_event, attempt=attempt)


def test_ledger_diagnostic_summary_rejects_missing_attempt(tmp_path) -> None:
    _diagnostic_fixture(tmp_path)

    with pytest.raises(DiagnosticError):
        build_ledger_diagnostic_summary(tmp_path, TASK_ID, Ledger(tmp_path / "history.db"), "missing-attempt")


def test_ledger_diagnostic_summary_rejects_signature_conflict(tmp_path) -> None:
    _diagnostic_fixture(tmp_path)
    observed = datetime(2026, 8, 29, tzinfo=timezone.utc)
    event = SignalEvent(source="issue", external_id="issue-1", content="failure", observed_at=observed)
    other_event = SignalEvent(source="issue", external_id="issue-2", content="failure", observed_at=observed)
    ledger = Ledger(tmp_path / "history.db")
    attempt = IngestionService(
        ledger,
        base_commit="a" * 40,
        strategy_version="strategy-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
    ).ingest(event)

    with pytest.raises(DiagnosticError):
        build_ledger_diagnostic_summary(tmp_path, TASK_ID, ledger, attempt.attempt_id, signal_event=other_event)


def test_ledger_diagnostic_summary_is_stable_across_repeated_fixture_replay(tmp_path) -> None:
    _diagnostic_fixture(tmp_path)
    observed = datetime(2026, 8, 29, tzinfo=timezone.utc)
    signal = IssueSignal(issue_id="issue-replay", title="Failure", body="Details", observed_at=observed)
    ledger = Ledger(tmp_path / "history.db")
    first_attempt = replay_fixture_signals([signal], ledger, base_commit="a" * 40)[0]
    replay_attempt = replay_fixture_signals([signal], ledger, base_commit="a" * 40)[0]

    first = build_ledger_diagnostic_summary(tmp_path, TASK_ID, ledger, first_attempt.attempt_id)
    replay = build_ledger_diagnostic_summary(tmp_path, TASK_ID, ledger, replay_attempt.attempt_id)

    assert replay_attempt == first_attempt
    assert first.model_dump(mode="json") == replay.model_dump(mode="json")
    assert first.to_json() == replay.to_json()


@pytest.mark.parametrize(
    "root,task_id",
    [
        (Path("/does/not/exist"), TASK_ID),
    ],
)
def test_diagnostic_summary_fails_closed_for_missing_input(root, task_id) -> None:
    with pytest.raises(DiagnosticError):
        build_diagnostic_summary(root, task_id)


def test_diagnostic_summary_fails_closed_for_malformed_or_oversized_input(tmp_path) -> None:
    _diagnostic_fixture(tmp_path, malformed_harness=True)
    with pytest.raises(DiagnosticError):
        build_diagnostic_summary(tmp_path, TASK_ID)

    _diagnostic_fixture(tmp_path / "large")
    with pytest.raises(DiagnosticError):
        build_diagnostic_summary(tmp_path / "large", TASK_ID, max_text=10)


def test_diagnostic_summary_rejects_unsafe_progress_evidence(tmp_path) -> None:
    _diagnostic_fixture(tmp_path)
    progress = tmp_path / "harness/tasks/task_diag123-fixture/progress.md"
    progress.write_text(
        progress.read_text(encoding="utf-8").replace(
            "test:tests/test_diagnostics.py:fixture", "test:../escape:fixture"
        ),
        encoding="utf-8",
    )
    with pytest.raises(DiagnosticError):
        build_diagnostic_summary(tmp_path, TASK_ID)
