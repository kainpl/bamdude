"""Drive the real print-start consumer with synthetic image/camera failures."""

import logging
from unittest.mock import AsyncMock, Mock

import cv2
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.services import plate_detection as pd

POLYGON = [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9}, {"x": 0.1, "y": 0.9}]


@pytest.mark.asyncio
@pytest.mark.parametrize("polygon", [True, False])
@pytest.mark.parametrize("job", ["normal", "internal-path", "internal-subtask"])
@pytest.mark.parametrize(
    "case",
    [
        "clear",
        "occupied",
        "no-reference",
        "resolution",
        "corrupt-reference",
        "decode",
        "capture",
        "exception",
        "opencv",
    ],
)
async def test_real_print_start_classifies_clear_occupied_and_unavailable(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, caplog, polygon, case, job
):
    from backend.app import main

    caplog.set_level(logging.INFO)
    p = await printer_factory(plate_detection_enabled=True)
    p.plate_detection_polygon = POLYGON if polygon else None
    await db_session.commit()
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(pd, "_get_calibration_dir", lambda: tmp_path)
    monkeypatch.setattr(pd, "cv2", cv2)
    monkeypatch.setattr(pd, "np", np)
    monkeypatch.setattr(pd, "OPENCV_AVAILABLE", True)
    image = np.random.default_rng(42).integers(0, 100, (480, 640, 3), dtype=np.uint8)

    def jpeg(a):
        return cv2.imencode(".png", a)[1].tobytes()

    detector = pd.PlateDetector(polygon=p.plate_detection_polygon)
    if case != "no-reference":
        assert detector.calibrate(jpeg(image), p.id)[0]
    if case == "corrupt-reference":
        detector._get_reference_paths(p.id)[0].write_bytes(b"not an image")
    if case == "occupied":
        image[150:350, 150:350] = 255
    if case == "resolution":
        image = cv2.resize(image, (800, 600))
    monkeypatch.setattr(pd, "OPENCV_AVAILABLE", case != "opencv")
    capture = AsyncMock(
        return_value=(None if case == "capture" else b"invalid" if case == "decode" else jpeg(image), "synthetic")
    )
    if case == "exception":
        capture.side_effect = RuntimeError("synthetic camera failure")
    monkeypatch.setattr(pd, "capture_camera_image", capture)
    manager = Mock()
    manager.is_awaiting_plate_clear.return_value = False
    manager.get_printer.return_value = None
    monkeypatch.setattr(main, "printer_manager", manager)
    hint = Mock()
    monkeypatch.setattr(main, "set_expected_pause_reason", hint)
    ws = Mock(send_print_start=AsyncMock(), broadcast=AsyncMock())
    monkeypatch.setattr(main, "ws_manager", ws)
    notice = AsyncMock()
    monkeypatch.setattr(main.notification_service, "on_plate_not_empty", notice)
    for path in [
        "backend.app.main._send_print_start_notification",
        "backend.app.main.notify_missing_spool_assignments_on_print_start",
        "backend.app.services.macro_trigger.fire_event_macros",
        "backend.app.services.usage_tracker.on_print_start",
        "backend.app.main.smart_plug_manager.on_print_start",
    ]:
        monkeypatch.setattr(path, AsyncMock())
    # The plate check is this test's subject. Every print is archived, so the
    # handler would go on to create the archive against the mocked manager: it
    # stops owning the start as soon as the check has answered.
    real_check = main._check_plate_at_print_start

    async def check_then_lose_the_start(*args, **kwargs):
        try:
            return await real_check(*args, **kwargs)
        finally:
            manager.life_holds.return_value = False

    monkeypatch.setattr(main, "_check_plate_at_print_start", check_then_lose_the_start)
    data = {"filename": "Product A.gcode", "subtask_name": "Product A"}
    if job == "internal-path":
        data = {"filename": "/usr/etc/print/auto_cali_for_user.gcode", "subtask_name": "auto_cali_for_user"}
    elif job == "internal-subtask":
        data = {"filename": "", "subtask_name": "auto_pa_line_calib_mode"}
    await main._on_print_start_impl(p.id, data)

    if job != "normal":
        capture.assert_not_awaited()
        manager.get_client.return_value.pause_print.assert_not_called()
        ws.broadcast.assert_not_awaited()
        notice.assert_not_awaited()
        hint.assert_not_called()
        return

    unavailable = case not in ("clear", "occupied") and not (case == "resolution" and not polygon)
    paused = case == "occupied" or (polygon and unavailable)
    assert manager.get_client.return_value.pause_print.call_count == int(paused)
    assert ws.broadcast.await_count == int(paused)
    if paused:
        reason = "plate_check_unavailable" if unavailable else "plate_objects"
        hint.assert_called_once_with(p.id, reason)
        payload = ws.broadcast.await_args.args[0]
        assert payload["reason"] == reason
        assert ("Objects detected" in payload["message"]) == (not unavailable)
    assert notice.await_count == int(case == "occupied")
    if unavailable:
        assert "Plate is empty" not in caplog.text
        if not polygon:
            assert "legacy rectangle policy" in caplog.text
