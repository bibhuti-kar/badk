"""Model router (design 8): the only module that reaches a model."""

import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from badk.agents.definition import AgentDef
from badk.config import Settings
from badk.engine.compress import COMPRESS_INSTRUCTION
from badk.engine.tokens import estimate_messages, estimate_tokens
from badk.errors import BadkError, Code, register_secret
from badk.exec.document import Message
from badk.router import anthropic, claude_cli, openai_compat
from badk.router.reply import ModelReply

__all__ = ["ModelReply", "complete", "preflight"]

_SELECT_DEFAULT: dict[str, Any] = {
    "done": False,
    "tool_calls": [],
    "result": {"calls": []},
    "notes": "fake",
}
_DEFAULT: dict[str, Any] = {"done": True, "tool_calls": [], "result": {}, "notes": "fake"}


def complete(agent: AgentDef, messages: list[Message], settings: Settings) -> ModelReply:
    register_secret(settings.llm_api_key.get_secret_value())
    if agent.provider is None or agent.model is None:
        raise BadkError(Code.INTERNAL_ERROR, f"route not resolved for {agent.name}")
    if agent.provider == "fake":
        return _fake(agent, messages, settings)
    if agent.provider == "openai":
        return openai_compat.complete(agent, messages, settings)
    if agent.provider == "anthropic":
        return anthropic.complete(agent, messages, settings)
    if agent.provider == "claude_cli":
        return claude_cli.complete(agent, messages, settings, agent.model)
    raise BadkError(Code.INTERNAL_ERROR, f"unknown provider {agent.provider}")


preflight = claude_cli.preflight


def _unreadable(why: object) -> BadkError:
    return BadkError(Code.VALIDATION_ERROR, f"FAKE_SCRIPT unreadable: {why}")


def _check_entry(entry: object) -> None:
    if not isinstance(entry, dict):
        raise _unreadable(f"reply entry must be an object, got {entry!r}")
    sleep = entry.get("sleep_ms", 0)
    if isinstance(sleep, bool) or not isinstance(sleep, int) or sleep < 0:
        raise _unreadable(f"sleep_ms must be a non-negative integer, got {sleep!r}")


def _check_script(script: object) -> dict[str, Any]:
    if not isinstance(script, dict):
        raise _unreadable("must be a JSON object")
    replies = script.get("replies", {})
    if not isinstance(replies, dict):
        raise _unreadable("replies must be an object")
    for name, entries in replies.items():
        if not isinstance(entries, list):
            raise _unreadable(f"replies.{name} must be a list")
        for e in entries:
            _check_entry(e)
    if "default" in script:
        _check_entry(script["default"])
    return script


def _inline(spec: str) -> bool:
    return not spec or spec.lstrip().startswith("{")


def _stamp(spec: str) -> tuple[int, int]:
    """(mtime_ns, size) of a path spec; (0, 0) for inline/empty/unstatable specs."""
    if _inline(spec):
        return 0, 0
    try:
        st = Path(spec).stat()
    except OSError:
        return 0, 0
    return st.st_mtime_ns, st.st_size


@lru_cache(maxsize=8)
def _parse_script(spec: str, mtime_ns: int, size: int) -> dict[str, Any]:
    """Parsed and shape-checked once per (FAKE_SCRIPT value, file mtime, size)."""
    if not spec:
        return {}
    try:
        text = spec if _inline(spec) else Path(spec).read_text()
        return _check_script(json.loads(text))
    except (OSError, ValueError) as e:
        raise _unreadable(e) from e


def _fake_compress(messages: list[Message]) -> ModelReply:
    """Design 8.3: journal = last 400 chars of the rendered history."""
    env = {
        "done": True,
        "tool_calls": [],
        "result": {"journal": messages[2].content[-400:]},
        "notes": "fake compress",
    }
    text = json.dumps(env)
    return ModelReply(text, estimate_messages(messages), estimate_tokens(text), 0)


def _phase(messages: list[Message]) -> str | None:
    """The newest `PHASE: X` input: scans past CONTINUE/TOOL_RESULT turns and reads the input a
    compress marker carries, so a restart's re-sent input keeps its phase (WF-6)."""
    for m in reversed(messages):
        text = m.content.split("CURRENT INPUT:\n", 1)[-1] if m.role == "system" else m.content
        if m.content.startswith(("TOOL_RESULT", "CONTINUE")):
            continue
        if m.role == "user" or m.content.startswith("COMPRESSED:"):
            found = re.findall(r"^PHASE: (SELECT|CONSOLIDATE)", text, re.M)
            if found:
                return str(found[-1])
    return None


def _fake(agent: AgentDef, messages: list[Message], settings: Settings) -> ModelReply:
    """Design 8.3: deterministic; entry index = assistant messages so far, clamped."""
    if len(messages) > 2 and messages[1].content.startswith(COMPRESS_INSTRUCTION):
        return _fake_compress(messages)
    script = _parse_script(settings.fake_script, *_stamp(settings.fake_script))
    entries = script.get("replies", {}).get(agent.name) or []
    entry: dict[str, Any] = _DEFAULT
    if _phase(messages) == "SELECT":  # WF-3: the select phase needs done false and `calls`
        entry = _SELECT_DEFAULT
    if entries:
        idx = sum(m.role == "assistant" for m in messages)
        entry = entries[min(idx, len(entries) - 1)]
    elif "default" in script:
        entry = script["default"]
    sleep_ms = int(entry.get("sleep_ms", 0))
    if sleep_ms:
        time.sleep(sleep_ms / 1000)
    text = (
        str(entry["raw"])
        if "raw" in entry
        else json.dumps({k: v for k, v in entry.items() if k != "sleep_ms"})
    )
    return ModelReply(text, estimate_messages(messages), estimate_tokens(text), sleep_ms)
