import hashlib
import json
from pathlib import Path

import pytest

from src.ha_cli import HaCliAdapter, HaCliConfig, ProcessResult
from src.ha_diagnosis import (
    HaDiagnosisError,
    HaDiagnosisStatus,
    collect_squad_diagnosis,
    record_squad_diagnosis_attempt,
    rehydrate_diagnosis,
)
from src.models import AttemptStage, AttemptStatus
from src.storage import Ledger, LedgerConflictError


class Transport:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def run(self, argv, **kwargs):
        return ProcessResult(0, json.dumps(self.payloads.pop(0)).encode(), b"")


def adapter(tmp_path: Path, payloads) -> HaCliAdapter:
    paths = []
    for name, value in (("node", "x"), ("cli.js", "x"), ("build.txt", "build")):
        path = tmp_path / name
        path.write_text(value)
        paths.append(path)
    return HaCliAdapter(
        HaCliConfig(
            executable=paths[0], cli_entry=paths[1], build_id_file=paths[2],
            expected_version="0.1.0", expected_build_id="build",
        ),
        Transport(payloads),
    )


def test_collect_squad_diagnosis_normalizes_findings_and_persists_idempotently(tmp_path: Path):
    diagnosis = collect_squad_diagnosis(
        adapter(tmp_path, [
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "squadRunId": "run-1"},
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "status": "completed", "decision": {
                "kind": "converged", "summary": "Found one boundary issue.",
                "findings": [{"path": "src/main.py", "observation": "Write path is duplicated."}],
            }},
        ]),
        tmp_path,
        diagnosis_id="diag-1", squad_id="mi-ha-governance", instance="leader", cwd=".", task_id="task-1",
        prompt="Inspect the repository.", interval_seconds=0, deadline_seconds=2,
    )
    assert diagnosis.status is HaDiagnosisStatus.CONVERGED
    assert diagnosis.findings[0].path == "src/main.py"
    assert rehydrate_diagnosis(diagnosis) == diagnosis
    ledger = Ledger(tmp_path / "history.db")
    attempt = record_squad_diagnosis_attempt(
        ledger,
        diagnosis,
        base_commit="a" * 40,
        strategy_version="ha-diagnosis-v1",
        model_version="ha-squad/provider-0.1.0",
        prompt_version="diagnosis-prompt-v1",
    )
    replay = record_squad_diagnosis_attempt(
        ledger,
        diagnosis,
        base_commit="a" * 40,
        strategy_version="ha-diagnosis-v1",
        model_version="ha-squad/provider-0.1.0",
        prompt_version="diagnosis-prompt-v1",
    )

    assert replay == attempt
    assert attempt.status is AttemptStatus.PROPOSED
    assert attempt.stage is AttemptStage.CAPTURED
    assert attempt.source_diagnosis_id == diagnosis.diagnosis_id
    assert attempt.source_diagnosis_hash == diagnosis.record_hash
    assert attempt.signal == diagnosis.record_hash
    assert attempt.baseline_hash is None
    assert attempt.patch_hash is None
    assert attempt.acceptance_receipt_hash is None
    assert ledger.count() == 1
    assert list(ledger.iter_diagnoses("task-1")) == [diagnosis]


def test_collect_squad_diagnosis_rejects_missing_finding_for_sole_leader(tmp_path: Path):
    with pytest.raises(ValueError, match="requires a summary or finding"):
        collect_squad_diagnosis(
            adapter(tmp_path, [
                {"ok": True, "command": "version", "version": "0.1.0"},
                {"schema": "command-receipt/v2", "ok": True, "squadRunId": "run-1"},
                {"ok": True, "command": "version", "version": "0.1.0"},
                {"schema": "command-receipt/v2", "ok": True, "status": "completed", "decision": {"kind": "converged"}},
            ]),
            tmp_path,
            diagnosis_id="diag-1", squad_id="mi-ha-governance", instance="leader", cwd=".", task_id="task-1",
            prompt="Inspect the repository.", interval_seconds=0, deadline_seconds=2,
        )


def test_nested_leader_decision_is_normalized(tmp_path: Path):
    diagnosis = collect_squad_diagnosis(
        adapter(tmp_path, [
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "squadRunId": "run-1"},
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "status": "converged", "leaders": [{
                "status": "succeeded", "decision": {"kind": "converged", "summary": "nested"}
            }]},
        ]),
        tmp_path,
        diagnosis_id="diag-1", squad_id="mi-ha-governance", instance="leader", cwd=".", task_id="task-1",
        prompt="Inspect the repository.", interval_seconds=0, deadline_seconds=2,
    )
    assert diagnosis.status is HaDiagnosisStatus.CONVERGED
    assert diagnosis.summary == "nested"


def test_diagnosis_rehydration_and_ledger_conflict_fail_closed(tmp_path: Path):
    diagnosis = collect_squad_diagnosis(
        adapter(tmp_path, [
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "squadRunId": "run-1"},
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "status": "completed", "decision": {
                "kind": "converged", "findings": [{"path": "README.md", "observation": "Review contract."}],
            }},
        ]),
        tmp_path,
        diagnosis_id="diag-1", squad_id="mi-ha-governance", instance="leader", cwd=".", task_id="task-1",
        prompt="Inspect the repository.", interval_seconds=0, deadline_seconds=2,
    )
    stale = diagnosis.model_copy(update={"summary": "different"})
    with pytest.raises(HaDiagnosisError):
        rehydrate_diagnosis(stale)
    mutated_payload = diagnosis.model_dump(mode="json", by_alias=True, exclude={"record_hash"})
    mutated_payload["summary"] = "different"
    mutated = type(diagnosis).model_validate(mutated_payload)
    ledger = Ledger(tmp_path / "history.db")
    record_squad_diagnosis_attempt(
        ledger,
        diagnosis,
        base_commit="a" * 40,
        strategy_version="ha-diagnosis-v1",
        model_version="ha-squad/provider-0.1.0",
        prompt_version="diagnosis-prompt-v1",
    )
    with pytest.raises(LedgerConflictError):
        record_squad_diagnosis_attempt(
            ledger,
            mutated,
            base_commit="a" * 40,
            strategy_version="ha-diagnosis-v1",
            model_version="ha-squad/provider-0.1.0",
            prompt_version="diagnosis-prompt-v1",
        )


@pytest.mark.parametrize(
    ("responses", "cwd", "expected_status"),
    [
        (
            [
                {"ok": True, "command": "version", "version": "0.1.0"},
                {
                    "schema": "command-receipt/v2",
                    "ok": False,
                    "command": "squad-run",
                    "error": "provider rejected request",
                },
            ],
            ".",
            HaDiagnosisStatus.REJECTED,
        ),
        (
            [{"ok": True, "command": "version", "version": "0.1.0"}],
            "../outside",
            HaDiagnosisStatus.UNSUPPORTED,
        ),
    ],
)
def test_nonconverged_provider_diagnosis_creates_only_a_captured_attempt(
    tmp_path: Path, responses, cwd: str, expected_status: HaDiagnosisStatus
):
    diagnosis = collect_squad_diagnosis(
        adapter(tmp_path, responses),
        tmp_path,
        diagnosis_id="diag-rejected",
        squad_id="mi-ha-governance",
        instance="leader",
        cwd=cwd,
        task_id="task-1",
        prompt="Inspect the repository.",
        interval_seconds=0,
        deadline_seconds=2,
    )
    ledger = Ledger(tmp_path / "history.db")

    attempt = record_squad_diagnosis_attempt(
        ledger,
        diagnosis,
        base_commit="a" * 40,
        strategy_version="ha-diagnosis-v1",
        model_version="ha-squad/provider-0.1.0",
        prompt_version="diagnosis-prompt-v1",
    )

    assert diagnosis.status is expected_status
    assert attempt.status is AttemptStatus.REJECTED
    assert attempt.stage is AttemptStage.CAPTURED
    assert attempt.failure_reason == diagnosis.reason
    assert attempt.baseline_hash is None
    assert attempt.diagnosis_hash is None
    assert [event.stage for event in ledger.iter_attempt_events(attempt.attempt_id)] == [
        AttemptStage.CAPTURED,
        AttemptStage.CAPTURED,
    ]
