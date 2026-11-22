"""The printer card's cover is the picture of the print running now.

The cover used to find its archive by the file name the printer echoes, and
only by that. A print dispatched from the library carries the library's name
into its archive, and the printer echoes the name it was uploaded under — when
the two did not fold into each other (a #1542 doubled suffix did exactly
that), the running print's own archive, picture and all, was never asked, and
the card and the queue's "now printing" block showed an empty frame.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from backend.tests.integration.queue_test_database import test_engine  # noqa: F401


def _png(tag: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag


def _thumbnail(data: bytes) -> str:
    from backend.app.core.config import settings

    thumb = settings.base_dir / "archive" / f"thumb_{secrets.token_hex(4)}.png"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(data)
    return str(thumb.relative_to(settings.base_dir))


@pytest.fixture
def printing(monkeypatch):
    """The live state of a printer running ``Autel_legs_plate_5`` off its card."""
    from backend.app.api.routes import printers as printers_routes
    from backend.app.services.bambu_mqtt import PrinterState

    state = PrinterState()
    state.connected = True
    state.state = "RUNNING"
    state.subtask_name = "Autel_legs_plate_5"
    state.gcode_file = "Autel_legs_plate_5.3mf"
    monkeypatch.setattr(printers_routes.printer_manager, "get_status", lambda _printer_id: state)
    printers_routes._cover_cache.clear()
    yield state
    printers_routes._cover_cache.clear()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_running_prints_own_archive_answers_whatever_it_is_called(
    async_client: AsyncClient, printer_factory, archive_factory, printing
):
    printer = await printer_factory()
    picture = _png(b"the print running now")
    await archive_factory(
        printer.id,
        filename="Order 42 kit.gcode.3mf",
        print_name="Order 42 kit",
        file_path="",
        thumbnail_path=_thumbnail(picture),
        status="printing",
    )
    resp = await async_client.get(f"/api/v1/printers/{printer.id}/camera-cover")
    assert resp.status_code == 200, resp.text
    assert resp.content == picture


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_running_print_without_its_picture_yet_falls_back_to_the_name(
    async_client: AsyncClient, printer_factory, archive_factory, printing
):
    """The 3MF of an external print is still downloading: nothing of the running
    print's archive is on disk, and the last print of the same file answers."""
    printer = await printer_factory()
    earlier = _png(b"the same file, printed before")
    await archive_factory(
        printer.id,
        filename="Autel_legs_plate_5.gcode.3mf",
        print_name="Autel_legs_plate_5",
        file_path="",
        thumbnail_path=_thumbnail(earlier),
        status="completed",
    )
    await archive_factory(
        printer.id,
        filename="Autel_legs_plate_5.gcode.3mf",
        print_name="Autel_legs_plate_5",
        file_path="",
        thumbnail_path=None,
        status="printing",
    )
    resp = await async_client.get(f"/api/v1/printers/{printer.id}/camera-cover")
    assert resp.status_code == 200, resp.text
    assert resp.content == earlier


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_fallback_is_not_cached_past_the_prints_own_picture(
    async_client: AsyncClient, printer_factory, archive_factory, db_session, printing
):
    """The fallback answer is an earlier print's picture. Cached under this
    print's name it would stay for the whole print after its own 3MF landed."""
    printer = await printer_factory()
    earlier = _png(b"the same file, printed before")
    own = _png(b"the print running now")
    await archive_factory(
        printer.id,
        filename="Autel_legs_plate_5.gcode.3mf",
        print_name="Autel_legs_plate_5",
        file_path="",
        thumbnail_path=_thumbnail(earlier),
        status="completed",
    )
    running = await archive_factory(
        printer.id,
        filename="Autel_legs_plate_5.gcode.3mf",
        print_name="Autel_legs_plate_5",
        file_path="",
        thumbnail_path=None,
        status="printing",
    )
    url = f"/api/v1/printers/{printer.id}/camera-cover"
    assert (await async_client.get(url)).content == earlier

    running.thumbnail_path = _thumbnail(own)  # the 3MF arrived and its picture was extracted
    await db_session.commit()
    assert (await async_client.get(url)).content == own
    assert (await async_client.get(url)).content == own


@pytest.mark.asyncio
@pytest.mark.integration
async def test_renamed_dispatch_cover_fallback_uses_the_reported_upload_name(
    async_client: AsyncClient, printer_factory, archive_factory, printing
):
    printer = await printer_factory()
    uploaded_stem = "bamdude_auto_cali_for_user"
    picture = _png(b"renamed dispatch")
    now = datetime.now(timezone.utc)
    await archive_factory(
        printer.id,
        filename="auto_cali_for_user.gcode.3mf",
        print_name="auto_cali_for_user",
        file_path="",
        thumbnail_path=_thumbnail(picture),
        status="completed",
        created_at=now - timedelta(minutes=1),
        extra_data={"dispatch_intent": {"version": 1, "remote_filename": f"{uploaded_stem}.3mf"}},
    )
    await archive_factory(
        printer.id,
        filename=f"{uploaded_stem}.gcode.3mf",
        print_name=uploaded_stem,
        file_path="",
        thumbnail_path=_thumbnail(_png(b"different file with the same stem")),
        status="completed",
        created_at=now,
    )
    url = f"/api/v1/printers/{printer.id}/camera-cover"
    printing.subtask_name = "auto_cali_for_user"
    printing.gcode_file = "auto_cali_for_user.3mf"
    response = await async_client.get(url)
    assert response.status_code == 404, response.text

    printing.subtask_name = uploaded_stem
    printing.gcode_file = f"{uploaded_stem}.3mf"
    response = await async_client.get(url)
    assert response.status_code == 200, response.text
    assert response.content == picture
