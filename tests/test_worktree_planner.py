import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.sandbox import (
    GitOperation,
    RepositorySnapshot,
    WorktreeActionPlan,
    WorktreeCreationReceipt,
    WorktreeDestination,
    WorktreeOperationKind,
    WorktreePlanningError,
    WorktreePolicy,
    WorktreePorcelainAttestation,
    WorktreeRequest,
    plan_cleanup,
    plan_recovery,
    plan_worktree,
    rehydrate_action_plan,
)


BASE_COMMIT = "a" * 40
SOURCE = "/srv/ha-target"
WORKTREE_ROOT = "/srv/mi-worktrees"
WORKTREE = f"{WORKTREE_ROOT}/attempt-1"
COMMON_GIT_DIR = f"{SOURCE}/.git"
WORKTREE_GITDIR = f"{COMMON_GIT_DIR}/worktrees/attempt-1"
TRANSPORT_OPERATION_ID = "sha256:" + "e" * 64


def absolute_chain(path: str, *, kind: str = "directory") -> list[dict[str, str]]:
    current = ""
    result: list[dict[str, str]] = []
    for component in path.split("/")[1:]:
        current += "/" + component
        result.append({"path": current, "kind": kind})
    return result


def request(**overrides: object) -> WorktreeRequest:
    values: dict[str, object] = {
        "base_commit": BASE_COMMIT,
        "path_allowlist": ["src", "tests"],
        "max_files": 20,
        "repository_id": "ha-target",
        "source_root": SOURCE,
        "worktree_path": WORKTREE,
        "request_id": "attempt-1",
    }
    values.update(overrides)
    return WorktreeRequest.model_validate(values)


def policy(**overrides: object) -> WorktreePolicy:
    values: dict[str, object] = {
        "repository_allowlist": [{"repository_id": "ha-target", "source_root": SOURCE}],
        "worktree_root": WORKTREE_ROOT,
    }
    values.update(overrides)
    return WorktreePolicy.model_validate(values)


def snapshot(**overrides: object) -> RepositorySnapshot:
    values: dict[str, object] = {
        "repository_id": "ha-target",
        "source_root": SOURCE,
        "base_commit": BASE_COMMIT,
        "source_root_chain": absolute_chain(SOURCE),
        "entries": [
            {"path": "src/main.py", "kind": "file"},
            {"path": "tests/test_main.py", "kind": "file"},
        ],
    }
    values.update(overrides)
    if "source_root_chain" not in overrides:
        values["source_root_chain"] = absolute_chain(str(values["source_root"]))
    return RepositorySnapshot.model_validate(values)


def destination(
    *,
    path: str = WORKTREE,
    kind: str = "absent",
    parent_chain: list[dict[str, str]] | None = None,
) -> WorktreeDestination:
    return WorktreeDestination(
        path=path,
        kind=kind,
        parent_chain=parent_chain or absolute_chain(path.rsplit("/", 1)[0]),
    )


def make_plan(
    value: WorktreeRequest | None = None,
    repo: RepositorySnapshot | None = None,
    rules: WorktreePolicy | None = None,
    attestation: WorktreeDestination | None = None,
):
    return plan_worktree(
        value or request(),
        repo or snapshot(),
        rules or policy(),
        attestation or destination(),
    )


def receipt(plan, **overrides: object) -> WorktreeCreationReceipt:
    values: dict[str, object] = {
        "parent_plan_hash": plan.plan_hash,
        "create_operation_id": plan.operations[2].operation_id,
        "repository_id": plan.repository_id,
        "source_root": plan.source_root,
        "common_git_dir": f"{plan.source_root}/.git",
        "worktree_gitdir": f"{plan.source_root}/.git/worktrees/{plan.request_id}",
        "worktree_path": plan.worktree_path,
        "base_commit": plan.base_commit,
        "head_commit": plan.base_commit,
        "success": True,
    }
    values.update(overrides)
    return WorktreeCreationReceipt.model_validate(values)


