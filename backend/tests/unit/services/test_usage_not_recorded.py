"""A finished print that could not charge a tray it drew from says so (audit D4 «ч.3», upstream 08a58b1e, #2812).

Everything up to the debit succeeds — the 3MF is found, the grams are read, the
tray is resolved — and then the tray has no spool to charge: its assignment was
removed mid-print (an inventory-mode switch clears every assignment of the mode
being left), or it never had one. The print still reported success, the skip was
an INFO line nobody sees at the default log level, and the filament simply went
missing from the inventory — upstream's reporter found 65 g gone only because a
spool's remaining weight looked wrong.

Now the skip is a WARNING naming the grams, and one notification per print — on
the missing-spool-assignment event, so it rides the same provider toggle, Telegram
subscription and inbox entry — says which slots and how much. Its own template:
the print-start one says "print started with missing spool assignments", which is
not what happened here.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.spool_assignment_notifications import notify_usage_not_recorded

MODULE = "backend.app.services.spool_assignment_notifications"


class _Session:
    async def get(self, model, key):
        return SimpleNamespace(name="Printer A")


def _notifier_patches():
    return (
        patch(f"{MODULE}.printer_manager.get_status", return_value=None),
        patch(f"{MODULE}.notification_service.on_print_usage_not_recorded", new_callable=AsyncMock),
        patch(f"{MODULE}.ws_manager.send_missing_spool_assignment", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_one_notification_names_every_uncharged_slot_and_its_grams():
    status, notify, ws = _notifier_patches()
    with status, notify as mock_notify, ws as mock_ws:
        await notify_usage_not_recorded(1, [(1, 25.46), (254, 3.0)], _Session(), logging.getLogger(__name__))

    mock_notify.assert_awaited_once()
    kwargs = mock_notify.await_args.kwargs
    assert kwargs["printer_id"] == 1
    assert kwargs["printer_name"] == "Printer A"
    assert kwargs["missing_slots"] == [
        {"slot": "A2", "profile": "Unknown", "color": "Unknown", "grams": "25.5"},
        {"slot": "Ext-L", "profile": "Unknown", "color": "Unknown", "grams": "3.0"},
    ]
    # The print-start toast says "print started without an assignment" — not this.
    mock_ws.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_filaments_on_one_tray_are_one_line():
    status, notify, ws = _notifier_patches()
    with status, notify as mock_notify, ws:
        await notify_usage_not_recorded(1, [(0, 10.0), (0, 5.3)], _Session(), logging.getLogger(__name__))

    assert mock_notify.await_args.kwargs["missing_slots"] == [
        {"slot": "A1", "profile": "Unknown", "color": "Unknown", "grams": "15.3"}
    ]


@pytest.mark.asyncio
async def test_nothing_uncharged_sends_nothing():
    status, notify, ws = _notifier_patches()
    with status, notify as mock_notify, ws:
        await notify_usage_not_recorded(1, [], _Session(), logging.getLogger(__name__))
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_notification_never_reaches_the_completion():
    status, notify, ws = _notifier_patches()
    with status, notify as mock_notify, ws:
        mock_notify.side_effect = RuntimeError("provider down")
        await notify_usage_not_recorded(1, [(0, 1.0)], _Session(), logging.getLogger(__name__))


# -- the internal tracker collects what it could not charge -------------------


@pytest.mark.asyncio
async def test_the_3mf_path_reports_an_unassigned_tray_with_its_grams(caplog):
    from backend.app.services.usage_tracker import _track_from_3mf
    from backend.tests.unit.services.test_usage_tracker import (
        _make_printer_manager,
        _make_printer_state,
        _no_queue_item,
    )

    archive = MagicMock()
    archive.file_path = "archives/test.3mf"
    db = AsyncMock()
    # archive, queue_item(None), assignment lookup -> nothing
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=archive)),
            _no_queue_item(),
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
        ]
    )
    pm = _make_printer_manager(_make_printer_state([], tray_now=0))
    usage = [{"slot_id": 1, "used_g": 25.5, "type": "PLA", "color": "#FF0000"}]
    uncharged: list = []

    with (
        patch("backend.app.core.config.settings") as mock_settings,
        patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=usage),
        caplog.at_level(logging.WARNING, logger="backend.app.services.usage_tracker"),
    ):
        path = MagicMock()
        path.exists.return_value = True
        mock_settings.base_dir.__truediv__ = MagicMock(return_value=path)
        results = await _track_from_3mf(
            printer_id=1,
            archive_id=10,
            status="completed",
            print_name="p",
            handled_trays=set(),
            printer_manager=pm,
            db=db,
            uncharged=uncharged,
        )

    assert results == []
    assert uncharged == [(0, 25.5)]
    assert any("25.5" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_completion_notifies_once_after_its_writes():
    """The notification goes out after the tracker's own commit, once per print."""
    from backend.app.services import usage_tracker

    async def fake_track(*args, **kwargs):
        kwargs["uncharged"].extend([(0, 12.0), (1, 3.0)])
        return []

    pm = MagicMock()
    pm.get_status.return_value = None
    db = AsyncMock()

    with (
        patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        patch.object(usage_tracker, "_track_from_3mf", side_effect=fake_track),
        patch.object(usage_tracker, "restore_session", new_callable=AsyncMock),
        patch(f"{MODULE}.notify_usage_not_recorded", new_callable=AsyncMock) as notify,
    ):
        await usage_tracker.on_print_complete(1, {"status": "completed"}, pm, db, archive_id=10)

    notify.assert_awaited_once()
    args = notify.await_args.args
    assert args[0] == 1 and args[1] == [(0, 12.0), (1, 3.0)]


# -- Spoolman: an unresolved slot is not charged either -------------------------


@pytest.mark.asyncio
async def test_spoolman_reports_a_slot_it_could_not_resolve():
    from backend.app.services import spoolman_tracking

    client = MagicMock()
    client.find_spool_by_tag = AsyncMock(return_value=None)
    client.use_spool = AsyncMock()
    ams_trays = {0: {"tray_type": "PLA", "tray_uuid": "", "tag_uid": ""}}
    uncharged: list = []

    with patch.object(spoolman_tracking, "_resolve_spool_id_via_slot_assignment", new=AsyncMock(return_value=None)):
        updated = await spoolman_tracking._report_spool_usage_for_slots(
            client,
            [(1, 7.5)],
            ams_trays,
            [0],
            "Archive 1",
            printer_serial="",
            printer_id=1,
            uncharged_out=uncharged,
        )

    assert updated == 0
    client.use_spool.assert_not_awaited()
    assert uncharged == [(0, 7.5)]
