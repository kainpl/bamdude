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
