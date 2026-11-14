"""Run-document store: one upsert path used by the writer (design 4.5)."""

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from badk.errors import BadkError, Code

_EVENT = text(
    "INSERT INTO run_events (run_id, eid, seq, agent_key, trace, logs, tokens, latency_ms, steps) "
    "VALUES (:run_id, :eid, CAST(:seq AS bigint), :agent_key, CAST(:trace AS jsonb), "
    "CAST(:logs AS jsonb), :tokens, :latency_ms, :steps) ON CONFLICT (run_id, eid) DO NOTHING"
)  # same eid = duplicate delivery: no-op; a clashing seq with a new eid raises (23505)
_RUN = text(
    "INSERT INTO runs (run_id, status, started_at, header, header_eid) "
    "VALUES (:run_id, :status, :started_at, CAST(:header AS jsonb), :eid) "
    "ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, header = EXCLUDED.header, "
    "header_eid = EXCLUDED.header_eid, started_at = EXCLUDED.started_at "
    "WHERE EXCLUDED.header_eid > runs.header_eid"
)
_AGENT = text(
    "INSERT INTO run_agents (run_id, agent_key, eid, capsule) "
    "VALUES (:run_id, :agent_key, :eid, CAST(:capsule AS jsonb)) "
    "ON CONFLICT (run_id, agent_key) DO UPDATE SET capsule = EXCLUDED.capsule, eid = EXCLUDED.eid "
    "WHERE EXCLUDED.eid > run_agents.eid"
)
_LIST = text(
    """
SELECT count(*) OVER () AS total, r.run_id::text AS run_id, r.status,
  r.header->>'started_at' AS started_at,
  r.header->>'ended_at' AS ended_at,
  (SELECT jsonb_build_object('tokens', COALESCE(sum(tokens), 0),
            'seconds', round(COALESCE(sum(latency_ms), 0) / 1000.0, 3),
            'steps', COALESCE(sum(steps), 0))
   FROM run_events WHERE run_id = r.run_id) AS totals
FROM runs r ORDER BY r.started_at DESC, r.run_id LIMIT :lim OFFSET :off
"""
)
_GET = text(
    """
SELECT (r.header || jsonb_build_object(
  'run_id', r.run_id::text,
  'agents', COALESCE((SELECT jsonb_object_agg(agent_key, capsule)
                      FROM run_agents WHERE run_id = r.run_id), '{}'::jsonb),
  'totals', (SELECT jsonb_build_object(
               'tokens', COALESCE(sum(tokens), 0),
               'seconds', round(COALESCE(sum(latency_ms), 0) / 1000.0, 3),
               'steps', COALESCE(sum(steps), 0))
             FROM run_events WHERE run_id = r.run_id),
  'trace', COALESCE((SELECT jsonb_agg(trace ORDER BY seq)
                     FROM run_events WHERE run_id = r.run_id AND trace IS NOT NULL),
                    '[]'::jsonb)))::text
FROM runs r WHERE r.run_id = :run_id
"""
)


def _j(v: object) -> str | None:
    return None if v is None else json.dumps(v)


async def apply(conn: AsyncConnection, event: dict[str, Any]) -> bool:
    """Apply one step event in the caller's transaction. True if the event row was new."""
    rid, eid = uuid.UUID(event["run_id"]), event["eid"]
    trace, m, hdr = event.get("trace"), event["metrics"], event["header"]
    new = await conn.execute(
        _EVENT,
        {
            "run_id": rid,
            "eid": eid,
            "seq": trace["seq"] if trace else None,
            "agent_key": event.get("agent_key"),
            "trace": _j(trace),
            "logs": json.dumps(event["logs"]),
            "tokens": m["tokens_in"] + m["tokens_out"],
            "latency_ms": m["latency_ms"],
            "steps": m["steps"],
        },
    )
    await conn.execute(
        _RUN,
        {
            "run_id": rid,
            "status": hdr["status"],
            "started_at": datetime.fromisoformat(hdr["started_at"]),
            "header": json.dumps(hdr),
            "eid": eid,
        },
    )
    if event.get("agent_key") is not None:
        await conn.execute(
            _AGENT,
            {
                "run_id": rid,
                "agent_key": event["agent_key"],
                "eid": eid,
                "capsule": json.dumps(event["capsule"]),
            },
        )
    return bool(new.rowcount)


async def get_run(conn: AsyncConnection, run_id: str) -> str | None:
    """Run document JSON text, or None for an unknown or non-uuid id."""
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return None
    return (await conn.execute(_GET, {"run_id": rid})).scalar_one_or_none()


async def list_runs(
    conn: AsyncConnection, page: int, per_page: int
) -> tuple[list[dict[str, Any]], int]:
    """One page of run summaries, newest first, and the total run count."""
    rows = [
        dict(r._mapping)
        for r in await conn.execute(_LIST, {"lim": per_page, "off": (page - 1) * per_page})
    ]
    if rows:  # total rides the same statement/snapshot as the items
        return [{k: v for k, v in r.items() if k != "total"} for r in rows], rows[0]["total"]
    # empty page (past the end or no runs): fallback count
    return [], (await conn.execute(text("SELECT count(*) FROM runs"))).scalar_one()


async def list_runs_page(engine: AsyncEngine, page: int, per_page: int) -> dict[str, Any]:
    """Connect and return the page envelope shared by the API and the HTML index."""
    async with engine.connect() as conn:
        items, total = await list_runs(conn, page, per_page)
    return {"items": items, "page": page, "per_page": per_page, "total": total}


async def get_run_or_404(engine: AsyncEngine, run_id: str) -> str:
    """Connect and return the run document JSON, or raise RUN_NOT_FOUND."""
    async with engine.connect() as conn:
        doc = await get_run(conn, run_id)
    if doc is None:
        raise BadkError(Code.RUN_NOT_FOUND, f"run {run_id} not found")
    return doc
