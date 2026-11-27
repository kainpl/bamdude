"""Spoolman-side mid-print tray-split accounting (#1793).

Reporter (@ojimpo) shipped the OP shape:
- H2S, AMS filament backup ON, two same-material spools loaded
- Single-slot print (72.56g on slot 1)
- Origin ran dry at layer 37, AMS auto-switched to backup, print finished
- Pre-fix: whole 72.56g charged to origin (via tag path) + separate 30g to
  backup (via remain-delta) — origin exceeded initial_weight, backup double-count

The fix ports usage_tracker's split path to spoolman_tracking so both inventory
backends attribute segments identically. These tests pin the OP's shape plus the
Path 2 (remain-delta) skip guarantee so it can't double-charge tray IDs the
split path already covered.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import spoolman_charge

# C047 part 2: the accounting tail runs inside an admitted charge (services/spoolman_charge).
pytestmark = pytest.mark.usefixtures("spoolman_charge_admitted")


class _AsyncCtx:
    """async_session() shim — same shape as test_spoolman_no3mf_remain_fallback."""

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


def _completion_from(printer_manager):
    """The completion event's copy of what the printer reported at its terminal frame —
    the writers take the layers from the event, never from the printer at accounting time."""
    from backend.app.utils.tray_split import CompletionSnapshot

    state = printer_manager.get_status.return_value
    return CompletionSnapshot(
        layer_num=getattr(state, "layer_num", None) or None,
        total_layers=getattr(state, "total_layers", None) or None,
        progress=getattr(state, "progress", None) or None,
    )


class TestReportUsageTraySplit:
    """report_usage must consult state.tray_change_log and split per-segment."""

    @pytest.mark.asyncio
    async def test_op_sample_a_seamless_switch_splits_origin_to_backup(self):
        """Sample A from the reporter, verbatim: 72.56g single-slot print, AMS
        runout switch tray 0 → tray 1 at layer 37 of ~100 total.

        No gcode layer_usage is provided → linear-by-layer-ratio fallback:
        - seg 0 (tray 0, layers 0-37) = 72.56 * 37/100 = 26.85g → spool 8
        - seg 1 (tray 1, layers 37-end) = 72.56 - 26.85 = 45.71g → spool 7
        Path 2 (remain-delta) must NOT run against either tray — the split path
        already covered them.
        """
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            filament_usage=[{"slot_id": 1, "used_g": 72.56}],
            ams_trays={
                0: {"tray_uuid": "AAAA", "tag_uid": "T1TAG", "tray_type": "PLA"},
                1: {"tray_uuid": "BBBB", "tag_uid": "T2TAG", "tray_type": "PLA"},
            },
            slot_to_tray=[0],
            tray_remain_start={
                "0-0": {"remain": 3, "tray_uuid": "AAAA"},  # origin near-empty at start-of-completion snapshot
                "0-1": {"remain": 73, "tray_uuid": "BBBB"},
            },
            layer_usage={},
            filament_properties={},
        )

        db = _make_db(tracking)

        client = AsyncMock()

        async def _find_spool_by_tag(tag):
            return (
                {"id": 8, "filament": {"color_hex": "000000"}}
                if tag == "AAAA"
                else {"id": 7, "filament": {"color_hex": "000000"}}
                if tag == "BBBB"
                else None
            )

        client.find_spool_by_tag = AsyncMock(side_effect=_find_spool_by_tag)
        client.use_spool = AsyncMock()

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            tray_change_log=[(0, 0), (1, 37)],
            total_layers=100,
            layer_num=100,
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_uuid": "AAAA", "remain": 0},
                            {"id": 1, "tray_uuid": "BBBB", "remain": 70},
                        ],
                    }
                ]
            },
        )

        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
            patch(
                "backend.app.services.spoolman_tracking._apply_spool_colors_to_archive",
                AsyncMock(),
            ),
            patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        ):
            await report_usage(printer_id=1, archive_id=143, completion=_completion_from(printer_manager))

        # Exactly two use_spool calls — one per segment. Origin (spool 8) gets
        # the layers-0-37 slice, backup (spool 7) gets the remainder.
        calls = client.use_spool.await_args_list
        assert len(calls) == 2, f"expected 2 use_spool calls, got {len(calls)}: {calls}"

        by_spool = {c.args[0]: c.args[1] for c in calls}
        assert set(by_spool.keys()) == {8, 7}

        # Sum must equal the OP's total — no phantom grams created or lost.
        assert round(sum(by_spool.values()), 2) == 72.56

        # Origin (spool 8) should carry roughly the layers-0-37 fraction.
        # Linear: 72.56 * 37/100 = 26.85g. Allow small rounding wiggle.
        assert 26.0 < by_spool[8] < 28.0
        # Backup (spool 7) carries the remainder.
        assert 44.0 < by_spool[7] < 46.6

    @pytest.mark.asyncio
    async def test_path_2_remain_delta_skips_tray_handled_by_split(self):
        """After the split path attributes segments to tray 0 AND tray 1, the
        Path 2 remain-delta iterator must skip BOTH — otherwise backup would
        get charged twice (~30g double-count in the OP's Sample A).
        """
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            filament_usage=[{"slot_id": 1, "used_g": 100.0}],
            ams_trays={
                0: {"tray_uuid": "AAAA", "tag_uid": "T1TAG", "tray_type": "PLA"},
                1: {"tray_uuid": "BBBB", "tag_uid": "T2TAG", "tray_type": "PLA"},
            },
            slot_to_tray=[0],
            tray_remain_start={
                "0-0": {"remain": 20, "tray_uuid": "AAAA"},
                "0-1": {"remain": 80, "tray_uuid": "BBBB"},
            },
            layer_usage={},
            filament_properties={},
        )

        db = _make_db(tracking)
        client = AsyncMock()

        async def _find_spool_by_tag(tag):
            return {"id": 8, "filament": {}} if tag == "AAAA" else {"id": 7, "filament": {}}

        client.find_spool_by_tag = AsyncMock(side_effect=_find_spool_by_tag)
        client.use_spool = AsyncMock()
        # If Path 2 ever runs, it needs a filament.weight to compute grams.
        # Making it valid means a failure to guard = extra use_spool calls, not
        # a silent skip. Combined with a truthy slot-assignment result below,
        # this is what actually proves the double-count guard works.
        client.get_spool = AsyncMock(return_value={"filament": {"weight": 1000.0}})

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            tray_change_log=[(0, 0), (1, 50)],
            total_layers=100,
            layer_num=100,
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_uuid": "AAAA", "remain": 0},
                            {"id": 1, "tray_uuid": "BBBB", "remain": 60},
                        ],
                    }
                ]
            },
        )

        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
            patch(
                # Path 2 uses this to resolve trays. Return valid IDs so the
                # ONLY thing stopping Path 2 from double-charging is
                # ``handled_global_tray_ids``. If the guard is broken, Path 2
                # would successfully call ``use_spool`` two more times and this
                # test would fail with 4 calls, not 2.
                "backend.app.services.spoolman_tracking._resolve_bound_spool",
                AsyncMock(side_effect=lambda pid, ams, tray: (999 if (ams, tray) == (0, 0) else 888, None)),
            ),
            patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        ):
            await report_usage(printer_id=1, archive_id=200, completion=_completion_from(printer_manager))

        # EXACTLY 2 — one per segment; Path 2 must not add a third.
        assert client.use_spool.await_count == 2, (
            f"Path 2 leaked past the split — expected 2 use_spool calls, got "
            f"{client.use_spool.await_count}: {client.use_spool.await_args_list}"
        )

    @pytest.mark.asyncio
    async def test_multi_slot_print_does_not_activate_split_even_with_tray_changes(self):
        """Multi-colour prints normally cycle trays every colour change, so
        ``tray_change_log`` has many entries — but splitting each slot's grams
        across all of them would attribute slot 1's usage to segments where
        slot 2's tray was loaded (and vice versa).

        Mirrors ``usage_tracker``'s gate: split only when there's exactly one
        nonzero slot. Multi-slot prints fall through to the existing single-tray
        path with its stable ``slot_to_tray`` mapping.
        """
        from backend.app.services.spoolman_tracking import report_usage

        # Two nonzero slots — regular multi-colour print
        tracking = SimpleNamespace(
            filament_usage=[
                {"slot_id": 1, "used_g": 30.0},
                {"slot_id": 2, "used_g": 20.0},
            ],
            ams_trays={
                0: {"tray_uuid": "AAAA", "tag_uid": "T1TAG", "tray_type": "PLA"},
                1: {"tray_uuid": "BBBB", "tag_uid": "T2TAG", "tray_type": "PLA"},
            },
            slot_to_tray=[0, 1],
            tray_remain_start={
                "0-0": {"remain": 90, "tray_uuid": "AAAA"},
                "0-1": {"remain": 80, "tray_uuid": "BBBB"},
            },
            layer_usage={},
            filament_properties={},
        )

        db = _make_db(tracking)
        client = AsyncMock()

        async def _find_spool_by_tag(tag):
            return {"id": 100, "filament": {}} if tag == "AAAA" else {"id": 200, "filament": {}}

        client.find_spool_by_tag = AsyncMock(side_effect=_find_spool_by_tag)
        client.use_spool = AsyncMock()

        printer_manager = MagicMock()
        # Multi-colour print naturally cycles between trays many times. If we
        # don't gate on single-slot, my split would attribute slot 1's grams to
        # every segment — including segments where tray 1 was loaded.
        printer_manager.get_status.return_value = SimpleNamespace(
            tray_change_log=[(0, 0), (1, 10), (0, 20), (1, 30), (0, 40)],
            total_layers=50,
            layer_num=50,
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_uuid": "AAAA", "remain": 87},
                            {"id": 1, "tray_uuid": "BBBB", "remain": 78},
                        ],
                    }
                ]
            },
        )

        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
            patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        ):
            await report_usage(printer_id=1, archive_id=42, completion=_completion_from(printer_manager))

        # Split path must NOT engage. The single-tray path charges each slot to
        # its stable slot_to_tray mapping: slot 1 → tray 0 → spool 100 (30g),
        # slot 2 → tray 1 → spool 200 (20g). Two calls, exact weights from the
        # 3MF (not split).
        assert client.use_spool.await_count == 2
        by_spool = {c.args[0]: c.args[1] for c in client.use_spool.await_args_list}
        assert by_spool == {100: 30.0, 200: 20.0}

    @pytest.mark.asyncio
    async def test_single_tray_change_entry_uses_normal_path(self):
        """Only ONE entry in tray_change_log (start-of-print seed, no switch)
        must fall through to the existing single-tray charging path — not
        accidentally split when there's nothing to split.
        """
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            filament_usage=[{"slot_id": 1, "used_g": 50.0}],
            ams_trays={0: {"tray_uuid": "AAAA", "tag_uid": "T1TAG", "tray_type": "PLA"}},
            slot_to_tray=[0],
            tray_remain_start={"0-0": {"remain": 80, "tray_uuid": "AAAA"}},
            layer_usage={},
            filament_properties={},
        )

        db = _make_db(tracking)
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(return_value={"id": 8, "filament": {}})
        client.use_spool = AsyncMock()

        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            tray_change_log=[(0, 0)],  # just the start-of-print seed
            total_layers=100,
            layer_num=100,
            raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "tray_uuid": "AAAA", "remain": 75}]}]},
        )

        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
            patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        ):
            await report_usage(printer_id=1, archive_id=42, completion=_completion_from(printer_manager))

        # Single-tray path: exactly one use_spool call, all 50g to spool 8.
        client.use_spool.assert_awaited_once_with(8, 50.0)


def _journal_rows(rows):
    """rows: (event, kind, tray, layer, spoolman_id) → journal-shaped events."""
    return [
        SimpleNamespace(
            id=idx,
            event=event,
            kind=kind,
            global_tray_id=tray,
            layer_num=layer,
            spool_id=None,
            spoolman_spool_id=spoolman_id,
            slot_occupied=None,
            archive_id=143,
            printer_id=1,
        )
        for idx, (event, kind, tray, layer, spoolman_id) in enumerate(rows, start=1)
    ]


class TestFeedIntervalsOnSpoolman:
    """Spoolman charges each feed interval to the reel the journal froze for it
    (vault usage-segment-attribution-spec) — not to whatever tag the tray holds
    at completion, which is the reel that arrived LAST."""

    @pytest.mark.asyncio
    async def test_a_return_to_a_refilled_tray_charges_the_frozen_reels(self):
        from backend.app.models.print_usage_event import (
            EVENT_RUNOUT,
            EVENT_SPOOL_LOADED,
            EVENT_START,
            EVENT_TRAY_CHANGE,
            KIND_AUTOSWITCH,
        )
        from backend.app.services.spoolman_tracking import _report_spool_usage_split_by_tray_changes
        from backend.app.utils.tray_split import PrintedBound

        events = _journal_rows(
            [
                (EVENT_START, None, 0, 0, 11),
                (EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 50, 11),
                (EVENT_TRAY_CHANGE, None, 1, 50, 12),
                (EVENT_SPOOL_LOADED, None, 0, 90, 13),
                (EVENT_TRAY_CHANGE, None, 0, 150, 13),
            ]
        )
        client = AsyncMock()
        # At completion tray 0 holds the NEW reel (13); tray 1 the backup (12).
        client.find_spool_by_tag = AsyncMock(
            side_effect=lambda tag: {"id": 13 if tag == "AAAA" else 12, "filament": {}}
        )
        client.use_spool = AsyncMock()
        client.get_spool = AsyncMock(return_value={"filament": {}})

        await _report_spool_usage_split_by_tray_changes(
            client,
            [{"slot_id": 1, "used_g": 300.0}],
            [(0, 0), (1, 50), (0, 150)],
            {0: {"tray_uuid": "AAAA", "tray_type": "PLA"}, 1: {"tray_uuid": "BBBB", "tray_type": "PLA"}},
            None,
            PrintedBound(completed=True, layer=200, source="total_layers", total_layers=200),
            "Archive 143",
            "SERIAL",
            printer_id=1,
            journal_events=events,
        )

        charged = [(c.args[0], c.args[1]) for c in client.use_spool.await_args_list]
        assert charged == [(11, 75.0), (12, 150.0), (13, 75.0)]

    @pytest.mark.asyncio
    async def test_bmcu_blips_take_the_journal_split(self):
        """Archive 4015 on Spoolman: the blips normalize away and the same-slot
        refill splits at the runout layer onto the frozen ids."""
        from backend.app.models.print_usage_event import (
            EVENT_RUNOUT,
            EVENT_SPOOL_LOADED,
            EVENT_TRAY_CHANGE,
            KIND_MANUAL,
        )
        from backend.app.services.spoolman_tracking import report_usage

        events = _journal_rows(
            [
                (EVENT_TRAY_CHANGE, None, 1, 0, 21),
                (EVENT_RUNOUT, KIND_MANUAL, 1, 9, 21),
                (EVENT_TRAY_CHANGE, None, 1, 9, 21),
                (EVENT_SPOOL_LOADED, None, 1, 9, 22),
            ]
        )
        tracking = SimpleNamespace(
            filament_usage=[{"slot_id": 1, "used_g": 273.43}],
            ams_trays={1: {"tray_uuid": "NEWR", "tray_type": "PETG"}},
            slot_to_tray=[1],
            tray_remain_start={},
            layer_usage={},
            filament_properties={},
        )
        db = _make_db(tracking)
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(return_value={"id": 22, "filament": {}})
        client.use_spool = AsyncMock()
        client.get_spool = AsyncMock(return_value={"filament": {}})
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            tray_change_log=[(1, 0), (254, 0), (1, 0), (254, 0), (1, 0), (254, 0), (1, 0), (254, 9), (1, 9)],
            total_layers=44,
            layer_num=44,
            raw_data={"ams": []},
        )
        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
            patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
            patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
            patch(
                "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
                AsyncMock(return_value=0),
            ),
            patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        ):
            await report_usage(printer_id=1, archive_id=143, completion=_completion_from(printer_manager))

        charged = {c.args[0]: c.args[1] for c in client.use_spool.await_args_list}
        assert set(charged) == {21, 22}
        assert charged[21] == pytest.approx(273.43 * 9 / 44, abs=0.05)
        assert charged[22] == pytest.approx(273.43 * 35 / 44, abs=0.05)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mapping", [None, [0]])
    async def test_a_layer_zero_switch_charges_the_tray_that_fed(self, mapping):
        """[(A, 0), (B, 0)] normalizes to B alone: B fed the whole print, whatever
        the dispatched mapping says — as the tray split charged it before, and as
        the internal tracker does."""
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            filament_usage=[{"slot_id": 1, "used_g": 100.0}],
            ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}, 1: {"tray_uuid": "BBBB", "tray_type": "PLA"}},
            slot_to_tray=mapping,
            tray_remain_start={},
            layer_usage={},
            filament_properties={},
        )
        db = _make_db(tracking)
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(
            side_effect=lambda tag: {"id": 31 if tag == "AAAA" else 32, "filament": {}}
        )
        client.use_spool = AsyncMock()
        client.get_spool = AsyncMock(return_value={"filament": {}})
        printer_manager = MagicMock()
        printer_manager.get_status.return_value = SimpleNamespace(
            tray_change_log=[(0, 0), (1, 0)],
            total_layers=50,
            layer_num=50,
            tray_now=255,
            last_loaded_tray=-1,
            raw_data={"ams": []},
        )
        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
            patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
            patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
            patch(
                "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
                AsyncMock(return_value=0),
            ),
            patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        ):
            await report_usage(printer_id=1, archive_id=143, completion=_completion_from(printer_manager))

        charged = [(c.args[0], c.args[1]) for c in client.use_spool.await_args_list]
        assert charged == [(32, pytest.approx(100.0, abs=0.05))]


@pytest.mark.asyncio
async def test_an_interval_the_journal_left_without_a_reel_is_reported_not_recorded():
    """A reel loaded with no Spoolman id: its interval stays uncharged — and the
    operator hears so (audit D4), as the internal tracker reports it, instead of
    the grams vanishing into a log line."""
    from backend.app.models.print_usage_event import (
        EVENT_RUNOUT,
        EVENT_SPOOL_LOADED,
        EVENT_START,
        EVENT_TRAY_CHANGE,
        KIND_AUTOSWITCH,
    )
    from backend.app.services.spoolman_tracking import report_usage

    events = _journal_rows(
        [
            (EVENT_START, None, 0, 0, 11),
            (EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 50, 11),
            (EVENT_TRAY_CHANGE, None, 1, 50, 12),
            (EVENT_SPOOL_LOADED, None, 0, 90, None),
            (EVENT_TRAY_CHANGE, None, 0, 150, None),
        ]
    )
    tracking = SimpleNamespace(
        filament_usage=[{"slot_id": 1, "used_g": 300.0}],
        ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}, 1: {"tray_uuid": "BBBB", "tray_type": "PLA"}},
        slot_to_tray=[0],
        tray_remain_start={},
        layer_usage={},
        filament_properties={},
    )
    db = _make_db(tracking)
    client = AsyncMock()
    # Tray 1 has no reel boundary of its own: its tag names it, as before.
    client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: {"id": 12 if tag == "BBBB" else 99, "filament": {}})
    client.use_spool = AsyncMock()
    client.get_spool = AsyncMock(return_value={"filament": {}})
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        tray_change_log=[(0, 0), (1, 50), (0, 150)],
        total_layers=200,
        layer_num=200,
        raw_data={"ams": []},
    )
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch(
            "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
            AsyncMock(return_value=0),
        ),
        patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await report_usage(printer_id=1, archive_id=143, completion=_completion_from(printer_manager))

    charged = [(c.args[0], c.args[1]) for c in client.use_spool.await_args_list]
    assert charged == [(11, 75.0), (12, 150.0)]
    notify.assert_awaited_once()
    assert notify.await_args.args[1] == [(0, 75.0)]


@pytest.mark.asyncio
async def test_a_journal_segment_nobody_fed_is_reported_not_recorded_on_spoolman():
    """The single-tray journal split on Spoolman: the stretch after an
    autoswitch the journal saw no backup for is reported, not only logged."""
    from backend.app.models.print_usage_event import EVENT_RUNOUT, EVENT_START, KIND_AUTOSWITCH
    from backend.app.services.spoolman_tracking import report_usage

    events = _journal_rows([(EVENT_START, None, 0, 0, 11), (EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 50, 11)])
    tracking = SimpleNamespace(
        filament_usage=[{"slot_id": 1, "used_g": 100.0}],
        ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}},
        slot_to_tray=[0],
        tray_remain_start={},
        layer_usage={},
        filament_properties={},
    )
    db = _make_db(tracking)
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(return_value={"id": 11, "filament": {}})
    client.use_spool = AsyncMock()
    client.get_spool = AsyncMock(return_value={"filament": {}})
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        tray_change_log=[(0, 0)],
        total_layers=100,
        layer_num=100,
        tray_now=0,
        last_loaded_tray=0,
        raw_data={"ams": []},
    )
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch(
            "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
            AsyncMock(return_value=0),
        ),
        patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await report_usage(printer_id=1, archive_id=143, completion=_completion_from(printer_manager))

    charged = [(c.args[0], c.args[1]) for c in client.use_spool.await_args_list]
    assert charged == [(11, 50.0)]
    notify.assert_awaited_once()
    assert notify.await_args.args[1] == [(0, 50.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("log", [[(-1, 0)], [(0, 0), (-1, 0)]], ids=["only_unknown", "known_then_unknown_on_one_layer"])
async def test_an_explicit_unknown_feed_is_not_recorded_on_spoolman_and_no_fallback_charges_it(log):
    """Review r2 F2-R1: a chronology that says the feed is unknown — even as its only entry
    after normalization — charges no Spoolman reel: not by the dispatched mapping, not by the
    tag, not by the remain-delta fallback (the tray's remain dropped during the print)."""
    from backend.app.services.spoolman_tracking import report_usage

    tracking = SimpleNamespace(
        filament_usage=[{"slot_id": 1, "used_g": 300.0}],
        ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}},
        slot_to_tray=[0],
        tray_remain_start={"0-0": {"remain": 80, "tray_uuid": "AAAA"}},
        layer_usage={},
        filament_properties={},
    )
    db = _make_db(tracking)
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(return_value={"id": 11, "filament": {"color_hex": "000000"}})
    client.use_spool = AsyncMock()
    client.get_spool = AsyncMock(return_value={"id": 11, "filament": {"weight": 1000}})
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        tray_change_log=log,
        total_layers=200,
        layer_num=200,
        raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "tray_uuid": "AAAA", "remain": 40}]}]},
    )
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(db)),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch(
            "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
            AsyncMock(return_value=0),
        ),
        patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        # The slot is bound and its reel has a reference weight: the 40 % remain drop would
        # be 400 g on reel 11 if the remain-delta fallback were let near the unknown feed.
        patch(
            "backend.app.services.spoolman_tracking._resolve_bound_spool",
            AsyncMock(return_value=(11, None)),
        ),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await report_usage(printer_id=1, archive_id=144, completion=_completion_from(printer_manager))

    client.use_spool.assert_not_awaited()
    notify.assert_awaited_once()
    assert notify.await_args.args[1] == [(-1, 300.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "log, charged, not_recorded",
    [
        ([(-1, 0)], {}, [(-1, 150.0)]),  # only an unknown feed
        ([(0, 0), (-1, 0)], {}, [(-1, 150.0)]),  # known, then unknown on the same layer
        # An AMS backup took over at 50 of a print stopped at 100 of 200: the partial 150 g are
        # what layers 0–100 used, so each half of THAT printed stretch is 75 g — not 37.5/112.5,
        # which divided the early stretch by the whole plate (partial-print-layer-split A23).
        ([(0, 0), (1, 50)], {11: 75.0, 12: 75.0}, []),
        ([], {11: 150.0}, []),  # no chronology at all: the slot's mapping, as before
    ],
)
async def test_a_cancelled_print_on_spoolman_is_split_by_the_feed_as_the_internal_writer_does(
    log, charged, not_recorded
):
    """Review r2 F2-R1 on the abort writer: a cancelled one-slot print's partial grams follow
    the tray chronology as the completion writer and the internal inventory do — a stretch
    nobody saw fed is reported as not recorded, never charged to the mapped reel; a backup's
    takeover divides the printed stretch at its own layer."""
    from backend.app.services.spoolman_tracking import _report_partial_usage

    tracking = SimpleNamespace(
        archive_id=144,
        filament_usage=[{"slot_id": 1, "used_g": 300.0}],
        layer_usage=None,
        filament_properties={},
        ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}, 1: {"tray_uuid": "BBBB", "tray_type": "PLA"}},
        slot_to_tray=[0],
        tray_remain_start={},
    )
    reels = {"AAAA": {"id": 11, "filament": {"color_hex": "000000"}}, "BBBB": {"id": 12, "filament": {}}}
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: reels.get(tag))
    client.use_spool = AsyncMock()
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        tray_change_log=log, layer_num=100, total_layers=200, raw_data={}
    )
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(AsyncMock())),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await _report_partial_usage(
            charge=await spoolman_charge.admit("test"),
            printer_id=1,
            tracking=tracking,
            completion=_completion_from(printer_manager),
        )

    assert {call.args[0]: call.args[1] for call in client.use_spool.await_args_list} == charged
    if not_recorded:
        notify.assert_awaited_once()
        assert notify.await_args.args[1] == not_recorded
    else:
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_multi_colour_print_on_spoolman_keeps_its_slot_mapping():
    """The abort writer asks the chronology only of a one-slot print, as the completion writer
    and the internal inventory do: a multi-colour print changes trays at every colour, so its
    slots stay on the dispatched mapping whatever the chronology says."""
    from backend.app.services.spoolman_tracking import _report_partial_usage

    tracking = SimpleNamespace(
        archive_id=144,
        filament_usage=[{"slot_id": 1, "used_g": 300.0}, {"slot_id": 2, "used_g": 100.0}],
        layer_usage=None,
        filament_properties={},
        ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}, 1: {"tray_uuid": "BBBB", "tray_type": "PLA"}},
        slot_to_tray=[0, 1],
        tray_remain_start={},
    )
    reels = {"AAAA": {"id": 11, "filament": {}}, "BBBB": {"id": 12, "filament": {}}}
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: reels.get(tag))
    client.use_spool = AsyncMock()
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        tray_change_log=[(-1, 0)], layer_num=100, total_layers=200, raw_data={}
    )
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(AsyncMock())),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await _report_partial_usage(
            charge=await spoolman_charge.admit("test"),
            printer_id=1,
            tracking=tracking,
            completion=_completion_from(printer_manager),
        )

    assert {call.args[0]: call.args[1] for call in client.use_spool.await_args_list} == {11: 150.0, 12: 50.0}
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_print_honours_an_unknown_feed_only_the_journal_holds():
    """Review r5 F5-R1 on the abort writer: the client's memory never heard of the unknown
    feed the journal stated at 50; the cancelled print's partial grams still go to the start's
    reel up to 50 and the rest is reported as not recorded — not to the slot's reel now."""
    from backend.app.services.spoolman_tracking import _report_partial_usage

    events = [
        SimpleNamespace(id=1, event="start", global_tray_id=0, layer_num=0, spool_id=None, spoolman_spool_id=11,
                        kind=None, slot_occupied=True),
        SimpleNamespace(id=2, event="tray_change", global_tray_id=-1, layer_num=50, spool_id=None,
                        spoolman_spool_id=None, kind=None, slot_occupied=None),
    ]  # fmt: skip
    tracking = SimpleNamespace(
        archive_id=144,
        filament_usage=[{"slot_id": 1, "used_g": 300.0}],
        layer_usage=None,
        filament_properties={},
        ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}},
        slot_to_tray=[0],
        tray_remain_start={},
    )
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(return_value={"id": 12, "filament": {}})
    client.use_spool = AsyncMock()
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        tray_change_log=[(0, 0)], layer_num=100, total_layers=100, raw_data={}
    )
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(AsyncMock())),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await _report_partial_usage(
            charge=await spoolman_charge.admit("test"),
            printer_id=1,
            tracking=tracking,
            completion=_completion_from(printer_manager),
        )

    assert {call.args[0]: call.args[1] for call in client.use_spool.await_args_list} == {11: 150.0}
    assert notify.await_args.args[1] == [(-1, 150.0)]


