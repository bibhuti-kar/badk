"""`claude -p` backend (design 8.2, D-17). The only module that spawns the `claude` binary."""

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from badk.agents.definition import AgentDef
from badk.config import Settings
from badk.engine.tokens import estimate_messages, estimate_tokens
from badk.errors import BadkError, Code
from badk.exec.document import Message
from badk.proc import run_group
from badk.router.reply import ModelReply, count, fail

CLAUDE_BIN: Final[str] = "claude"
MAX_REPLY_BYTES: Final = 16_000_000  # a cut JSON reply fails to parse: PROVIDER_ERROR


def _fail(code: Code, why: str) -> BadkError:
    return fail("claude_cli", code, why)


def _missing() -> BadkError:
    return _fail(Code.PROVIDER_UNAVAILABLE, f"`{CLAUDE_BIN}` not found on PATH")


def preflight(agents: Mapping[str, AgentDef]) -> None:
    """RT-2.2 (design 10.1: over `ctx.agents`): a `claude_cli` agent and no `claude` on PATH
    ends the run before any step."""
    if any(a.provider == "claude_cli" for a in agents.values()) and not shutil.which(CLAUDE_BIN):
        raise _missing()


def render_prompt(messages: list[Message]) -> tuple[str, str]:
    """(system prompt = the prefix, stdin = the rest in payload order)."""
    rest = "\n\n".join(f"[{m.role.upper()}]\n{m.content}" for m in messages[1:])
    return messages[0].content, rest


def _argv(system: str, model: str) -> list[str]:
    argv = [CLAUDE_BIN, "-p", "--output-format", "json", "--no-session-persistence"]
    argv += ["--tools", "", "--strict-mcp-config", "--setting-sources", ""]
    argv += ["--system-prompt", system]
    return argv + (["--model", model] if model else [])


def _spawn(argv: list[str], stdin: str, timeout: int) -> subprocess.CompletedProcess[str]:
    # Neutral cwd: the nested claude must not pick up this repo's CLAUDE.md or project state.
    # Full environment: claude needs HOME and the Keychain context.
    with tempfile.TemporaryFile() as f:
        f.write(stdin.encode())
        f.seek(0)
        try:
            code, out, err, _ = run_group(
                argv,
                stdin=f,
                cwd=Path(tempfile.gettempdir()),
                timeout=timeout,
                env=None,
                cap=MAX_REPLY_BYTES,
            )
        except OSError as e:  # FileNotFoundError, PermissionError, ENOEXEC
            raise _missing() from e
        except subprocess.TimeoutExpired as e:
            raise _fail(Code.PROVIDER_TIMEOUT, f"no reply within {timeout}s") from e
    return subprocess.CompletedProcess(argv, code, out, err)


def _parse(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """stdout JSON first: real claude reports auth/rate-limit failures as is_error JSON with
    exit 1 and empty stderr. stderr is used only when stdout is not JSON."""
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        if proc.returncode != 0:
            raise _fail(
                Code.PROVIDER_ERROR, f"exit {proc.returncode}: {proc.stderr[-200:].strip()}"
            )
        raise _fail(Code.PROVIDER_ERROR, "stdout is not a JSON object")
    if data.get("is_error"):
        raise _fail(Code.PROVIDER_ERROR, f"{data.get('subtype')}: {str(data.get('result'))[:200]}")
    if proc.returncode != 0:
        raise _fail(Code.PROVIDER_ERROR, f"exit {proc.returncode}")
    if not isinstance(data.get("result"), str):
        raise _fail(Code.PROVIDER_ERROR, "reply has no string `result`")
    return data


def complete(
    agent: AgentDef, messages: list[Message], settings: Settings, model: str
) -> ModelReply:
    system, stdin = render_prompt(messages)
    t0 = time.perf_counter()
    proc = _spawn(_argv(system, model), stdin, settings.provider_timeout_sec)
    latency = int((time.perf_counter() - t0) * 1000)
    data = _parse(proc)
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    base = count(usage.get("input_tokens"), -1)
    tin = estimate_messages(messages) if base < 0 else base
    if base >= 0:
        tin += count(usage.get("cache_read_input_tokens"), 0)
        tin += count(usage.get("cache_creation_input_tokens"), 0)
    text = data["result"]
    return ModelReply(text, tin, count(usage.get("output_tokens"), estimate_tokens(text)), latency)
