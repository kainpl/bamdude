"""Info must explain an unproven run even when its preserved archive lost its claim."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_state_recovery_store as store, queue_sources
from backend.app.services.print_state_recovery import RUNTIME_OWNERS
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_print_state_recovery import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio]


def quiet():
    return {
        "owners": dict.fromkeys(RUNTIME_OWNERS, False),
        "resolution": False,
        "pending_terminal": False,
        "finishing": 0,
        "layer_timelapse": False,
        "admission": None,
        "current": None,
    }


async def _scenario(db, printer_factory, archive_factory, telemetry, monkeypatch):  # noqa: F811
    p = await printer_factory()
    q = PrinterQueue(printer_id=p.id, status="idle", is_paused=True)
    db.add(q)
    await db.flush()
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    a = await archive_factory(p.id, queue_id=q.id, status="printing", started_at=started, completed_at=None)
    await db.commit()
    telemetry.state = "RUNNING"
    telemetry.subtask_id = "0"
    life = getattr(printer_manager, "_printer_lives", {}).get(p.id, 0)
    unproven = dict(getattr(printer_manager, "_unproven_starts", {}) or {})
    unproven[p.id] = {"reason": "file change without run proof", "epoch": queue_sources.current_epoch(), "life": life}
    monkeypatch.setattr(printer_manager, "_unproven_starts", unproven, raising=False)
    t = [10.0]
    monkeypatch.setattr(store, "clock", lambda: t[0])
    monkeypatch.setattr(store, "runtime_snapshot", lambda _pid: quiet())
    store._observations.clear()
    await store.list_issues(db, None)
    t[0] += 11
    page = await store.list_issues(db, None)
    return p, a, [i for i in page["issues"] if i.get("printer_id") in (p.id, None)]


async def test_unresolved_file_change_is_visible_on_info(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    _p, a, issues = await _scenario(db_session, printer_factory, archive_factory, telemetry, monkeypatch)
    codes = sorted({i["code"] for i in issues})
    assert "run_identity_unproven" in codes, f"Info issues: {codes}"
    assert any(i["kind"] == "archive" and i["target_id"] == a.id for i in issues)
    for issue in issues:
        if issue["code"] == "run_identity_unproven":
            assert issue["actions"] == []


async def test_unresolved_file_change_with_a_claimed_archive_is_visible(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    """The usual shape: the old archive A still owns its printing claim (every print holds a row)."""
    from backend.app.models.print_queue import PrintQueueItem

    orig_factory = archive_factory

    async def factory_with_claim(printer_id, **kw):
        a = await orig_factory(printer_id, **kw)
        db_session.add(
            PrintQueueItem(
                queue_id=kw["queue_id"],
                status="printing",
                origin="external",
                archive_id=a.id,
                started_at=kw["started_at"],
                position=0,
            )
        )
        await db_session.flush()
        return a

    _p, a, issues = await _scenario(db_session, printer_factory, factory_with_claim, telemetry, monkeypatch)
    codes = sorted({i["code"] for i in issues})
    assert "run_identity_unproven" in codes, f"Info issues: {codes}"
    for issue in issues:
        if issue["code"] == "run_identity_unproven":
            assert issue["actions"] == []
