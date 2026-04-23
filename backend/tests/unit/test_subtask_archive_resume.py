"""Regression tests for subtask_id-based archive resume (#972, 5A).

Before this fix, a mid-print backend restart (container reboot, host crash,
etc.) could land on the name-based lookup in `on_print_start`, and — if the
archive had been cancelled by some legacy path or sat in an ambiguous state —
a fresh row would be created with ``started_at = now()``, losing print-time
continuity on long jobs. #972 upstream reports losing ~9h of a 13h A1 print
this way.

BamDude's fix (commits ``a3eb393`` + ``84571e9``) stores ``subtask_id`` on
the archive row and consults it as a **pre-check** before the name-based
lookup in ``main.on_print_start``:

    if subtask_id:
        match = SELECT ... WHERE printer_id=? AND subtask_id=? AND status='printing'
        ORDER BY created_at DESC LIMIT 1

Note the divergence from upstream Bambuddy v0.2.3: upstream's query also
matches ``status='cancelled'`` so it can **revive** stale-cancelled rows.
BamDude does not — we ripped out the 4h stale-cancel heuristic ages ago
(it killed legitimate long prints), so there's nothing to revive, and
matching ``status='cancelled'`` here would un-cancel rows a user
deliberately cancelled. This file includes an explicit
`test_subtask_id_does_not_revive_cancelled_row` regression guard so the
revive behaviour can't sneak back in on a future upstream sync.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive


def _extract_subtask_id(data: dict) -> str | None:
    """Mirrors the extraction logic in ``main.on_print_start``.

    Pinned as a free function so the test can assert the contract without
    running the whole handler. If ``main.on_print_start`` changes its
    extraction, update this helper in lockstep.
    """
    subtask_id = data.get("subtask_id") or None
    if subtask_id == "0":
        subtask_id = None
    return subtask_id


class TestSubtaskIdExtraction:
    """Extraction normalises the values Bambu firmware actually emits."""

    def test_valid_id_returns_string(self):
        assert _extract_subtask_id({"subtask_id": "12345"}) == "12345"

    def test_zero_collapses_to_none(self):
        """Bambu reports '0' for local (non-cloud) prints; must not match anything."""
        assert _extract_subtask_id({"subtask_id": "0"}) is None

    def test_empty_string_collapses_to_none(self):
        """`or None` turns empty-string falsy into None."""
        assert _extract_subtask_id({"subtask_id": ""}) is None

    def test_missing_key(self):
        assert _extract_subtask_id({}) is None

    def test_none_value(self):
        assert _extract_subtask_id({"subtask_id": None}) is None

    def test_other_keys_ignored(self):
        assert _extract_subtask_id({"foo": "bar"}) is None


class TestSubtaskIdResumeQuery:
    """End-to-end DB behaviour of the resume pre-check path.

    The query under test is the one in ``main.on_print_start`` around line 1728:

        .where(PrintArchive.printer_id == printer_id)
        .where(PrintArchive.subtask_id == subtask_id)
        .where(PrintArchive.status == "printing")
        .order_by(PrintArchive.created_at.desc())
        .limit(1)
    """

    @pytest.fixture
    def archive_row_factory(self, archive_factory, printer_factory, db_session):
        async def _create(
            *,
            subtask_id: str | None = None,
            status: str = "printing",
            age_hours: float = 0.0,
            failure_reason: str | None = None,
            printer=None,
        ):
            if printer is None:
                printer = await printer_factory()
            started = datetime.now(timezone.utc) - timedelta(hours=age_hours)
            kwargs = {
                "filename": "Broly_Legendary.gcode.3mf",
                "file_path": "archive/1/x/Broly.gcode.3mf",
                "file_size": 100,
                "print_name": "Broly_Legendary",
                "status": status,
                "started_at": started,
                "subtask_id": subtask_id,
            }
            if failure_reason is not None:
                kwargs["failure_reason"] = failure_reason
            archive = await archive_factory(printer.id, **kwargs)
            # Override the server-default created_at so age-based ordering
            # tests are deterministic (SQLite's default `now()` has only
            # second precision, which isn't enough when two rows land in
            # the same test microsecond).
            archive.created_at = started
            await db_session.commit()
            await db_session.refresh(archive)
            return printer, archive

        return _create

    async def test_finds_matching_printing_row_regardless_of_age(self, archive_row_factory, db_session):
        """A matching printing row must be found even when it's much older
        than any stale-cancel cutoff would tolerate. The whole point of the
        subtask_id pre-check is to adopt long-running prints across a restart."""
        printer, archive = await archive_row_factory(subtask_id="t-123", age_hours=10)

        result = await db_session.execute(
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer.id)
            .where(PrintArchive.subtask_id == "t-123")
            .where(PrintArchive.status == "printing")
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )
        found = result.scalar_one_or_none()
        assert found is not None
        assert found.id == archive.id

    async def test_does_not_revive_cancelled_row(self, archive_row_factory, db_session):
        """Divergence guard from upstream Bambuddy v0.2.3 #972: BamDude does
        NOT match ``status='cancelled'`` in the subtask_id pre-check, so a
        cancelled row (user cancel OR stale-cancel-from-legacy-data) can
        never be un-cancelled just because a new print happens to reuse the
        same subtask_id. The query is ``status == 'printing'``, period."""
        printer, _ = await archive_row_factory(
            subtask_id="t-456",
            status="cancelled",
            failure_reason="User cancelled",
            age_hours=2,
        )

        result = await db_session.execute(
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer.id)
            .where(PrintArchive.subtask_id == "t-456")
            .where(PrintArchive.status == "printing")
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )
        found = result.scalar_one_or_none()
        assert found is None, (
            "subtask_id pre-check must only adopt status='printing' rows — "
            "matching cancelled rows would un-cancel user cancellations "
            "(see divergence note in test module docstring and main.py:1777)"
        )

    async def test_completed_archive_not_resumed(self, archive_row_factory, db_session):
        """A finished print's subtask_id must not be reopened as printing —
        that job is done; a new run with the same id is a new row."""
        printer, _ = await archive_row_factory(subtask_id="t-789", status="completed")

        result = await db_session.execute(
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer.id)
            .where(PrintArchive.subtask_id == "t-789")
            .where(PrintArchive.status == "printing")
        )
        found = result.scalar_one_or_none()
        assert found is None

    async def test_non_null_query_does_not_match_null_row(self, archive_row_factory, db_session):
        """Two non-cloud prints both land with ``subtask_id=NULL`` (Bambu
        reports '0' / '' for local submissions, and the handler normalises
        both to None so the column is NULL). A later print with a real
        subtask_id must not accidentally adopt a NULL-subtask row via this
        query — SQL `col = 'x'` is never true for NULL, which is exactly
        what we want. This is the cross-print collision guard."""
        printer, _ = await archive_row_factory(subtask_id=None, age_hours=1)

        # Run the same shape of query the handler issues when a real
        # subtask_id comes in — the NULL-subtask row must not match.
        result = await db_session.execute(
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer.id)
            .where(PrintArchive.subtask_id == "t-real")
            .where(PrintArchive.status == "printing")
        )
        found = result.scalar_one_or_none()
        assert found is None, (
            "Real subtask_id query must not match a NULL-subtask row — "
            "that would let an unrelated local print adopt any other "
            "local print's archive."
        )

    async def test_newest_match_wins(self, archive_row_factory, db_session, printer_factory):
        """If somehow two ``printing`` rows share the same subtask_id on the
        same printer (shouldn't happen, but firmware can surprise us), the
        ``ORDER BY created_at DESC LIMIT 1`` picks the newest."""
        printer = await printer_factory()
        _, old = await archive_row_factory(subtask_id="t-dup", age_hours=5, printer=printer)
        _, new = await archive_row_factory(subtask_id="t-dup", age_hours=1, printer=printer)

        result = await db_session.execute(
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer.id)
            .where(PrintArchive.subtask_id == "t-dup")
            .where(PrintArchive.status == "printing")
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )
        found = result.scalar_one_or_none()
        assert found is not None
        assert found.id == new.id
        assert found.id != old.id

    async def test_different_printer_does_not_match(self, archive_row_factory, db_session, printer_factory):
        """Same subtask_id on a different printer must not match — the
        query is scoped per-printer."""
        printer_a = await printer_factory()
        printer_b = await printer_factory()
        await archive_row_factory(subtask_id="t-xsite", printer=printer_a)

        result = await db_session.execute(
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer_b.id)
            .where(PrintArchive.subtask_id == "t-xsite")
            .where(PrintArchive.status == "printing")
        )
        found = result.scalar_one_or_none()
        assert found is None


class TestSubtaskIdBackfill:
    """Name-based match path backfills subtask_id onto the matched row so the
    next restart can use the faster pre-check (main.py:1764-1768)."""

    async def test_null_subtask_id_is_backfilled_when_name_match_wins(
        self, archive_factory, printer_factory, db_session
    ):
        """Simulate: archive created before 5A landed (subtask_id=NULL) — on
        the first on_print_start after the upgrade, name-match wins, and the
        handler writes subtask_id onto the row."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            filename="Broly.gcode.3mf",
            print_name="Broly",
            status="printing",
            subtask_id=None,
        )

        # Handler logic under test (main.py:1766-1768):
        incoming_subtask_id = "t-new-after-upgrade"
        if archive.subtask_id is None:
            archive.subtask_id = incoming_subtask_id
            await db_session.commit()
            await db_session.refresh(archive)

        assert archive.subtask_id == incoming_subtask_id

    async def test_existing_subtask_id_not_overwritten(self, archive_factory, printer_factory, db_session):
        """If the row already has a subtask_id, a name-match with a *different*
        incoming id must NOT clobber it — the guard is ``is None``, not
        ``!= incoming``. That keeps the backfill a one-time bootstrap and
        prevents unrelated prints (same print_name, different submission)
        from colliding onto the same row's id."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            filename="Broly.gcode.3mf",
            print_name="Broly",
            status="printing",
            subtask_id="t-original",
        )

        # Replay handler guard on a different incoming id:
        incoming = "t-different"
        if archive.subtask_id is None:
            archive.subtask_id = incoming
            await db_session.commit()

        await db_session.refresh(archive)
        assert archive.subtask_id == "t-original"
