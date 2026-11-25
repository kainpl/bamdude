"""Integration tests for the bulk firmware routes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.services import firmware_batch
from backend.app.services.bambu_ftp import FtpFailure


@pytest.mark.asyncio
async def test_start_batch_returns_run_id(async_client, printer_factory, monkeypatch):
    printer = await printer_factory(model="P1S")

    class FakeSvc:
        async def start_batch(self, targets, actor_id):
            assert len(targets) == 1
            assert targets[0].version == "01.02.03.04"
            return 42

    monkeypatch.setattr("backend.app.api.routes.firmware.firmware_batch_service", FakeSvc())
    monkeypatch.setattr(
        "backend.app.api.routes.firmware.get_firmware_service",
        lambda: SimpleNamespace(
            get_available_versions=AsyncMock(
                return_value=[SimpleNamespace(version="01.02.03.04", download_url="https://example.com/fw.bin")]
            )
        ),
    )
    monkeypatch.setattr("backend.app.api.routes.firmware.firmware_store.list_cached", AsyncMock(return_value=[]))

    r = await async_client.post(
        "/api/v1/firmware/batch",
        json={"targets": [{"printer_id": printer.id, "version": "01.02.03.04"}]},
    )
    assert r.status_code == 200
    assert r.json()["run_id"] == 42


@pytest.mark.asyncio
async def test_batch_preview_excludes_wiki_only_versions(async_client, printer_factory, monkeypatch):
    printer = await printer_factory(model="P2S")
    monkeypatch.setattr(
        "backend.app.api.routes.firmware.get_firmware_service",
        lambda: SimpleNamespace(
            get_available_versions=AsyncMock(
                return_value=[
                    SimpleNamespace(version="01.03.00.00", download_url=""),
                    SimpleNamespace(version="01.02.00.00", download_url="https://example.com/fw.bin"),
                    SimpleNamespace(version="01.01.03.00", download_url=""),
                ]
            )
        ),
    )
    monkeypatch.setattr(
        "backend.app.api.routes.firmware.firmware_store.list_cached",
        AsyncMock(return_value=[SimpleNamespace(version="01.01.03.00")]),
    )

    r = await async_client.post("/api/v1/firmware/batch/preview", json={"targets": [{"printer_id": printer.id}]})
    assert r.status_code == 200
    group = r.json()["groups"][0]
    assert group["available_versions"] == ["01.02.00.00", "01.01.03.00"]
    assert group["default_version"] == "01.02.00.00"
    assert group["cached_versions"] == ["01.01.03.00"]


@pytest.mark.asyncio
async def test_batch_rejects_wiki_only_firmware_before_creating_run(async_client, printer_factory, monkeypatch):
    printer = await printer_factory(model="P2S")
    start = AsyncMock()
    monkeypatch.setattr("backend.app.api.routes.firmware.firmware_batch_service", SimpleNamespace(start_batch=start))
    monkeypatch.setattr(
        "backend.app.api.routes.firmware.get_firmware_service",
        lambda: SimpleNamespace(
            get_available_versions=AsyncMock(return_value=[SimpleNamespace(version="01.03.00.00", download_url="")])
        ),
    )
    monkeypatch.setattr("backend.app.api.routes.firmware.firmware_store.list_cached", AsyncMock(return_value=[]))

    r = await async_client.post(
        "/api/v1/firmware/batch", json={"targets": [{"printer_id": printer.id, "version": "01.03.00.00"}]}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "firmware_file_unavailable"
    start.assert_not_called()


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


@pytest.mark.asyncio
async def test_bulk_upload_surfaces_byte_progress_queue_and_terminal_results(
    async_client, printer_factory, test_engine, monkeypatch, tmp_path
):
    first = await printer_factory(model="A1 Mini")
    second = await printer_factory(model="A1 Mini")
    file = tmp_path / "fw.zip"
    file.write_bytes(b"x" * 100)
    transferring = asyncio.Event()
    verifying = asyncio.Event()
    send_remaining = asyncio.Event()
    confirm = asyncio.Event()

    async def fake_upload(ip, code, local, remote, *, progress_callback, report, **kwargs):
        if ip == second.ip_address:
            report.failure = FtpFailure("storage", code="553")
            return False
        await asyncio.to_thread(progress_callback, 42, 100)
        transferring.set()
        await send_remaining.wait()
        await asyncio.to_thread(progress_callback, 100, 100)
        verifying.set()
        await confirm.wait()
        return True

    service = firmware_batch.FirmwareBatchService()
    monkeypatch.setattr("backend.app.api.routes.firmware.firmware_batch_service", service)
    monkeypatch.setattr(firmware_batch, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(firmware_batch, "_get_concurrency", AsyncMock(return_value=1))
    monkeypatch.setattr(firmware_batch, "_is_printing", lambda _: False)
    monkeypatch.setattr(firmware_batch, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30)))
    monkeypatch.setattr(firmware_batch, "upload_file_async", fake_upload)
    monkeypatch.setattr(
        firmware_batch.firmware_store,
        "get_or_download",
        AsyncMock(return_value=SimpleNamespace(path=file, filename=file.name)),
    )
    monkeypatch.setattr(firmware_batch, "_broadcast", AsyncMock())
    run_id = await service.start_batch(
        [firmware_batch.BatchTarget(printer.id, "A1 Mini", "01.09.00.00") for printer in (first, second)],
        actor_id=None,
    )
    task = service._tasks[run_id]
    try:
        await asyncio.wait_for(transferring.wait(), timeout=5)
        active = (await async_client.get("/api/v1/firmware/batch?active_only=true")).json()
        assert [run["id"] for run in active] == [run_id]
        items = {item["printer_id"]: item for item in active[0]["items"]}
        assert (items[first.id]["status"], items[first.id]["percent"]) == ("uploading", 42)
        assert (items[second.id]["status"], items[second.id]["percent"]) == ("pending", None)
        send_remaining.set()
        await asyncio.wait_for(verifying.wait(), timeout=5)
        run = (await async_client.get(f"/api/v1/firmware/batch/{run_id}")).json()
        assert next(item for item in run["items"] if item["printer_id"] == first.id)["percent"] == 99
        confirm.set()
        await asyncio.wait_for(task, timeout=5)
        run = (await async_client.get(f"/api/v1/firmware/batch/{run_id}")).json()
        items = {item["printer_id"]: item for item in run["items"]}
        assert (run["status"], run["succeeded"], run["failed"]) == ("completed", 1, 1)
        assert (items[first.id]["status"], items[first.id]["percent"]) == ("uploaded", 100)
        assert items[second.id]["status"] == "failed" and "553" in items[second.id]["error"]
        assert (await async_client.get("/api/v1/firmware/batch?active_only=true")).json() == []
    finally:
        send_remaining.set()
        confirm.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=5)
