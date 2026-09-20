"""What the database can say about its own health.

Every probe is its own coroutine and its own ``try``/``except``: this is opened
when something is wrong, so one probe that raises must not blank the nine that
would have answered. A failed probe leaves its field ``None`` and puts its name
in ``probes_failed`` — visible, rather than indistinguishable from «no data».

⚠️ The mode is NOT the dialect. ``is_postgres()`` is true for
``DATABASE_URL=embedded`` as well, because config rewrites the URL — so telling
«BamDude runs this server» from «a service runs it» from «somebody else's
server» has to come from the settings, and it is the first thing a support
conversation needs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import query_timing
from backend.app.core.config import settings
from backend.app.core.database import get_pool_status
from backend.app.core.db_dialect import is_postgres

logger = logging.getLogger(__name__)

Mode = Literal["sqlite", "embedded", "embedded_service", "external"]

# pg_stat_statements is only as good as its preload. In external-service mode
# BamDude never writes bamdude.conf, so the extension can exist while the
# library was never loaded — and then the view answers nothing useful.
_NO_STATEMENTS_EXTERNAL = (
    "pg_stat_statements is not available on this server. BamDude only configures "
    "shared_preload_libraries for the PostgreSQL it starts itself."
)
_NO_STATEMENTS = "pg_stat_statements is not available on this server."


def mode() -> Mode:
    if not is_postgres():
        return "sqlite"
    if getattr(settings, "embedded_postgres", False):
        return "embedded_service" if getattr(settings, "embedded_pg_external_service", False) else "embedded"
    return "external"


async def _guard(name: str, probe: Callable[[], Awaitable[Any]], failures: list[str]) -> Any:
    try:
        return await probe()
    except Exception as exc:  # noqa: BLE001 — a diagnostics page never 500s
        logger.debug("db-health probe %s failed: %s", name, exc)
        failures.append(name)
        return None


async def probe_version(db: AsyncSession) -> str | None:
    if is_postgres():
        return (await db.execute(text("SHOW server_version"))).scalar_one()
    return (await db.execute(text("SELECT sqlite_version()"))).scalar_one()


def _sqlite_files() -> tuple[Path, Path, Path]:
    main = Path(settings.base_dir) / "bamdude.db"
    return main, Path(f"{main}-wal"), Path(f"{main}-shm")


def _size_of(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


async def probe_size_bytes(db: AsyncSession) -> int | None:
    """The real size on either backend.

    ⚠️ ``/system/info`` reports this by statting ``bamdude.db``, which does not
    exist on a PostgreSQL install — so that field reads 0 there. This one does
    not lie; the old field is left alone because changing its shape is a
    separate, riskier edit.
    """
    if is_postgres():
        return int((await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one())
    return sum(_size_of(p) for p in _sqlite_files())


async def probe_sqlite(db: AsyncSession) -> dict | None:
    """Pragmas plus the file sizes.

    ⚠️ ``PRAGMA quick_check`` is deliberately absent: it reads the whole file.
    It belongs in a support bundle somebody asked for, not on a page they
    opened because things already feel slow.
    """
    if is_postgres():
        return None
    out: dict[str, Any] = {}
    for key, pragma in (
        ("journal_mode", "journal_mode"),
        ("page_size", "page_size"),
        ("page_count", "page_count"),
        ("freelist_count", "freelist_count"),
        ("busy_timeout_ms", "busy_timeout"),
        ("cache_size", "cache_size"),
    ):
        out[key] = (await db.execute(text(f"PRAGMA {pragma}"))).scalar()
    _, wal, shm = _sqlite_files()
    out["wal_bytes"] = _size_of(wal)
    out["shm_bytes"] = _size_of(shm)
    return out


async def probe_postgres(db: AsyncSession) -> dict | None:
    if not is_postgres():
        return None
    row = (
        await db.execute(
            text(
                """
                SELECT numbackends, xact_commit, xact_rollback, deadlocks,
                       temp_files, temp_bytes, blks_hit, blks_read
                  FROM pg_stat_database
                 WHERE datname = current_database()
                """
            )
        )
    ).one()
    hit, read = int(row.blks_hit or 0), int(row.blks_read or 0)
    max_connections = (
        await db.execute(text("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'"))
    ).scalar()
    return {
        "connections": {"used": int(row.numbackends or 0), "max": int(max_connections or 0)},
        "cache_hit_ratio": round(hit / (hit + read), 4) if (hit + read) else None,
        "commits": int(row.xact_commit or 0),
        "rollbacks": int(row.xact_rollback or 0),
        "deadlocks": int(row.deadlocks or 0),
        "temp_files": int(row.temp_files or 0),
        "temp_bytes": int(row.temp_bytes or 0),
    }


async def probe_largest_tables(db: AsyncSession) -> list[dict] | None:
    if not is_postgres():
        return None
    rows = (
        await db.execute(
            text(
                """
                SELECT relname, pg_total_relation_size(relid) AS bytes, n_live_tup
                  FROM pg_stat_user_tables
                 ORDER BY bytes DESC
                 LIMIT 10
                """
            )
        )
    ).all()
    return [{"table": r[0], "bytes": int(r[1] or 0), "rows": int(r[2] or 0)} for r in rows]


async def probe_scans(db: AsyncSession) -> list[dict] | None:
    """Sequential scans beside index scans — the shape that says «this table
    wants an index»."""
    if not is_postgres():
        return None
    rows = (
        await db.execute(
            text(
                """
                SELECT relname, seq_scan, idx_scan
                  FROM pg_stat_user_tables
                 ORDER BY seq_scan DESC NULLS LAST
                 LIMIT 10
                """
            )
        )
    ).all()
    return [{"table": r[0], "seq_scan": int(r[1] or 0), "idx_scan": int(r[2] or 0)} for r in rows]


async def _pg_statements(db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            text(
                """
                SELECT query, calls, total_exec_time, mean_exec_time
                  FROM pg_stat_statements
                 ORDER BY total_exec_time DESC
                 LIMIT 20
                """
            )
        )
    ).all()
    return [
        {
            # ⚠️ Redacted and shortened before it leaves the process: admin-only
            # or not, this card gets screenshotted into public issues.
            "statement": query_timing.fingerprint(r[0] or ""),
            "count": int(r[1] or 0),
            "total_ms": float(r[2] or 0.0),
            "mean_ms": float(r[3] or 0.0),
        }
        for r in rows
    ]


async def probe_statements(db: AsyncSession) -> tuple[list[dict], str, str | None]:
    """The slowest statements, from whichever source can answer.

    On SQLite there is no ``pg_stat_statements``, which is exactly why BamDude
    keeps its own bounded table — see ``core/query_timing``. When neither has
    anything to say, the reason is returned rather than an empty list that
    would read as «no slow queries».
    """
    if is_postgres():
        try:
            return await _pg_statements(db), "pg_stat_statements", None
        except Exception as exc:  # noqa: BLE001
            logger.debug("pg_stat_statements unavailable: %s", exc)
            reason = _NO_STATEMENTS_EXTERNAL if mode() == "embedded_service" else _NO_STATEMENTS
            return [], "pg_stat_statements", reason

    rows = [
        {"statement": s.statement, "count": s.count, "total_ms": s.total_ms, "mean_ms": s.total_ms / s.count}
        for s in query_timing.slowest()
        if s.count
    ]
    reason = None
    if not rows and not query_timing.query_threshold_ms():
        reason = "Slow-query logging is off. Set slow_query_ms in Settings to start collecting."
    return rows, "in_process", reason


async def collect(db: AsyncSession) -> dict[str, Any]:
    failures: list[str] = []
    statements, source, reason = await _guard("statements", lambda: probe_statements(db), failures) or (
        [],
        "in_process",
        None,
    )
    return {
        "engine": "PostgreSQL" if is_postgres() else "SQLite",
        "version": await _guard("version", lambda: probe_version(db), failures),
        "mode": mode(),
        "size_bytes": await _guard("size_bytes", lambda: probe_size_bytes(db), failures),
        "pool": await _guard("pool", lambda: _as_coro(get_pool_status()), failures),
        "instrumentation": {
            "query_threshold_ms": query_timing.query_threshold_ms(),
            "request_threshold_ms": query_timing.request_threshold_ms(),
            "source": source,
            "reason": reason,
            "slowest": statements,
        },
        "sqlite": await _guard("sqlite", lambda: probe_sqlite(db), failures),
        "postgres": await _guard("postgres", lambda: probe_postgres(db), failures),
        "largest_tables": await _guard("largest_tables", lambda: probe_largest_tables(db), failures),
        "scans": await _guard("scans", lambda: probe_scans(db), failures),
        "probes_failed": failures,
    }


async def _as_coro(value: Any) -> Any:
    """Let a synchronous probe ride the same guard as the async ones."""
    return value
