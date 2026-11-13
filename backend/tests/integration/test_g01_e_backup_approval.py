"""Actual SQLite transactions for export approval, occurrences and inbox episodes."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.git_backup import GitBackupConfig, GitBackupLog
from backend.app.models.notification_template import NotificationTemplate
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services import git_backup, notification_service
from backend.app.services.git_backup_approval import approval_reason


@pytest.fixture
async def backup(test_engine, monkeypatch):
    from backend.app.core.credential_config import initialize_credential_config_anchor

    async with test_engine.begin() as conn:
        await initialize_credential_config_anchor(conn)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(git_backup, "async_session", factory)
    auth_runtime.open_admission()
    service = git_backup.GitBackupService()
    service.test_connection = AsyncMock(return_value={"success": True, "is_private": True})
    service._collect_backup_data = AsyncMock(return_value={"private.json": {"secret": "test-only"}})
    service._push_to_provider = AsyncMock(return_value={"status": "success", "commit_sha": "a" * 40})
    async with factory() as db:
        admin = User(username="approver", role="admin", is_active=True, groups=[])
        viewer = User(username="viewer", role="user", is_active=True, groups=[])
        db.add_all([admin, viewer])
        db.add(
            NotificationTemplate(
                event_type="git_backup_not_admitted",
                name="Git backup paused",
                title_template="Git backup paused",
                body_template="Review Settings → Backup.",
            )
        )
        await db.flush()
        config = GitBackupConfig(
            repository_url="https://github.com/test/private",
            access_token="test-token",
            schedule_enabled=True,
            schedule_type="hourly",
            next_scheduled_run=datetime.utcnow() - timedelta(seconds=1),
            approved_by_identity=admin.account_identity,
            revision=str(uuid4()),
        )
        db.add(config)
        await db.commit()
        ids = config.id, admin.id, viewer.id
    monkeypatch.setattr(notification_service, "_internal_templates_ready", True)
    yield service, factory, ids
    assert await service.quiesce_for_restore(2)


async def counts(factory):
    async with factory() as db:
        return (
            await db.scalar(select(func.count()).select_from(GitBackupLog)),
            await db.scalar(select(func.count()).select_from(UserNotification)),
        )


@pytest.mark.parametrize("state", ["legacy", "inactive", "demoted", "deleted", "recreated"])
async def test_refused_admission_never_contacts_provider_or_captures(backup, state):
    service, factory, (cid, aid, _) = backup
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        admin = await db.get(User, aid)
        if state == "legacy":
            config.approved_by_identity = None
        elif state == "inactive":
            admin.is_active = False
        elif state == "demoted":
            admin.role = "user"
        else:
            await db.delete(admin)
            await db.flush()
            if state == "recreated":
                db.add(User(id=aid, username="approver", role="admin", groups=[]))
        await db.commit()
    result = await service.run_backup(cid, "scheduled")
    assert not result["success"]
    assert result["reason_code"] == ("approval_required" if state == "legacy" else "approver_not_authorized")
    service.test_connection.assert_not_awaited()
    service._collect_backup_data.assert_not_awaited()
    service._push_to_provider.assert_not_awaited()
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        log = await db.get(GitBackupLog, result["log_id"])
        assert log.status == config.last_backup_status == "blocked"
        assert log.completed_at and log.scheduled_for
        assert config.next_scheduled_run > datetime.utcnow()
        assert config.last_successful_backup_at is None


async def test_occurrences_episode_regrant_and_new_episode(backup):
    service, factory, (cid, aid, _) = backup
    async with factory() as db:
        (await db.get(GitBackupConfig, cid)).approved_by_identity = None
        await db.commit()
    await service.run_backup(cid, "scheduled")
    assert await counts(factory) == (1, 1)
    await service.run_backup(cid, "scheduled")
    assert await counts(factory) == (1, 1)
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        episode = config.admission_episode_id
        config.next_scheduled_run = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
    # New process object, same durable episode and a distinct actual due slot.
    restarted = git_backup.GitBackupService()
    await restarted.run_backup(cid, "scheduled")
    assert await counts(factory) == (2, 1)
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        assert config.admission_episode_id == episode
        config.approved_by_identity = (await db.get(User, aid)).account_identity
        await db.commit()
    assert (await service.run_backup(cid))["success"]
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        success = config.last_successful_backup_at
        assert success and config.admission_episode_id is None
        (await db.get(User, aid)).role = "user"
        await db.commit()
    await service.run_backup(cid)
    # No eligible admins: close the delivery marker, never notify a viewer.
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        assert config.admission_episode_id != episode
        assert config.last_successful_backup_at == success
        assert config.admission_episode_notified_at
        (await db.get(User, aid)).role = "admin"
        (await db.get(User, aid)).session_generation += 1
        await db.commit()
    assert (await service.run_backup(cid))["success"]  # same identity, no re-save


@pytest.mark.parametrize("phase", ["privacy", "capture"])
@pytest.mark.parametrize("mutation", ["demote", "config", "restore"])
async def test_tuple_and_current_authority_rechecked_before_secret_push(backup, phase, mutation):
    service, factory, (cid, aid, _) = backup

    async def change(*args, **kwargs):
        async with factory() as db:
            if mutation == "demote":
                (await db.get(User, aid)).role = "user"
            elif mutation == "config":
                (await db.get(GitBackupConfig, cid)).branch = "changed"
            else:
                service._generation += 1
            await db.commit()
        return {"success": True, "is_private": True} if phase == "privacy" else {"secret.json": {"secret": 1}}

    if phase == "privacy":
        service.test_connection.side_effect = change
    else:
        service._collect_backup_data.side_effect = change
    result = await service.run_backup(cid)
    assert not result["success"]
    service._push_to_provider.assert_not_awaited()
    if phase == "privacy":
        service._collect_backup_data.assert_not_awaited()


async def test_optout_has_atomic_attempt_marker_without_inbox_rows(backup):
    service, factory, (cid, aid, _) = backup
    async with factory() as db:
        (await db.get(GitBackupConfig, cid)).approved_by_identity = None
        (await db.get(User, aid)).inbox_events = []
        await db.commit()
    await service.run_backup(cid)
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        assert config.admission_episode_id and config.admission_episode_notified_at
    assert await counts(factory) == (1, 0)


async def test_missing_template_rolls_back_whole_occurrence_then_retries(backup, monkeypatch):
    from fastapi import HTTPException

    service, factory, (cid, _, _) = backup
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        config.approved_by_identity = None
        due = config.next_scheduled_run
        await db.commit()
    monkeypatch.setattr(notification_service, "_internal_templates_ready", False)
    with pytest.raises(HTTPException) as error:
        await service.run_backup(cid, "scheduled")
    assert error.value.status_code == 503
    assert await counts(factory) == (0, 0)
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        assert config.admission_episode_id is None
        assert config.admission_episode_notified_at is None
        assert config.next_scheduled_run == due
    monkeypatch.setattr(notification_service, "_internal_templates_ready", True)
    await service.run_backup(cid, "scheduled")
    assert await counts(factory) == (1, 1)


async def test_quiesce_keeps_owned_work_and_prevents_push(backup):
    service, factory, (cid, _, _) = backup
    entered, release = asyncio.Event(), asyncio.Event()

    async def collect(*args):
        entered.set()
        await release.wait()
        return {"secret": 1}

    service._collect_backup_data.side_effect = collect
    task = asyncio.create_task(service.run_backup(cid))
    await entered.wait()
    assert not await service.quiesce_for_restore(0.01)
    assert task in service._tasks and not task.done()
    release.set()
    assert not (await task)["success"]
    assert await service.quiesce_for_restore(1)
    service._push_to_provider.assert_not_awaited()


async def test_pending_legacy_is_visible_before_any_run(backup):
    from backend.app.api.routes.support import _collect_git_backup_info

    service, factory, (cid, _, _) = backup
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        config.approved_by_identity = None
        await db.commit()
        assert await approval_reason(db, config) == "approval_required"
        info = await _collect_git_backup_info(db)
        assert info["blocked_count"] == 1
        assert info["blocked_by_reason"] == {"approval_required": 1}
        assert info["last_failure_count"] == 0
    assert await counts(factory) == (0, 0)


async def test_private_export_still_contains_allowed_credentials(backup):
    from backend.app.core.credential_config import CONFIG_GUARD_KEY
    from backend.app.models.settings import Settings

    _, factory, _ = backup
    async with factory() as db:
        db.add_all(
            [
                Settings(key="mqtt_password", value="fabricated-private-password"),
                Settings(key="ha_token", value="fabricated-private-token"),
            ]
        )
        await db.commit()
        files = {}
        await git_backup.GitBackupService()._collect_settings(db, files)
        settings = files["settings/app_settings.json"]["settings"]
        assert settings["mqtt_password"] == "fabricated-private-password"
        assert settings["ha_token"] == "fabricated-private-token"
        assert CONFIG_GUARD_KEY not in settings


async def test_manual_legacy_returns_409_with_safe_reason(async_client, db_session, test_engine, monkeypatch):
    service = git_backup.GitBackupService()
    monkeypatch.setattr(git_backup, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr("backend.app.api.routes.git_backup.git_backup_service", service)
    db_session.add(GitBackupConfig(repository_url="https://github.com/test/private", access_token="never-echo"))
    db_session.add(
        NotificationTemplate(
            event_type="git_backup_not_admitted",
            name="Paused",
            title_template="Paused",
            body_template="Review backup settings.",
        )
    )
    monkeypatch.setattr(notification_service, "_internal_templates_ready", True)
    await db_session.commit()
    response = await async_client.post("/api/v1/git-backup/run")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_required"
    assert response.json()["detail"]["log_id"]
    assert "never-echo" not in response.text


@pytest.mark.parametrize("action", ["logout", "password_reset"])
async def test_revoked_request_cannot_save_new_approval_after_privacy_wait(
    async_client, test_engine, monkeypatch, action
):
    from sqlalchemy import update

    from backend.app.models.auth_session import AuthSession

    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def privacy(*args, **kwargs):
        async with factory() as db:
            admin = await db.scalar(select(User).where(User.username == "test_admin"))
            if action == "logout":
                await db.execute(
                    update(AuthSession)
                    .where(AuthSession.account_identity == admin.account_identity)
                    .values(revoked_at=datetime.utcnow())
                )
            else:
                admin.session_generation += 1
            await db.commit()
        return {"success": True, "is_private": True}

    monkeypatch.setattr(git_backup.git_backup_service, "test_connection", privacy)
    response = await async_client.post(
        "/api/v1/git-backup/config",
        json={
            "repository_url": "https://github.com/test/new",
            "access_token": "new-secret",
        },
    )
    assert response.status_code == 401
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GitBackupConfig)) == 0


@pytest.mark.parametrize("after_commit", [False, True])
async def test_blocked_commit_failure_and_lost_reply_reconcile_without_duplicate(backup, monkeypatch, after_commit):
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    service, factory, (cid, _, _) = backup
    async with factory() as db:
        (await db.get(GitBackupConfig, cid)).approved_by_identity = None
        await db.commit()
    original = AsyncSession.commit
    once = True

    async def commit(db):
        nonlocal once
        # Flush stages the inbox before this seam. Only fail the actual
        # admission transaction, never the follow-up reader's cleanup.
        config = next((row for row in db.identity_map.values() if isinstance(row, GitBackupConfig)), None)
        if once and config and config.admission_episode_notified_at:
            once = False
            if after_commit:
                await original(db)
            raise OperationalError("test", {}, Exception("private text must not escape"))
        return await original(db)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with pytest.raises(HTTPException) as error:
        await service.run_backup(cid, "scheduled")
    assert error.value.status_code == 503
    assert await counts(factory) == ((1, 1) if after_commit else (0, 0))
    await service.run_backup(cid, "scheduled")
    assert await counts(factory) == (1, 1)
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        log = await db.scalar(select(GitBackupLog))
        assert config.last_backup_status == log.status == "blocked"
        assert config.admission_episode_id and config.admission_episode_notified_at


async def test_cancelled_postcommit_hint_cannot_rewrite_blocked_result(backup, monkeypatch):
    from backend.app.services import notification_inbox

    service, factory, (cid, _, _) = backup
    async with factory() as db:
        (await db.get(GitBackupConfig, cid)).approved_by_identity = None
        await db.commit()
    entered = asyncio.Event()

    async def dispatch(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(notification_inbox, "dispatch_staged", dispatch)
    task = asyncio.create_task(service.run_backup(cid, "scheduled"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await counts(factory) == (1, 1)
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        assert config.last_backup_status == "blocked"
        assert (await db.scalar(select(GitBackupLog))).status == "blocked"


async def test_old_completion_cannot_overwrite_reused_config_and_log_ids(backup):
    from sqlalchemy import delete

    service, factory, (cid, aid, _) = backup
    entered, release = asyncio.Event(), asyncio.Event()

    async def push(*args):
        entered.set()
        await release.wait()
        return {"status": "success", "commit_sha": "old-authorized-run"}

    service._push_to_provider.side_effect = push
    task = asyncio.create_task(service.run_backup(cid))
    await entered.wait()
    async with factory() as db:
        old_log = await db.scalar(select(GitBackupLog))
        old_log_id = old_log.id
        old_identity = old_log.attempt_identity
        await db.execute(delete(GitBackupLog))
        await db.execute(delete(GitBackupConfig))
        admin = await db.get(User, aid)
        fresh = GitBackupConfig(
            id=cid,
            repository_url="https://github.com/test/recreated",
            access_token="new",
            approved_by_identity=admin.account_identity,
            revision=str(uuid4()),
        )
        db.add(fresh)
        await db.flush()
        db.add(GitBackupLog(id=old_log_id, config_id=cid, status="running", trigger="manual"))
        await db.commit()
    release.set()
    await task
    async with factory() as db:
        config = await db.get(GitBackupConfig, cid)
        log = await db.get(GitBackupLog, old_log_id)
        assert log.attempt_identity != old_identity
        assert log.status == "running" and log.commit_sha is None
        assert config.last_backup_status is None
