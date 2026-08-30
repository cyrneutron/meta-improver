import json
from pathlib import Path

from src.ha_cli import HaCliAdapter, HaCliConfig, ProcessResult
from src.orchestrator import ProposalOrchestrator
from src.proposal import ProposalPayload, plan_proposal


class Transport:
    def __init__(self, capabilities):
        self.capabilities = capabilities

    def run(self, argv, **kwargs):
        payload = {"ok": True, "command": "version", "version": "0.1.0"} if "version" in argv else self.capabilities
        return ProcessResult(0, json.dumps(payload).encode(), b"")


def test_orchestrator_only_plans_when_squad_run_is_confirmed(tmp_path: Path) -> None:
    files = []
    for name, value in (("node", "x"), ("cli.js", "x"), ("build.txt", "build")):
        path = tmp_path / name
        path.write_text(value)
        files.append(path)
    config = HaCliConfig(executable=files[0], cli_entry=files[1], build_id_file=files[2],
                         expected_version="0.1.0", expected_build_id="build")
    adapter = HaCliAdapter(config, Transport({"ok": True, "squad": ["squad-list"]}))
    proposal = plan_proposal(ProposalPayload(
        repo="org/repo", head="mi/change", base="main", base_commit="a" * 40,
        patch_hash="sha256:" + "b" * 64, acceptance_receipt_hash="sha256:" + "c" * 64,
        changed_paths=["src/x.py"], title="Candidate change", body="Evidence-backed proposal.",
    ))
    plan = ProposalOrchestrator(adapter).plan(tmp_path, proposal)
    assert plan.mode == "proposal_only"
    assert plan.status == "unsupported"
    assert plan.squad_run_supported is False
