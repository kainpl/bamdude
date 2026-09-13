import uuid

import pytest

from backend.app.services.camera_worker_capture import WorkerCaptureCommand
from backend.app.services.camera_worker_protocol import CameraWorkerProtocolError


def test_external_capture_command_roundtrips_without_exposing_url_credentials():
    command = WorkerCaptureCommand(
        kind="external",
        purpose="snapshot",
        timeout_ms=15_000,
        media_session_id=str(uuid.uuid4()),
        url="http://operator:secret@camera.example/snapshot.jpg",
        camera_type="snapshot",
    )
    assert WorkerCaptureCommand.from_payload(command.to_payload()) == command
    assert "operator:secret" not in repr(command)


def test_capture_command_rejects_an_unexpected_or_malformed_payload():
    with pytest.raises(CameraWorkerProtocolError, match="invalid schema"):
        WorkerCaptureCommand.from_payload({})

    with pytest.raises(ValueError, match="timeout"):
        WorkerCaptureCommand(
            kind="builtin",
            purpose="snapshot",
            timeout_ms=121_000,
            media_session_id=str(uuid.uuid4()),
            ip_address="192.0.2.10",
            access_code="secret",
        )