def observation(plan, created: WorktreeCreationReceipt, **overrides: object) -> WorktreePorcelainAttestation:
    values: dict[str, object] = {
        "parent_plan_hash": plan.plan_hash,
        "create_operation_id": created.create_operation_id,
        "repository_id": created.repository_id,
        "source_root": created.source_root,
        "common_git_dir": created.common_git_dir,
        "worktree_gitdir": created.worktree_gitdir,
        "worktree_path": created.worktree_path,
        "base_commit": created.base_commit,
        "head_commit": created.head_commit,
        "request_id": plan.request_id,
        "concurrency_key": plan.concurrency_key,
        "transport_receipt_digest": created.receipt_digest,
        "transport_operation_id": TRANSPORT_OPERATION_ID,
    }
    values.update(overrides)
    return WorktreePorcelainAttestation.model_validate(values)


def evidence(plan):
    created = receipt(plan)
    return created, observation(plan, created)


def test_plan_is_deterministic_and_contains_only_git_argv() -> None:
    first = make_plan()
    second = make_plan()

    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert "cleanup_operations" not in type(first).model_fields
    assert "recovery_operations" not in type(first).model_fields
    assert first.concurrency_key.startswith("worktree:sha256:")
    assert [operation.kind for operation in first.operations] == [
        WorktreeOperationKind.VERIFY_BASE_COMMIT,
        WorktreeOperationKind.INSPECT_WORKTREES,
        WorktreeOperationKind.CREATE_WORKTREE,
    ]
    assert first.operations[0].argv == [
        "git",
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{BASE_COMMIT}^{{commit}}",
    ]
    assert first.operations[1].argv == ["git", "worktree", "list", "--porcelain"]
    assert first.operations[-1].argv == [
        "git",
        "worktree",
        "add",
        "--detach",
        WORKTREE,
        BASE_COMMIT,
    ]
    assert first.operations[-1].approved_paths == [WORKTREE]


def test_resource_lock_is_shared_for_same_target_and_separate_for_other_target() -> None:
    first = make_plan()
    same_target_different_request = make_plan(
        request(request_id="attempt-2", base_commit="b" * 40),
        snapshot(base_commit="b" * 40),
    )
    different_target = make_plan(
        request(
            repository_id="other",
            source_root="/srv/other",
            worktree_path=f"{WORKTREE_ROOT}/attempt-2",
            request_id="attempt-2",
        ),
        snapshot(repository_id="other", source_root="/srv/other"),
        policy(repository_allowlist=[{"repository_id": "other", "source_root": "/srv/other"}]),
        destination(path=f"{WORKTREE_ROOT}/attempt-2"),
    )

    assert first.concurrency_key == same_target_different_request.concurrency_key
    assert first.plan_hash != same_target_different_request.plan_hash
    assert first.concurrency_key != different_target.concurrency_key


def test_permuted_allowlist_has_byte_identical_plan() -> None:
    first = make_plan(request(path_allowlist=["tests", "src"]))
    second = make_plan(request(path_allowlist=["src", "tests"]))

    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert first.request.path_allowlist == ["src", "tests"]


def test_repository_allowlist_accepts_owner_repo_identity() -> None:
    value = request(repository_id="cyrneutron/harness-anything")
    repo = snapshot(repository_id="cyrneutron/harness-anything")
    approved = policy(
        repository_allowlist=[{"repository_id": "cyrneutron/harness-anything", "source_root": SOURCE}]
    )
    assert make_plan(value, repo, approved).repository_id == "cyrneutron/harness-anything"


def test_planner_requires_allowlisted_repository_and_matching_full_base() -> None:
    with pytest.raises(WorktreePlanningError, match="allowlist"):
        make_plan(request(repository_id="other"), snapshot(repository_id="other"))
    with pytest.raises(WorktreePlanningError, match="base_commit"):
        make_plan(request(base_commit="b" * 40), snapshot())
    with pytest.raises(WorktreePlanningError, match="full"):
        make_plan(request(base_commit="a" * 39), snapshot(base_commit="a" * 39))


