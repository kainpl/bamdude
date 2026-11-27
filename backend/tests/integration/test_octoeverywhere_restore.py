"""A full ZIP restore never starts sending frames to OctoEverywhere's cloud on its own.

The prepared copy has OctoEverywhere off and a marker; the first start of the restored database
tells every active administrator in the inbox and consumes the marker in one transaction.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services import failure_detection_restore as restore

pytestmark = pytest.mark.asyncio

KEY = "oe-test-key"


def _backup(tmp_path, rows: dict[str, str]):
    path = tmp_path / "bamdude.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", list(rows.items()))
    conn.commit()
    conn.close()
    return path


def _rows(path) -> dict[str, str]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT key, value FROM settings").fetchall())
    finally:
        conn.close()


@pytest.mark.parametrize("stored", ["true", "True"])
async def test_a_zip_with_octoeverywhere_on_restores_it_off_with_its_key_and_a_marker(tmp_path, stored):
    path = _backup(
        tmp_path,
        {"octoeverywhere_enabled": stored, "octoeverywhere_api_key": KEY, "octoeverywhere_enabled_printers": "[1]"},
    )
    operation = restore.turn_off_in_prepared_copy(path)
    rows = _rows(path)
    assert rows["octoeverywhere_enabled"] == "false"
    assert rows["octoeverywhere_api_key"] == KEY and rows["octoeverywhere_enabled_printers"] == "[1]"
    assert json.loads(rows[restore.MARKER]) == {"operation": operation}


@pytest.mark.parametrize(
    "rows", [{"octoeverywhere_enabled": "false"}, {"obico_enabled": "true"}, {"octoeverywhere_api_key": KEY}, {}]
)
async def test_a_zip_without_octoeverywhere_on_restores_as_it_is(tmp_path, rows):
    path = _backup(tmp_path, rows)
    assert restore.turn_off_in_prepared_copy(path) is None
    assert _rows(path) == rows


async def test_only_the_zip_restore_turns_octoeverywhere_off():
    """The SQLite -> PostgreSQL move keeps the setting: nothing but the restore route uses it."""
    from pathlib import Path

    backend = Path(restore.__file__).resolve().parents[1]
    users = [
        p.relative_to(backend).as_posix()
        for p in backend.rglob("*.py")
        if "turn_off_in_prepared_copy" in p.read_text(encoding="utf-8") and p.name != "failure_detection_restore.py"
    ]
    assert users == ["api/routes/settings.py"]
    from backend.app.api.routes import settings as settings_routes

    assert "turn_off_in_prepared_copy" in inspect.getsource(settings_routes._restore_backup)


# ---- the first start of the restored database ----


@pytest.fixture
async def restored(db_session, monkeypatch):
    """A restored database with its marker, admins subscribed to nothing or to defaults, an operator."""
    import uuid

    from backend.app.services import notification_service as ns

    monkeypatch.setattr(ns, "render_internal_event", _render)
    for role, events, active in (
        ("admin", [], True),
        ("admin", None, True),
        ("admin", [], False),
        ("user", None, True),
    ):
        db_session.add(
            User(username=str(uuid.uuid4()), password_hash="x", role=role, inbox_events=events, is_active=active)
        )
    db_session.add(Settings(key=restore.MARKER, value=json.dumps({"operation": "op-1"})))
    await db_session.commit()
    users = (await db_session.execute(select(User))).scalars().all()
    return SimpleNamespace(admins=[u.id for u in users if u.is_active and u.is_admin])


async def _render(db, event_type, variables):
    assert event_type == restore.EVENT
    return "OctoEverywhere turned off by a restore", "It is off."


def _factory(db_session):
    class _Session:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    return _Session


async def _notices(db_session):
    return (
        (await db_session.execute(select(UserNotification.user_id).where(UserNotification.event_type == restore.EVENT)))
        .scalars()
        .all()
    )


async def test_the_first_start_tells_every_admin_whatever_their_subscriptions(db_session, restored):
    assert restored.admins
    assert await restore.deliver_restore_notice(_factory(db_session), wait=1)
    assert sorted(await _notices(db_session)) == sorted(restored.admins)
    assert await db_session.scalar(select(Settings.value).where(Settings.key == restore.MARKER)) is None


async def test_a_second_start_creates_nothing(db_session, restored):
    assert await restore.deliver_restore_notice(_factory(db_session), wait=1)
    assert not await restore.deliver_restore_notice(_factory(db_session), wait=1)
    assert len(await _notices(db_session)) == len(restored.admins)


async def test_a_failure_before_the_commit_keeps_the_marker_for_the_next_start(db_session, restored, monkeypatch):
    from backend.app.services import notification_inbox

    real = notification_inbox.stage_admin_event

    async def fail(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("inbox unavailable")

    monkeypatch.setattr(notification_inbox, "stage_admin_event", fail)
    with pytest.raises(RuntimeError):
        await restore.deliver_restore_notice(_factory(db_session), wait=1)
    await db_session.rollback()
    assert await db_session.scalar(select(Settings.value).where(Settings.key == restore.MARKER)) is not None
    assert await _notices(db_session) == []
    monkeypatch.setattr(notification_inbox, "stage_admin_event", real)
    assert await restore.deliver_restore_notice(_factory(db_session), wait=1)


async def test_another_restore_writes_a_new_marker_and_tells_again(db_session, restored):
    assert await restore.deliver_restore_notice(_factory(db_session), wait=1)
    db_session.add(Settings(key=restore.MARKER, value=json.dumps({"operation": "op-2"})))
    await db_session.commit()
    assert await restore.deliver_restore_notice(_factory(db_session), wait=1)
    assert len(await _notices(db_session)) == 2 * len(restored.admins)


async def test_no_marker_no_notice(db_session):
    await db_session.execute(delete(Settings).where(Settings.key == restore.MARKER))
    await db_session.commit()
    assert not await restore.deliver_restore_notice(_factory(db_session), wait=0)


async def test_the_notice_has_a_shipped_template_in_both_languages():
    from pathlib import Path

    data = Path(restore.__file__).resolve().parents[1] / "data"
    for lang in ("en", "uk"):
        template = json.loads((data / f"notification_templates_{lang}.json").read_text(encoding="utf-8"))[restore.EVENT]
        assert template["title_template"] and template["body_template"]
    from backend.app.services.notification_events import EVENT_CATALOG, IN_APP_ONLY_EVENTS

    assert restore.EVENT in EVENT_CATALOG and restore.EVENT in IN_APP_ONLY_EVENTS


async def test_a_restore_waits_for_octoeverywhere_to_stop():
    from backend.app.api.routes import settings as settings_routes

    source = inspect.getsource(settings_routes._restore_backup)
    assert "octoeverywhere_detection_service.quiesce()" in source
