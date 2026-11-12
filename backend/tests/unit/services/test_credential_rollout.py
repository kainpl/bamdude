"""Durable rollout notice is informational, atomic and restore-owned."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from backend.app.core import database
from backend.app.i18n import set_language_cache
from backend.app.models.credential_rollout import CredentialRolloutEpisode
from backend.app.models.notification_template import NotificationTemplate
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services import credential_rollout as cr, notification_inbox as inbox, notification_service as ns


async def seed(db, *, admin=True, subscribed=None):
    user = User(
        username=str(uuid.uuid4()), password_hash="x", role="admin" if admin else "user", inbox_events=subscribed
    )
    episode = CredentialRolloutEpisode(
        id=str(uuid.uuid4()),
        contract_key=cr.CONTRACT_KEY,
        ownerless_api_key_count=2,
        owned_api_key_count=3,
        long_lived_token_count=4,
    )
    db.add_all(
        [
            user,
            episode,
            NotificationTemplate(
                event_type=cr.EVENT,
                name="Reissue",
                title_template="Reissue",
                body_template="Owned {owned_api_key_count}; LL {long_lived_token_count}",
            ),
        ]
    )
    await db.commit()
    set_language_cache("en")
    ns.complete_internal_template_reconcile()
    return user, episode


@pytest.mark.asyncio
async def test_stage_is_atomic_without_commit_or_ws_and_default_deliver_still_commits(db_session):
    user, episode = await seed(db_session)
    user_id, identity, episode_id = user.id, user.account_identity, episode.id
    with patch.object(inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock) as ws:
        batch = await inbox.stage_admin_event(
            db_session, event_type=cr.EVENT, episode_id=episode_id, title="T", message="M"
        )
        assert batch.items[0].account_identity == identity
        assert batch.items[0].unread_count == 1
        assert ws.await_count == 0
        await db_session.rollback()
        assert await db_session.scalar(select(func.count()).select_from(UserNotification)) == 0
        await inbox.deliver(db_session, event_type="print_failed", title="T", message="M")
        await db_session.rollback()
        assert await db_session.scalar(select(func.count()).select_from(UserNotification)) == 1
        assert ws.await_args.args[0] == user_id


@pytest.mark.asyncio
@pytest.mark.parametrize("admin,subscribed,expected", [(True, None, 1), (True, [], 0), (False, None, 0)])
async def test_atomic_delivery_current_admin_subscription_and_terminal_zero(db_session, admin, subscribed, expected):
    user, episode = await seed(db_session, admin=admin, subscribed=subscribed)
    episode_id, identity = episode.id, user.account_identity
    runtime = cr.CredentialRolloutRuntime()
    with patch.object(inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock) as ws:
        assert await runtime._attempt() is False
        assert await runtime._attempt() is False
        assert ws.await_count == expected
        if expected:
            assert ws.await_args.kwargs == {"expected_account_identity": identity}
    db_session.expire_all()
    saved = await db_session.get(CredentialRolloutEpisode, episode_id)
    assert saved.delivered_at is not None
    assert await db_session.scalar(select(func.count()).select_from(UserNotification)) == expected


@pytest.mark.asyncio
async def test_staging_failure_rolls_back_rows_and_marker_and_retries(db_session):
    _, episode = await seed(db_session)
    episode_id = episode.id
    real = cr.stage_admin_event

    async def fail(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("CANARY_NO_PUBLIC_LOG")

    with patch.object(cr, "stage_admin_event", fail):
        assert await cr.CredentialRolloutRuntime()._attempt() is True
    db_session.expire_all()
    assert (await db_session.get(CredentialRolloutEpisode, episode_id)).delivered_at is None
    assert await db_session.scalar(select(func.count()).select_from(UserNotification)) == 0
    with patch.object(inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock):
        assert await cr.CredentialRolloutRuntime()._attempt() is False


@pytest.mark.asyncio
async def test_missing_template_readiness_is_bounded_pending_and_does_not_change_facts(db_session):
    _, episode = await seed(db_session)
    episode_id = episode.id
    ns.begin_internal_template_reconcile()
    runtime = cr.CredentialRolloutRuntime(attempt_seconds=0.3, readiness_seconds=0.04)
    start = asyncio.get_running_loop().time()
    assert await runtime._attempt() is True
    assert asyncio.get_running_loop().time() - start < 0.3
    db_session.expire_all()
    assert (await db_session.get(CredentialRolloutEpisode, episode_id)).delivered_at is None
    ns.complete_internal_template_reconcile()


@pytest.mark.asyncio
async def test_after_actual_admission_only_and_retry_single_flight_after_cleanup():
    ready = False
    runtime = cr.CredentialRolloutRuntime(retry_seconds=0.03)
    calls = []

    async def attempt():
        calls.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.02)
        return len(calls) < 2

    with patch.object(runtime, "_attempt", attempt):
        runtime.start(lambda: ready)
        await asyncio.sleep(0.06)
        assert calls == []
        ready = True
        await asyncio.wait_for(asyncio.shield(runtime._loop_task), 1)
        assert len(calls) == 2 and calls[1] - calls[0] >= 0.05
        assert await runtime.shutdown(0.1)


@pytest.mark.asyncio
async def test_timeout_owned_cleanup_prevents_restore_and_second_attempt():
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runtime = cr.CredentialRolloutRuntime(attempt_seconds=0.02, retry_seconds=0.01)
    calls = 0

    async def reconcile(deadline):
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await cr.owned(release.wait())

    with patch.object(runtime, "_reconcile", reconcile):
        runtime.start(lambda: True)
        await entered.wait()
        await cleanup.wait()
        assert not await runtime.quiesce_for_restore(0.01)
        assert calls == 1 and not runtime._attempt_task.done()
        release.set()
        assert await runtime.shutdown(1)


@pytest.mark.asyncio
async def test_lost_commit_reply_reconciles_without_duplicate(db_session):
    await seed(db_session)
    original = database.async_session.class_.commit
    first = True

    async def lost_reply(self):
        nonlocal first
        await original(self)
        if first:
            first = False
            raise OSError("unknown commit reply")

    with patch.object(database.async_session.class_, "commit", lost_reply):
        assert await cr.CredentialRolloutRuntime()._attempt() is True
    with patch.object(inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock) as ws:
        assert await cr.CredentialRolloutRuntime()._attempt() is False
        ws.assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(UserNotification)) == 1


@pytest.mark.asyncio
async def test_sqlite_busy_timeout_restored_even_after_committing(db_session, test_engine):
    if test_engine.dialect.name != "sqlite":
        pytest.skip("SQLite connection-local timeout")
    async with test_engine.connect() as connection:
        before = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
    async with cr._session(asyncio.get_running_loop().time() + 0.5) as db:
        await db.commit()
    async with test_engine.connect() as connection:
        assert (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one() == before


@pytest.mark.asyncio
async def test_dispatch_never_opens_sql_and_ws_failure_does_not_propagate():
    batch = inbox.StagedInboxBatch((inbox.StagedInboxItem(9, str(uuid.uuid4()), {"id": 1}, 1),))
    with patch.object(inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
        await inbox.dispatch_staged(batch)


@pytest.mark.asyncio
async def test_stage_dedupe_survives_repeat_and_never_resends_existing_rows(db_session):
    user, episode = await seed(db_session)
    kwargs = {"event_type": cr.EVENT, "episode_id": episode.id, "title": "T", "message": "M"}
    first = await inbox.stage_admin_event(db_session, **kwargs)
    await db_session.commit()
    second = await inbox.stage_admin_event(db_session, **kwargs)
    await db_session.commit()
    assert len(first.items) == 1 and second.items == ()
    assert await db_session.scalar(select(func.count()).select_from(UserNotification)) == 1
    row = (await db_session.execute(select(UserNotification))).scalar_one()
    assert row.dedupe_key == f"{cr.EVENT}:{episode.id}:{user.account_identity}"


@pytest.mark.asyncio
async def test_strict_template_is_fresh_not_cached_and_missing_row_never_falls_back(db_session):
    await seed(db_session)
    row = (await db_session.execute(select(NotificationTemplate))).scalar_one()
    row.title_template = "Fresh {owned_api_key_count}"
    row.is_default = False
    await db_session.commit()
    assert await ns.render_internal_event(
        db_session, cr.EVENT, {"owned_api_key_count": 3, "long_lived_token_count": 4}
    ) == (
        "Fresh 3",
        "Owned 3; LL 4",
    )
    with pytest.raises(ns.InternalTemplateNotReady):
        await ns.render_internal_event(db_session, cr.EVENT, {})
    await db_session.delete(row)
    await db_session.commit()
    with pytest.raises(ns.InternalTemplateNotReady):
        await ns.render_internal_event(db_session, cr.EVENT, {})


@pytest.mark.asyncio
async def test_locale_mismatch_and_missing_shipped_template_are_pending(db_session, monkeypatch, tmp_path):
    from backend.app.models.settings import Settings

    await seed(db_session)
    db_session.add(Settings(key="language", value="uk"))
    await db_session.commit()
    with pytest.raises(ns.InternalTemplateNotReady):
        await ns.render_internal_event(db_session, cr.EVENT, {})
    set_language_cache("uk")
    assert (
        await ns.render_internal_event(db_session, cr.EVENT, {"owned_api_key_count": 3, "long_lived_token_count": 4})
    )[0] == "Reissue"
    # Asset disappearance is checked on each attempt, not a cached fallback.
    monkeypatch.setattr(ns, "__file__", str(tmp_path / "services" / "notification_service.py"))
    with pytest.raises(ns.InternalTemplateNotReady):
        await ns.render_internal_event(db_session, cr.EVENT, {})
    set_language_cache("en")


@pytest.mark.asyncio
async def test_live_failure_after_commit_does_not_reopen_or_replay_episode(db_session):
    await seed(db_session)
    with patch.object(
        inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock, side_effect=OSError("CANARY")
    ) as ws:
        runtime = cr.CredentialRolloutRuntime()
        assert await runtime._attempt() is False
        assert await runtime._attempt() is False
        assert ws.await_count == 1
    assert await db_session.scalar(select(func.count()).select_from(UserNotification)) == 1


@pytest.mark.asyncio
async def test_fresh_install_has_no_episode_and_safe_status_uses_historical_counts(db_session):
    assert await cr.CredentialRolloutRuntime()._attempt() is False
    result = await cr.status(db_session)
    assert result["notification_state"] == "none" and result["episode_id"] is None
    _, episode = await seed(db_session)
    result = await cr.status(db_session)
    assert result["notification_state"] == "pending"
    assert result["episode_id"] == episode.id
    assert result["ownerless_api_key_count"] == 2 and result["owned_api_key_count"] == 3
    assert "username" not in result and "secret" not in result


@pytest.mark.asyncio
async def test_required_readiness_sql_is_inside_its_budget_and_cleanup_stays_owned(db_session):
    await seed(db_session)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def slow_template(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    # Exercise the actual production budget. A 30ms test budget can expire
    # during a legitimate SQLite checkout under parallel load, before this
    # template cancellation/cleanup scenario has even begun.
    runtime = cr.CredentialRolloutRuntime()
    with patch.object(cr, "render_internal_event", slow_template):
        assert await runtime._attempt() is True
    assert entered.is_set() and cleaned.is_set()


@pytest.mark.asyncio
async def test_sqlite_timeout_reset_after_rollback(db_session, test_engine):
    if test_engine.dialect.name != "sqlite":
        pytest.skip("SQLite connection-local timeout")
    async with test_engine.connect() as connection:
        before = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
    with pytest.raises(RuntimeError):
        async with cr._session(asyncio.get_running_loop().time() + 0.5):
            raise RuntimeError("rollback")
    async with test_engine.connect() as connection:
        assert (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one() == before


@pytest.mark.asyncio
async def test_unproven_cleanup_halts_retries_and_refuses_restore_even_when_task_ended():
    runtime = cr.CredentialRolloutRuntime(retry_seconds=0.001)
    with patch.object(runtime, "_reconcile", new_callable=AsyncMock, side_effect=cr.CleanupUnproven()) as reconcile:
        runtime.start(lambda: True)
        await asyncio.wait_for(asyncio.shield(runtime._loop_task), 1)
        assert reconcile.await_count == 1
        assert runtime.phase == "cleanup_unproven"
        assert not await runtime.quiesce_for_restore(0.1)
        with pytest.raises(RuntimeError):
            runtime.start(lambda: True)


@pytest.mark.asyncio
async def test_cancelled_sql_cleanup_cannot_hide_unproven_driver_close(monkeypatch):
    from types import SimpleNamespace

    close_entered, release = asyncio.Event(), asyncio.Event()
    connection = SimpleNamespace(closed=False)
    connection.exec_driver_sql = AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 15000))
    connection.commit = AsyncMock()

    async def close():
        close_entered.set()
        await release.wait()
        raise OSError("driver close unproven")

    connection.close = close
    connection.invalidate = AsyncMock(side_effect=OSError("invalidate unproven"))
    session = SimpleNamespace(
        rollback=AsyncMock(),
        close=AsyncMock(),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )

    class Factory:
        kw = {"bind": SimpleNamespace(connect=AsyncMock(return_value=connection))}

        def __call__(self, **kwargs):
            return session

    monkeypatch.setattr(database, "async_session", Factory())

    async def work():
        async with cr._session(asyncio.get_running_loop().time() + 1):
            pass

    task = asyncio.create_task(work())
    await close_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(cr.CleanupUnproven):
        await task
