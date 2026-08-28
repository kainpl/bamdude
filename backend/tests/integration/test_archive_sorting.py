"""Sorting the archive list by what a print actually consumed — and paging it.

Two things, both from upstream #2636's Print Log work, adapted: our archive IS
the run journal, so the sorting belongs on the archive list rather than on a
separate table we do not have.

⚠️ **The tiebreak is the load-bearing half.** The list PAGES, and rows sharing a
sort key have no defined order between two queries. Sorting a farm's history by
printer — twelve distinct values over hundreds of rows — could therefore repeat
one archive on page 2 and skip another entirely. Nothing errors; the operator
just never sees a print they printed.

⚠️ **Empty values are held last in BOTH directions.** These columns are
routinely NULL — an external print has no cost, a printer with no smart plug no
energy, a running print no duration — and the backends disagree: PostgreSQL
sorts NULL high, SQLite low. Left alone the same click opens on blanks on one
and on data on the other.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService


async def _archive(db_session, *, name: str, **fields):
    archive = PrintArchive(
        filename=f"{name}.3mf",
        print_name=name,
        file_path=f"/tmp/{name}.3mf",
        file_size=1024,
        content_hash=f"hash_{name}",
        status="completed",
        **fields,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _names(db_session, sort_by: str, **kwargs) -> list[str]:
    items, _total = await ArchiveService(db_session).list_archives(sort_by=sort_by, limit=None, **kwargs)
    return [a.print_name for a in items]


@pytest.mark.asyncio
@pytest.mark.integration
class TestSortingByWhatItConsumed:
    async def test_cost(self, db_session):
        await _archive(db_session, name="cheap", cost=1.0)
        await _archive(db_session, name="dear", cost=9.0)

        assert await _names(db_session, "cost-desc") == ["dear", "cheap"]
        assert await _names(db_session, "cost-asc") == ["cheap", "dear"]

    async def test_energy(self, db_session):
        await _archive(db_session, name="light", energy_kwh=0.2)
        await _archive(db_session, name="heavy", energy_kwh=2.5)

        assert await _names(db_session, "energy-desc") == ["heavy", "light"]

    async def test_filament(self, db_session):
        await _archive(db_session, name="small", filament_used_grams=12.0)
        await _archive(db_session, name="big", filament_used_grams=340.0)

        assert await _names(db_session, "filament-desc") == ["big", "small"]

    async def test_duration_uses_the_real_time_not_the_estimate(self, db_session):
        """``actual_time_seconds`` is what the print took; ``print_time_seconds``
        is what the slicer guessed. A history sorted by "longest" should answer
        with reality.

        ⚠️ The duration is DERIVED — ``core/database``'s before_flush event
        recomputes it from the timestamps on every write, so assigning the column
        directly is silently discarded. Set the timestamps the event reads. (An
        earlier version of this test set the column and passed anyway, on the id
        tiebreak alone — a false green found by giving the rows ids that
        contradict their durations.)"""
        base = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        # Ids ascend while durations descend, so only a working sort can pass.
        await _archive(
            db_session,
            name="long",
            started_at=base,
            completed_at=base + timedelta(hours=2),
            print_time_seconds=1,
        )
        await _archive(
            db_session,
            name="quick",
            started_at=base,
            completed_at=base + timedelta(minutes=10),
            print_time_seconds=99999,
        )

        assert await _names(db_session, "duration-desc") == ["long", "quick"]
        assert await _names(db_session, "duration-asc") == ["quick", "long"]


@pytest.mark.asyncio
@pytest.mark.integration
class TestEmptyValuesSinkInBothDirections:
    async def test_descending(self, db_session):
        await _archive(db_session, name="priced", cost=5.0)
        await _archive(db_session, name="unpriced")

        assert await _names(db_session, "cost-desc") == ["priced", "unpriced"]

    async def test_ascending(self, db_session):
        """The direction that would otherwise open on a screenful of blanks."""
        await _archive(db_session, name="priced", cost=5.0)
        await _archive(db_session, name="unpriced")

        assert await _names(db_session, "cost-asc") == ["priced", "unpriced"]


@pytest.mark.asyncio
@pytest.mark.integration
class TestPagingIsStable:
    async def test_the_tiebreak_is_actually_in_the_query(self, db_session):
        """⚠️ Asserted STRUCTURALLY, and deliberately.

        The behavioural tests below cannot prove this: SQLite happens to return
        equal-key rows in rowid order on a full scan, so they pass with the
        tiebreak removed — measured, not assumed. PostgreSQL under a different
        plan need not, and neither backend promises anything. So the guarantee
        is asserted where it actually lives: the last ORDER BY term."""
        from sqlalchemy.dialects import postgresql

        from backend.app.models.archive import PrintArchive as PA

        service = ArchiveService(db_session)
        captured: dict = {}
        original = db_session.execute

        async def spy(statement, *args, **kwargs):
            captured.setdefault("statements", []).append(statement)
            return await original(statement, *args, **kwargs)

        db_session.execute = spy
        try:
            await service.list_archives(sort_by="cost-desc", limit=3)
        finally:
            db_session.execute = original

        rendered = [
            str(s.compile(dialect=postgresql.dialect())) for s in captured["statements"] if hasattr(s, "compile")
        ]
        with_order = [r for r in rendered if "ORDER BY" in r]
        assert with_order, "the listing query should carry an ORDER BY"
        # The ORDER BY clause alone — the statement itself ends with LIMIT/OFFSET.
        order_by = with_order[-1].split("ORDER BY")[-1].split("LIMIT")[0].strip()
        assert order_by.endswith(f"{PA.__tablename__}.id DESC"), (
            f"the last ORDER BY term must be the id tiebreak, got: {order_by!r}"
        )

    async def test_a_low_cardinality_sort_neither_repeats_nor_skips(self, db_session):
        """Nine rows sharing one cost, paged three at a time."""
        for index in range(9):
            await _archive(db_session, name=f"row{index}", cost=1.0)

        service = ArchiveService(db_session)
        seen: list[str] = []
        for offset in (0, 3, 6):
            items, total = await service.list_archives(sort_by="cost-desc", limit=3, offset=offset)
            seen.extend(a.print_name for a in items)

        assert total == 9
        assert len(set(seen)) == 9, f"a page repeated or skipped a row: {seen}"

    async def test_the_same_query_twice_gives_the_same_page(self, db_session):
        for index in range(6):
            await _archive(db_session, name=f"row{index}", cost=1.0)

        service = ArchiveService(db_session)
        first = [a.id for a in (await service.list_archives(sort_by="cost-asc", limit=3, offset=0))[0]]
        again = [a.id for a in (await service.list_archives(sort_by="cost-asc", limit=3, offset=0))[0]]

        assert first == again

    async def test_the_tiebreak_does_not_disturb_the_primary_key(self, db_session):
        """It orders ties only — a row with a real value still wins."""
        await _archive(db_session, name="tied_a", cost=1.0)
        await _archive(db_session, name="dearest", cost=99.0)
        await _archive(db_session, name="tied_b", cost=1.0)

        assert (await _names(db_session, "cost-desc"))[0] == "dearest"


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheOldKeysStillWork:
    async def test_an_unknown_key_falls_back_rather_than_erroring(self, db_session):
        """A stale bookmark should still open the page."""
        await _archive(db_session, name="only")

        assert await _names(db_session, "no-such-sort") == ["only"]

    async def test_name_sorting_is_unchanged(self, db_session):
        await _archive(db_session, name="bbb")
        await _archive(db_session, name="aaa")

        assert await _names(db_session, "name-asc") == ["aaa", "bbb"]
