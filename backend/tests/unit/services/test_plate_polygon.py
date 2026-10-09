import cv2
import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError

from backend.app.schemas.plate_detection import PlatePolygon
from backend.app.services import plate_detection

POLYGON = [{"x": 0.1, "y": 0.1}, {"x": 0.8, "y": 0.15}, {"x": 0.65, "y": 0.8}, {"x": 0.2, "y": 0.7}]


@pytest.fixture(autouse=True)
def real_opencv_backend(monkeypatch):
    # Existing availability tests reload this module with mocked imports. Use
    # real image operations here and restore the previous state after each test.
    monkeypatch.setattr(plate_detection, "cv2", cv2)
    monkeypatch.setattr(plate_detection, "np", np)
    monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", True)


@pytest.mark.parametrize(
    "points",
    [
        [],
        POLYGON[:2],
        POLYGON + [POLYGON[0]],
        [{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}, {"x": 1, "y": 0}],
        [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}, {"x": 0.3, "y": 0.3}],
        [{"x": float("nan"), "y": 0}, *POLYGON],
        [{"x": 2, "y": 0}, *POLYGON],
    ],
)
def test_invalid_contours_are_rejected(points):
    with pytest.raises(ValidationError):
        TypeAdapter(PlatePolygon).validate_python(points)


def test_concave_and_both_windings_are_allowed():
    concave = [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0.4, "y": 0.4}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
    assert TypeAdapter(PlatePolygon).validate_python(concave)
    assert TypeAdapter(PlatePolygon).validate_python(concave[::-1])


def jpeg(frame):
    return cv2.imencode(".png", frame)[1].tobytes()


def prepare(tmp_path, monkeypatch):
    from backend.app.services import plate_detection

    monkeypatch.setattr(plate_detection, "_get_calibration_dir", lambda: tmp_path)
    image = np.repeat(np.tile(np.linspace(50, 180, 240, dtype=np.uint8), (180, 1))[:, :, None], 3, axis=2)
    # PNG bytes stored with .jpg filename: imread detects the actual format.
    (tmp_path / "printer_1_ref_0.jpg").write_bytes(jpeg(image))
    return image, plate_detection.PlateDetector(polygon=POLYGON)


def test_outside_change_cannot_leak_through_blur_or_normalization(tmp_path, monkeypatch):
    image, detector = prepare(tmp_path, monkeypatch)
    changed = image.copy()
    changed[detector._polygon_mask(image) == 0] = 255
    result = detector.analyze_frame(jpeg(changed), 1, include_debug_image=True)
    assert result.is_empty
    assert result.difference_percent == pytest.approx(0, abs=0.0001)
    assert result.debug_image


def test_inside_object_is_detected(tmp_path, monkeypatch):
    image, detector = prepare(tmp_path, monkeypatch)
    cv2.rectangle(image, (70, 55), (120, 100), (0, 0, 0), -1)
    result = detector.analyze_frame(jpeg(image), 1)
    assert not result.is_empty
    assert result.difference_percent > 1


def test_mask_at_image_edge_and_too_small_raster_fail_closed(tmp_path, monkeypatch):
    image, _ = prepare(tmp_path, monkeypatch)
    full = plate_detection.PlateDetector(
        polygon=[{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
    )
    assert np.count_nonzero(full._polygon_mask(image)) == 180 * 240
    tiny = plate_detection.PlateDetector(polygon=[{"x": 0.1, "y": 0.1}, {"x": 0.11, "y": 0.1}, {"x": 0.1, "y": 0.11}])
    assert not tiny.analyze_frame(jpeg(image), 1).is_empty


def test_camera_resolution_change_does_not_resize_polygon_reference(tmp_path, monkeypatch):
    image, detector = prepare(tmp_path, monkeypatch)
    result = detector.analyze_frame(jpeg(cv2.resize(image, (120, 90))), 1)
    assert not result.is_empty
    assert result.needs_calibration


def test_rectangle_preprocessing_remains_unchanged():
    image = np.arange(120 * 160 * 3, dtype=np.uint8).reshape(120, 160, 3)
    expected = cv2.normalize(
        cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (51, 51), 0), None, 0, 255, cv2.NORM_MINMAX
    )
    assert np.array_equal(plate_detection.PlateDetector()._preprocess_for_comparison(image), expected)


def test_polygon_never_reports_empty_on_missing_or_corrupt_inputs(tmp_path, monkeypatch):
    image, detector = prepare(tmp_path, monkeypatch)
    assert not detector.analyze_frame(b"broken", 1).is_empty
    assert not detector.analyze_frame(jpeg(image), 999).is_empty
    (tmp_path / "printer_1_ref_0.jpg").write_bytes(b"broken")
    assert not detector.analyze_frame(jpeg(image), 1).is_empty


@pytest.mark.asyncio
async def test_checker_returns_original_frame_only_when_requested(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from backend.app.services import plate_detection

    image, _ = prepare(tmp_path, monkeypatch)
    raw = jpeg(image)
    monkeypatch.setattr(plate_detection, "capture_camera_image", AsyncMock(return_value=(raw, "synthetic")))
    result = await plate_detection.check_plate_empty(1, "192.0.2.1", "test", "A1M", polygon=POLYGON)
    assert result.is_empty
    assert result.source_image is None
    result = await plate_detection.check_plate_empty(
        1, "192.0.2.1", "test", "A1M", polygon=POLYGON, include_debug_image=True
    )
    assert result.source_image == raw
    assert result.debug_image != raw


@pytest.mark.asyncio
async def test_polygon_capture_failure_and_unavailable_opencv_do_not_report_empty(monkeypatch):
    from unittest.mock import AsyncMock

    from backend.app.services import plate_detection

    monkeypatch.setattr(plate_detection, "capture_camera_image", AsyncMock(return_value=(None, "none")))
    result = await plate_detection.check_plate_empty(1, "192.0.2.1", "test", "A1M", polygon=POLYGON)
    assert not result.is_empty
    monkeypatch.setattr(plate_detection, "OPENCV_AVAILABLE", False)
    result = await plate_detection.check_plate_empty(1, "192.0.2.1", "test", "A1M", polygon=POLYGON)
    assert not result.is_empty
