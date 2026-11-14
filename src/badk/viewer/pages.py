"""Viewer HTML pages (design 6.5): server-rendered from the store, no new SQL."""

import json
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from badk.exec.document import CapsuleStatus, DagNode
from badk.persistence import store

_HERE = Path(__file__).parent
STATIC_DIR = _HERE / "static"
router = APIRouter()
log = structlog.get_logger()
_templates = Jinja2Templates(directory=_HERE / "templates")  # autoescape on for .html


def _label(text: str) -> str:
    """Mermaid quoted-label text: entity-code every char that can end or inject markup."""
    return "".join(f"#{ord(c)};" if c in '"#<>&[]{}()|`\\' else c for c in text if c >= " ")


_STATUSES = {s.value for s in CapsuleStatus}


def dag_mermaid(dag: list[DagNode], statuses: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Flowchart source plus {synthetic node id: node key} (design 20.10).

    Mermaid only ever sees generated ids (nK); node text appears solely in escaped labels.
    One edge per (dependency instance, dependent) pair; one class line per class.
    """
    ids = {f"n{i}": n.key for i, n in enumerate(dag)}
    lines = ["flowchart LR"]
    for i, n in enumerate(dag):
        lines.append(f'  n{i}["{_label(n.key)} {_label(n.tool or n.agent)}"]')
        # mermaid reads "a,b" as one class name: one class line each
        lines += [f"  class n{i} {c}" for c in _classes(n, statuses)]
    by_key = {n.key: i for i, n in enumerate(dag)}
    by_agent: dict[str, list[int]] = {}
    for j, m in enumerate(dag):
        if not m.kind:
            by_agent.setdefault(m.agent, []).append(j)
    for i, n in enumerate(dag):
        lines += [f"  n{j} --> n{i}" for j in _sources(n, by_key, by_agent)]
    return "\n".join(lines), ids


def _sources(n: DagNode, by_key: dict[str, int], by_agent: dict[str, list[int]]) -> list[int]:
    """Indexes of the nodes with an edge into n: flow `after` keys, else dependency agents."""
    if n.kind:
        return [by_key[k] for k in dict.fromkeys(n.after) if k in by_key]
    return [j for d in dict.fromkeys(n.depends_on) for j in by_agent.get(d, [])]


def _classes(n: DagNode, statuses: dict[str, str]) -> list[str]:
    if n.kind:
        cls = [f"kind-{n.kind}"] + ([f"state-{n.state}"] if n.state else [])
    else:
        cls = [f"src-{n.source}"] + (["skipped"] if n.state == "skipped" else [])
    if statuses.get(n.key) in _STATUSES:
        cls.append(f"st-{statuses[n.key].lower()}")
    return cls


def _graph(run: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, str]]:
    """Dag graph for the run page; a pre-M5 or malformed document degrades to no graph."""
    try:
        dag = [DagNode.model_validate(n) for n in run.get("dag") or []]
        statuses = {k: str(a["status"]) for k, a in (run.get("agents") or {}).items()}
    except (ValidationError, TypeError, KeyError, AttributeError) as exc:
        log.warning("viewer.dag_invalid", run_id=run.get("run_id"), error=str(exc)[:200])
        return "", {}, {}
    if not dag:
        return "", {}, {}
    src, ids = dag_mermaid(dag, statuses)
    return src, ids, {f"n{i}": n.parent for i, n in enumerate(dag) if n.parent}


@router.get("/runs", response_class=HTMLResponse)
async def index_page(
    request: Request,
    page: int = Query(1, ge=1, le=1_000_000),
    per_page: int = Query(20, ge=1, le=100),
) -> HTMLResponse:
    ctx = await store.list_runs_page(request.app.state.engine, page, per_page)
    return _templates.TemplateResponse(request, "index.html", ctx)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str) -> HTMLResponse:
    doc = await store.get_run_or_404(request.app.state.engine, run_id)
    run = json.loads(doc)
    graph, ids, parents = _graph(run)
    has_tool = any(
        t.get("type") == "tool" and "status" in (t.get("detail") or {})
        for t in run.get("trace") or []
    )
    ctx = {"run": run, "graph": graph, "ids": ids, "parents": parents, "has_tool": has_tool}
    return _templates.TemplateResponse(request, "run.html", ctx)
