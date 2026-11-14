"""Shared by the toolkit tools: secrets from tools/secrets.env, ${NAME} fill, output masking.

Stdlib only and Python 3.9+, because `with-secrets` runs under the system python3.
secrets.env is KEY=VALUE lines (dotenv). It must be mode 600. badk's path guard refuses
`*.env` paths, so no agent tool can read it through a path argument.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
FILE = TOOLS / "secrets.env"
_REF = re.compile(r"\$\{(\w+)\}")


def load(path: Path = FILE) -> dict[str, str]:
    """KEY=VALUE pairs; `#` comments, `export ` prefix and one pair of quotes are allowed."""
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        sys.exit(f"{path.name}: other users can read it; run: chmod 600 {path}")
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def fill(obj: Any, secrets: dict[str, str]) -> Any:  # noqa: ANN401 - any JSON value
    """Replace each ${NAME} in the strings of a JSON value. An unknown NAME stops the tool."""

    def sub(m: re.Match[str]) -> str:
        if m[1] not in secrets:
            sys.exit(f"secret {m[1]} is not in tools/secrets.env")
        return secrets[m[1]]

    if isinstance(obj, str):
        return _REF.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: fill(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v, secrets) for v in obj]
    return obj


def redact(text: str, secrets: dict[str, str]) -> str:
    """Mask every secret value (4+ chars) so it never reaches the model, the trace or the DB."""
    for value in sorted(secrets.values(), key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "***")
    return text


def config(name: str) -> dict[str, Any]:
    """tools/<name> as a JSON object."""
    path = TOOLS / name
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read tools/{name}: {e}")
    if not isinstance(data, dict):
        sys.exit(f"tools/{name}: top level must be a JSON object")
    return data
