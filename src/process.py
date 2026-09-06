"""Bounded synchronous subprocess capture using Python 3.12 pipe support."""

from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import time
from typing import Mapping, Sequence


class BoundedProcessError(RuntimeError):
    """A process exceeded its execution or combined-output budget."""


def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[int, bytes, bytes]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    if not isinstance(max_output_bytes, int) or max_output_bytes < 1:
        raise ValueError("max_output_bytes must be a positive integer")
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
    )
    assert process.stdout is not None and process.stderr is not None
    streams = (process.stdout, process.stderr)
    buffers = [bytearray(), bytearray()]
    active = {0, 1}
    size = 0
    try:
        # Windows selectors only support sockets. Python 3.12 also supports
        # nonblocking Windows pipes, avoiding reader threads and unbounded queues.
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        while active:
            if time.monotonic() >= deadline:
                raise BoundedProcessError("timed out")
            progressed = False
            for index in tuple(active):
                try:
                    data = os.read(streams[index].fileno(), min(65_536, max_output_bytes - size + 1))
                except BlockingIOError:
                    continue
                progressed = True
                if not data:
                    active.remove(index)
                    continue
                size += len(data)
                if size > max_output_bytes:
                    raise BoundedProcessError("output limit exceeded")
                buffers[index].extend(data)
            if not progressed:
                time.sleep(max(0, min(0.01, deadline - time.monotonic())))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BoundedProcessError("timed out")
        try:
            exit_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise BoundedProcessError("timed out") from exc
        return exit_code, bytes(buffers[0]), bytes(buffers[1])
    finally:
        # Also reap the child on unexpected read/setup errors, not just limits.
        try:
            if process.poll() is None:
                process.kill()
            process.wait()
        finally:
            for stream in streams:
                stream.close()
