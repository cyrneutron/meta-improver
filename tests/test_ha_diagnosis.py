import hashlib
import json
from pathlib import Path

import pytest

from src.ha_cli import HaCliAdapter, HaCliConfig, ProcessResult
from src.ha_diagnosis import (
    HaDiagnosisError,
    HaDiagnosisStatus,
    collect_squad_diagnosis,
    rehydrate_diagnosis,
)
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
    assert ledger.record_diagnosis(diagnosis) == diagnosis
    assert ledger.record_diagnosis(diagnosis) == diagnosis
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
    ledger.record_diagnosis(diagnosis)
    with pytest.raises(LedgerConflictError):
        ledger.record_diagnosis(mutated)
