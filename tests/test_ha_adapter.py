from pathlib import Path

import pytest

from src.ingestion.ha_adapter import HAAdapterError, read_ha_context


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
