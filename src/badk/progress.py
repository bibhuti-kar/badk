"""Headless progress log (design 6.3). Write-only: nothing in src/ reads progress/ back."""

from pathlib import Path
from typing import Any, TextIO

from badk.exec.document import utc_now_iso
from badk.stderr_log import get_logger

log = get_logger()
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_ALIASES = {"WARN": "WARNING", "CRITICAL": "ERROR", "FATAL": "ERROR"}


def _norm(level: object) -> str:
    lvl = str(level).upper()
    lvl = _ALIASES.get(lvl, lvl)
    return lvl if lvl in _LEVELS else "INFO"


def _one_line(text: str) -> str:
    return text.replace("\r", "\\r").replace("\n", "\\n")


class ProgressLog:
    """A Sink writing `<ts> <LEVEL> <agent_key|run> <msg>` lines. Never raises."""

    def __init__(self, level: str, directory: Path = Path("progress")) -> None:
        self._min = _LEVELS[_norm(level)]
        self._dir = directory
        self._file: TextIO | None = None
        self._dead = False
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # design 6.3: a progress-log fault never fails a run
            self._disable("progress log unavailable", e)

    def _disable(self, what: str, e: Exception) -> None:
        self._dead = True
        log.warning(what, error=str(e))

    def __call__(self, event: dict[str, Any]) -> None:
        if self._dead:
            return
        try:
            if self._file is None:
                self._file = (self._dir / f"{event['run_id']}.log").open(
                    "a", buffering=1, encoding="utf-8"
                )
            ts = event.get("ts") or utc_now_iso()
            for lvl, who, msg, force in _lines(event):
                if force or _LEVELS[lvl] >= self._min:
                    self._file.write(f"{ts} {lvl} {who} {_one_line(msg)}\n")
        except Exception as e:  # logged once, later lines dropped
            self._disable("progress log write failed", e)

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        except Exception as e:
            log.warning("progress log close failed", error=str(e))
        self._file = None


def _lines(event: dict[str, Any]) -> list[tuple[str, str, str, bool]]:
    key, cap, head = event["agent_key"], event["capsule"], event["header"]
    metrics = event.get("metrics") or {}
    out = [
        ("INFO", key, f"event eid={event['eid']} status={cap['status']}", False),
        ("DEBUG", key, "metrics " + " ".join(f"{k}={v}" for k, v in metrics.items()), False),
    ]
    out += [
        (_norm(e.get("level", "INFO")), key, str(e.get("msg", "")), False)
        for e in event.get("logs") or []
    ]
    if head["status"] != "RUNNING":  # last line: terminal run status, whatever LOG_LEVEL
        err = (cap.get("error") or {}).get("code")
        status = f"status={head['status']}" + (f" code={err}" if err else "")
        out.append(("INFO", "run", status, True))
    return out