@pytest.mark.parametrize("path", [WORKTREE_ROOT, f"{WORKTREE_ROOT}/../escape", f"{SOURCE}/child"])
def test_planner_rejects_destination_escape_or_overlap(path: str) -> None:
    with pytest.raises((ValidationError, WorktreePlanningError)):
        make_plan(request(worktree_path=path), attestation=destination(path=path))


def test_planner_rejects_path_allowlist_traversal_and_redundant_scopes() -> None:
    with pytest.raises(ValidationError):
        request(path_allowlist=["src/../tests"])
    with pytest.raises(WorktreePlanningError, match="redundant"):
        make_plan(request(path_allowlist=["src", "src/lib"]))
    with pytest.raises(WorktreePlanningError, match=".git"):
        make_plan(request(path_allowlist=[".git"]))
    with pytest.raises(WorktreePlanningError, match=".gitmodules"):
        make_plan(request(path_allowlist=[".gitmodules"]))


@pytest.mark.parametrize("kind", ["symlink", "submodule"])
def test_planner_rejects_symlink_and_submodule_boundaries_in_both_directions(kind: str) -> None:
    value = snapshot(entries=[{"path": "src/vendor", "kind": kind}])
    with pytest.raises(WorktreePlanningError, match="boundary"):
        make_plan(repo=value)
    with pytest.raises(WorktreePlanningError, match="boundary"):
        make_plan(request(path_allowlist=["src/vendor/file.py"]), repo=value)


def test_snapshot_rejects_inventory_that_descends_through_boundary() -> None:
    with pytest.raises(ValidationError, match="boundary"):
        snapshot(
            entries=[
                {"path": "src/vendor", "kind": "symlink"},
                {"path": "src/vendor/file.py", "kind": "file"},
            ]
        )


def test_destination_chain_covers_all_ancestors_and_rejects_upper_symlinks() -> None:
    nested_path = f"{WORKTREE_ROOT}/job/attempt-1"
    nested_request = request(worktree_path=nested_path)
    expected = absolute_chain(nested_path.rsplit("/", 1)[0])
    assert make_plan(nested_request, attestation=destination(path=nested_path, parent_chain=expected)).worktree_path == nested_path

    cases = [
        expected[:-1],
        expected + [{"path": nested_path, "kind": "directory"}],
        [{"path": "/tmp", "kind": "directory"}, *expected[1:]],
        [{"path": "/srv", "kind": "symlink"}, *expected[1:]],
        [{"path": WORKTREE_ROOT, "kind": "symlink"}, *expected[1:-1]],
        [{"path": "/srv", "kind": "file"}, *expected[1:]],
    ]
    for chain in cases:
        with pytest.raises((ValidationError, WorktreePlanningError)):
            make_plan(nested_request, attestation=destination(path=nested_path, parent_chain=chain))


def test_source_root_requires_complete_directory_chain() -> None:
    expected = absolute_chain(SOURCE)
    assert snapshot().source_root_chain == [type(snapshot().source_root_chain[0]).model_validate(item) for item in expected]
    cases = [
        expected[:-1],
        expected + [{"path": f"{SOURCE}/extra", "kind": "directory"}],
        [{"path": "/tmp", "kind": "directory"}, *expected[1:]],
        [{"path": "/srv", "kind": "symlink"}, *expected[1:]],
    ]
    for chain in cases:
        with pytest.raises(ValidationError):
            snapshot(source_root_chain=chain)


def test_destination_attestation_path_and_kind_are_mandatory() -> None:
    with pytest.raises(TypeError):
        plan_worktree(request(), snapshot(), policy())
    with pytest.raises(WorktreePlanningError, match="attestation path"):
        make_plan(attestation=destination(path=f"{WORKTREE_ROOT}/other"))
    with pytest.raises(WorktreePlanningError, match="absent"):
        make_plan(attestation=destination(kind="directory"))


