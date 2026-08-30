import pytest
from pydantic import ValidationError

from src.sandbox import (
    FakePatchChecker,
    PatchBundle,
    PatchChangeKind,
    PatchCheckReceipt,
    PatchCheckStatus,
    PatchEntry,
    PatchLimits,
    WorktreeDestination,
    WorktreePolicy,
    WorktreeRequest,
    RepositorySnapshot,
    plan_patch,
    plan_patch_retry,
    plan_worktree,
    check_patch,
)


BASE = "a" * 40
SOURCE = "/srv/ha-target"
ROOT = "/srv/mi-worktrees"


def chain(path: str):
    current = ""
    result = []
    for component in path.split("/")[1:]:
        current += "/" + component
        result.append({"path": current, "kind": "directory"})
    return result


def worktree():
    request = WorktreeRequest(
        base_commit=BASE,
        path_allowlist=["src", "tests"],
        max_files=20,
        repository_id="ha-target",
        source_root=SOURCE,
        worktree_path=f"{ROOT}/attempt-1",
        request_id="attempt-1",
    )
    snapshot = RepositorySnapshot(
        repository_id="ha-target",
        source_root=SOURCE,
        source_root_chain=chain(SOURCE),
        base_commit=BASE,
        entries=[
            {"path": "src/main.py", "kind": "file"},
            {"path": "tests/test_main.py", "kind": "file"},
        ],
    )
    policy = WorktreePolicy(
        repository_allowlist=[{"repository_id": "ha-target", "source_root": SOURCE}],
        worktree_root=ROOT,
    )
    destination = WorktreeDestination(
        path=f"{ROOT}/attempt-1", parent_chain=chain(ROOT)
    )
    return plan_worktree(request, snapshot, policy, destination), snapshot


DIFF = "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n"


def test_patch_plan_is_deterministic_and_uses_bounded_checks():
    plan, snapshot = worktree()
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    first = plan_patch(bundle, plan, snapshot)
    second = plan_patch(bundle, plan, snapshot)
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert first.request.apply_check.model_dump() == {
        "command": "git", "args": ["apply", "--check", "--whitespace=error-all", "-"]
    }
    assert first.request.diff_check.args == ["diff", "--check", "--"]
    assert first.validation.paths == ["src/main.py"]


def test_checker_success_failure_and_request_binding():
    plan, snapshot = worktree()
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    planned = plan_patch(bundle, plan, snapshot)
    success = check_patch(planned, bundle, FakePatchChecker())
    assert success.status is PatchCheckStatus.PASSED
    failure = FakePatchChecker(default=PatchCheckReceipt(
        request_digest="sha256:" + "0" * 64, status=PatchCheckStatus.FAILED, reason="apply rejected"
    ))
    assert check_patch(planned, bundle, failure).status is PatchCheckStatus.FAILED


def test_retry_is_once_and_starts_from_clean_base():
    plan, snapshot = worktree()
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    planned = plan_patch(bundle, plan, snapshot)
    retry = plan_patch_retry(planned, bundle)
    assert retry.request.retry_index == 1 and retry.request.clean_base is True
    with pytest.raises(ValueError, match="exactly one retry"):
        plan_patch_retry(retry, bundle)


@pytest.mark.parametrize("entry", [
    {"path": "../escape", "patch": DIFF},
    {"path": ".git/config", "patch": DIFF},
    {"path": "src/main.py", "patch": "--- a/../escape\n+++ b/src/main.py\n@@ -1 +1 @@\n-a\n+b\n"},
])
def test_malicious_paths_are_rejected(entry):
    with pytest.raises((ValidationError, ValueError)):
        PatchEntry(**entry)


