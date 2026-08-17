"""The AMS sync no longer moves the books in silence.

Every other writer of ``weight_used`` — the 3MF path, the layer path, the
remain%-delta fallback — lands a ``SpoolUsageHistory`` row beside the number.
The two AMS-sync sites did not, which is why a single bad reading could write a
1 kg spool off as spent and leave nothing to contradict it: the spool's own
history summed to 154 g and the page said 1000 g, with no row in between to say
who was right.

⚠️ An increase and a decrease are not symmetric here, and the asymmetry is the
point. An increase is filament that genuinely left the spool while this instance
was not watching — a job started from the touchscreen, a purge, a spool carried
to another printer — so it is consumption and earns a row. A decrease is a
correction of *our* books, not a negative print; every reader of that table SUMs
it, so a negative row would quietly subtract from farm-wide consumption.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Constructing a SpoolUsageHistory configures the whole mapper registry, and
# Printer's relationships reach further than this test does. conftest only wires
# that up inside the DB-engine fixture, which these mock-DB tests never touch.
from backend.app.models import printer_location  # noqa: F401
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.usage_tracker import AMS_SYNC_STATUS, record_ams_sync_usage


def _make_spool(*, id=1, label_weight=1000, weight_used=0.0, baseline=0.0, notified=False):
    spool = MagicMock()
    spool.id = id
    spool.label_weight = label_weight
    spool.weight_used = weight_used
    spool.weight_used_baseline = baseline
    spool.low_stock_notified = notified
    spool.archived_at = None
    spool.color_name = "Black"
    spool.last_used = None
    return spool


def _rows_added(db) -> list[SpoolUsageHistory]:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], SpoolUsageHistory)]


@pytest.fixture
def _quiet_low_stock():
    """The warning has its own tests (m117); here it would only reach for the DB."""
    with patch("backend.app.services.usage_tracker._warn_if_low_stock", new_callable=AsyncMock) as m:
        yield m


class TestAnIncreaseIsConsumption:
    @pytest.mark.asyncio
    async def test_it_lands_a_row_for_the_difference(self, _quiet_low_stock):
        spool = _make_spool(weight_used=120.0)
        db = AsyncMock()
        db.add = MagicMock()

        delta = await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=2, new_used=300.0)

        assert delta == 180.0
        assert spool.weight_used == 300.0
        (row,) = _rows_added(db)
        assert row.spool_id == 1
        assert row.printer_id == 10
        assert row.weight_used == 180.0
        assert row.percent_used == 18
        assert row.status == AMS_SYNC_STATUS
        assert row.archive_id is None, "not a print — it must not adopt an archive"
        assert row.print_name is None, "there is no print to name; the status carries the meaning"

    @pytest.mark.asyncio
    async def test_the_row_is_the_delta_not_the_new_total(self, _quiet_low_stock):
        """⚠️ The readers SUM this table. A row carrying the running total would
        double-count everything the spool had already printed."""
        spool = _make_spool(weight_used=725.0)
        db = AsyncMock()
        db.add = MagicMock()

        await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=2, new_used=800.0)

        (row,) = _rows_added(db)
        assert row.weight_used == 75.0

    @pytest.mark.asyncio
    async def test_it_asks_the_low_stock_question(self, _quiet_low_stock):
        """Filament that vanished off-book still empties a spool."""
        spool = _make_spool(weight_used=100.0)
        db = AsyncMock()
        db.add = MagicMock()

        await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=2, new_used=900.0)

        _quiet_low_stock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unknown_label_weight_still_records_the_grams(self, _quiet_low_stock):
        """Percent needs a total; grams do not. The row keeps what it knows."""
        spool = _make_spool(label_weight=0, weight_used=10.0)
        db = AsyncMock()
        db.add = MagicMock()

        await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=0, new_used=60.0)

        (row,) = _rows_added(db)
        assert row.weight_used == 50.0
        assert row.percent_used == 0


class TestADecreaseIsACorrection:
    @pytest.mark.asyncio
    async def test_it_writes_no_row(self):
        spool = _make_spool(weight_used=400.0)
        db = AsyncMock()
        db.add = MagicMock()

        delta = await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=1, new_used=250.0)

        assert delta == -150.0
        assert spool.weight_used == 250.0
        assert _rows_added(db) == [], "a negative row would subtract from farm-wide consumption"

    @pytest.mark.asyncio
    async def test_it_re_arms_the_low_stock_warning(self):
        """m117: a spool topped up and burnt back down in one print would
        otherwise stay muted at the level it was muted at."""
        spool = _make_spool(weight_used=950.0, notified=True)
        db = AsyncMock()
        db.add = MagicMock()

        await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=1, new_used=300.0)

        assert spool.low_stock_notified is False

    @pytest.mark.asyncio
    async def test_it_pulls_the_baseline_down_with_it(self):
        """``max(0, weight_used - baseline)`` drives the "Total Consumed" widget;
        a baseline left above the new total would read as negative consumption."""
        spool = _make_spool(weight_used=900.0, baseline=800.0)
        db = AsyncMock()
        db.add = MagicMock()

        await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=1, new_used=200.0)

        assert spool.weight_used_baseline == 200.0

    @pytest.mark.asyncio
    async def test_a_baseline_already_below_is_left_alone(self):
        spool = _make_spool(weight_used=900.0, baseline=100.0)
        db = AsyncMock()
        db.add = MagicMock()

        await record_ams_sync_usage(db, spool, printer_id=10, ams_id=0, tray_id=1, new_used=200.0)

        assert spool.weight_used_baseline == 100.0
