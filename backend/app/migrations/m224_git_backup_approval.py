"""Leave legacy Git exports pending an explicit immutable-admin approval.

No migration-time notifications or inferred historical owner. Runtime owns
episodes and the caller-owned inbox transaction after startup admission.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 224
name = "git_backup_approval"


async def upgrade(conn):
    for definition in (
        "approved_by_identity VARCHAR(36)",
        "revision VARCHAR(36)",
        "last_successful_backup_at TIMESTAMP",
        "last_backup_reason_code VARCHAR(40)",
        "admission_episode_id VARCHAR(36)",
        "admission_episode_reason VARCHAR(40)",
        "admission_episode_notified_at TIMESTAMP",
    ):
        await add_column(conn, "git_backup_config", definition)
    await add_column(conn, "git_backup_logs", "scheduled_for TIMESTAMP")
    await add_column(conn, "git_backup_logs", "reason_code VARCHAR(40)")
    await add_column(conn, "git_backup_logs", "attempt_identity VARCHAR(36)")
    await conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_git_backup_occurrence ON git_backup_logs (config_id, scheduled_for)")
    )
    # Only a known last success can be recovered; never label a failed attempt
    # as a successful copy. Re-running preserves later runtime writes.
    await conn.execute(
        text(
            "UPDATE git_backup_config SET last_successful_backup_at = last_backup_at "
            "WHERE last_backup_status = 'success' AND last_successful_backup_at IS NULL"
        )
    )
    await conn.execute(
        text(
            "UPDATE git_backup_config SET last_successful_backup_at = "
            "(SELECT MAX(completed_at) FROM git_backup_logs "
            "WHERE config_id = git_backup_config.id AND status = 'success' "
            "AND trigger IN ('manual', 'scheduled')) "
            "WHERE last_successful_backup_at IS NULL"
        )
    )
