"""Integration tests for the bulk firmware routes."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_start_batch_returns_run_id(async_client, printer_factory, monkeypatch):
    printer = await printer_factory(model="P1S")

    class FakeSvc:
        async def start_batch(self, targets, actor_id):
            assert len(targets) == 1
            assert targets[0].version == "01.02.03.04"
            return 42

    monkeypatch.setattr("backend.app.api.routes.firmware.firmware_batch_service", FakeSvc())

    r = await async_client.post(
        "/api/v1/firmware/batch",
        json={"targets": [{"printer_id": printer.id, "version": "01.02.03.04"}]},
    )
    assert r.status_code == 200
    assert r.json()["run_id"] == 42


@pytest.mark.asyncio
async def test_start_batch_400_when_no_eligible_printers(async_client, monkeypatch):
    # An unknown printer id resolves to nothing → no eligible targets.
    r = await async_client.post(
        "/api/v1/firmware/batch",
        json={"targets": [{"printer_id": 999999, "version": "01.02.03.04"}]},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_batch_404_for_missing_run(async_client):
    r = await async_client.get("/api/v1/firmware/batch/999999")
    assert r.status_code == 404
