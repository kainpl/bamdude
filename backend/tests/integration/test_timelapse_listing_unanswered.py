"""A printer that did not answer is not a printer with no recordings (upstream 91acac2b).

Scan for timelapse and the manual pick both said 404 "no recordings" when the
printer's storage never answered — sending the operator to look for a video
that may well be there. They now answer 503 for a printer that did not answer,
which the listing knows since audit D6 part 4 (``last_listing_answered``).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.services import timelapse_files


@pytest.fixture(autouse=True)
def _fresh():
    timelapse_files._listing_answered.clear()
    yield
    timelapse_files._listing_answered.clear()


def _listing(answered: bool):
    async def listing(printer):
        timelapse_files._listing_answered[printer.id] = answered
        return [], None

    return listing


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(("answered", "status"), [(False, 503), (True, 404)])
async def test_scan_tells_silence_from_absence(
    async_client: AsyncClient, printer_factory, archive_factory, monkeypatch, answered, status
):
    printer = await printer_factory()
    archive = await archive_factory(printer.id)
    monkeypatch.setattr(timelapse_files, "list_timelapse_videos", _listing(answered))

    response = await async_client.post(f"/api/v1/archives/{archive.id}/timelapse/scan")

    assert response.status_code == status, response.text


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(("answered", "status"), [(False, 503), (True, 404)])
async def test_the_manual_pick_tells_silence_from_absence(
    async_client: AsyncClient, printer_factory, archive_factory, monkeypatch, answered, status
):
    printer = await printer_factory()
    archive = await archive_factory(printer.id)
    monkeypatch.setattr(timelapse_files, "list_timelapse_videos", _listing(answered))

    response = await async_client.post(
        f"/api/v1/archives/{archive.id}/timelapse/select", params={"filename": "video_1.mp4"}
    )

    assert response.status_code == status, response.text
