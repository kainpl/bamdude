"""Saving a printer's camera, or deleting the printer, retires its running camera upstream (upstream 31888777a).

What the retirement itself does is pinned in ``unit/test_camera_source_change_retires_stream.py``; here the
two routes are shown to ask for it — after their commit, only when the source really changed, and without
letting a camera failure undo a saved change.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

RETIRE = "backend.app.api.routes.camera.retire_printer_stream"
EXTERNAL = {
    "external_camera_enabled": True,
    "external_camera_url": "http://cam-a.invalid/stream",
    "external_camera_type": "mjpeg",
}


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "saved, change",
    [
        ({}, EXTERNAL),  # built-in -> external
        (EXTERNAL, {"external_camera_enabled": False}),  # external -> built-in
        (EXTERNAL, {"external_camera_url": "http://cam-b.invalid/stream"}),
        (EXTERNAL, {"external_camera_type": "snapshot"}),
        ({}, {"access_code": "87654321"}),  # the built-in camera's own credentials
    ],
)
async def test_a_saved_camera_change_retires_the_running_upstream(
    async_client: AsyncClient, printer_factory, saved, change
):
    printer = await printer_factory(**saved)
    with (
        patch(RETIRE, new_callable=AsyncMock) as retire,
        patch("backend.app.api.routes.printers.printer_manager.connect_printer", new_callable=AsyncMock),
    ):
        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json=change)
    assert response.status_code == 200, response.text
    retire.assert_awaited_once_with(printer.id)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "saved, change",
    [
        ({}, {"name": "Renamed"}),
        (EXTERNAL, {"name": "Renamed"}),
        (EXTERNAL, dict(EXTERNAL)),  # the form saved as it was
        # An external camera kept for later is not what the live view dials.
        ({}, {"external_camera_url": "http://cam-a.invalid/stream", "external_camera_type": "mjpeg"}),
    ],
)
async def test_a_save_that_leaves_the_camera_as_it_was_stops_nothing(
    async_client: AsyncClient, printer_factory, saved, change
):
    printer = await printer_factory(**saved)
    with patch(RETIRE, new_callable=AsyncMock) as retire:
        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json=change)
    assert response.status_code == 200, response.text
    retire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_camera_that_fails_to_retire_does_not_fail_the_saved_change(async_client: AsyncClient, printer_factory):
    printer = await printer_factory(**EXTERNAL)
    with patch(RETIRE, new_callable=AsyncMock, side_effect=RuntimeError("worker gone")):
        response = await async_client.patch(
            f"/api/v1/printers/{printer.id}", json={"external_camera_url": "http://cam-b.invalid/stream"}
        )
    assert response.status_code == 200, response.text
    assert response.json()["external_camera_url_configured"] is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_a_printer_retires_its_upstream_and_forgets_its_light(
    async_client: AsyncClient, printer_factory
):
    """SQLite gives the next printer this id: it must not join the deleted one's upstream, be lent its frames,
    or have its light switched off by a lease the deleted printer left."""
    from backend.app.services import camera_light

    printer = await printer_factory()
    printer_id = printer.id
    camera_light._lights[printer_id] = camera_light._PrinterLight(owned=True)
    try:
        with patch(RETIRE, new_callable=AsyncMock) as retire:
            response = await async_client.delete(f"/api/v1/printers/{printer_id}")
        assert response.status_code == 200, response.text
        retire.assert_awaited_once_with(printer_id)
        assert printer_id not in camera_light._lights
    finally:
        camera_light._lights.pop(printer_id, None)
