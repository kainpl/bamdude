"""Where a streamed model import is staged, how many run at once, and what a dead process leaves (review MB-I4)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app.core.config import settings as app_settings
from backend.app.services import model_import


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app_settings, "base_dir", tmp_path, raising=False)
    return tmp_path


def test_staging_lives_in_the_apps_data_directory_not_the_system_temp(data_dir):
    staging = model_import.staging_dir()
    assert staging == data_dir / "model-imports" and staging.is_dir()
    staged = model_import.new_staging_file()
    assert staged.parent == staging and staged.name.startswith(model_import.STAGING_PREFIX)
    assert staged.suffix == ".part" and not staged.exists()


def test_the_sweep_removes_only_this_features_old_leftovers(data_dir):
    staging = model_import.staging_dir()
    old = time.time() - model_import.STALE_STAGING_SECONDS - 60
    stale = staging / f"{model_import.STAGING_PREFIX}dead.part"
    fresh = staging / f"{model_import.STAGING_PREFIX}running.part"
    foreign = staging / "somebody-elses.part"
    folder = staging / f"{model_import.STAGING_PREFIX}folder.part"
    for path in (stale, fresh, foreign):
        path.write_bytes(b"x")
    folder.mkdir()
    (folder / "kept").write_bytes(b"x")
    for path in (stale, foreign, folder):
        os.utime(path, (old, old))
    outside = data_dir / f"{model_import.STAGING_PREFIX}outside.part"
    outside.write_bytes(b"x")
    os.utime(outside, (old, old))
    assert model_import.sweep_stale_staging() == 1
    assert not stale.exists()
    assert fresh.exists() and foreign.exists() and (folder / "kept").exists() and outside.exists()


def test_the_sweep_of_a_missing_directory_is_nothing(data_dir):
    assert not (data_dir / "model-imports").exists()
    assert model_import.sweep_stale_staging(create=False) == 0
    assert not (data_dir / "model-imports").exists()


async def test_at_most_two_imports_stream_at_once_and_the_rest_wait():
    assert model_import.MAX_CONCURRENT_IMPORTS == 2
    inside: list[int] = []
    peak = [0]
    release = asyncio.Event()

    async def one(index: int) -> None:
        async with model_import.import_slot():
            inside.append(index)
            peak[0] = max(peak[0], len(inside))
            await release.wait()
            inside.remove(index)

    tasks = [asyncio.create_task(one(index)) for index in range(5)]
    await asyncio.sleep(0.05)
    assert len(inside) == 2
    assert model_import.import_load() == {"active": 2, "waiting": 3, "limit": 2}
    release.set()
    await asyncio.gather(*tasks)
    assert peak[0] == 2
    assert model_import.import_load() == {"active": 0, "waiting": 0, "limit": 2}


async def test_a_wait_for_a_slot_is_bounded_and_answers_503(monkeypatch):
    monkeypatch.setattr(model_import, "IMPORT_QUEUE_WAIT_SECONDS", 0.05)
    async with model_import.import_slot(), model_import.import_slot():
        with pytest.raises(HTTPException) as caught:
            async with model_import.import_slot():
                pytest.fail("a third import ran")
    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == "source_import_busy"
    assert caught.value.headers["Retry-After"]
    assert model_import.import_load()["waiting"] == 0
    async with model_import.import_slot():
        pass  # the slots came back


async def test_a_cancelled_waiter_gives_nothing_away():
    async with model_import.import_slot(), model_import.import_slot():

        async def waiter():
            async with model_import.import_slot():
                pass

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert model_import.import_load() == {"active": 0, "waiting": 0, "limit": 2}


def test_space_is_asked_for_before_a_download(monkeypatch):
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**12, 10**12, 50 * 1024 * 1024))
    with pytest.raises(HTTPException) as caught:
        model_import.ensure_space(10 * 1024 * 1024)
    assert (caught.value.status_code, caught.value.detail["error"]) == (507, "storage_full")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**12, 0, 10**11))
    model_import.ensure_space(10 * 1024 * 1024)
    model_import.ensure_space(None)


class TestRemovingAStagingFileThatIsStillHeld:
    """Review MB-M10: on Windows a worker thread still copying the staging file refuses its unlink."""

    def held(self, monkeypatch, times: int) -> list[int]:
        calls: list[int] = []
        real = Path.unlink

        def unlink(self, missing_ok=False):
            calls.append(1)
            if len(calls) <= times:
                raise PermissionError(13, "The process cannot access the file because it is being used")
            return real(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        return calls

    def test_it_is_tried_again_until_the_copy_lets_go(self, data_dir, monkeypatch):
        staged = model_import.new_staging_file()
        staged.write_bytes(b"x")
        calls = self.held(monkeypatch, 2)
        assert model_import.remove_staged(staged, pause=0.01) is True
        assert len(calls) == 3 and not staged.exists()

    def test_a_file_that_stays_held_is_left_to_the_sweep_without_an_error(self, data_dir, monkeypatch):
        staged = model_import.new_staging_file()
        staged.write_bytes(b"x")
        self.held(monkeypatch, 10**6)
        assert model_import.remove_staged(staged, attempts=3, pause=0.01) is False
        assert staged.exists() and staged.name.startswith(model_import.STAGING_PREFIX)

    async def test_a_cancelled_import_stays_cancelled_when_its_file_is_held(self, data_dir, monkeypatch):
        """The refusal to unlink used to REPLACE the cancellation."""
        staged = model_import.new_staging_file()
        staged.write_bytes(b"x")
        self.held(monkeypatch, 10**6)
        monkeypatch.setattr(model_import, "REMOVE_ATTEMPTS", 2)
        monkeypatch.setattr(model_import, "REMOVE_PAUSE_SECONDS", 0.01)

        async def importing():
            async with model_import.staging(staged):
                await asyncio.sleep(30)

        task = asyncio.create_task(importing())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_the_staging_file_is_removed_on_success_and_on_failure(self, data_dir):
        first, second = model_import.new_staging_file(), model_import.new_staging_file()
        async with model_import.staging(first):
            first.write_bytes(b"x")
        with pytest.raises(RuntimeError):
            async with model_import.staging(second):
                second.write_bytes(b"x")
                raise RuntimeError("the download failed")
        assert not first.exists() and not second.exists()
