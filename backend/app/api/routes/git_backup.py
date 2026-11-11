"""API routes for Git backup (GitHub and GitLab)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequireAdmin, RequirePermission
from backend.app.core.credential_config import canonical_url, lock_credential_config, merge_secret_fields
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.git_backup import GitBackupConfig, GitBackupLog
from backend.app.models.user import User
from backend.app.schemas.git_backup import (
    REF_PATTERN,
    GitBackupConfigCreate,
    GitBackupConfigResponse,
    GitBackupConfigUpdate,
    GitBackupLogResponse,
    GitBackupStatus,
    GitBackupTriggerResponse,
    GitCommitListResponse,
    GitRestorePreview,
    GitRestoreRequest,
    GitRestoreResponse,
    GitTestConnectionRequest,
    GitTestConnectionResponse,
    RestoreCategory,
    _validate_api_base_url,
    _validate_repo_url,
)
from backend.app.services.git_backup import git_backup_service
from backend.app.services.git_restore import git_restore_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/git-backup", tags=["git-backup"])


# Privacy-gate copy for the "target repository must be private" rejection.
# BamDude backups bundle MQTT credentials, Home Assistant / Prometheus tokens,
# the Bambu Cloud email, and printer access codes (via K-profiles), so a
# non-private target is a hard reject. Upstream Bambuddy commit 48a7024b.
_PUBLIC_REPO_ERROR = (
    "Refusing to save: the target repository is not private. BamDude backups "
    "include MQTT credentials, Home Assistant tokens, Prometheus tokens, your "
    "Bambu Cloud email, the printer access codes via K-profiles, and other "
    "settings that must not be exposed publicly. Make the repository private "
    "in your provider's UI and try again."
)
_UNKNOWN_VISIBILITY_ERROR = (
    "Refusing to save: could not confirm the target repository is private. "
    "BamDude backups contain credentials and must never go to a public or "
    "internal-visibility repository. Verify the URL, the access token's scope, "
    "and that your provider exposes the 'private' / 'visibility' field on its "
    "repo API."
)


async def _enforce_private_repo(repo_url: str, token: str, provider: str, api_base_url: str | None = None) -> None:
    """Run a test_connection and refuse if the repo isn't confirmed private.

    Used by POST /config and PATCH /config (when URL / token / provider
    change) so a backup configuration can never be saved against a public or
    internal-visibility repository. Fails closed on unknown visibility — the
    cost of one false-positive rejection is far below the cost of one
    silently leaked credentials bundle.
    """
    result = await git_backup_service.test_connection(repo_url, token, provider=provider, api_base_url=api_base_url)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail="Cannot verify repository. Check the provider and credentials.")
    is_private = result.get("is_private")
    if is_private is None:
        raise HTTPException(status_code=400, detail=_UNKNOWN_VISIBILITY_ERROR)
    if is_private is False:
        raise HTTPException(status_code=400, detail=_PUBLIC_REPO_ERROR)


def _config_to_response(config: GitBackupConfig) -> dict:
    """Convert config model to response dict."""
    try:
        public_api_base = _validate_api_base_url(config.api_base_url)
    except ValueError:
        public_api_base = None
    return {
        "id": config.id,
        "provider": config.provider,
        "repository_url": config.repository_url,
        "has_token": bool(config.access_token),
        "branch": config.branch,
        "api_base_url": public_api_base,
        "api_base_url_private": bool(config.api_base_url and public_api_base is None),
        "schedule_enabled": config.schedule_enabled,
        "schedule_type": config.schedule_type,
        "backup_kprofiles": config.backup_kprofiles,
        "backup_cloud_profiles": config.backup_cloud_profiles,
        "backup_settings": config.backup_settings,
        "backup_spools": config.backup_spools,
        "backup_archives": config.backup_archives,
        "enabled": config.enabled,
        "last_backup_at": config.last_backup_at,
        "last_backup_status": config.last_backup_status,
        "last_backup_message": _safe_backup_message(config.last_backup_status) if config.last_backup_message else None,
        "last_backup_commit_sha": config.last_backup_commit_sha,
        "next_scheduled_run": config.next_scheduled_run,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _safe_backup_message(status: str | None) -> str:
    if status == "success":
        return "Git backup completed."
    if status == "skipped":
        return "No Git backup changes were written."
    return "Git backup did not complete. Check repository visibility, credentials and provider configuration."


def _connection_response(result: dict) -> GitTestConnectionResponse:
    success = bool(result.get("success"))
    message = "Connection successful." if success else "Connection failed. Check the provider and credentials."
    if success and result.get("is_private") is False:
        message = "Connected, but the repository is not private. Git backups require a private repository."
    return GitTestConnectionResponse(success=success, message=message, is_private=result.get("is_private"))


_CONFIG_FIELDS = tuple(GitBackupConfigCreate.model_fields.keys() - {"secret_actions"})


def _snapshot(config: GitBackupConfig | None) -> dict:
    return {field: getattr(config, field) for field in _CONFIG_FIELDS} if config else {}


def _git_scope(values: dict) -> tuple:
    from backend.app.services.git_providers import get_provider_backend

    provider = values.get("provider") or "github"
    repository = values.get("repository_url") or ""
    try:
        derived_api = get_provider_backend(provider).get_api_base(repository)
    except ValueError:
        derived_api = ""
    return (provider, canonical_url(repository), canonical_url(derived_api), canonical_url(values.get("api_base_url")))


async def _write_config(
    db: AsyncSession, body: GitBackupConfigCreate | GitBackupConfigUpdate, *, partial: bool
) -> dict:
    await lock_credential_config(db)
    config = (await db.execute(select(GitBackupConfig).limit(1))).scalar_one_or_none()
    if partial and config is None:
        raise HTTPException(404, "No configuration found")
    before = _snapshot(config)
    config_id = config.id if config else None
    changes = body.model_dump(exclude_unset=partial, exclude={"secret_actions", "access_token"}, mode="json")
    if config and "api_base_url" not in body.model_fields_set:
        try:
            _validate_api_base_url(before.get("api_base_url"))
        except ValueError:
            # An opaque legacy endpoint cannot be reconstructed from its read
            # DTO. Omission keeps it even in the legacy singleton POST writer.
            changes.pop("api_base_url", None)
    if "access_token" in body.model_fields_set:
        changes["access_token"] = body.access_token
    resolved = merge_secret_fields(
        before, changes, {"access_token"}, body.secret_actions, creating=config is None, scope=_git_scope
    )
    if any(resolved.get(field) is None for field in _CONFIG_FIELDS if field not in {"access_token", "api_base_url"}):
        raise HTTPException(422, "Required Git backup configuration fields cannot be null")
    resolved["access_token"] = resolved.get("access_token") or ""
    try:
        resolved["repository_url"] = _validate_repo_url(resolved.get("repository_url") or "", resolved.get("provider"))
    except ValueError as exc:
        raise HTTPException(422, "Invalid repository URL for the selected provider") from exc
    if not resolved.get("access_token") and (resolved.get("enabled") or resolved.get("schedule_enabled")):
        raise HTTPException(400, "A token is required while Git backup or its schedule is enabled")
    # Privacy calls use a frozen pair outside the short database transaction.
    verify = bool(resolved.get("access_token")) and (
        not config
        or any(before.get(k) != resolved.get(k) for k in ("provider", "repository_url", "api_base_url", "access_token"))
    )
    if verify:
        await db.rollback()
        await _enforce_private_repo(
            resolved["repository_url"], resolved["access_token"], resolved["provider"], resolved.get("api_base_url")
        )
        await lock_credential_config(db)
        config = (
            await db.execute(select(GitBackupConfig).limit(1).execution_options(populate_existing=True))
        ).scalar_one_or_none()
        if (config.id if config else None) != config_id or _snapshot(config) != before:
            raise HTTPException(409, "Git backup configuration changed during verification; reload and retry")
    if config is None:
        config = GitBackupConfig()
        db.add(config)
    for field, value in resolved.items():
        setattr(config, field, value)
    config.next_scheduled_run = (
        git_backup_service._calculate_next_run(config.schedule_type) if config.schedule_enabled else None
    )
    await db.commit()
    await db.refresh(config)
    return _config_to_response(config)


@router.get("/config", response_model=GitBackupConfigResponse | None, dependencies=[RequireAdmin()])
async def get_config(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Get the current Git backup configuration."""
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        return None

    return _config_to_response(config)


