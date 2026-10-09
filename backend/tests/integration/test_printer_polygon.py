import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
POINTS = [{"x": 0.1, "y": 0.2}, {"x": 0.8, "y": 0.2}, {"x": 0.7, "y": 0.8}, {"x": 0.2, "y": 0.8}]


async def test_polygon_roundtrip_switch_and_unrelated_patch(async_client, printer_factory):
    printer = await printer_factory()
    url = f"/api/v1/printers/{printer.id}"
    rsp = await async_client.patch(url, json={"plate_detection_polygon": POINTS})
    assert rsp.status_code == 200, rsp.text
    assert rsp.json()["plate_detection_polygon"] == POINTS
    rsp = await async_client.patch(url, json={"name": "Polygon test"})
    assert rsp.json()["plate_detection_polygon"] == POINTS
    listed = await async_client.get("/api/v1/printers/")
    assert next(p for p in listed.json() if p["id"] == printer.id)["plate_detection_polygon"] == POINTS
    rsp = await async_client.patch(url, json={"plate_detection_polygon": None})
    assert rsp.json()["plate_detection_polygon"] is None


async def test_invalid_polygon_does_not_replace_valid_setting(async_client, printer_factory):
    printer = await printer_factory(plate_detection_polygon=POINTS)
    rsp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"plate_detection_polygon": POINTS[:2]})
    assert rsp.status_code == 422
    listed = await async_client.get("/api/v1/printers/")
    assert next(p for p in listed.json() if p["id"] == printer.id)["plate_detection_polygon"] == POINTS


async def test_manual_check_uses_persisted_polygon_and_returns_original_frame(
    async_client, printer_factory, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.app.services import plate_detection

    printer = await printer_factory(plate_detection_polygon=POINTS)
    result = plate_detection.PlateDetectionResult(True, 1, 0, "empty")
    result.source_image = b"original-jpeg"
    check = AsyncMock(return_value=result)
    monkeypatch.setattr(plate_detection, "check_plate_empty", check)
    monkeypatch.setattr(plate_detection, "is_plate_detection_available", lambda: True)
    monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", True)
    rsp = await async_client.get(f"/api/v1/printers/{printer.id}/camera/check-plate?include_debug_image=true")
    assert rsp.status_code == 200, rsp.text
    assert check.call_args.kwargs["polygon"] == POINTS
    assert rsp.json()["polygon"] == POINTS
    assert rsp.json()["source_image_url"] == "data:image/jpeg;base64,b3JpZ2luYWwtanBlZw=="