def test_base_allowlist_and_boundary_rejected():
    plan, snapshot = worktree()
    with pytest.raises(ValueError, match="base_commit"):
        plan_patch(PatchBundle(base_commit="b" * 40, files=[PatchEntry(path="src/main.py", patch=DIFF)]), plan, snapshot)
    with pytest.raises(ValueError, match="allowlist"):
        out_diff = DIFF.replace("src/main.py", "docs/a.py")
        plan_patch(PatchBundle(base_commit=BASE, files=[PatchEntry(path="docs/a.py", patch=out_diff)]), plan, snapshot)
    boundary_payload = snapshot.model_dump(mode="json")
    boundary_payload["entries"] = [{"path": "src/vendor", "kind": "symlink"}]
    boundary = RepositorySnapshot.model_validate(boundary_payload)
    with pytest.raises(ValueError, match="boundary"):
        boundary_diff = DIFF.replace("src/main.py", "src/vendor")
        plan_patch(PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/vendor", patch=boundary_diff)]), plan, boundary)


def test_limits_and_feature_boundaries_default_deny():
    plan, snapshot = worktree()
    binary = PatchEntry(path="src/main.py", patch="GIT binary patch\n", kind=PatchChangeKind.BINARY, binary=True)
    with pytest.raises(ValueError, match="binary"):
        plan_patch(PatchBundle(base_commit=BASE, files=[binary]), plan, snapshot)
    deleted = PatchEntry(path="src/main.py", patch=DIFF, kind=PatchChangeKind.DELETE)
    with pytest.raises(ValueError, match="delete"):
        plan_patch(PatchBundle(base_commit=BASE, files=[deleted]), plan, snapshot)
    huge = PatchEntry(path="src/main.py", patch=DIFF + ("x" * 100))
    with pytest.raises(ValueError, match="byte"):
        plan_patch(PatchBundle(base_commit=BASE, files=[huge]), plan, snapshot, PatchLimits(max_file_bytes=10))
    fuzzy = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)], fuzzy=True)
    with pytest.raises(ValueError, match="fuzzy"):
        plan_patch(fuzzy, plan, snapshot)


def test_empty_patch_is_rejected():
    plan, snapshot = worktree()
    with pytest.raises(ValidationError):
        PatchBundle(base_commit=BASE, files=[])


def test_boundary_ancestor_rejects_descendant_patch_path():
    plan, snapshot = worktree()
    boundary_payload = snapshot.model_dump(mode="json")
    boundary_payload["entries"] = [{"path": "src/vendor", "kind": "symlink"}]
    boundary = RepositorySnapshot.model_validate(boundary_payload)
    descendant = DIFF.replace("src/main.py", "src/vendor/file.py")
    with pytest.raises(ValueError, match="boundary"):
        plan_patch(PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/vendor/file.py", patch=descendant)]), plan, boundary)


def test_entrypoint_rehydrates_and_rejects_mutated_objects():
    plan, snapshot = worktree()
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    planned = plan_patch(bundle, plan, snapshot)
    plan.request.base_commit = "b" * 40
    with pytest.raises(ValueError, match="integrity"):
        plan_patch(bundle, plan, snapshot)
    plan, snapshot = worktree()
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    planned = plan_patch(bundle, plan, snapshot)
    bundle.files.append(PatchEntry(path="tests/test_main.py", patch=DIFF.replace("src/main.py", "tests/test_main.py")))
    with pytest.raises(ValueError, match="integrity"):
        plan_patch(bundle, plan, snapshot)


def test_checker_failures_are_redacted_and_fail_closed():
    plan, snapshot = worktree()
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    planned = plan_patch(bundle, plan, snapshot)

    class Raising:
        def check(self, request, patch):
            raise RuntimeError("secret=must-not-leak")

    rejected = check_patch(planned, bundle, Raising())
    assert rejected.status is PatchCheckStatus.REJECTED
    assert "secret" not in rejected.model_dump_json()

    class Invalid:
        def check(self, request, patch):
            return object()

    assert check_patch(planned, bundle, Invalid()).status is PatchCheckStatus.REJECTED


def test_non_binary_requires_unified_headers_and_cwd_is_bound():
    with pytest.raises(ValueError, match="unified diff"):
        PatchEntry(path="src/main.py", patch="@@ -1 +1 @@\n-old\n+new\n")
    plan, snapshot = worktree()
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    planned = plan_patch(bundle, plan, snapshot)
    assert planned.request.cwd == planned.request.worktree_path == planned.worktree_path
    planned.request.cwd = "/srv/other"
    with pytest.raises(ValueError, match="integrity"):
        check_patch(planned, bundle, FakePatchChecker())


def test_change_kind_inventory_preconditions_are_fail_closed():
    plan, snapshot = worktree()
    with pytest.raises(ValueError, match="already exists"):
        plan_patch(PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF, kind=PatchChangeKind.ADD)]), plan, snapshot)
    with pytest.raises(ValueError, match="regular file"):
        delete_diff = DIFF.replace("src/main.py", "src/missing.py")
        plan_patch(PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/missing.py", patch=delete_diff, kind=PatchChangeKind.DELETE)]), plan, snapshot, PatchLimits(allow_deletes=True))
    with pytest.raises(ValueError, match="regular file"):
        rename_diff = "--- a/src/missing.py\n+++ b/src/new.py\n@@ -1 +1 @@\n-old\n+new\n"
        entry = PatchEntry(path="src/new.py", old_path="src/missing.py", new_path="src/new.py", patch=rename_diff, kind=PatchChangeKind.RENAME)
        plan_patch(PatchBundle(base_commit=BASE, files=[entry]), plan, snapshot, PatchLimits(allow_renames=True))
