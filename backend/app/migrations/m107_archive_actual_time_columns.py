"""Add + backfill ``print_archives.actual_time_seconds`` / ``time_accuracy``.

Persist the real print duration (``completed_at - started_at``, completed prints only)
and the estimate-vs-actual accuracy % (``print_time_seconds / actual * 100``, kept only
for 5..500%) so reads come from columns instead of per-request math. The backfill mirrors
``services/archive_time_metrics.compute_time_metrics`` — keep the two in step if the
clamp/rounding ever changes.

Fresh installs get the columns from the model's ``create_all``; this adds them to existing
DBs. Idempotent (the DEBUG-startup re-run just recomputes). See docs spec
``2026-07-24-archive-actual-time-columns-design.md``.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column

version = 107
name = "archive_actual_time_columns"


async def upgrade(conn):
    await add_column(conn, "print_archives", "actual_time_seconds INTEGER")
    await add_column(conn, "print_archives", "time_accuracy FLOAT")

    secs = (
        "CAST((julianday(completed_at) - julianday(started_at)) * 86400 AS INTEGER)"
        if is_sqlite()
        else "CAST(EXTRACT(EPOCH FROM (completed_at - started_at)) AS INTEGER)"
    )

    # 1) actual_time_seconds for completed prints with a positive real duration.
    await conn.execute(
        text(
            f"UPDATE print_archives SET actual_time_seconds = {secs} "
            f"WHERE status = 'completed' AND started_at IS NOT NULL "
            f"AND completed_at IS NOT NULL AND {secs} > 0"
        )
    )
    # 2) time_accuracy = estimate/actual %, kept only when the raw ratio is 5..500.
    await conn.execute(
        text(
            "UPDATE print_archives SET time_accuracy = "
            "ROUND(print_time_seconds * 100.0 / actual_time_seconds, 1) "
            "WHERE actual_time_seconds IS NOT NULL AND actual_time_seconds > 0 "
            "AND print_time_seconds IS NOT NULL AND print_time_seconds > 0 "
            "AND (print_time_seconds * 100.0 / actual_time_seconds) BETWEEN 5 AND 500"
        )
    )
