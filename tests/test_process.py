import sys
import time

import pytest

from src import process as process_module
from src.ha_cli import HaCliError, SubprocessTransport
from src.target_publication import SubprocessPublicationTransport, TargetPublicationError


@pytest.fixture(params=[(SubprocessTransport, HaCliError), (SubprocessPublicationTransport, TargetPublicationError)])
def transport(request):
    cls, error = request.param
    return cls(), error


def run(transport, tmp_path, code, *, limit=1024, timeout=5):
    return transport.run(
        [sys.executable, "-c", code], cwd=tmp_path, env={},
        timeout_seconds=timeout, max_output_bytes=limit,
    )


def test_captures_both_streams_and_nonzero_exit(transport, tmp_path):
    result = run(transport[0], tmp_path, "import os; os.write(1, b'out'); os.write(2, b'err'); raise SystemExit(7)", limit=6)
    assert (result.exit_code, result.stdout, result.stderr) == (7, b"out", b"err")


@pytest.mark.parametrize("fd", [1, 2])
def test_bounds_continuous_output_on_either_stream(transport, tmp_path, fd):
    with pytest.raises(transport[1], match="output limit"):
        run(transport[0], tmp_path, f"import os\nwhile True: os.write({fd}, b'x' * 65536)")


def test_bounds_combined_output(transport, tmp_path):
    with pytest.raises(transport[1], match="output limit"):
        run(transport[0], tmp_path, "import os; os.write(1, b'123'); os.write(2, b'456')", limit=5)


def test_empty_output_and_stdin_eof(transport, tmp_path):
    result = run(transport[0], tmp_path, "import sys; assert sys.stdin.read() == ''")
    assert (result.exit_code, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process startup contract")
def test_empty_environment_keeps_windows_system_root(tmp_path):
    result = run(
        SubprocessTransport(),
        tmp_path,
        "import os; assert os.environ.get('SystemRoot')",
    )
    assert result.exit_code == 0


@pytest.mark.parametrize("code", [
    "import time; time.sleep(30)",
    "import os, time; os.close(1); os.close(2); time.sleep(30)",
])
def test_timeout_reaps_child_and_closes_pipes(transport, tmp_path, monkeypatch, code):
    real_popen = process_module.subprocess.Popen
    children = []

    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(process_module.subprocess, "Popen", capture)
    started = time.monotonic()
    with pytest.raises(transport[1], match="timed out"):
        run(transport[0], tmp_path, code, timeout=0.5)
    assert time.monotonic() - started < 5
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


def test_setup_error_reaps_child_and_closes_pipes(tmp_path, monkeypatch):
    real_popen = process_module.subprocess.Popen
    children = []

    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail(*args):
        raise OSError("pipe setup failed")

    monkeypatch.setattr(process_module.subprocess, "Popen", capture)
    monkeypatch.setattr(process_module.os, "set_blocking", fail)
    with pytest.raises(OSError, match="pipe setup failed"):
        run(SubprocessTransport(), tmp_path, "import time; time.sleep(30)")
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed
