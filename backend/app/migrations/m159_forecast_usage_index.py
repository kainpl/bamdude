"""An index for the questions the forecast engine asks on every pass.

Until the forecast-server-side rewrite, ``spool_usage_history`` had zero
indexes beyond its primary key — tolerable while the only reader fetched "the
newest 5000 rows" once per panel load. The engine (m159's contemporary,
``services/forecast_engine.py``) instead scans the table on every forecast
computation and every 6-hour alert tick: day-bucketed grams per spool over a
90-day window, plus the newest event per spool for the archived-only retention
rule. Both walks enter through ``(spool_id, created_at)``.

Plain ``CREATE INDEX IF NOT EXISTS`` (the m140 idiom): idempotent on both
engines, no uniqueness to refuse over, no seed. Fresh installs get the same
index from the model's ``__table_args__`` via ``create_all``.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 159
name = "forecast_usage_index"


async def upgrade(conn):
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_spool_usage_history_spool_created "
            "ON spool_usage_history (spool_id, created_at)"
        )
    )
    logger.info("m159: indexed spool_usage_history (spool_id, created_at) for the forecast engine's window scans")
