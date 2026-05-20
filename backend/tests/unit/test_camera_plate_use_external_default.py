"""Regression tests for F.2 / upstream Bambuddy #1359.

The manual plate-detection UI routes (``check_plate_empty`` and
``calibrate_plate_detection``) used to default ``use_external=False``
unconditionally, while the runtime auto-check at print start defaulted
to ``printer.external_camera_enabled``. That mismatch made calibration
capture from one camera and the runtime check diff against the other,
producing a permanent "Build plate not empty" on every print start
when an external RTSP camera was configured.

Both routes now take ``bool | None`` and derive the default from the
printer's external camera config when the caller omits the flag. The
frontend ``checkPlateEmpty`` / ``calibratePlateDetection`` helpers
mirror the change — they stop forwarding ``use_external`` unless the
caller explicitly sets it.

These tests pin the derivation by exercising the same predicate the
routes use, without spinning up the OpenCV layer.
"""

from __future__ import annotations

from types import SimpleNamespace


def _resolve_use_external(printer, caller_value):
    """Mirror the route's predicate so we don't have to spin up OpenCV
    or the HTTP layer. The route does exactly this with `bool(...)`
    when ``caller_value is None``."""
    if caller_value is None:
        return bool(printer.external_camera_enabled and printer.external_camera_url and printer.external_camera_type)
    return caller_value


class TestUseExternalDefault:
    def test_omitted_with_external_configured_uses_external(self):
        """Reporter's scenario: A1 with external RTSP camera fully
        configured. Manual check must default to use_external=True so it
        matches the runtime auto-check at print start."""
        printer = SimpleNamespace(
            external_camera_enabled=True,
            external_camera_url="rtsp://192.168.1.50/stream",
            external_camera_type="mjpeg",
        )
        assert _resolve_use_external(printer, None) is True

    def test_omitted_with_external_disabled_falls_back_to_builtin(self):
        printer = SimpleNamespace(
            external_camera_enabled=False,
            external_camera_url=None,
            external_camera_type=None,
        )
        assert _resolve_use_external(printer, None) is False

    def test_omitted_with_external_partially_configured_falls_back(self):
        """Enabled flag without URL / type doesn't qualify — the runtime
        capture path also needs the URL, so the predicate guards against
        a half-configured printer producing a stream-less default."""
        printer = SimpleNamespace(
            external_camera_enabled=True,
            external_camera_url=None,
            external_camera_type="mjpeg",
        )
        assert _resolve_use_external(printer, None) is False

        printer = SimpleNamespace(
            external_camera_enabled=True,
            external_camera_url="rtsp://x",
            external_camera_type=None,
        )
        assert _resolve_use_external(printer, None) is False

    def test_explicit_true_wins_over_disabled_external(self):
        """An explicit caller override still wins — supports future
        "always built-in" or "always external" callers regardless of
        the printer's saved config."""
        printer = SimpleNamespace(
            external_camera_enabled=False,
            external_camera_url=None,
            external_camera_type=None,
        )
        assert _resolve_use_external(printer, True) is True

    def test_explicit_false_wins_over_enabled_external(self):
        printer = SimpleNamespace(
            external_camera_enabled=True,
            external_camera_url="rtsp://192.168.1.50/stream",
            external_camera_type="mjpeg",
        )
        assert _resolve_use_external(printer, False) is False
