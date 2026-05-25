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


@pytest.mark.asyncio
async def test_single_printer_update_appears_in_log(async_client, printer_factory):
    """A per-printer (legacy modal) update is recorded into the same log with
    source='single' so the update journal shows both mechanisms."""
    printer = await printer_factory(model="P1S")
    from backend.app.services.firmware_batch import record_single_update

    await record_single_update(
        printer.id, "P1S", from_version="01.00.00.00", to_version="01.02.00.00", status="uploaded"
    )

    r = await async_client.get("/api/v1/firmware/batch")
    assert r.status_code == 200
    runs = r.json()
    single = [run for run in runs if run["source"] == "single"]
    assert single, "single-source run must appear in the log"
    assert single[0]["items"][0]["to_version"] == "01.02.00.00"
    assert single[0]["created_at"]  # timestamp present for the journal