def test_destination_parent_chain_is_not_optional_on_deserialization() -> None:
    with pytest.raises(ValidationError):
        WorktreeDestination(path=WORKTREE, kind="absent")


def test_max_files_is_enforced_from_complete_snapshot() -> None:
    value = snapshot(
        entries=[
            {"path": "src/a.py", "kind": "file"},
            {"path": "src/b.py", "kind": "file"},
        ]
    )
    with pytest.raises(WorktreePlanningError, match="max_files"):
        make_plan(request(max_files=1), value)
    with pytest.raises(WorktreePlanningError, match="incomplete"):
        make_plan(repo=snapshot(inventory_complete=False))


def test_cleanup_requires_receipt_and_matching_observation() -> None:
    plan = make_plan()
    created, observed = evidence(plan)
    with pytest.raises(TypeError):
        plan_cleanup(plan)
    with pytest.raises(WorktreePlanningError, match="receipt"):
        plan_cleanup(plan, None, observed)
    with pytest.raises(WorktreePlanningError, match="observation"):
        plan_cleanup(plan, created, observation(plan, created, head_commit="b" * 40))

    cleanup = plan_cleanup(plan, created, observed)
    assert cleanup.disposition == "remove_ready"
    assert cleanup.operations[1].argv == ["git", "worktree", "remove", "--", WORKTREE]
    assert cleanup.operations[1].precondition is not None
    assert cleanup.operations[1].precondition.expected_head == BASE_COMMIT


def test_recovery_without_complete_receipt_is_inspect_only() -> None:
    plan = make_plan()
    created, observed = evidence(plan)

    for recovery in (
        plan_recovery(plan),
        plan_recovery(plan, None, observed),
        plan_recovery(plan, created, None),
    ):
        assert recovery.disposition == "manual_review_required"
        assert "manual_review_required" in recovery.reason
        assert [operation.kind for operation in recovery.operations] == [WorktreeOperationKind.INSPECT_WORKTREES]
        assert recovery.remove_precondition is None

    ready = plan_recovery(plan, created, observed)
    assert ready.disposition == "remove_ready"
    assert len(ready.operations) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_plan_hash", "sha256:" + "b" * 64),
        ("create_operation_id", "sha256:" + "b" * 64),
        ("common_git_dir", "/srv/other/.git"),
        ("worktree_gitdir", "/srv/other/.git/worktrees/job"),
        ("worktree_path", f"{WORKTREE_ROOT}/other"),
        ("head_commit", "b" * 40),
    ],
)
def test_receipt_or_observation_mismatch_never_produces_remove(field: str, value: str) -> None:
    plan = make_plan()
    created, observed = evidence(plan)
    if field in {"parent_plan_hash", "create_operation_id", "common_git_dir", "worktree_gitdir", "worktree_path", "head_commit"}:
        bad_values = {**created.model_dump(by_alias=True), field: value, "receipt_digest": None}
        bad_receipt = created.model_validate(bad_values)
        with pytest.raises(WorktreePlanningError):
            plan_cleanup(plan, bad_receipt, observed)
    else:
        bad_observation = observed.model_validate({**observed.model_dump(by_alias=True), field: value})
        with pytest.raises(WorktreePlanningError):
            plan_cleanup(plan, created, bad_observation)


def test_observation_requires_transport_bindings_and_no_fresh_boolean() -> None:
    plan = make_plan()
    created, observed = evidence(plan)
    with pytest.raises(ValidationError):
        WorktreePorcelainAttestation.model_validate({**observed.model_dump(), "fresh": True})
    with pytest.raises(ValidationError):
        WorktreeCreationReceipt.model_validate({**created.model_dump(), "success": False})


