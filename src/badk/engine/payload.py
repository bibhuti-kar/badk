"""Payload layout (design 7.3): stable bytes first so the provider prefix cache hits.

Pure: no I/O, no clock. message[0] is GLOBAL_SYSTEM_PREFIX for every agent and run (ENG-2.1).
"""

import json
from collections.abc import Mapping
from typing import Final

from badk.agents.definition import AgentDef, is_coordinator
from badk.exec.document import Capsule, Message
from badk.tools.definition import ToolDef

GLOBAL_SYSTEM_PREFIX: Final[str] = (
    "You are one agent in badk. Reply with one JSON object and nothing else.\n"
    'Envelope: {"done": bool, "tool_calls": [{"name": str, "args": object}], '
    '"result": object, "notes": str}.\n'
    "Set done true only when your objective is met. Then result matches OUTPUT_SCHEMA.\n"
    'When tool_calls is not empty, "result" is {}.\n'
    "To use a tool, put it in tool_calls. Use only tools in TOOLS. "
    "Tool output comes back as TOOL_RESULT.\n"
    "notes: top level, never inside result; 300 characters or less.\n"
    "Style: ASD-STE100 Simplified Technical English. Short sentences. No conversational filler.\n"
    "File output in Markdown. Diagrams in Mermaid."
)


def build(
    agent: AgentDef, capsule: Capsule, tools: Mapping[str, ToolDef] | None = None
) -> list[Message]:
    """`tools`: the agent's permitted definitions (sorted by name: the block stays stable)."""
    listing = "\n".join(
        f"- {n}: {t.description} args={json.dumps(t.args, sort_keys=True)}"
        for n, t in sorted((tools or {}).items())
    )
    sel = ""
    if is_coordinator(agent.name):  # WF-3: catalog + selection schema, same bytes in both phases
        schema = json.dumps(agent.selection_schema, sort_keys=True)
        sel = f"CATALOG:\n{agent.catalog}\n\nSELECTION_SCHEMA:\n{schema}\n\n"
    role = (
        f"ROLE:\n{agent.role_instruction}\n\n{sel}TOOLS:\n{listing}\n\n"
        f"OUTPUT_SCHEMA:\n{json.dumps(agent.output_schema, sort_keys=True)}\n\n"
        f"JOURNAL:\n{capsule.journal_summary}"
    )
    return [
        Message(role="system", content=GLOBAL_SYSTEM_PREFIX),
        Message(role="system", content=role),
        *(m.model_copy() for m in capsule.active_chat_history),
    ]
