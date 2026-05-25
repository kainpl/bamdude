"""Tests for the bulk firmware orchestrator."""

import types

import pytest

from backend.app.services import firmware_batch


def _fake_item(item_id, printer_id, model, to_version="v2"):
    return types.SimpleNamespace(id=item_id, printer_id=printer_id, model=model, to_version=to_version)


@pytest.mark.asyncio
async def test_groups_by_model_downloads_once_skips_printing_continues_on_failure(monkeypatch):
    downloads: list[tuple[str, str]] = []

    async def fake_get_or_download(model, version, progress_callback=None):
        downloads.append((model, version))
        return types.SimpleNamespace(path=f"/tmp/{model}.bin", filename=f"{model}.bin")

    monkeypatch.setattr(firmware_batch.firmware_store, "get_or_download", fake_get_or_download)

    # printer 2 is printing → skipped; printer 3 FTP fails → failed; 1 ok.
    monkeypatch.setattr(firmware_batch, "_is_printing", lambda pid: pid == 2)

    async def fake_ftp(item, sf):
        if item.printer_id == 3:
            raise RuntimeError("ftp boom")

    monkeypatch.setattr(firmware_batch, "_ftp_upload", fake_ftp)

    # No DB: monkeypatch the persistence + broadcast seams.
    async def noop_set_item(item_id, **fields):
        return None

    async def noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(firmware_batch, "_set_item", noop_set_item)
    monkeypatch.setattr(firmware_batch, "_broadcast", noop_broadcast)

    targets = [
        firmware_batch.BatchTarget(printer_id=1, model="P1S", version="v2", from_version="v1"),
        firmware_batch.BatchTarget(printer_id=2, model="P1S", version="v2", from_version="v1"),
        firmware_batch.BatchTarget(printer_id=3, model="X1C", version="v9", from_version="v8"),
    ]
    items_by_printer = {
        1: _fake_item(101, 1, "P1S", "v2"),
        2: _fake_item(102, 2, "P1S", "v2"),
        3: _fake_item(103, 3, "X1C", "v9"),
    }

    outcome = await firmware_batch._run_targets(
        run_id=1, targets=targets, items_by_printer=items_by_printer, concurrency=2
    )

    assert sorted(downloads) == [("P1S", "v2"), ("X1C", "v9")]  # once per model
    assert outcome["1"] == "uploaded"
    assert outcome["2"] == "skipped"
    assert outcome["3"] == "failed"


@pytest.mark.asyncio
async def test_model_download_failure_fails_only_that_model(monkeypatch):
    async def fake_get_or_download(model, version, progress_callback=None):
        if model == "X1C":
            return None  # download/cache unavailable for this model only
        return types.SimpleNamespace(path=f"/tmp/{model}.bin", filename=f"{model}.bin")

    monkeypatch.setattr(firmware_batch.firmware_store, "get_or_download", fake_get_or_download)
    monkeypatch.setattr(firmware_batch, "_is_printing", lambda pid: False)

    async def fake_ftp(item, sf):
        return None

    monkeypatch.setattr(firmware_batch, "_ftp_upload", fake_ftp)

    async def noop_set_item(item_id, **fields):
        return None

    async def noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(firmware_batch, "_set_item", noop_set_item)
    monkeypatch.setattr(firmware_batch, "_broadcast", noop_broadcast)

    targets = [
        firmware_batch.BatchTarget(printer_id=1, model="P1S", version="v2"),
        firmware_batch.BatchTarget(printer_id=2, model="X1C", version="v9"),
    ]
    items_by_printer = {1: _fake_item(1, 1, "P1S", "v2"), 2: _fake_item(2, 2, "X1C", "v9")}

    outcome = await firmware_batch._run_targets(
        run_id=1, targets=targets, items_by_printer=items_by_printer, concurrency=2
    )
    assert outcome["1"] == "uploaded"  # P1S unaffected
    assert outcome["2"] == "failed"  # X1C download failure isolated
