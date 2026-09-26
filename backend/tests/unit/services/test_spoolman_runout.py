"""Spoolman mirror of runout zero-point accounting.

Same journal, same boundaries, same order as usage_tracker: segment charges
via use_spool with spoolman ids frozen at event time, then corrections close
the run-out spool at exactly empty (positive remaining → use_spool the rest;
negative → clamp remaining to 0 via update_spool).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.print_usage_event import (
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    EVENT_TRAY_CHANGE,
    KIND_AMBIGUOUS,
    KIND_PAUSE,
)
from backend.app.services.spoolman import SpoolmanClientError


class _AsyncCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_):
        return False


def _make_db(tracking):
    db = AsyncMock()
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = tracking
    db.execute = AsyncMock(return_value=select_result)
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    return db


def _event(eid, event, kind, tray, layer, spoolman_id):
    return SimpleNamespace(
        id=eid,
        event=event,
        kind=kind,
        global_tray_id=tray,
        layer_num=layer,
        spool_id=None,
        spoolman_spool_id=spoolman_id,
        archive_id=143,
    )


def _tracking(**kw):
    args = {
        "filament_usage": [{"slot_id": 1, "used_g": 300.0}],
        "ams_trays": {0: {"tray_uuid": "AAAA", "tag_uid": "", "tray_type": "PLA"}},
        "slot_to_tray": [0],
        "tray_remain_start": {},
        "layer_usage": {},
        "filament_properties": {},
        "archive_id": 143,
    }
    args.update(kw)
    return SimpleNamespace(**args)


def _pm(total_layers=200):
    pm = MagicMock()
    pm.get_status.return_value = SimpleNamespace(
        tray_change_log=[],
        total_layers=total_layers,
        layer_num=total_layers,
        raw_data={"ams": []},
    )
    return pm


def _client(remaining=30.0):
    client = AsyncMock()

    async def _find_spool_by_tag(tag):
        return {"id": 8, "filament": {"color_hex": "000000"}} if tag == "AAAA" else None

    client.find_spool_by_tag = AsyncMock(side_effect=_find_spool_by_tag)
    client.use_spool = AsyncMock()
    client.update_spool = AsyncMock()
    client.get_spool = AsyncMock(return_value={"id": 8, "remaining_weight": remaining, "filament": {}})
    return client


async def _run(tracking, client, events, pm=None, get_setting=None):
    from backend.app.services.spoolman_tracking import report_usage

    db = _make_db(tracking)
    setting = get_setting or AsyncMock(return_value="true")
    broadcast = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
        patch("backend.app.api.routes.settings.get_setting", setting),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.printer_manager.printer_manager", pm or _pm()),
        patch("backend.app.core.websocket.ws_manager.broadcast", broadcast),
    ):
        await report_usage(printer_id=1, archive_id=143)
    return broadcast


class TestSpoolmanRunoutZeroPoint:
    @pytest.mark.asyncio
    async def test_same_slot_refill_splits_and_zeroes(self):
        events = [
            _event(1, EVENT_RUNOUT, KIND_PAUSE, 0, 140, 8),
            _event(2, EVENT_SPOOL_LOADED, None, 0, 140, 9),
        ]
        client = _client(remaining=30.0)
        await _run(_tracking(), client, events)

        use_calls = {tuple(c.args) for c in client.use_spool.await_args_list}
        # Linear split 140/200 of 300 g → 210 to the origin, 90 to the
        # replacement, then the correction drains the origin's remaining 30 g.
        assert (8, 210.0) in use_calls
        assert (9, 90.0) in use_calls
        assert (8, 30.0) in use_calls
        assert len(use_calls) == 3

    @pytest.mark.asyncio
    async def test_an_open_pause_episode_is_not_drained(self):
        """Same reel reinserted — no spool_loaded closed the episode, and the
        reel demonstrably kept printing. Draining it would zero a half-full
        spool (X2D archive 734, 2026-08-24)."""
        events = [_event(1, EVENT_RUNOUT, KIND_PAUSE, 0, 140, 8)]
        client = _client(remaining=400.0)
        await _run(_tracking(), client, events)

        drained = {tuple(c.args) for c in client.use_spool.await_args_list}
        assert (8, 400.0) not in drained

    @pytest.mark.asyncio
    async def test_an_autoswitch_runout_drains_without_spool_loaded(self):
        """The firmware moved to the backup — the origin could not have
        continued; autoswitch closes the episode by definition."""
        from backend.app.models.print_usage_event import EVENT_TRAY_CHANGE, KIND_AUTOSWITCH

        events = [
            _event(1, EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 100, 8),
            _event(2, EVENT_TRAY_CHANGE, None, 2, 100, 9),
        ]
        client = _client(remaining=30.0)
        await _run(_tracking(), client, events)

        drained = {tuple(c.args) for c in client.use_spool.await_args_list}
        assert (8, 30.0) in drained

    @pytest.mark.asyncio
    async def test_negative_remaining_clamps_via_update(self):
        events = [_event(1, EVENT_RUNOUT, KIND_PAUSE, 0, 140, 8)]
        client = _client(remaining=-25.0)
        await _run(_tracking(), client, events)

        client.update_spool.assert_awaited_once_with(8, remaining_weight=0)

    @pytest.mark.asyncio
    async def test_ambiguous_runout_never_corrects(self):
        events = [_event(1, EVENT_RUNOUT, KIND_AMBIGUOUS, 0, 140, 8)]
        client = _client(remaining=30.0)
        await _run(_tracking(), client, events)

        # No correction write of either shape. (get_spool alone is fine — the
        # colour/material enrichment uses it on the normal path.)
        client.update_spool.assert_not_awaited()
        assert {tuple(c.args) for c in client.use_spool.await_args_list} == {(8, 300.0)}

    @pytest.mark.asyncio
    async def test_journal_trays_are_excluded_from_remain_delta(self):
        tracking = _tracking(
            filament_usage=[],
            tray_remain_start={"0-2": {"remain": 80, "tray_uuid": "CCCC"}},
        )
        pm = _pm()
        pm.get_status.return_value = SimpleNamespace(
            tray_change_log=[],
            total_layers=200,
            layer_num=200,
            raw_data={"ams": [{"id": 0, "tray": [{"id": 2, "remain": 70, "tray_uuid": "CCCC"}]}]},
        )
        events = [_event(1, EVENT_TRAY_CHANGE, None, 2, 100, 12)]
        client = _client()
        await _run(tracking, client, events, pm=pm)

        client.use_spool.assert_not_awaited()  # the delta was NOT charged

    @pytest.mark.asyncio
    async def test_zero_point_setting_off_disables_corrections(self):
        events = [_event(1, EVENT_RUNOUT, KIND_PAUSE, 0, 140, 8)]
        client = _client(remaining=30.0)

        async def _setting(db, key):
            return "false" if key == "runout_zero_point_enabled" else "true"

        await _run(_tracking(), client, events, get_setting=AsyncMock(side_effect=_setting))

        # No correction drains the 30 g left on the spool: only the print's own
        # 300 g are charged. (Not asserted as "get_spool never awaited" any more —
        # each journal segment now fetches its spool for the price, #2591.)
        # The print's own rows are unaffected — split still applies (runout
        # boundary with no spool_loaded keeps the origin on both segments).
        assert sum(c.args[1] for c in client.use_spool.await_args_list) == 300.0


class TestAutoswitchPurgeGramsSpoolman:
    @pytest.mark.asyncio
    async def test_purge_lands_on_the_backup_segment(self):
        events = [
            _event(1, EVENT_RUNOUT, "autoswitch", 0, 100, 8),
            _event(2, EVENT_TRAY_CHANGE, None, 2, 100, 9),
        ]
        client = _client(remaining=0)

        async def _setting(db, key):
            return {"spoolman_enabled": "true", "runout_purge_grams": "25"}.get(key)

        await _run(_tracking(), client, events, get_setting=AsyncMock(side_effect=_setting))

        by_spool = {}
        for c in client.use_spool.await_args_list:
            by_spool[c.args[0]] = by_spool.get(c.args[0], 0) + c.args[1]
        # 300 g over 200 layers, runout at 100: origin 150, backup 150 + 25 purge.
        assert by_spool[8] == pytest.approx(150.0)
        assert by_spool[9] == pytest.approx(175.0)


def _setting_map(**values):
    """A key-aware get_setting: every key answers "true" like ``_run``'s default
    (``spoolman_enabled`` must, or nothing is tracked at all) except the ones
    given here; a key given as None answers None, like an absent row."""

    async def _get(db, key):
        return values.get(key, "true")

    return AsyncMock(side_effect=_get)


_ARCHIVE_ON = {"runout_archive_spool_enabled": "true"}


def _closed_episode():
    return [
        _event(1, EVENT_RUNOUT, KIND_PAUSE, 0, 140, 8),
        _event(2, EVENT_SPOOL_LOADED, None, 0, 140, 9),
    ]


class TestSpoolmanRunoutArchivesTheSpool:
    @pytest.mark.asyncio
    async def test_closed_episode_archives_the_empty_spool(self):
        client = _client(remaining=30.0)

        broadcast = await _run(_tracking(), client, _closed_episode(), get_setting=_setting_map(**_ARCHIVE_ON))

        client.set_spool_archived.assert_awaited_once_with(8, archived=True)
        # The drain is untouched: 30 g remaining still leaves via use_spool.
        assert any(c.args == (8, 30.0) for c in client.use_spool.await_args_list)
        assert {"type": "inventory_changed"} in [c.args[0] for c in broadcast.await_args_list]

    @pytest.mark.asyncio
    async def test_an_open_episode_never_archives(self):
        events = [_event(1, EVENT_RUNOUT, KIND_PAUSE, 0, 140, 8)]
        client = _client(remaining=-5.0)  # negative → the clamp fires even on an open episode

        broadcast = await _run(_tracking(), client, events, get_setting=_setting_map(**_ARCHIVE_ON))

        client.update_spool.assert_awaited_once_with(8, remaining_weight=0)
        client.set_spool_archived.assert_not_awaited()
        assert {"type": "inventory_changed"} not in [c.args[0] for c in broadcast.await_args_list]

    @pytest.mark.asyncio
    async def test_absent_setting_means_off(self):
        client = _client(remaining=30.0)

        await _run(_tracking(), client, _closed_episode(), get_setting=_setting_map(runout_archive_spool_enabled=None))

        client.set_spool_archived.assert_not_awaited()
        assert any(c.args == (8, 30.0) for c in client.use_spool.await_args_list)  # zero-point itself still on

    @pytest.mark.asyncio
    async def test_a_failed_archive_keeps_the_drain(self):
        client = _client(remaining=30.0)
        client.set_spool_archived = AsyncMock(side_effect=SpoolmanClientError("boom", 400))

        broadcast = await _run(_tracking(), client, _closed_episode(), get_setting=_setting_map(**_ARCHIVE_ON))

        client.set_spool_archived.assert_awaited_once_with(8, archived=True)
        assert any(c.args == (8, 30.0) for c in client.use_spool.await_args_list)
        assert {"type": "inventory_changed"} not in [c.args[0] for c in broadcast.await_args_list]

    @pytest.mark.asyncio
    async def test_a_spool_without_remaining_is_still_archived(self):
        """No remaining figure means no books to close — not a reason to keep an empty reel active."""
        client = _client(remaining=None)

        await _run(_tracking(), client, _closed_episode(), get_setting=_setting_map(**_ARCHIVE_ON))

        client.set_spool_archived.assert_awaited_once_with(8, archived=True)

    @pytest.mark.asyncio
    async def test_an_already_archived_spool_is_left_alone(self):
        client = _client(remaining=0.0)
        client.get_spool = AsyncMock(return_value={"id": 8, "remaining_weight": 0.0, "archived": True, "filament": {}})

        broadcast = await _run(_tracking(), client, _closed_episode(), get_setting=_setting_map(**_ARCHIVE_ON))

        client.set_spool_archived.assert_not_awaited()
        assert {"type": "inventory_changed"} not in [c.args[0] for c in broadcast.await_args_list]

    @pytest.mark.asyncio
    async def test_a_reload_of_the_same_reel_drains_but_does_not_archive(self):
        """spool_loaded froze the SAME spoolman id as the runout: the same reel re-fed.
        The drain gate is unchanged; only a different reel retires this one."""
        events = [
            _event(1, EVENT_RUNOUT, KIND_PAUSE, 0, 140, 8),
            _event(2, EVENT_SPOOL_LOADED, None, 0, 140, 8),
        ]
        client = _client(remaining=30.0)

        await _run(_tracking(), client, events, get_setting=_setting_map(**_ARCHIVE_ON))

        assert any(c.args == (8, 30.0) for c in client.use_spool.await_args_list)
        client.set_spool_archived.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_closed_episode_with_books_over_the_label_clamps_and_archives(self):
        client = _client(remaining=-12.0)

        broadcast = await _run(_tracking(), client, _closed_episode(), get_setting=_setting_map(**_ARCHIVE_ON))

        client.update_spool.assert_awaited_once_with(8, remaining_weight=0)
        client.set_spool_archived.assert_awaited_once_with(8, archived=True)
        assert [c.args[0] for c in broadcast.await_args_list].count({"type": "inventory_changed"}) == 1

    @pytest.mark.asyncio
    async def test_a_failed_archive_keeps_the_count(self):
        """The corrections count is about the books; a failed archive neither adds to nor takes from it."""
        from backend.app.services.spoolman_tracking import _apply_runout_zero_corrections_spoolman

        client = _client(remaining=30.0)
        client.set_spool_archived = AsyncMock(side_effect=SpoolmanClientError("boom", 400))
        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(_make_db(None))),
            patch("backend.app.api.routes.settings.get_setting", _setting_map(**_ARCHIVE_ON)),
            patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()),
        ):
            updated = await _apply_runout_zero_corrections_spoolman(client, _closed_episode(), 143)

        assert updated == 1
        client.use_spool.assert_awaited_once_with(8, 30.0)