@router.post("/config", response_model=GitBackupConfigResponse, dependencies=[RequireAdmin()])
async def save_config(
    config_data: GitBackupConfigCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Create or replace the singleton config, preserving only scoped secrets."""
    return await _write_config(db, config_data, partial=False)


@router.patch("/config", response_model=GitBackupConfigResponse, dependencies=[RequireAdmin()])
async def update_config(
    update_data: GitBackupConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Partially update the singleton config under the shared credential guard."""
    return await _write_config(db, update_data, partial=True)


@router.delete("/config", dependencies=[RequireAdmin()])
async def delete_config(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Delete the Git backup configuration and all logs."""
    await lock_credential_config(db)
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="No configuration found")

    await db.delete(config)
    await db.commit()

    logger.info("Deleted Git backup config")

    return {"message": "Configuration deleted"}


@router.post("/test", response_model=GitTestConnectionResponse, dependencies=[RequireAdmin()])
async def test_connection(
    body: GitTestConnectionRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Resolve the complete draft pair; a test never persists the draft."""
    await lock_credential_config(db)
    config = (await db.execute(select(GitBackupConfig).limit(1))).scalar_one_or_none()
    stored = _snapshot(config)
    changes = {"repository_url": body.repo_url, "provider": body.provider.value}
    if "api_base_url" in body.model_fields_set:
        changes["api_base_url"] = body.api_base_url
    if "token" in body.model_fields_set:
        changes["access_token"] = body.token
    actions = body.secret_actions
    if actions is not None:
        if set(actions) - {"token"}:
            raise HTTPException(422, "Unknown secret action field")
        actions = {"access_token": actions["token"]} if "token" in actions else {}
    resolved = merge_secret_fields(
        stored, changes, {"access_token"}, actions, creating=config is None, scope=_git_scope
    )
    token = resolved.get("access_token")
    if not token:
        raise HTTPException(400, "No access token configured")
    await db.rollback()
    result = await git_backup_service.test_connection(
        body.repo_url, token, provider=body.provider.value, api_base_url=resolved.get("api_base_url")
    )
    if not result.get("success"):
        result = {"success": False, "message": "Connection failed. Check the provider and credentials."}
    return _connection_response(result)


@router.post("/test-stored", response_model=GitTestConnectionResponse, dependencies=[RequireAdmin()])
async def test_stored_connection(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Test the stored pair without applying any draft destination."""
    await lock_credential_config(db)
    config = (await db.execute(select(GitBackupConfig).limit(1))).scalar_one_or_none()
    if not config:
        raise HTTPException(404, "No configuration found")
    frozen = _snapshot(config)
    if not frozen["access_token"]:
        raise HTTPException(400, "No access token configured")
    await db.rollback()
    result = await git_backup_service.test_connection(
        frozen["repository_url"],
        frozen["access_token"],
        provider=frozen["provider"],
        api_base_url=frozen["api_base_url"],
    )
    if not result.get("success"):
        result = {"success": False, "message": "Connection failed. Check the provider and credentials."}
    return _connection_response(result)


@router.post("/run", response_model=GitBackupTriggerResponse, dependencies=[RequireAdmin()])
async def trigger_backup(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Manually trigger a backup."""
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="No configuration found. Configure backup first.")

    if not config.enabled:
        raise HTTPException(status_code=400, detail="Backup is disabled")

    backup_result = await git_backup_service.run_backup(config.id, trigger="manual")

    return GitBackupTriggerResponse(
        **{**backup_result, "message": _safe_backup_message("success" if backup_result.get("success") else "failed")}
    )


@router.get("/status", response_model=GitBackupStatus)
async def get_status(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Get current backup status."""
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        return GitBackupStatus(
            configured=False,
            enabled=False,
            is_running=False,
            restore_running=False,
            progress=None,
            last_backup_at=None,
            last_backup_status=None,
            next_scheduled_run=None,
        )

    return GitBackupStatus(
        configured=True,
        enabled=config.enabled,
        is_running=git_backup_service.is_running,
        restore_running=git_restore_service.is_running,
        # Either operation's progress: only one of the two can run at a time
        # (each refuses while the other holds), so there is never a choice to
        # make here.
        progress=git_backup_service.progress or git_restore_service.progress,
        last_backup_at=config.last_backup_at,
        last_backup_status=config.last_backup_status,
        next_scheduled_run=config.next_scheduled_run,
    )


@router.get("/logs", response_model=list[GitBackupLogResponse], dependencies=[RequireAdmin()])
async def get_logs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Get backup logs."""
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        return []

    logs_result = await db.execute(
        select(GitBackupLog)
        .where(GitBackupLog.config_id == config.id)
        .order_by(desc(GitBackupLog.started_at))
        .offset(offset)
        .limit(limit)
    )
    logs = logs_result.scalars().all()

    return [
        GitBackupLogResponse(
            id=log.id,
            config_id=log.config_id,
            started_at=log.started_at,
            completed_at=log.completed_at,
            status=log.status,
            trigger=log.trigger,
            commit_sha=log.commit_sha,
            files_changed=log.files_changed,
            error_message=_safe_backup_message(log.status) if log.error_message else None,
        )
        for log in logs
    ]


@router.delete("/logs", dependencies=[RequireAdmin()])
async def clear_logs(
    keep_last: int = Query(default=10, ge=0, le=100, description="Number of recent logs to keep"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_BACKUP),
):
    """Clear backup logs, optionally keeping the most recent entries."""
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        return {"deleted": 0, "message": "No configuration found"}

    if keep_last > 0:
        # Get IDs to keep
        keep_result = await db.execute(
            select(GitBackupLog.id)
            .where(GitBackupLog.config_id == config.id)
            .order_by(desc(GitBackupLog.started_at))
            .limit(keep_last)
        )
        keep_ids = [row[0] for row in keep_result.fetchall()]

        if keep_ids:
            delete_result = await db.execute(
                delete(GitBackupLog).where(GitBackupLog.config_id == config.id, GitBackupLog.id.not_in(keep_ids))
            )
        else:
            delete_result = await db.execute(delete(GitBackupLog).where(GitBackupLog.config_id == config.id))
    else:
        delete_result = await db.execute(delete(GitBackupLog).where(GitBackupLog.config_id == config.id))

    await db.commit()

    deleted_count = delete_result.rowcount
    logger.info("Deleted %s Git backup logs (kept %s)", deleted_count, keep_last)

    return {"deleted": deleted_count, "message": f"Deleted {deleted_count} logs"}


# The permission that owns each category's rows, required ON TOP OF
# ``git:restore``. Backup is its own permission group, so without this a role
# holding only Backup could write — via a restore — rows it cannot write through
# the endpoint that owns them.
#
#   * SETTINGS  → settings:update
#   * SPOOLS    → inventory:update (spool rows and their usage history both
#     restore under this category)
#   * ARCHIVES  → archives:update_all, NOT archives:create. ⚠️ A restore writes
#     rows owned by other users — that is the whole point of carrying the owner
#     across — and update_all is the permission that means "may write an archive
#     that is not yours". create alone would let create_own-shaped access seed
#     history onto somebody else.
#   * KPROFILES → kprofiles:update, which is what the restore ultimately calls.
_CATEGORY_WRITE_PERMISSION = {
    RestoreCategory.SETTINGS: Permission.SETTINGS_UPDATE,
    RestoreCategory.SPOOLS: Permission.INVENTORY_UPDATE,
    RestoreCategory.ARCHIVES: Permission.ARCHIVES_UPDATE_ALL,
    RestoreCategory.KPROFILES: Permission.KPROFILES_UPDATE,
}


@router.get("/commits", response_model=GitCommitListResponse, dependencies=[RequireAdmin()])
async def list_commits(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_RESTORE),
):
    """List recent backup commits so the user can pick one to restore from."""
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="No configuration found. Configure backup first.")

    commit_result = await git_restore_service.list_commits(config, limit=limit)
    return GitCommitListResponse(**commit_result)


@router.get("/restore/preview", response_model=GitRestorePreview, dependencies=[RequireAdmin()])
async def preview_restore(
    ref: str = Query(default="HEAD", pattern=REF_PATTERN),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.GIT_RESTORE),
):
    """Report which categories a given backup commit contains."""
    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="No configuration found. Configure backup first.")

    preview = await git_restore_service.preview(db, config, ref=ref)
    return GitRestorePreview(**preview)


