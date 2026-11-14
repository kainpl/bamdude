"""No pooled database connection is held across a camera capture (upstream cf0ba31f1 leg 6).

A capture takes seconds — up to the worker's timeout against a printer that does not answer. Four routes read
their printer through ``Depends(get_db)`` and kept that session, in a transaction since the SELECT, until the
request ended: N operators calibrating plates, or a script probing ``/camera/test`` on unreachable printers,
each pinned a pooled connection for the whole wait. The stream and snapshot routes already read the printer
in a short session of their own (#2572); these four now do the same.

The capture is replaced by a probe that looks at every live SQLAlchemy session at the moment the capture
starts; everything before it — auth, the printer read — is the real route.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.session import _sessions

from backend.tests.auth_helpers import mint_test_authority


def _in_transaction() -> set[int]:
    return {key for key, session in list(_sessions.items()) if session.in_transaction()}


class _Probe:
    """Stands where the capture is awaited; records the sessions a transaction was opened on for this request."""

    def __init__(self, result):
        self.result = result
        self.before = _in_transaction()  # the test's own fixture session, mid-transaction after its refresh
        self.open_at_capture: set[int] | None = None

    async def __call__(self, *args, **kwargs):
        self.open_at_capture = _in_transaction() - self.before
        return self.result


def _plate_result() -> MagicMock:
    result = MagicMock(debug_image=None, source_image=None)
    result.to_dict.return_value = {"is_empty": True, "confidence": 0.9, "message": "Plate appears empty"}
    return result


def _detector() -> MagicMock:
    return MagicMock(MAX_REFERENCES=5, **{"get_calibration_count.return_value": 0})


async def _camera_token() -> str:
    from backend.app.core.auth import create_camera_stream_token

    return await create_camera_stream_token(authority=await mint_test_authority())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_camera_test_holds_no_session_while_it_dials(async_client: AsyncClient, printer_factory):
    printer = await printer_factory()
    probe = _Probe({"success": True})
    with patch("backend.app.api.routes.camera.test_camera_connection", probe):
        response = await async_client.get(f"/api/v1/printers/{printer.id}/camera/test")
    assert response.status_code == 200, response.text
    assert probe.open_at_capture == set()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_camera_diagnose_holds_no_session_while_it_probes(async_client: AsyncClient, printer_factory):
    printer = await printer_factory()
    probe = _Probe(SimpleNamespace(to_dict=lambda: {"ok": True}))
    with patch("backend.app.services.camera_diagnose.diagnose_camera", probe):
        response = await async_client.post(f"/api/v1/printers/{printer.id}/camera/diagnose")
    assert response.status_code == 200, response.text
    assert probe.open_at_capture == set()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_check_plate_holds_no_session_while_it_captures(async_client: AsyncClient, printer_factory):
    printer = await printer_factory()
    probe = _Probe(_plate_result())
    with (
        patch("backend.app.services.plate_detection.is_plate_detection_available", return_value=True),
        patch("backend.app.services.plate_detection.check_plate_empty", probe),
        patch("backend.app.services.plate_detection.PlateDetector", return_value=_detector()),
    ):
        response = await async_client.get(
            f"/api/v1/printers/{printer.id}/camera/check-plate", params={"token": await _camera_token()}
        )
    assert response.status_code == 200, response.text
    assert probe.open_at_capture == set()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_plate_calibration_holds_no_session_while_it_captures(async_client: AsyncClient, printer_factory):
    printer = await printer_factory()
    probe = _Probe((True, "Calibration saved (1/5 references)", 0))
    with (
        patch("backend.app.services.plate_detection.is_plate_detection_available", return_value=True),
        patch("backend.app.services.plate_detection.calibrate_plate", probe),
    ):
        response = await async_client.post(f"/api/v1/printers/{printer.id}/camera/plate-detection/calibrate")
    assert response.status_code == 200, response.text
    assert probe.open_at_capture == set()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("path", ["camera/test", "camera/check-plate"])
async def test_a_missing_printer_is_still_404(async_client: AsyncClient, path):
    response = await async_client.get(f"/api/v1/printers/99999/{path}", params={"token": await _camera_token()})
    assert response.status_code == 404


class _LitByTheCheck:
    """The check's capture: the lease lights the chamber (or cannot) before the frame is taken."""

    def __init__(self, state, *, lights: bool):
        self.state, self.lights = state, lights

    async def __call__(self, *args, **kwargs):
        if self.lights:
            self.state.chamber_light = True
        return _plate_result()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("lights, warning", [(True, False), (False, True)])
async def test_the_light_warning_is_about_the_frame_that_was_judged(
    async_client: AsyncClient, printer_factory, lights, warning
):
    """The check lights a dark chamber itself: warning about a light that was off BEFORE it is a false alarm.
    The warning stands only when the chamber was still dark after the check (no light to switch, or refused)."""
    printer = await printer_factory()
    state = SimpleNamespace(chamber_light=False)
    with (
        patch("backend.app.services.plate_detection.is_plate_detection_available", return_value=True),
        patch("backend.app.services.plate_detection.check_plate_empty", _LitByTheCheck(state, lights=lights)),
        patch("backend.app.services.plate_detection.PlateDetector", return_value=_detector()),
        patch("backend.app.services.printer_manager.printer_manager.get_status", return_value=state),
    ):
        response = await async_client.get(
            f"/api/v1/printers/{printer.id}/camera/check-plate", params={"token": await _camera_token()}
        )
    assert response.status_code == 200, response.text
    assert response.json()["light_warning"] is warning
