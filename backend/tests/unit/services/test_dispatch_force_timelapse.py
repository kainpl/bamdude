"""Tests for _resolve_effective_timelapse (#1397).

BamDude forces timelapse recording on at dispatch time when the
capture_finish_photo setting is enabled and the user did not opt in
to timelapse for the specific print. The forced bit is recorded on
the archive so the post-extraction cleanup path can delete the file.

These tests exercise the four decision shapes the helper has to handle:

  1. capture_finish_photo OFF → no override regardless of user choice
  2. capture_finish_photo ON, user chose timelapse → no override (the
     user's choice already covers the photo path)
  3. capture_finish_photo ON, user chose NO timelapse → override to ON,
     mark archive.bamdude_forced_timelapse=True
  4. capture_finish_photo unset (None / missing) → defaults to ON, so
     the same override applies as case 3
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.background_dispatch import (
    BackgroundDispatchService,
    PrintDispatchJob,
)


def _make_job(timelapse: bool | None) -> PrintDispatchJob:
    """Mint a job with the smallest valid shape — the only field
    _resolve_effective_timelapse reads from job is `options`."""
    return PrintDispatchJob(
        id=1,
        kind="print_library_file",
        source_id=42,
        source_name="test.gcode.3mf",
        printer_id=10,
        printer_name="Printer A",
        options={"timelapse": timelapse} if timelapse is not None else {},
    )


def _make_archive() -> SimpleNamespace:
    """Stand-in archive object; the helper only touches .id and
    .bamdude_forced_timelapse."""
    return SimpleNamespace(id=99, bamdude_forced_timelapse=False)


def _make_db() -> AsyncMock:
    """Fake db with a no-op .commit()."""
    db = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_capture_finish_photo_off_means_no_override():
    """Master toggle off → user's timelapse=False stays False, no flag set."""
    service = BackgroundDispatchService()
    archive = _make_archive()
    db = _make_db()
    job = _make_job(timelapse=False)

    with patch(
        "backend.app.api.routes.settings.get_setting",
        new=AsyncMock(return_value="false"),
    ):
        effective = await service._resolve_effective_timelapse(db, archive, job)

    assert effective is False
    assert archive.bamdude_forced_timelapse is False
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_opted_in_passes_through_unchanged():
    """User asked for a timelapse → no override needed (their normal flow
    already records one). bamdude_forced_timelapse stays False so cleanup
    leaves the file alone."""
    service = BackgroundDispatchService()
    archive = _make_archive()
    db = _make_db()
    job = _make_job(timelapse=True)

    # get_setting shouldn't even be consulted — but if it is, no override
    # should still fire.
    with patch(
        "backend.app.api.routes.settings.get_setting",
        new=AsyncMock(return_value="true"),
    ):
        effective = await service._resolve_effective_timelapse(db, archive, job)

    assert effective is True
    assert archive.bamdude_forced_timelapse is False
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_on_user_off_forces_timelapse_and_marks_flag():
    """The whole point of the fix: capture_finish_photo=on + user-timelapse=off
    flips the MQTT command to timelapse=True and marks the archive for
    post-extraction cleanup."""
    service = BackgroundDispatchService()
    archive = _make_archive()
    db = _make_db()
    job = _make_job(timelapse=False)

    with patch(
        "backend.app.api.routes.settings.get_setting",
        new=AsyncMock(return_value="true"),
    ):
        effective = await service._resolve_effective_timelapse(db, archive, job)

    assert effective is True
    assert archive.bamdude_forced_timelapse is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_finish_photo_unset_defaults_to_enabled():
    """Setting absent from DB → default is True (per the Field default in the
    schema), so the override fires just like when explicitly enabled."""
    service = BackgroundDispatchService()
    archive = _make_archive()
    db = _make_db()
    job = _make_job(timelapse=False)

    with patch(
        "backend.app.api.routes.settings.get_setting",
        new=AsyncMock(return_value=None),
    ):
        effective = await service._resolve_effective_timelapse(db, archive, job)

    assert effective is True
    assert archive.bamdude_forced_timelapse is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_missing_timelapse_treated_as_false():
    """Some queue paths pass options without a timelapse key. Treat absent
    as False (matches existing job.options.get('timelapse', False) default
    that the caller previously used)."""
    service = BackgroundDispatchService()
    archive = _make_archive()
    db = _make_db()
    job = _make_job(timelapse=None)  # falls through to {}

    with patch(
        "backend.app.api.routes.settings.get_setting",
        new=AsyncMock(return_value="true"),
    ):
        effective = await service._resolve_effective_timelapse(db, archive, job)

    assert effective is True
    assert archive.bamdude_forced_timelapse is True
