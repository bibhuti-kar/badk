"""Model route of an agent: load-time resolution and per-run inheritance (design 22.2, RT-8)."""

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from badk.config import Settings
from badk.errors import BadkError, Code

if TYPE_CHECKING:
    from badk.agents.definition import AgentDef

_MODEL_VARS = {"$LLM_MODEL": "llm_model", "$CLAUDE_MODEL": "claude_model"}


class Route(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["openai", "anthropic", "claude_cli", "fake"]
    model: str
    inherited: bool = False


def _present(data: dict[str, Any], name: str, path: Path) -> bool:
    from badk.agents.definition import is_coordinator  # lazy: definition imports this module

    absent = [k for k in ("provider", "model") if data.get(k) is None]
    if len(absent) == 2 and not is_coordinator(name):
        return False  # sub-agent with neither inherits at run start (with_routes)
    if absent:
        raise BadkError(
            Code.VALIDATION_ERROR,
            f"{name} ({path}): {absent[0]} is required: a coordinator sets provider and model;"
            " a sub-agent sets both or neither",
        )
    return True


def _model(model: object, settings: Settings) -> object:
    if isinstance(model, str) and model.startswith("$"):
        if model not in _MODEL_VARS:
            raise BadkError(Code.VALIDATION_ERROR, f"model: unknown variable {model}")
        return getattr(settings, _MODEL_VARS[model])
    return model


def resolve_route(data: dict[str, Any], settings: Settings, path: Path) -> None:
    """Literal provider/model win; `$LLM_PROVIDER`, `$LLM_MODEL`, `$CLAUDE_MODEL` from Settings."""
    name = str(data.get("name"))
    if not _present(data, name, path):
        return
    provider = data["provider"]
    if provider == "$LLM_PROVIDER":
        provider = settings.llm_provider
    ref = data["model"] if isinstance(data["model"], str) else ""
    model = _model(data["model"], settings)
    try:
        data["route"] = Route(provider=provider, model=str(model))
    except ValidationError:
        pass  # AgentDef validation reports the bad provider/model
    if settings.provider_override:
        provider = "fake"
    # refuse on the RESOLVED provider (after $LLM_PROVIDER and PROVIDER_OVERRIDE)
    if settings.cloud_deploy and provider == "claude_cli":
        raise BadkError(
            Code.PROVIDER_NOT_ALLOWED,
            f"agent {name!r} ({path}): claude_cli not allowed when CLOUD_DEPLOY=true"
            " (check LLM_PROVIDER and the agent file)",
        )
    if model == "" and provider != "fake":
        raise BadkError(Code.VALIDATION_ERROR, f"model: {ref or 'value'} is empty; set it in .env")
    data["provider"], data["model"] = provider, model


def with_routes(agents: Mapping[str, "AgentDef"], coord: "AgentDef") -> dict[str, "AgentDef"]:
    """New dict per run: sub-agents with no provider take the coordinator's provider/model/route."""
    if coord.route is None:  # load_agent always sets it; a hand-built AgentDef may not
        raise BadkError(Code.INTERNAL_ERROR, f"route not resolved for {coord.name}")
    inherited = coord.route.model_copy(update={"inherited": True})
    return {
        n: a.model_copy(
            update={"provider": coord.provider, "model": coord.model, "route": inherited}
        )
        if a.provider is None
        else a
        for n, a in agents.items()
    }