@router.post("/restore", response_model=GitRestoreResponse, dependencies=[RequireAdmin()])
async def restore_backup(
    request: GitRestoreRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.GIT_RESTORE),
):
    """Restore selected categories from one backup commit.

    Note there is no private-repo gate here, unlike the config endpoints: that
    check exists to stop credentials leaving the instance, and this path only
    reads. A config can only be saved against a private repo anyway.

    Every category needs the permission that owns the rows it writes, on top of
    ``git:restore`` — see ``_CATEGORY_WRITE_PERMISSION`` and the check below.
    """
    if current_user is not None:
        # Each category rewrites rows some other endpoint already owns, and
        # Backup is its own permission group — so a role holding only Backup
        # could otherwise write, through a restore, what it cannot write through
        # the endpoint that owns them. This module already makes that argument;
        # it is why the four protected auth keys are refused outright.
        #
        # current_user is None only when auth is disabled: git:restore is in
        # _APIKEY_DENIED_PERMISSIONS, so an API key never gets past the
        # dependency to reach this line.
        missing = sorted(
            {
                permission.value
                for category, permission in _CATEGORY_WRITE_PERMISSION.items()
                if category in request.categories and not current_user.has_all_permissions(permission.value)
            }
        )
        if missing:
            raise HTTPException(
                status_code=403,
                detail=f"Missing required permissions: {', '.join(missing)}",
            )

    result = await db.execute(select(GitBackupConfig).limit(1))
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="No configuration found. Configure backup first.")

    restore_result = await git_restore_service.run_restore(
        config.id,
        ref=request.ref,
        categories=request.categories,
        overwrite_existing=request.overwrite_existing,
    )
    return GitRestoreResponse(**restore_result)
