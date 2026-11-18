"""Write a badk sub-agent file from a prompt, as a System 1 or a System 2 agent.

System 1: a fast decision, no tools, small budget. The answer is one of
three decision formats, enforced by output_schema:
  choice  pick one option         result {"choice": "<option>"}
  bool    true or false           result {"answer": true}
  order   rank every option once  result {"order": ["<option>", ...]}
System 2: a free answer, reasoned with care; tools allowed.
  result {"answer": "<text>"}

Usage:
  uv run python scripts/skill_builder.py system1 --name sub-triage --decision choice \
      --options bug,feature,question --use-when "Sort an issue." --prompt "Classify the issue."
  uv run python scripts/skill_builder.py system2 --name sub-analyst --use-when "Explain a trace." \
      --prompt-file prompt.md --permissions trace_search

Before it writes, the file is loaded with the other agents in --out by badk's own loader,
with the project's .env (the folder above --out), so a file badk would refuse is never written.
YAML is a superset of JSON, so every value is written as JSON.
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from badk.agents.definition import load_agents
from badk.config import load_settings
from badk.errors import BadkError
from badk.tools.definition import check_permission_names

BUDGETS = {  # context_window, token_budget, time_budget_sec, max_tries
    "system1": (4096, 1500, 60, 2),
    "system2": (4096, 3000, 120, 2),
}
RULES = {
    "system1": "Decide fast. Do not explain. Do not call tools.",
    "system2": "Reason through the task before you answer. Put a short reason in notes.",
}


def fail(msg: str) -> None:
    sys.exit(f"skill_builder: {msg}")


def decision(kind: str, options: list[str]) -> tuple[dict[str, Any], str]:
    """Result properties and the instruction line for one System 1 decision format."""
    if kind == "bool":
        if options:
            fail("bool takes no --options")
        return {"answer": {"type": "boolean"}}, "Answer true or false."
    if len(options) < 2 or len(set(options)) != len(options):
        fail(f"{kind} needs 2 or more distinct --options")
    listed = ", ".join(options)
    if kind == "choice":
        return {"choice": {"enum": options}}, f"Pick exactly one option: {listed}."
    n = len(options)
    order = {"type": "array", "items": {"enum": options}, "minItems": n, "maxItems": n}
    return {
        "order": order | {"uniqueItems": True}
    }, f"Put every option in order, best first: {listed}."


def build(a: argparse.Namespace, prompt: str) -> str:
    if a.system == "system1":
        props, line = decision(a.decision, a.options)
        rules = f"{RULES['system1']}\n{line}"
    else:
        props, rules = {"answer": {"type": "string"}}, RULES["system2"]
    ctx, tokens, secs, tries = BUDGETS[a.system]
    front = {
        "name": a.name,
        "context_window": ctx,
        "compression_threshold": 0.75,
        "token_budget": tokens,
        "time_budget_sec": secs,
        "max_tries": tries,
        "permissions": a.permissions,
        "workspace": ["."],
        "use_when": a.use_when.strip(),
        "depends_on": a.depends_on,
        "output_schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }
    head = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items())
    return f"---\n{head}\n---\n{prompt}\n\n{rules}\n"


def check(text: str, out: Path, name: str) -> None:
    """Load the new file with every other agent in `out`, as `badk check` would."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for p in out.glob("*.md"):
            if p.name != f"{name}.md":
                shutil.copy(p, d)
        (d / f"{name}.md").write_text(text)
        try:
            agents = load_agents(d, load_settings(out.parent / ".env"))
            if agents[name].permissions:
                origin = {name: d / f"{name}.md"}
                check_permission_names({name: agents[name]}, origin, out.parent / "tools")
        except BadkError as e:
            fail(f"badk would refuse this agent: {str(e).replace(str(d), str(out))}")


def read_prompt(a: argparse.Namespace) -> str:
    try:
        prompt: str = Path(a.prompt_file).read_text() if a.prompt_file else a.prompt
    except OSError as e:
        fail(f"cannot read --prompt-file: {e}")
    if not prompt.strip():
        fail("the prompt is empty")
    return prompt.strip()


def csv(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("system", choices=sorted(BUDGETS))
    p.add_argument("--name", required=True, help="sub-<name>; also the file name")
    p.add_argument("--use-when", required=True, help="one line: when the coordinator picks it")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="role text")
    src.add_argument("--prompt-file", help="file with the role text")
    p.add_argument("--decision", choices=["choice", "bool", "order"], help="system1 only")
    p.add_argument("--options", type=csv, default=[], help="comma list, for choice and order")
    p.add_argument("--permissions", type=csv, default=[], help="tool names, system2 only")
    p.add_argument("--depends-on", type=csv, default=[], help="comma list of sub-agents")
    p.add_argument("--out", default="agents", help="agents folder (default: agents)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    return p


def main(argv: list[str] | None = None) -> None:
    p = parser()
    a = p.parse_args(argv)
    if (a.system == "system1") != bool(a.decision):
        p.error("--decision is required for system1 and not allowed for system2")
    if a.system == "system1" and a.permissions:
        p.error("system1 agents call no tools; drop --permissions")
    if a.system == "system2" and a.options:
        p.error("--options is for system1 choice and order only")
    out = Path(a.out)
    path = out / f"{a.name}.md"
    if path.exists() and not a.force:
        fail(f"{path} exists; use --force to overwrite")
    text = build(a, read_prompt(a))
    check(text, out, a.name)
    out.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sys.stdout.write(f"{path}\n")


if __name__ == "__main__":
    main()
