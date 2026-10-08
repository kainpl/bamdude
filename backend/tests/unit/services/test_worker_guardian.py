"""The shared guardian must keep preview and camera bootstrap formats separate."""

import base64
import json

import pytest

from backend.app.worker_guardian import _child_bootstrap


def _line(payload: dict) -> bytes:
    return json.dumps(payload).encode() + b"\n"


def test_preview_wire_stays_newline_json():
    module, frame = _child_bootstrap(_line({"module": "backend.app.preview_render", "root": "cache"}))
    assert module == "backend.app.preview_render"
    assert json.loads(frame) == {"root": "cache"}
    assert frame.endswith(b"\n")


def test_camera_wire_stays_length_prefixed():
    payload = b'{"generation":"test"}'
    frame = len(payload).to_bytes(4, "big") + payload
    module, relayed = _child_bootstrap(
        _line({"module": "backend.app.camera_worker", "camera_frame": base64.b64encode(frame).decode()})
    )
    assert module == "backend.app.camera_worker"
    assert relayed == frame


@pytest.mark.parametrize(
    "envelope",
    [
        {"module": "os"},
        {"module": "backend.app.camera_worker", "camera_frame": "!"},
        {"module": "backend.app.camera_worker", "camera_frame": base64.b64encode(b"\0\0\0\x04a").decode()},
        {"module": "backend.app.camera_worker", "camera_frame": "", "root": "unexpected"},
    ],
)
def test_rejects_untrusted_entrypoint_or_camera_frame(envelope):
    with pytest.raises(ValueError):
        _child_bootstrap(_line(envelope))


def test_preview_limit_is_not_raised_to_camera_limit():
    with pytest.raises(ValueError, match="preview bootstrap too large"):
        _child_bootstrap(_line({"module": "backend.app.preview_service", "padding": "x" * 16384}))


def test_the_part_render_modules_are_allowed():
    from backend.app.worker_guardian import _PREVIEW_MODULES

    assert {"backend.app.part_render_service", "backend.app.part_render"} <= _PREVIEW_MODULES


def test_the_guardian_records_part_render_before_its_bootstrap(tmp_path):
    """Plan E3, R12: child.launch before the spawn, child.pid before the child has its bootstrap."""
    import json
    import time

    from backend.app.services.preview_process import PreviewProcess

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    missing = {
        "path": str(tmp_path / "gone.3mf"),
        "root": str(tmp_path),
        "kind": "3mf",
        "sha256": "0" * 64,
        "size": 1,
        "plate_index": 1,
    }
    boot = {"root": str(attempt), "task": missing, "deadline_ns": time.monotonic_ns() + 30 * 10**9, "node": None}
    child = PreviewProcess("backend.app.part_render", boot, tmp_path / "cache")
    try:
        assert child.process.wait(timeout=60) == 0
    finally:
        child.stop()
    assert (attempt / "child.launch").exists()
    recorded = json.loads((attempt / "child.pid").read_text(encoding="ascii"))
    assert recorded["pid"] != child.process.pid  # the part_render process, recorded by its guardian
    assert json.loads((attempt / "result.json").read_text(encoding="utf-8"))["reason"] == "source_read_failed"
