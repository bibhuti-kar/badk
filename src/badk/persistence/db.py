"""Engine and schema bootstrap (design 4.4, D-9). Not a migration system."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from badk.config import Settings
from badk.errors import BadkError, Code

_DDL = Path(__file__).with_name("schema.sql").read_text()
_LOCK = 7_340_001  # arbitrary constant for pg_advisory_xact_lock


def make_engine(settings: Settings) -> AsyncEngine:
    if not settings.database_url:
        raise BadkError(Code.VALIDATION_ERROR, "DATABASE_URL is not set: see .env.example")
    return create_async_engine(settings.database_url, pool_pre_ping=True)


async def ensure_schema(engine: AsyncEngine) -> None:
    stmts = [
        s.strip()
        for s in "\n".join(ln.split("--")[0] for ln in _DDL.splitlines()).split(";")
        if s.strip()
    ]
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_LOCK})")
        for s in stmts:
            await conn.exec_driver_sql(s)
