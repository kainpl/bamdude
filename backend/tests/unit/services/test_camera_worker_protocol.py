import io
import json
import struct
import uuid

import pytest

from backend.app.services.camera_worker_protocol import (
    MAX_BOOTSTRAP_BYTES,
    CameraWorkerProtocolError,
    WorkerBootstrap,
    make_reply,
    make_request,
    read_bootstrap,
    validate_reply,
)


def test_bootstrap_roundtrip_is_length_prefixed_and_hides_secret_in_repr():
    bootstrap = WorkerBootstrap.create(control_port=40123, media_port=40124)
    encoded = bootstrap.to_bytes()
    length = struct.unpack(">I", encoded[:4])[0]
    assert length == len(encoded) - 4
    assert read_bootstrap(io.BytesIO(encoded)) == bootstrap
    assert bootstrap.secret.hex() not in repr(bootstrap)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        json.dumps({"version": 999}).encode(),
        b"x" * (MAX_BOOTSTRAP_BYTES + 1),
    ],
    ids=["empty", "array", "wrong-schema", "oversize"],
)
def test_bootstrap_rejects_invalid_or_oversized_input(payload):
    framed = struct.pack(">I", len(payload)) + payload
    with pytest.raises(CameraWorkerProtocolError):
        read_bootstrap(io.BytesIO(framed))


def test_reply_requires_matching_generation_request_and_fixed_error_enum():
    generation, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    reply = make_reply(generation=generation, request_id=request_id, ok=False, error="unknown_operation")
    assert validate_reply(reply, generation=generation, request_id=request_id)["error"] == "unknown_operation"
    with pytest.raises(CameraWorkerProtocolError):
        validate_reply(reply, generation=str(uuid.uuid4()), request_id=request_id)
    reply["operation"] = "status"
    with pytest.raises(CameraWorkerProtocolError, match="not a reply"):
        validate_reply(reply, generation=generation, request_id=request_id)


def test_control_request_uses_versioned_object_schema():
    generation, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    request = make_request(generation=generation, request_id=request_id, operation="status", payload={})
    assert request == {
        "version": 1,
        "generation": generation,
        "request_id": request_id,
        "operation": "status",
        "payload": {},
    }
