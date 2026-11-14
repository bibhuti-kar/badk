"""CLI entry point (design 6.2, 20.8): `badk run` is one call to the public `badk.run`."""

import argparse
import asyncio
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from badk.agents.definition import load_agents
from badk.agents.flow import require_coordinator
from badk.check import check
from badk.config import Settings, load_settings, peek_log_level
from badk.coordinator.input import is_file_arg
from badk.errors import BadkError, Code
from badk.logging_setup import configure_logging
from badk.sdk import run as sdk_run
from badk.tools.definition import tools_dir, validate_all
from badk.tools.guard import check_input_path, read_bounded


def _input_text(arg: str) -> str:
    """Design 6.2 input rule: a file argument is read, else the argument is the text."""
    if is_file_arg(arg):
        # The user typed this path: the resolved target is checked, so a target outside cwd is
        # allowed unless secret. No byte cap: the coordinator context_window is unknown early.
        target = check_input_path(arg, Path.cwd())
        if not target.is_file():
            raise BadkError(Code.INPUT_FILE_NOT_FOUND, arg)
        return read_bounded(target, -1, arg, "strict")
    return arg


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="badk")
    sub = p.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("text")
    run_p.add_argument("--headless", action="store_true")
    sub.add_parser("serve").add_argument("--heartbeat", action="store_true")
    sub.add_parser("mcp").add_argument("--agents", default="agents")
    sub.add_parser("check", help="check .env, agents, tools, provider and the stack")
    args = p.parse_args(argv)
    if args.cmd == "check":
        return check(Path.cwd())
    try:
        if args.cmd == "mcp":
            from badk.mcp_server import serve  # lazy: pulls in the MCP SDK

            env_file = Path(args.agents).resolve().parent / ".env"  # the runs read this one
            configure_logging(peek_log_level(env_file=env_file), sys.stderr)  # stdout: MCP only
            # stdin close interrupts a run with SIGINT; an inherited SIG_IGN (nohup) would block it
            signal.signal(signal.SIGINT, signal.default_int_handler)
            return serve(args.agents)
        if args.cmd == "run":
            text = _input_text(args.text)
            configure_logging(peek_log_level(), sys.stderr)  # 6.2: stdout is final
            return _run(text, args.headless)
        settings = load_settings()
        configure_logging(settings.log_level, sys.stderr)
        return _serve(settings, args.heartbeat)
    except BadkError as e:
        sys.stderr.write(f"{e.code.value}: {e.message}\n")
        return 2 if e.code in (Code.INPUT_FILE_NOT_FOUND, Code.WORKSPACE_DENIED) else 1


def _serve(settings: Settings, heartbeat: bool) -> int:
    from badk.coordinator.serve import serve  # lazy: serve imports the SDK

    tdir = tools_dir(None)
    agents = load_agents(Path("agents"), settings, tools=tdir)
    validate_all(tdir)  # TOOL_TYPE_INVALID etc.: exit 1 before any run
    require_coordinator(agents)
    try:
        asyncio.run(serve(settings, agents, heartbeat, install_signals=_install_signals))
    except BadkError as e:  # serve() maps every infrastructure failure to a coded error
        sys.stderr.write(f"{e.code.value}: {e.message}\n")
        return 1
    return 0


def _install_signals(handler: Callable[[], None]) -> None:
    """Serve's SIGTERM/SIGINT handling is registered here, never in the library (SDK-3.1)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handler)


def _run(text: str, headless: bool) -> int:
    announced: list[bool] = []

    def announce(run_id: str) -> None:
        sys.stderr.write(f"run_id: {run_id}\n")
        sys.stderr.flush()
        announced.append(True)

    try:
        result = sdk_run(text, progress_log=headless, on_run_id=announce)
    except KeyboardInterrupt:  # SIGINT stays a KeyboardInterrupt; the SDK cleaned up
        return 130
    except BadkError as e:
        tail = f" run_id={e.run_id}" if e.run_id else ""
        sys.stderr.write(f"{e.code.value}: {e.message}{tail}\n")
        if e.answer:  # the run was finalised in memory: its final text still goes to stdout
            sys.stdout.write(e.answer + "\n")
        return 3 if announced else 1  # once announced the run exists: failure is a run failure
    sys.stdout.write(result.answer + "\n")
    return 0 if result.status == "COMPLETED" else 3
