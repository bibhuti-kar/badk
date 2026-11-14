"""`badk check`: config, agents, tools, provider, broker, viewer and database, before any run."""

import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from badk.agents.definition import AgentDef, load_agents
from badk.agents.flow import is_coordinator, require_coordinator
from badk.config import Settings, load_settings
from badk.errors import BadkError, Code
from badk.router import preflight
from badk.stack_check import stack_check
from badk.tools.definition import validate_all

_DB_CHECK_SEC = 4


def _agents(conf: Settings, root: Path) -> dict[str, AgentDef]:
    agents = load_agents(root / "agents", conf, tools=root / "tools", env_file=root / ".env")
    validate_all(root / "tools")
    require_coordinator(agents)
    return agents


def _provider(agents: dict[str, AgentDef] | None, conf: Settings) -> str:
    """Every provider in use: the coordinator's (sub-agents with none inherit it) and literals."""
    used = {a.provider for a in (agents or {}).values() if a.provider} or {conf.llm_provider}
    key = conf.llm_api_key.get_secret_value()
    if "anthropic" in used and not key:
        raise BadkError(Code.VALIDATION_ERROR, "provider anthropic needs LLM_API_KEY")
    preflight(agents or {})  # claude_cli in use and no `claude` on PATH
    where = {
        "openai": conf.llm_base_url,
        "anthropic": os.environ.get("ANTHROPIC_BASE_URL") or "SDK default",
    }
    shown = ", ".join(f"{p} at {where[p]}" if p in where else p for p in sorted(used))
    return f"{shown}; key {'set' if key else 'empty'}"


def _database(conf: Settings) -> str:
    try:
        r = httpx.get(f"{conf.viewer_url}/api/v1/runs?per_page=1", timeout=_DB_CHECK_SEC)
        total = r.json()["total"] if r.status_code == httpx.codes.OK else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        raise BadkError(Code.VIEWER_UNAVAILABLE, f"viewer API: {type(e).__name__}") from e
    if total is None:
        raise BadkError(Code.VIEWER_UNAVAILABLE, f"run list gave HTTP {r.status_code}")
    return f"{total} runs stored"


def _stack(conf: Settings) -> str:
    stack_check(conf)
    return "reachable"


def _coordinator(agents: dict[str, AgentDef]) -> str:
    c = agents.get("coordinator") or next(a for n, a in sorted(agents.items()) if is_coordinator(n))
    return f"{len(agents)} agents; coordinator {c.provider} / {c.model}"


def _step[T](name: str, fn: Callable[[], T], show: Callable[[T], str]) -> T | None:
    try:
        out = fn()
    except BadkError as e:
        sys.stdout.write(f"FAIL  {name}: {e.code.value}: {e.message}\n")
        return None
    sys.stdout.write(f"ok    {name}: {show(out)}\n")
    return out


def check(root: Path) -> int:
    """One line per item; exit 0 when every item is ok. A config failure stops the rest."""
    conf = _step("config", lambda: load_settings(root / ".env"), lambda c: c.llm_provider)
    if conf is None:
        return 1
    agents = _step("agents", lambda: _agents(conf, root), _coordinator)
    results = [
        agents,
        _step("provider", lambda: _provider(agents, conf), str),
        _step("broker+viewer", lambda: _stack(conf), str),
        _step("database", lambda: _database(conf), str),
    ]
    return 0 if all(r is not None for r in results) else 1
