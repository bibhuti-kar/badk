"""Workspace guard (design 9.3): check() for tool calls, check_path() for paths and pins."""

import errno
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import fastjsonschema

from badk.agents.definition import AgentDef
from badk.engine.envelope import ToolCall
from badk.errors import BadkError, Code
from badk.tools.definition import NO_CWD, ToolDef

WALK_CAP: Final = 10_000  # directory args: entries scanned before failing closed
_TEMPLATES = {".env.example", ".env.sample"}
_NAMES = {".git", ".git-credentials", ".netrc", ".npmrc", ".pypirc", ".ssh", ".aws", ".gnupg"}
_NAMES |= {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}


def is_secret_name(part: str, final: bool = True) -> bool:
    """`.pem`/`.key` mark a file, so they count only on the final path part."""
    p = part.lower()  # APFS is case-insensitive
    return (
        p in _NAMES
        or p.startswith(".envrc")
        or p.endswith(".env")
        or (final and p.endswith((".pem", ".key")))
        or (p.startswith(".env.") and p not in _TEMPLATES)
    )


def _resolve(p: Path, raw: str) -> Path:
    """Resolve fully. A loop, NUL byte or I/O fault is denied; a missing path resolves loosely.
    strict=True raises on loops in 3.12 and 3.13 alike (non-strict no longer does in 3.13)."""
    try:
        return p.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        pass
    except (RuntimeError, ValueError, OSError) as e:  # ELOOP, NUL byte, I/O: fail closed
        raise BadkError(Code.WORKSPACE_DENIED, f"path cannot be resolved: {raw}") from e
    try:
        return p.resolve()
    except (RuntimeError, ValueError, OSError) as e:
        raise BadkError(Code.WORKSPACE_DENIED, f"path cannot be resolved: {raw}") from e


def _own(path: Path, cwd_r: Path) -> tuple[str, ...]:
    """Parts that count for the secret check: relative to cwd, else every part."""
    return path.relative_to(cwd_r).parts if path.is_relative_to(cwd_r) else path.parts


def _check_secret(parts: tuple[str, ...], raw: str) -> None:
    if any(is_secret_name(p, final=False) for p in parts[:-1]) or (
        parts and is_secret_name(parts[-1])
    ):
        raise BadkError(Code.WORKSPACE_DENIED, f"secret path denied: {raw}")


def tool_cwd(agent: AgentDef, root: Path) -> Path:
    """WF-8.1: where `agent`'s tools run: `(root / workspace[0])` resolved. A loop or a secret
    folder (.git, .ssh ...) is WORKSPACE_DENIED; a missing or non-directory one is TOOL_ERROR."""
    name = agent.workspace[0]
    path = _resolve(root / name, name)
    _check_secret(_own(path, root.resolve()), name)
    if not path.is_dir():
        raise BadkError(Code.TOOL_ERROR, f"workspace folder is missing or not a directory: {name}")
    return path


def check_path(raw: str, workspace: Sequence[str], cwd: Path, base: Path | None = None) -> Path:
    """Resolve `raw` (.., absolute, symlinks) and require it under some workspace root.
    Workspace entries always resolve from `cwd` (the project root); a relative `raw` resolves
    from `base` (the tool's working directory, WF-8.2), which defaults to `cwd`."""
    target = _resolve((base or cwd) / raw, raw)
    roots = [_resolve(cwd / w, raw) for w in workspace]
    hits = [r for r in roots if target.is_relative_to(r)]
    if not hits:
        raise BadkError(Code.WORKSPACE_DENIED, f"path outside workspace: {raw}")
    cwd_r = cwd.resolve()
    for r in hits:  # a root that is itself a secret dir (.git), inside cwd or not, is secret too
        _check_secret((*_own(r, cwd_r), *target.relative_to(r).parts), raw)
    return target


def _deny_hardlink(st: os.stat_result, ref: str) -> None:
    """Shared by read_bounded and the path-arg checks: a second name may point at a secret."""
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise BadkError(Code.WORKSPACE_DENIED, f"hard-linked file denied: {ref}")


def _lstat(path: str, ref: str, follow: bool = False) -> os.stat_result:
    try:
        return os.stat(path) if follow else os.lstat(path)
    except OSError as e:
        raise BadkError(Code.WORKSPACE_DENIED, f"cannot stat: {ref}") from e


