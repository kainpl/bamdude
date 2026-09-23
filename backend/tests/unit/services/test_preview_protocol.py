"""Untrusted local messages must be rejected before opening any artifact."""

import uuid

import pytest

from backend.app.services.preview_protocol import Command, PreviewError, decode, encode


def command():
    return {
        "protocol_version": 1,
        "app_generation": "a",
        "service_epoch": "b",
        "attempt_id": uuid.uuid4().hex,
        "attempt_seq": 1,
        "operation": "run",
        "deadline_monotonic_ns": 123,
        "manifest": [],
    }


def test_roundtrip():
    value = command()
    assert Command.parse(decode(encode(value)), "a", "b").wire() == value


@pytest.mark.parametrize(
    "change",
    [
        {"protocol_version": 2},
        {"protocol_version": True},
        {"service_epoch": "old"},
        {"app_generation": "old"},
        {"attempt_id": "../secret"},
        {"attempt_seq": True},
        {"attempt_seq": 0},
        {"operation": "exec"},
        {"path": "secret"},
        {"manifest": {}},
    ],
)
def test_closed_envelope(change):
    with pytest.raises(PreviewError):
        Command.parse({**command(), **change}, "a", "b")


def test_artifact_cannot_address_another_attempt():
    value = command()
    value["manifest"] = [{"role": "mesh", "key": "other_mesh", "kind": "stl", "size": 10, "digest": "a" * 64}]
    with pytest.raises(PreviewError):
        Command.parse(value, "a", "b")


@pytest.mark.parametrize("payload", [b"[]", b"null", b"{", b" " * 16385], ids=["array", "null", "invalid", "oversized"])
def test_invalid_json(payload):
    with pytest.raises(PreviewError):
        decode(payload)


@pytest.mark.asyncio
async def test_service_failed_cleanup_cannot_leave_permanent_busy():
    import time
    from unittest.mock import AsyncMock

    from backend.app.preview_service import Service

    service = Service({})
    value = command()
    value["deadline_monotonic_ns"] = time.monotonic_ns() + 5_000_000_000
    value["operation"] = "run"
    value["manifest"] = [
        {"role": "mesh", "key": f"{value['attempt_id']}_mesh", "kind": "stl", "size": 1, "digest": "a" * 64}
    ]
    service.run = AsyncMock(side_effect=OSError("cleanup I/O failure"))
    assert await service.command(Command.parse(value, "a", "b")) == {"outcome": "unavailable"}
    assert service.active is None and service.task is None
    value["attempt_seq"] += 1
    service.run = AsyncMock(return_value={"outcome": "ok"})
    assert await service.command(Command.parse(value, "a", "b")) == {"outcome": "ok"}
