"""Single configuration module: frozen Settings + .env loader (design 12)."""

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from badk.errors import BadkError, Code


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    app_port: int = Field(8000, alias="APP_PORT")
    database_url: str | None = Field(None, alias="DATABASE_URL")
    rabbitmq_url: str | None = Field(None, alias="RABBITMQ_URL")
    trace_enabled: bool = Field(True, alias="TRACE_ENABLED")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", alias="LOG_LEVEL")
    llm_provider: Literal["openai", "anthropic", "claude_cli"] = Field(
        "openai", alias="LLM_PROVIDER"
    )
    llm_base_url: str = Field("http://127.0.0.1:11434", alias="LLM_BASE_URL")
    llm_api_key: SecretStr = Field(SecretStr(""), alias="LLM_API_KEY")
    llm_model: str = Field("", alias="LLM_MODEL")
    claude_model: str = Field("", alias="CLAUDE_MODEL")
    llm_max_tokens: int = Field(16000, ge=1, alias="LLM_MAX_TOKENS")  # anthropic only
    provider_timeout_sec: int = Field(120, alias="PROVIDER_TIMEOUT_SEC")
    cloud_deploy: bool = Field(False, alias="CLOUD_DEPLOY")
    provider_override: Literal["", "fake"] = Field("", alias="PROVIDER_OVERRIDE")
    fake_script: str = Field("", alias="FAKE_SCRIPT")

    @property
    def viewer_url(self) -> str:
        """Where the host reaches the viewer (SDK RunResult.url); the literal lives here."""
        return f"http://127.0.0.1:{self.app_port}"

    @field_validator("cloud_deploy", "trace_enabled", mode="before")
    @classmethod
    def _strict_bool(cls, v: object) -> object:
        if isinstance(v, str) and v.lower() in ("true", "false"):
            return v.lower() == "true"
        raise ValueError("must be 'true' or 'false'")


_VAR = re.compile(r"\$\$|\$\{(\w+)\}")
_COMMENT = re.compile(r"\s+#.*$")


def _parse_env(text: str, environ: Mapping[str, str], where: str = ".env") -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        quote = val[0] if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'" else ""
        if quote == "'":
            out[key] = val[1:-1]
            continue
        val = val[1:-1] if quote else _COMMENT.sub("", val)

        def sub(m: re.Match[str], key: str = key) -> str:
            if m.group(1) is None:
                return "$"
            name = m.group(1)
            found = {**out, **environ}.get(name)
            if found is None:
                raise BadkError(
                    Code.VALIDATION_ERROR, f"{key}: undefined variable ${{{name}}} in {where}"
                )
            return found

        out[key] = _VAR.sub(sub, val)
    return out


def parse_env_file(path: Path, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """A KEY=VALUE file by the `.env` rules; {} when absent. Errors name the file."""
    if not path.is_file():
        return {}
    try:
        return _parse_env(path.read_text(), environ, path.name)
    except (OSError, UnicodeDecodeError) as e:
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: cannot read: {e}") from e


def default_env_file() -> Path:
    """The .env load_settings reads when not told otherwise (a seam: tests hide the real one)."""
    return Path(".env")


def read_env_keys(
    names: Iterable[str], env_file: Path | None = None, environ: Mapping[str, str] = os.environ
) -> dict[str, str]:
    """Values of `names` from `.env` merged with the process env (process env wins, as in
    `load_settings`); a missing key is absent from the result. Read once per start (TOOL-5)."""
    env_file = env_file or default_env_file()
    vals = _parse_env(env_file.read_text(), environ) if env_file.is_file() else {}
    merged = {**vals, **environ}
    return {n: merged[n] for n in names if n in merged}


def peek_log_level(environ: Mapping[str, str] = os.environ, env_file: Path | None = None) -> str:
    """LOG_LEVEL without building Settings (the CLI sets logging before the SDK loads them)."""
    env_file = env_file or default_env_file()
    try:
        vals = _parse_env(env_file.read_text(), environ) if env_file.is_file() else {}
    except (BadkError, OSError):
        vals = {}  # the SDK reports a bad .env with the proper code
    level = environ.get("LOG_LEVEL") or vals.get("LOG_LEVEL", "INFO")
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO"


def load_settings(
    env_file: Path | None = None,
    environ: Mapping[str, str] = os.environ,
    overrides: Mapping[str, str] = {},  # noqa: B006 - read-only, never mutated
) -> Settings:
    """Code overrides win over process env, which wins over .env (SDK-1.3); override keys must
    be existing Settings aliases; nothing is written back. Missing .env without RABBITMQ_URL
    in env is an error."""
    known = {f.alias for f in Settings.model_fields.values()}
    if bad := sorted(set(overrides) - known):  # before .env is parsed
        raise BadkError(Code.VALIDATION_ERROR, f"unknown setting override: {', '.join(bad)}")
    env_file = env_file or default_env_file()
    env = {**environ, **overrides}  # code > process env; ${VAR} in .env sees both
    if env_file.is_file():
        file_vals = _parse_env(env_file.read_text(), env)
    elif "RABBITMQ_URL" in env:
        file_vals = {}
    else:
        raise BadkError(Code.VALIDATION_ERROR, "missing .env: copy .env.example to .env")
    merged = {**file_vals, **env}
    raw = {
        f.alias: merged[f.alias]
        for f in Settings.model_fields.values()
        if f.alias and f.alias in merged
    }
    fs = raw.get("FAKE_SCRIPT", "")
    if fs and not fs.lstrip().startswith("{") and not Path(fs).is_absolute():
        raw["FAKE_SCRIPT"] = str(env_file.parent.resolve() / fs)  # design 20.7: root, not cwd
    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        keys = ", ".join(str(err["loc"][0]) for err in e.errors())
        raise BadkError(
            Code.VALIDATION_ERROR,
            f"invalid config value for: {keys}",
            [{"key": str(err["loc"][0]), "msg": err["msg"]} for err in e.errors()],
        ) from e
