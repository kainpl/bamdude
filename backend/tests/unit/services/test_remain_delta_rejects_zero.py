"""A zero reading cannot bill a whole reel through the remain%-delta paths.

Two fallbacks compute consumption as ``start% - current%`` and multiply by the
spool's weight: ``usage_tracker.on_print_complete`` Path 2 for the internal
inventory, and ``spoolman_tracking._report_remain_delta_for_slots`` for Spoolman.
Both used to accept ``remain: 0`` as a reading, which makes the delta the whole
of the starting percentage — up to a full spool charged on the sentinel the
firmware emits when it has nothing to report.

⚠️ **The Spoolman one is the only weight-accounting path that reaches the
external spool at all.** `_snapshot_tray_remain` is the sole reader of
``vt_tray``; the live AMS sync, the manual sync, the ``from-slot`` endpoint and
the internal delta path all iterate ``raw_data["ams"]``, where the external slot
never appears. So the gate on this function is what closes the external slot,
and it closes both ends of the delta because the function serves both.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The billing test constructs a SpoolUsageHistory, which configures the whole
# mapper registry; Printer's relationships reach further than this test does.
from backend.app.models import printer_location  # noqa: F401
from backend.app.services.spoolman_tracking import _snapshot_tray_remain
from backend.app.services.usage_tracker import PrintSession, _active_sessions, on_print_complete


def _raw(*, ams_remain: int, vt_remain: int) -> dict:
    return {
        "ams": [{"id": 0, "tray": [{"id": 0, "remain": ams_remain, "tray_uuid": "AAA"}]}],
        "vt_tray": [{"id": 254, "remain": vt_remain, "tray_uuid": "BBB"}],
    }


class TestTheSpoolmanSnapshot:
    def test_it_covers_the_external_spool_at_all(self) -> None:
        """The premise of everything below — if this ever stops being true, the
        external slot has no weight accounting rather than a safe one."""
        snap = _snapshot_tray_remain(_raw(ams_remain=80, vt_remain=60))
        assert snap["0-0"]["remain"] == 80
        assert snap["255-0"]["remain"] == 60, "vt_tray id 254 maps to 255-0"

    def test_a_zero_external_reading_is_not_snapshotted(self) -> None:
        snap = _snapshot_tray_remain(_raw(ams_remain=80, vt_remain=0))
        assert "255-0" not in snap
        assert "0-0" in snap, "one bad slot must not drop the others"

    def test_a_zero_ams_reading_is_not_snapshotted_either(self) -> None:
        snap = _snapshot_tray_remain(_raw(ams_remain=0, vt_remain=60))
        assert "0-0" not in snap
        assert "255-0" in snap

    def test_no_slot_survives_a_report_of_all_zeros(self) -> None:
        """The shape of the incident: one push, every slot reading zero."""
        assert _snapshot_tray_remain(_raw(ams_remain=0, vt_remain=0)) == {}

    def test_the_same_function_guards_both_ends_of_the_delta(self) -> None:
        """It is called once at print start and again at completion, so a zero
        cannot enter as a start baseline either."""
        assert _snapshot_tray_remain(_raw(ams_remain=0, vt_remain=0)) == {}
        assert _snapshot_tray_remain(_raw(ams_remain=55, vt_remain=55)) != {}


class TestTheInternalDeltaPath:
    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        _active_sessions.clear()
        yield
        _active_sessions.clear()

    @pytest.fixture(autouse=True)
    def _mock_get_setting(self):
        with patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            return_value=None,
        ):
            yield

    @staticmethod
    def _printer_manager(current_remain: int):
        state = MagicMock()
        state.raw_data = {"ams": [{"id": 0, "tray": [{"id": 0, "remain": current_remain}]}]}
        state.tray_now = 255
        pm = MagicMock()
        pm.get_status = MagicMock(return_value=state)
        return pm

    @pytest.mark.asyncio
    async def test_a_zero_at_completion_bills_nothing(self) -> None:
        """Started at 90%, completion reads 0 → the old code charged 90% of the
        label weight. There is no assignment lookup at all now, because the
        reading is rejected before a spool is ever resolved."""
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="test",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 90},
        )
        db = AsyncMock()

        results = await on_print_complete(1, {"status": "completed"}, self._printer_manager(0), db)

        assert results == []
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_real_drop_still_bills(self) -> None:
        """The gate must not swallow ordinary consumption: 90% → 80% is 100 g."""
        _active_sessions[1] = PrintSession(
            printer_id=1,
            print_name="test",
            started_at=datetime.now(timezone.utc),
            tray_remain_start={(0, 0): 90},
        )
        spool = MagicMock()
        spool.id = 1
        spool.label_weight = 1000
        spool.weight_used = 0
        spool.cost_per_kg = None
        spool.material = "PLA"
        spool.rgba = None
        assignment = MagicMock()
        assignment.spool_id = 1

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=assignment)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=spool)),
            ]
        )

        results = await on_print_complete(1, {"status": "completed"}, self._printer_manager(80), db)

        assert len(results) == 1
        assert results[0]["weight_used"] == 100.0