class _Walk:
    """One directory argument: shared entry budget, so link-following cannot loop forever."""

    def __init__(self, raw: str, workspace: Sequence[str], cwd: Path) -> None:
        self.raw, self.workspace, self.cwd, self.seen = raw, workspace, cwd, 0
        self.done: set[Path] = set()  # scanned directories: link loops end here

    def scan(self, root: Path) -> None:
        if root in self.done:
            return
        self.done.add(root)
        for dir_, dirs, files in os.walk(root, followlinks=False):
            for final, names in ((False, dirs), (True, files)):
                for name in names:
                    self.entry(dir_, name, final)

    def entry(self, dir_: str, name: str, final: bool) -> None:
        self.seen += 1
        if self.seen > WALK_CAP:
            raise BadkError(Code.WORKSPACE_DENIED, f"directory too large to check: {self.raw}")
        full, ref = os.path.join(dir_, name), f"{self.raw}/{name}"
        if is_secret_name(name, final=final):
            raise BadkError(Code.WORKSPACE_DENIED, f"secret path denied: {ref}")
        st = _lstat(full, ref)
        if not stat.S_ISLNK(st.st_mode):
            _deny_hardlink(st, ref)
            return
        # a symlink is fine only if its target is inside a workspace root and not secret
        target = check_path(full, self.workspace, self.cwd)
        _scan_target(target, ref, self)


def _scan_target(target: Path, raw: str, walk: _Walk) -> None:
    """A directory argument exposes everything below it: deny the first secret entry, hard-linked
    file, or symlink leaving the workspace, and fail closed past WALK_CAP entries."""
    try:
        st = os.stat(target)
    except (FileNotFoundError, NotADirectoryError):
        return  # does not exist yet: nothing to scan
    except OSError as e:
        raise BadkError(Code.WORKSPACE_DENIED, f"cannot stat: {raw}") from e
    if stat.S_ISDIR(st.st_mode):
        walk.scan(target)
    else:
        _deny_hardlink(st, raw)


def check(
    agent: AgentDef,
    call: ToolCall,
    tool: ToolDef | None,
    cwd: Path | None = None,
    base: Path | None = None,
) -> ToolDef:
    """`cwd` is the project root, `base` the tool's working directory (see `check_path`).
    Deny by default, in order: permitted and loaded, args valid (defaults applied), each path
    arg inside the workspace (and a directory free of secrets). Path args in `call.args` are
    replaced by their resolved path, so what runs is what was checked. NO_CWD tools (http, mcp)
    have no workspace check. Returns the narrowed tool; nothing runs on a raise."""
    if call.name not in agent.permissions:
        raise BadkError(Code.PERMISSION_DENIED, f"tool not listed in permissions: {call.name}")
    if tool is None:
        raise BadkError(
            Code.PERMISSION_DENIED,
            f"tool listed but not loaded (no tools/{call.name}.md): {call.name}",
        )
    try:
        tool.validate_args(call.args)  # fills schema defaults into call.args in place
    except fastjsonschema.JsonSchemaValueException as e:
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: args invalid: {e.message}") from e
    if tool.type in NO_CWD:
        return tool
    root = cwd or Path.cwd()
    for name in sorted(tool.path_args & call.args.keys()):
        v = call.args[name]
        if not isinstance(v, str):
            raise BadkError(Code.WORKSPACE_DENIED, f"path argument {name} is not a string")
        target = check_path(v, agent.workspace, root, base)
        _scan_target(target, v, _Walk(v, agent.workspace, root))
        call.args[name] = str(target)
    return tool


def check_input_path(raw: str, cwd: Path) -> Path:
    """User-typed input file: symlinks followed, target may lie outside cwd, secret names deny."""
    target = _resolve(cwd / raw, raw)
    _check_secret(_own(target, cwd.resolve()), raw)
    return target


def read_bounded(path: Path, limit: int, ref: str, errors: str = "replace") -> str:
    """Read up to `limit` chars (-1 = all) of a regular, singly-linked, non-symlink file via one fd.
    st_nlink > 1 is denied (fail closed; false positive for pnpm/uv hard-linked files)."""
    try:  # O_NONBLOCK: a FIFO opens without blocking; fstat on the fd closes the check/open race
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as e:
        code = Code.WORKSPACE_DENIED if e.errno == errno.ELOOP else Code.VALIDATION_ERROR
        raise BadkError(code, f"cannot read file {ref}: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BadkError(Code.VALIDATION_ERROR, f"not a regular file: {ref}")
        _deny_hardlink(st, ref)
        with os.fdopen(fd, encoding="utf-8", errors=errors) as f:  # takes ownership of fd
            fd = -1
            return f.read(limit)
    except (OSError, UnicodeDecodeError) as e:
        raise BadkError(Code.VALIDATION_ERROR, f"cannot read file {ref}: {e}") from e
    finally:
        if fd >= 0:
            os.close(fd)