@pytest.mark.asyncio
async def test_a_cancelled_print_with_no_client_at_all_reads_the_journal():
    """Review r6 F6-R1 on the abort writer: no printer state at all at the cancel. The journal
    is the chronology — the start's reel up to the marker at 50, the rest not recorded — and
    the layer the print stopped at (the completion's last layer) is what the grams divide by."""
    from backend.app.services.spoolman_tracking import _report_partial_usage

    events = [
        SimpleNamespace(id=1, event="start", global_tray_id=0, layer_num=0, spool_id=None, spoolman_spool_id=11,
                        kind=None, slot_occupied=True),
        SimpleNamespace(id=2, event="tray_change", global_tray_id=-1, layer_num=50, spool_id=None,
                        spoolman_spool_id=None, kind=None, slot_occupied=None),
    ]  # fmt: skip
    tracking = SimpleNamespace(
        archive_id=144,
        filament_usage=[{"slot_id": 1, "used_g": 300.0}],
        layer_usage=None,
        filament_properties={},
        ams_trays={0: {"tray_uuid": "AAAA", "tray_type": "PLA"}},
        slot_to_tray=[0],
        tray_remain_start={},
    )
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(return_value={"id": 12, "filament": {}})
    client.use_spool = AsyncMock()
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = None
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(AsyncMock())),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=client),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", printer_manager),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
    ):
        await _report_partial_usage(
            charge=await spoolman_charge.admit("test"),
            printer_id=1,
            tracking=tracking,
            last_layer_num=100,
            last_progress=50,
        )

    assert {call.args[0]: call.args[1] for call in client.use_spool.await_args_list} == {11: 75.0}
    assert notify.await_args.args[1] == [(-1, 75.0)]
