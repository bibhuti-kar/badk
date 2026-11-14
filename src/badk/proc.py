"""Run a child in its own session with file-backed stdio; the whole group is killed whenever
the call returns or fails (background processes never outlive it).
Shared by tools/execute.py and router/claude_cli.py."""

import atexit
import contextlib
import contextvars
import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import IO


def _read_capped(f: IO[bytes], cap: int) -> tuple[str, bool]:
    f.seek(0)
    data = f.read(cap + 1)
    return data[:cap].decode(errors="replace"), len(data) > cap


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # already gone/zombie
        os.killpg(pgid, signal.SIGKILL)


class Children:
    """The live child process groups of one SDK run (SDK-4). `cancel` kills them all and makes
    every later spawn die at once, so no worker thread starts new work after an interrupt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pgids: set[int] = set()
        self.cancelled = False

    def add(self, pgid: int) -> None:
        with self._lock:
            self._pgids.add(pgid)
            if self.cancelled:
                _kill_group(pgid)

    def discard(self, pgid: int) -> None:
        with self._lock:
            self._pgids.discard(pgid)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            for pgid in self._pgids:
                _kill_group(pgid)


# Set by the SDK for the run; threads started with `contextvars.copy_context()` inherit it.
CURRENT: contextvars.ContextVar[Children | None] = contextvars.ContextVar("children", default=None)


LIVE: set[Children] = set()  # runs in flight in this process (SDK-4.3)


def begin() -> tuple[Children, contextvars.Token[Children | None]]:
    """Start tracking one run's children; pair with `end`."""
    ch = Children()
    LIVE.add(ch)
    return ch, CURRENT.set(ch)


def end(ch: Children, token: contextvars.Token[Children | None]) -> None:
    LIVE.discard(ch)
    CURRENT.reset(token)


@atexit.register
def cancel_all() -> None:
    """At interpreter exit kill every live run's child groups (a run started in a non-main
    thread has no KeyboardInterrupt path). ponytail: the runs stay RUNNING in the DB, nothing
    here can finalise them reliably once the interpreter is shutting down."""
    for ch in list(LIVE):
        ch.cancel()


def cancelled() -> bool:
    c = CURRENT.get()
    return c is not None and c.cancelled


class RunInterrupted(BaseException):  # noqa: N818 - BaseException so retry code never swallows it
    """Raised at a cancel point (model call, tool call) once the run was interrupted (20.9)."""


def run_group(  # noqa: PLR0913 - keyword-only knobs, one call shape
    argv: list[str],
    *,
    stdin: IO[bytes] | int,
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None,
    cap: int,
) -> tuple[int, str, str, bool]:
    """(exit, stdout, stderr, truncated). Raises subprocess.TimeoutExpired after killing the
    group; KeyboardInterrupt or any other error kills it too, and so does a normal return:
    background children never outlive the call. ponytail: the leader is reaped before the
    kill (os.waitid is absent on macOS); POSIX keeps the pgid unreusable while any member lives,
    and only an empty group in a pid-wraparound window could be mis-hit.
    OSError from the spawn propagates.
    Output goes to temp files, not pipes: a descendant that escaped the group and holds the
    descriptor cannot block us after the kill. ponytail: disk fills until the timeout."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(  # noqa: S603 - callers pass quoted or fixed argv
            argv,
            start_new_session=True,
            env=env,
            cwd=cwd,
            stdin=stdin,
            stdout=out,
            stderr=err,
        )
        ch = CURRENT.get()
        if ch:
            ch.add(p.pid)
        try:
            p.wait(timeout=timeout)
        except BaseException:
            _kill_group(p.pid)
            p.wait()
            raise
        finally:
            if ch:
                ch.discard(p.pid)
        _kill_group(p.pid)  # background children must not outlive the call
        so, cut_o = _read_capped(out, cap)
        se, cut_e = _read_capped(err, cap)
    return p.returncode, so, se, cut_o or cut_e
