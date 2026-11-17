"""Archive video work has an owner a restore can drain (G10 C4)."""

import ast
import asyncio
from pathlib import Path

import pytest

from backend.app.services import archive_media
from backend.app.services.archive_media_runtime import (
    ArchiveMediaRuntime,
    MediaAdmissionClosed,
    archive_media_runtime,
)

APP = Path(__file__).resolve().parents[2] / "app"


class TestAdmission:
    async def test_work_runs_and_answers(self):
        runtime = ArchiveMediaRuntime()

        async def work():
            return 7

        assert await runtime.own("x", work()) == 7
        assert runtime.in_flight() == 0

    async def test_closed_admission_refuses_and_never_starts_the_work(self):
        runtime = ArchiveMediaRuntime()
        started = []

        async def work():
            started.append(1)

        runtime.close_admission()
        with pytest.raises(MediaAdmissionClosed):
            await runtime.own("x", work())
        assert started == []

    async def test_the_works_own_failure_reaches_the_caller(self):
        runtime = ArchiveMediaRuntime()

        async def work():
            raise ValueError("disk")

        with pytest.raises(ValueError):
            await runtime.own("x", work())
        assert runtime.in_flight() == 0


class TestACancelledCaller:
    async def test_the_work_finishes_and_the_caller_is_held_until_it_has(self):
        runtime = ArchiveMediaRuntime()
        gate, entered, done = asyncio.Event(), asyncio.Event(), []

        async def work():
            entered.set()
            await gate.wait()
            done.append(1)

        caller = asyncio.create_task(runtime.own("publish:1", work()))
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()  # a second cancellation does not detach it either
        await asyncio.sleep(0)
        assert not caller.done(), "the caller would close its session under the running work"
        assert runtime.running() == ["publish:1"]

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert done == [1]
        assert runtime.in_flight() == 0


class TestQuiesce:
    async def test_it_waits_for_running_work_and_then_proves_the_drain(self):
        runtime = ArchiveMediaRuntime()
        gate, entered = asyncio.Event(), asyncio.Event()

        async def work():
            entered.set()
            await gate.wait()

        caller = asyncio.create_task(runtime.own("convert:1", work()))
        await entered.wait()
        drain = asyncio.create_task(runtime.quiesce(5))
        await asyncio.sleep(0)
        assert runtime.closed and not drain.done()
        gate.set()
        assert await drain is True
        await caller

    async def test_work_still_running_is_an_unproven_drain_and_is_not_cancelled(self):
        runtime = ArchiveMediaRuntime()
        gate, entered = asyncio.Event(), asyncio.Event()

        async def work():
            entered.set()
            await gate.wait()
            return "finished"

        caller = asyncio.create_task(runtime.own("convert:1", work()))
        await entered.wait()
        assert await runtime.quiesce(0.01) is False
        assert runtime.running() == ["convert:1"]
        with pytest.raises(RuntimeError):
            runtime.reopen()
        assert runtime.closed
        gate.set()
        assert await caller == "finished"
        runtime.reopen()
        assert runtime.admitting()

    async def test_nothing_new_starts_after_a_drain(self):
        runtime = ArchiveMediaRuntime()
        assert await runtime.quiesce(1) is True

        async def work():
            return 1

        with pytest.raises(MediaAdmissionClosed):
            await runtime.own("x", work())


class TestTheWritersAreOwned:
    """Each door answers a closed admission with its own refusal, and changes nothing."""

    async def test_publication_is_refused_and_its_staged_file_goes(self, db_session, tmp_path):
        staged = tmp_path / ".x.part"
        staged.write_bytes(b"v")
        archive_media_runtime.close_admission()
        result = await archive_media.publish_primary(
            db_session,
            1,
            staged=staged,
            directory=tmp_path,
            filename="a.mp4",
            base_dir=tmp_path,
            origin="upload",
            expected_identity="I",
            expected_epoch=0,
        )
        assert (result.ok, result.refusal) == (False, archive_media.REFUSED_RESTORING)
        assert not staged.exists()

    async def test_removal_is_refused(self, db_session, tmp_path):
        archive_media_runtime.close_admission()
        result = await archive_media.remove_primary(
            db_session, 1, base_dir=tmp_path, expected_identity="I", expected_epoch=0
        )
        assert (result.ok, result.refusal) == (False, archive_media.REFUSED_RESTORING)

    async def test_an_attach_is_refused_before_it_stages_a_byte(
        self, db_session, archive_factory, printer_factory, tmp_path, monkeypatch
    ):
        from backend.app.core.config import settings
        from backend.app.services.archive import ArchiveService

        monkeypatch.setattr(settings, "base_dir", tmp_path)
        archive = await archive_factory((await printer_factory()).id, file_path="")
        archive_id = archive.id
        archive_media_runtime.close_admission()
        service = ArchiveService(db_session)
        assert await service.attach_timelapse(archive_id, b"video", "a.mp4") is False
        assert service.media_refusal == archive_media.REFUSED_RESTORING
        assert [p for p in tmp_path.rglob("*") if p.is_file()] == []

    async def test_a_conversion_does_not_start(self, tmp_path, monkeypatch):
        from backend.app.services import archive as archive_module

        ran = []

        async def owned(*args, **kwargs):
            ran.append(1)

        monkeypatch.setattr(archive_module, "_convert_timelapse_to_mp4_owned", owned)
        archive_media_runtime.close_admission()
        await archive_module._convert_timelapse_to_mp4(1, tmp_path / "a.avi")
        assert ran == []

    async def test_a_running_attach_holds_the_drain(
        self, db_session, archive_factory, printer_factory, tmp_path, monkeypatch
    ):
        """The restore's question, end to end: an attach that began first is waited for, to its commit."""
        from backend.app.core.config import settings
        from backend.app.services.archive import ArchiveService

        monkeypatch.setattr(settings, "base_dir", tmp_path)
        archive = await archive_factory((await printer_factory()).id, file_path="")
        archive_id = archive.id
        real = archive_media._publish_primary
        entered, gate = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await gate.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(archive_media, "_publish_primary", slow)
        service = ArchiveService(db_session)
        attach = asyncio.create_task(service.attach_timelapse(archive_id, b"video", "a.mp4"))
        await entered.wait()
        drain = asyncio.create_task(archive_media_runtime.quiesce(5))
        await asyncio.sleep(0.05)
        assert not drain.done()
        gate.set()
        # Admitted before the restore asked: it runs to its commit, and only then is the drain proven.
        assert await attach is True
        assert await drain is True
        assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == ["a.mp4"]
        assert archive_media_runtime.in_flight() == 0


class TestTheRestoreAsks:
    def test_the_restore_drains_archive_media_after_printer_files(self):
        source = (APP / "api" / "routes" / "settings.py").read_text(encoding="utf-8")
        files = source.index("await printer_file_runtime.quiesce()")
        media = source.index("await archive_media_runtime.quiesce()")
        assert files < media
        assert "Archive video work has not stopped" in source[media : media + 300]

    def test_only_lifespan_and_the_test_bootstrap_reopen_admission(self):
        callers = []
        for path in APP.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "reopen"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "archive_media_runtime"
                ):
                    callers.append(path.name)
        assert callers == ["main.py"]

    def test_shutdown_waits_bounded_and_logs_an_unproven_end(self):
        source = (APP / "main.py").read_text(encoding="utf-8")
        at = source.index("await archive_media_runtime.quiesce(10)")
        assert "admission stays closed" in source[at : at + 300]
