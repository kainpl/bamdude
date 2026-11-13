"""Short, anchor-serialized Git export admission and durable blocked episodes.

The owning backup service releases transactions before network work. Approval
is for the exact saved configuration and immutable account, independently of
that account's password or browser sessions. No second identity/notification
engine lives here.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select

from backend.app.core.credential_config import lock_credential_config
from backend.app.core.permissions import Permission
from backend.app.models.git_backup import GitBackupConfig, GitBackupLog
from backend.app.models.user import User

BLOCK_MESSAGES = {
    "approval_required": "Git backup is paused. An administrator must review and save the configuration.",
    "approver_not_authorized": "Git backup is paused because its approving administrator is no longer authorized.",
}


async def approval_reason(db, config: GitBackupConfig, *, lock=False) -> str | None:
    if not config.approved_by_identity:
        return "approval_required"
    if lock:
        from backend.app.core.auth_lifetime import AccountCapture, guard_accounts

        # Refusal fanout must never follow this one-account lock: a caller
        # whose allowed read turned refused rolls back and starts again.
        existing = await db.scalar(select(User.id).where(User.account_identity == config.approved_by_identity))
        if existing is None:
            return "approver_not_authorized"
        from fastapi import HTTPException

        try:
            await guard_accounts(db, [AccountCapture(config.approved_by_identity, None, existing)])
        except HTTPException as exc:
            if exc.status_code == 401:
                return "approver_not_authorized"
            raise
    user = (
        await db.execute(
            select(User)
            .where(User.account_identity == config.approved_by_identity)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or not user.is_admin or not user.has_permission(Permission.GIT_BACKUP):
        return "approver_not_authorized"
    return None


async def load_config(db, config_id: int):
    await lock_credential_config(db)
    return (
        await db.execute(
            select(GitBackupConfig)
            .where(GitBackupConfig.id == config_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def blocked_attempt(config, log, reason):
    now = datetime.utcnow()
    log.status = "blocked"
    log.reason_code = reason
    log.error_message = BLOCK_MESSAGES[reason]
    log.completed_at = now
    config.last_backup_at = now
    config.last_backup_status = "blocked"
    config.last_backup_message = BLOCK_MESSAGES[reason]
    config.last_backup_reason_code = reason
    if config.admission_episode_id is None:
        config.admission_episode_id = str(uuid4())
        config.admission_episode_notified_at = None
    config.admission_episode_reason = reason


def close_episode(config):
    config.admission_episode_id = None
    config.admission_episode_reason = None
    config.admission_episode_notified_at = None
    config.last_backup_reason_code = None


async def stage_blocked_attempt(db, config, log, reason):
    """The caller holds anchor/config and NO account guard from admission.

    Episode, status, log, notification marker and recipient rows share its
    commit. An informational-channel failure rolls back this entire attempt;
    current status/support still expose the refusal without persisting it.
    """
    from backend.app.services.notification_inbox import StagedInboxBatch, stage_admin_event
    from backend.app.services.notification_service import render_internal_event

    blocked_attempt(config, log, reason)
    batch = StagedInboxBatch()
    if config.admission_episode_notified_at is None:
        title, message = await render_internal_event(db, "git_backup_not_admitted", {})
        batch = await stage_admin_event(
            db,
            event_type="git_backup_not_admitted",
            episode_id=config.admission_episode_id,
            title=title,
            message=message,
            extra_data={"reason_code": reason, "url": "/settings?tab=backup"},
        )
        config.admission_episode_notified_at = datetime.utcnow()
    return batch


async def claim_attempt(db, config, trigger, now):
    """Caller holds the stable anchor. Claim due and advance cadence together."""
    scheduled_for = None
    if trigger == "scheduled":
        if not config.schedule_enabled or config.next_scheduled_run is None:
            return None
        scheduled_for = config.next_scheduled_run.replace(tzinfo=None)
        if scheduled_for > now.replace(tzinfo=None):
            return None
        prior = await db.scalar(
            select(GitBackupLog.id).where(
                GitBackupLog.config_id == config.id, GitBackupLog.scheduled_for == scheduled_for
            )
        )
        if prior is not None:
            return None
    log = GitBackupLog(config_id=config.id, status="running", trigger=trigger, scheduled_for=scheduled_for)
    db.add(log)
    return log
