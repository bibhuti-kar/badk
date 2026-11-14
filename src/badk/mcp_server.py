"""`badk mcp` (design 28.3): the workflows of an agents folder as MCP tools over stdio."""

import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from badk import sdk
from badk.agents.definition import AgentDef, load_agents
from badk.agents.flow import require_coordinator
from badk.config import load_settings
from badk.errors import BadkError, Code
from badk.mcp_jobs import WAIT_MAX, Jobs, RunFn
from badk.tools.definition import validate_all

NOTE = f" Starts a run, waits up to {WAIT_MAX} s; if not done, returns a job_id: poll badk_result."
PREFIX = "coordinator-"


def tool_names(agents: Mapping[str, AgentDef]) -> dict[str, str | None]:
    """Tool name -> workflow (None = `coordinator`); a clash is refused naming file and tool."""
    out: dict[str, str | None] = {}
    files = {"badk_result": "the result tool"}
    for name in sorted(agents):
        if name == "coordinator":
            tool, wf = "badk_run", None
        elif name.startswith(PREFIX):
            wf = name[len(PREFIX) :]
            tool = "badk_" + wf.replace("-", "_")
        else:
            continue
        if tool in files:
            msg = f"agents/{name}.md would take the MCP tool name {tool}, taken by {files[tool]}"
            raise BadkError(Code.VALIDATION_ERROR, msg)
        out[tool], files[tool] = wf, f"agents/{name}.md"
    return out


def _description(agent: AgentDef) -> str:
    first = next((ln.strip() for ln in agent.role_instruction.splitlines() if ln.strip()), "")
    return first + NOTE


def _workflow_tool(jobs: Jobs, wf: str | None) -> Callable[..., Any]:
    async def start(ask: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
        return await jobs.wait(jobs.submit(ask, wf), WAIT_MAX)

    return start


def build_server(agents: Mapping[str, AgentDef], jobs: Jobs) -> MCPServer:
    server = MCPServer("badk")
    for tool, wf in tool_names(agents).items():
        agent = agents["coordinator" if wf is None else PREFIX + wf]
        server.add_tool(_workflow_tool(jobs, wf), name=tool, description=_description(agent))

    async def badk_result(
        job_id: str, wait_sec: Annotated[int, Field(ge=0, le=WAIT_MAX)] = WAIT_MAX
    ) -> dict[str, Any]:
        try:
            job = jobs.get(job_id)
        except ValueError as e:
            raise ToolError(str(e)) from e
        return await jobs.wait(job, wait_sec)

    desc = (
        f"Result of a workflow job by job_id; waits up to wait_sec (0..{WAIT_MAX}) for it to end."
    )
    server.add_tool(badk_result, name="badk_result", description=desc)
    return server


def serve(agents_dir: str, run_fn: RunFn | None = None) -> int:
    """Load, then serve on a daemon thread while jobs run on this (main) thread."""
    adir = Path(agents_dir).resolve()
    conf = load_settings(adir.parent / ".env")
    tdir = adir.parent / "tools"
    agents = load_agents(adir, conf, tools=tdir, env_file=adir.parent / ".env")
    validate_all(tdir)
    require_coordinator(agents)  # as `badk serve`: a folder with no workflow is refused
    jobs = Jobs(run_fn or sdk.run, str(adir), conf.viewer_url)
    server = build_server(agents, jobs)  # a name clash exits here, before serving

    def listen() -> None:
        try:
            anyio.run(server.run_stdio_async)
        except Exception as e:  # a dead transport ends the process like closed stdin
            sys.stderr.write(f"mcp server stopped: {e!r}\n")
        finally:
            jobs.close()

    threading.Thread(target=listen, daemon=True).start()
    jobs.work()
    return 0
