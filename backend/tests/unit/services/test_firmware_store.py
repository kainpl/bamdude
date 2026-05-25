"""Tests for the durable indexed firmware store."""

import hashlib

import pytest

from backend.app.services import firmware_store


@pytest.mark.asyncio
async def test_get_or_download_returns_cached_by_model_version_without_url(tmp_path, monkeypatch):
    """A version already in the index is returned even when the vendor has
    dropped its download_url — the whole point of the durable store."""
    monkeypatch.setattr(firmware_store, "_firmware_dir", lambda: tmp_path)

    blob = b"fake-firmware-bytes"
    fpath = tmp_path / "fw-01.00.00.00.bin"
    fpath.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()

    async def fake_find_cached(model, version):
        return firmware_store.StoredFirmware(
            model=model,
            version=version,
            filename=fpath.name,
            path=fpath,
            sha256=sha,
            size_bytes=len(blob),
            source_url=None,
            release_notes=None,
        )

    monkeypatch.setattr(firmware_store, "_find_cached", fake_find_cached)

    # No firmware service needed — must not be called on a cache hit.
    def boom():
        raise AssertionError("get_firmware_service must not be called on a cache hit")

    monkeypatch.setattr(firmware_store, "get_firmware_service", boom)

    result = await firmware_store.get_or_download("P1S", "01.00.00.00")
    assert result is not None
    assert result.path == fpath
    assert result.sha256 == sha
    assert result.source_url is None


@pytest.mark.asyncio
async def test_get_or_download_downloads_and_indexes_on_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(firmware_store, "_firmware_dir", lambda: tmp_path)

    async def no_cache(model, version):
        return None

    monkeypatch.setattr(firmware_store, "_find_cached", no_cache)

    blob = b"x" * 2048
    fpath = tmp_path / "fw.bin"
    fpath.write_bytes(blob)

    class FakeInfo:
        download_url = "https://example/fw.bin"
        release_notes = "notes"

    class FakeSvc:
        async def get_version_info(self, m, v):
            return FakeInfo()

        async def download_firmware(self, m, version=None, progress_callback=None):
            return fpath

    monkeypatch.setattr(firmware_store, "get_firmware_service", lambda: FakeSvc())

    recorded = {}

    async def fake_record(sf):
        recorded["sf"] = sf

    monkeypatch.setattr(firmware_store, "_record", fake_record)

    result = await firmware_store.get_or_download("P1S", "01.02.03.04")
    assert result.version == "01.02.03.04"
    assert result.size_bytes == 2048
    assert result.source_url == "https://example/fw.bin"
    assert recorded["sf"].sha256 == result.sha256


@pytest.mark.asyncio
async def test_get_or_download_returns_none_when_uncached_and_no_url(monkeypatch):
    async def no_cache(model, version):
        return None

    monkeypatch.setattr(firmware_store, "_find_cached", no_cache)

    class FakeInfo:
        download_url = ""  # vendor dropped it and we have no cache
        release_notes = None

    class FakeSvc:
        async def get_version_info(self, m, v):
            return FakeInfo()

    monkeypatch.setattr(firmware_store, "get_firmware_service", lambda: FakeSvc())

    assert await firmware_store.get_or_download("P1S", "99.99.99.99") is None


@pytest.mark.asyncio
async def test_available_versions_includes_cached_only(monkeypatch):
    """A version held only in the local store (vendor dropped it) still appears
    in the available list, flagged cached, so the operator can roll back to it."""
    from backend.app.services import firmware_check as fc, firmware_store as fs

    svc = fc.get_firmware_service()

    async def fake_online(model):
        return [fc.FirmwareVersion(version="01.02.00.00", download_url="https://x/a.bin")]

    monkeypatch.setattr(svc, "_get_available_versions_online", fake_online)

    async def fake_cached(model):
        return [
            fs.StoredFirmware(
                model="P1S",
                version="01.01.00.00",
                filename="old.bin",
                path=None,
                sha256="sha",
                size_bytes=1,
                source_url=None,
                release_notes="old",
            )
        ]

    monkeypatch.setattr(fs, "list_cached", fake_cached)

    versions = await svc.get_available_versions("P1S")
    by_version = {v.version: v for v in versions}
    assert "01.01.00.00" in by_version  # cached-only surfaced
    assert by_version["01.01.00.00"].cached is True
    assert by_version["01.02.00.00"].cached is False
