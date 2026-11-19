"""Wear = measured printing time x the printer's rate per hour; no figure is None, never 0."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services import wear_cost
from backend.app.services.wear_cost import carry_restored, measured_seconds, stamp_measured_end, wear_for

START = datetime(2026, 11, 1, 10, 0, 0)


class _Db:
    """The one thing the writer asks of its session: a scalar read of the printer's rate."""

    def __init__(self, rate):
        self.rate = rate
        self.reads = 0

    async def scalar(self, _statement):
        self.reads += 1
        return self.rate


def _archive(**values) -> PrintArchive:
    defaults = {"filename": "f", "file_path": "", "file_size": 0, "printer_id": 1, "started_at": START}
    return PrintArchive(**{**defaults, **values})


async def _stamp(archive, rate, *, seconds=3600, printer_id=1, holds=True, db=None):
    return await stamp_measured_end(
        db or _Db(rate),
        archive,
        printer_id=printer_id,
        ended_at=archive.started_at + timedelta(seconds=seconds) if archive.started_at else START,
        record_holds=lambda: holds,
    )


def test_an_hour_at_the_rate_is_the_rate():
    assert wear_for(3600, 2.5) == 2.5


def test_the_figure_is_proportional_and_rounded_to_three_decimals():
    assert wear_for(5400, 2.0) == 3.0
    assert wear_for(1000, 1.5) == 0.417  # 0.41666…
    assert wear_for(1, 1.0) == 0.0  # 0.000277… rounds to nothing, and that is still a figure
    assert wear_cost.PRECISION == 3


@pytest.mark.parametrize("rate", [None, 0, 0.0, -1, "", "abc", float("nan"), float("inf"), True])
def test_no_rate_is_no_figure(rate):
    assert wear_for(3600, rate) is None


@pytest.mark.parametrize("seconds", [None, 0, -5, "", "abc", float("nan"), float("inf"), True])
def test_an_unknown_duration_is_no_figure(seconds):
    assert wear_for(seconds, 2.0) is None


def test_the_duration_is_the_wall_time_from_the_measured_start():
    archive = _archive()

    assert measured_seconds(archive, START + timedelta(minutes=30)) == 1800
    # A stored start is naive UTC, a fresh end is aware: the same instant either way.
    assert measured_seconds(archive, (START + timedelta(minutes=30)).replace(tzinfo=timezone.utc)) == 1800


def test_a_missing_or_reconstructed_start_is_no_duration():
    end = START + timedelta(hours=1)

    assert measured_seconds(_archive(started_at=None), end) is None
    assert measured_seconds(_archive(extra_data={"started_at_reconstructed": True}), end) is None
    assert measured_seconds(_archive(extra_data="not a dict"), end) is None
    assert measured_seconds(_archive(), None) is None
    assert measured_seconds(_archive(), START) is None  # an end that is not after the start
    assert measured_seconds(_archive(), START - timedelta(seconds=5)) is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_the_writer_does_not_ask_how_the_print_ended(status):
    # The machine wore in all of them; the outcome is not an input of the writer.
    archive = _archive(status=status)

    assert await _stamp(archive, 2.0) == 2.0
    assert (archive.wear_cost, archive.wear_rate) == (2.0, 2.0)


async def test_the_figure_and_the_rate_applied_are_written_together():
    archive = _archive()

    assert await _stamp(archive, 3.0, seconds=1800) == 1.5
    assert (archive.wear_cost, archive.wear_rate) == (1.5, 3.0)


@pytest.mark.parametrize("rate", [None, 0, 0.0])
async def test_a_printer_without_a_rate_leaves_both_columns_empty(rate):
    # ``None`` is also what the read answers when the printer's row is gone.
    archive = _archive()

    assert await _stamp(archive, rate) is None
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


@pytest.mark.parametrize(
    "values",
    [{"started_at": None}, {"extra_data": {"started_at_reconstructed": True}}],
)
async def test_an_unmeasured_start_leaves_both_columns_empty_and_reads_no_rate(values):
    archive = _archive(**values)
    db = _Db(2.0)

    assert await _stamp(archive, 2.0, db=db) is None
    assert (archive.wear_cost, archive.wear_rate, db.reads) == (None, None, 0)


async def test_a_printer_record_whose_life_ended_leaves_both_columns_empty():
    # The rate that was read may belong to the printer that took the deleted one's id.
    archive = _archive()

    assert await _stamp(archive, 2.0, holds=False) is None
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_the_life_is_asked_after_the_rate_was_read():
    archive = _archive()
    db = _Db(2.0)
    order = []

    async def scalar(_statement):
        order.append("rate")
        return 2.0

    db.scalar = scalar

    def holds():
        order.append("life")
        return True

    await stamp_measured_end(db, archive, printer_id=1, ended_at=START + timedelta(hours=1), record_holds=holds)
    assert order == ["rate", "life"]


async def test_an_archive_of_another_printer_is_left_alone():
    archive = _archive(printer_id=2)

    assert await _stamp(archive, 2.0, printer_id=1) is None
    assert await _stamp(_archive(printer_id=None), 2.0, printer_id=1) is None
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_a_later_rate_change_does_not_move_a_stamped_figure():
    archive = _archive()
    await _stamp(archive, 2.0)
    db = _Db(9.0)

    assert await _stamp(archive, 9.0, seconds=7200, db=db) is None  # nothing was written
    assert (archive.wear_cost, archive.wear_rate, db.reads) == (2.0, 2.0, 0)


async def test_a_stamped_zero_is_a_figure_and_is_not_overwritten():
    archive = _archive()
    await _stamp(archive, 1.0, seconds=1)
    assert archive.wear_cost == 0.0

    await _stamp(archive, 100.0)
    assert (archive.wear_cost, archive.wear_rate) == (0.0, 1.0)


def test_a_restored_pair_is_taken_as_recorded():
    archive = _archive()

    assert carry_restored(archive, 1.234, 2.5) is True
    assert (archive.wear_cost, archive.wear_rate) == (1.234, 2.5)


async def test_a_restore_never_overwrites_a_local_figure():
    archive = _archive()
    await _stamp(archive, 2.0)

    assert carry_restored(archive, 9.0, 9.0) is False
    assert (archive.wear_cost, archive.wear_rate) == (2.0, 2.0)


@pytest.mark.parametrize(
    ("cost", "rate"),
    [
        (None, 2.0),
        (1.0, None),
        (None, None),
        (1.0, 0),
        (-1.0, 2.0),
        ("1.0", 2.0),
        (1.0, "2.0"),
        (True, 2.0),
        (1.0, True),
        (float("nan"), 2.0),
        (float("inf"), 2.0),
        (1.0, float("nan")),
    ],
)
def test_a_restored_pair_is_one_bundle_or_nothing(cost, rate):
    archive = _archive()

    assert carry_restored(archive, cost, rate) is False
    assert (archive.wear_cost, archive.wear_rate) == (None, None)