def test_plan_rehydration_rejects_assignment_model_copy_and_list_mutation() -> None:
    original = make_plan()
    created, observed = evidence(original)

    assigned = original
    assigned.worktree_path = f"{WORKTREE_ROOT}/other"
    with pytest.raises(WorktreePlanningError):
        plan_cleanup(assigned, created, observed)

    copied = original.model_copy(update={"worktree_path": f"{WORKTREE_ROOT}/other"})
    with pytest.raises(WorktreePlanningError):
        plan_recovery(copied, created, observed)

    list_mutated = make_plan()
    list_mutated.operations[2].argv[4] = f"{WORKTREE_ROOT}/other"
    created, observed = evidence(list_mutated)
    with pytest.raises(WorktreePlanningError):
        plan_recovery(list_mutated, created, observed)


def test_action_rehydration_closes_nested_evidence_and_operation_mutation() -> None:
    plan = make_plan()
    created, observed = evidence(plan)
    action = plan_cleanup(plan, created, observed)

    action_copy = action.model_copy(update={"worktree_path": f"{WORKTREE_ROOT}/other"})
    with pytest.raises(WorktreePlanningError):
        rehydrate_action_plan(action_copy)

    action.operations[1].approved_paths[0] = f"{WORKTREE_ROOT}/other"
    with pytest.raises(WorktreePlanningError):
        rehydrate_action_plan(action)

    with pytest.raises(ValidationError):
        WorktreeActionPlan.model_validate(
            {
                **action.model_dump(),
                "disposition": "remove_ready",
                "creation_receipt": None,
                "porcelain_observation": None,
            }
        )


def test_operation_contract_rejects_shell_and_unapproved_absolute_paths() -> None:
    with pytest.raises(ValidationError):
        GitOperation(
            kind=WorktreeOperationKind.CREATE_WORKTREE,
            cwd=SOURCE,
            argv=["git", "worktree", "add", "/tmp/escape", BASE_COMMIT],
        )
    with pytest.raises(ValidationError):
        GitOperation(
            kind=WorktreeOperationKind.INSPECT_WORKTREES,
            cwd=SOURCE,
            argv=["git", "worktree", "list;rm", "--porcelain"],
        )


def test_security_critical_identity_and_path_values_cannot_be_redacted() -> None:
    with pytest.raises(ValidationError):
        request(request_id="sk-secret")
    with pytest.raises(ValidationError):
        request(path_allowlist=["src/sk-secret"])
    with pytest.raises(ValidationError):
        snapshot(source_root="/srv/sk-secret")
    with pytest.raises(ValidationError):
        policy(worktree_root="/srv/sk-secret")
    with pytest.raises(ValidationError):
        destination(path="/srv/sk-secret/attempt-1")


def test_planner_sources_have_no_execution_or_filesystem_dependency() -> None:
    forbidden_modules = {"subprocess", "os", "pathlib", "shutil", "docker", "podman"}
    forbidden_calls = {"open", "unlink", "remove", "rmtree", "mkdir", "makedirs", "run", "Popen", "system"}
    for source in (
        Path(__file__).parents[1] / "src" / "sandbox" / "worktree.py",
        Path(__file__).parents[1] / "src" / "sandbox" / "worktree_contracts.py",
    ):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden_modules for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden_modules
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls


def test_action_does_not_include_repo_wide_prune() -> None:
    plan = make_plan()
    created, observed = evidence(plan)
    action = plan_cleanup(plan, created, observed)
    assert all("prune" not in operation.argv for operation in action.operations)


def test_base_snapshot_is_attestation_but_verify_operation_rechecks_exact_commit() -> None:
    plan = make_plan()
    assert plan.snapshot.base_commit_verified is True
    assert plan.operations[0].argv == [
        "git",
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{BASE_COMMIT}^{{commit}}",
    ]


def test_worktree_request_keeps_phase_3a_minimal_constructor_compatible() -> None:
    value = WorktreeRequest(base_commit=BASE_COMMIT, path_allowlist=["src"], max_files=1)
    assert value.repository_id is None
