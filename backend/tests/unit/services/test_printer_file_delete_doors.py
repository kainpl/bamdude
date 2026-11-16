"""Every door that deletes on the printer keeps the file a job reads (G10 B, M07–M09).

The rule itself is ``test_printer_file_guard``; here each door is shown to ask
it at the command, after its wait, and to read what the transport answered.
"""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import printer_file_guard as guard
from backend.app.services.bambu_ftp import DeleteResult


@pytest.fixture
def printer(monkeypatch):
    """One observed printer whose state the test changes mid-operation."""
    box = SimpleNamespace(
        state=SimpleNamespace(connected=True, state="FINISH", gcode_file="/sdcard/A.3mf", subtask_name="A"),
        expected={},
    )
    monkeypatch.setattr(guard.printer_manager, "peek_status", lambda _pid: (box.state, 1.0, False))
    monkeypatch.setattr(guard, "_expected_archive", lambda _pid, path: box.expected.get(path.rsplit("/", 1)[-1]))
    monkeypatch.setattr(guard, "_printer_expects_a_print", lambda _pid: bool(box.expected))

    def start(name="A", state="PREPARE"):
        box.state = SimpleNamespace(connected=True, state=state, gcode_file=f"/sdcard/{name}.3mf", subtask_name=name)

    box.start = start
    return box


class TestAPrintThatStartsDuringTheWait:
    """M07/M08: B begins under A's name, with A's bytes, while the delete waits for the file channel."""

    async def test_the_card_delete_is_not_sent(self, printer, monkeypatch):
        from backend.app.services import bambu_ftp

        reached, release = threading.Event(), threading.Event()
        client = MagicMock()

        def connect():
            reached.set()
            assert release.wait(5)
            return True

        client.connect.side_effect = connect
        monkeypatch.setattr(bambu_ftp, "BambuFTPClient", MagicMock(return_value=client))
        task = asyncio.create_task(
            bambu_ftp.delete_file_async("b-door-delete", "secret", "/A.3mf", may_run=guard.may_delete(1, "/A.3mf"))
        )
        try:
            assert await asyncio.to_thread(reached.wait, 5)
            printer.start("A")  # the same name: an external reprint of the same file
        finally:
            release.set()
        assert await task == DeleteResult.FAILED
        client.delete_file.assert_not_called()
        client.disconnect.assert_called_once()

    async def test_the_internal_delete_is_not_sent(self, printer, monkeypatch):
        from backend.app.services.printer_files.tunnel import TunnelTransport

        client = SimpleNamespace(delete_files=AsyncMock())

        @asynccontextmanager
        async def connection():
            printer.start("A")
            yield client

        transport = TunnelTransport(SimpleNamespace(ip_address="b-door-tunnel", access_code="secret"))
        monkeypatch.setattr(transport, "_client", connection)
        path = "/userdata/model/history/A.gcode.3mf"
        assert await transport.delete(path, may_run=guard.may_delete(1, path)) == DeleteResult.FAILED
        client.delete_files.assert_not_awaited()

    async def test_a_matching_hash_does_not_free_the_file(self, printer):
        """The content proof passes — B prints the same 3MF — and the copy still stays."""
        from backend.app.services.printer_cleanup import remove_verified_copies

        data = b"the same sliced file"

        async def read(_path):
            printer.start("A")
            return data

        delete = AsyncMock(return_value=DeleteResult.DELETED)
        removed = await remove_verified_copies(
            entries=[{"name": "A.gcode.3mf", "path": "/cache/A.gcode.3mf"}],
            wanted={"A.gcode.3mf"},
            expected_hashes={hashlib.sha256(data).hexdigest()},
            read_bytes=read,
            delete=lambda p: delete(p) if guard.may_delete(1, p)() else _refused(),
            label="test",
            may_run=guard.may_delete(1, "/cache/A.gcode.3mf"),
        )
        assert removed == 0
        delete.assert_not_awaited()

    async def test_the_lookup_on_internal_storage_asks_per_file(self, printer):
        from backend.app.services.background_dispatch import delete_internal_by_name

        async def listing(_path):
            printer.start("A")
            return [SimpleNamespace(name="A.3mf", path="/userdata/model/history/A.3mf")]

        asked = []

        async def delete(path, *, may_run=None):
            asked.append(path)
            return DeleteResult.DELETED if may_run is None or may_run() else DeleteResult.FAILED

        transport = SimpleNamespace(list_files=listing, delete=delete)
        assert not await delete_internal_by_name(transport, "A.3mf", gate=lambda p: guard.may_delete(1, p))
        assert asked == ["/userdata/model/history/A.3mf"]


async def _refused():
    return DeleteResult.FAILED


class TestTheAnswerIsRead:
    async def test_a_copy_the_printer_did_not_delete_is_not_counted(self):
        from backend.app.services.printer_cleanup import remove_verified_copies

        data = b"x"
        for answer, expected in ((DeleteResult.FAILED, 0), (DeleteResult.NOT_FOUND, 0), (DeleteResult.DELETED, 1)):
            removed = await remove_verified_copies(
                entries=[{"name": "A.gcode.3mf", "path": "/cache/A.gcode.3mf"}],
                wanted={"A.gcode.3mf"},
                expected_hashes={hashlib.sha256(data).hexdigest()},
                read_bytes=AsyncMock(return_value=data),
                delete=AsyncMock(return_value=answer),
                label="test",
            )
            assert removed == expected, answer

    async def test_a_recording_the_printer_kept_is_not_reported_removed(self, printer):
        from backend.app.services.timelapse_files import remove_recording_after_attach

        device = SimpleNamespace(id=1, name="X2D", model="X2D")
        for answer, expected in ((DeleteResult.FAILED, False), (DeleteResult.DELETED, True)):
            transport = SimpleNamespace(delete=AsyncMock(return_value=answer))
            with (
                patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
                patch("backend.app.services.printer_files.factory.transport_for", return_value=transport),
            ):
                assert await remove_recording_after_attach(device, "/timelapse/video.mp4") is expected
            assert transport.delete.await_args.kwargs["may_run"]() is True

    async def test_a_recording_is_not_removed_from_a_printer_nobody_hears(self, printer):
        from backend.app.services.timelapse_files import remove_recording_after_attach

        printer.state.connected = False
        seen = {}

        async def delete(path, *, may_run=None):
            seen["allowed"] = may_run()
            return DeleteResult.DELETED if seen["allowed"] else DeleteResult.FAILED

        device = SimpleNamespace(id=1, name="X2D", model="X2D")
        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
            patch(
                "backend.app.services.printer_files.factory.transport_for",
                return_value=SimpleNamespace(delete=delete),
            ),
        ):
            assert await remove_recording_after_attach(device, "/timelapse/video.mp4") is False
        assert seen == {"allowed": False}


class TestTheCardClear:
    def _client(self, tree, deleted, removed_dirs):
        from backend.app.services.bambu_ftp import BambuFTPClient

        client = BambuFTPClient("b-door-clear", "secret")
        client._ftp = object()
        client.list_files = lambda path: tree.get(path, [])

        def delete_file(path):
            deleted.append(path)
            return DeleteResult.DELETED

        def remove_dir(path):
            removed_dirs.append(path)
            return True

        client.delete_file = delete_file
        client.remove_dir = remove_dir
        return client

    def _tree(self):
        def f(path):
            return {"name": path.rsplit("/", 1)[-1], "path": path, "is_directory": False}

        def d(path):
            return {"name": path.rsplit("/", 1)[-1], "path": path, "is_directory": True}

        return {
            "/": [f("/a.3mf"), d("/cache"), f("/b.3mf")],
            "/cache": [f("/cache/c.gcode"), d("/cache/deep"), f("/cache/e.bbl")],
            "/cache/deep": [f("/cache/deep/x")],
        }

    def test_a_whole_walk_reports_that_it_was_not_stopped(self):
        deleted, dirs = [], []
        result = self._client(self._tree(), deleted, dirs).clear_all("/", lambda: True)
        assert result == {"deleted": 5, "failed": 0, "folders_removed": 1, "stopped": False}
        assert dirs == ["/cache/deep"]

    def test_a_print_that_becomes_expected_stops_the_walk_at_once(self):
        """Nothing after the refusal: no further file, and no folder either."""
        deleted, dirs = [], []
        answers = iter([True, True])  # /a.3mf goes, entering /cache is allowed, then a print is expected
        result = self._client(self._tree(), deleted, dirs).clear_all("/", lambda: next(answers, False))
        assert deleted == ["/a.3mf"]
        assert dirs == []
        assert result["stopped"] is True and result["deleted"] == 1 and result["failed"] == 0

    async def test_a_print_that_started_during_the_wait_empties_nothing(self, printer, monkeypatch):
        from backend.app.services import bambu_ftp

        made = MagicMock()
        monkeypatch.setattr(bambu_ftp, "BambuFTPClient", made)
        printer.start("B", state="RUNNING")
        summary = await bambu_ftp.clear_sdcard_async("b-door-wipe", "secret", may_run=guard.may_delete_wide(1))
        assert summary == {"deleted": 0, "failed": 0, "folders_removed": 0, "stopped": True, "connected": True}
        made.assert_not_called()  # not even a connection


class TestTheDispatcher:
    """A place in the file channel is no right to delete: J waited, B began under the same name."""

    def _job(self, **over):
        from backend.app.services.background_dispatch import PrintDispatchJob

        job = MagicMock(spec=PrintDispatchJob)
        job.printer_id = 1
        job.printer_life = None
        job.execution_archive_id = over.get("archive_id", 50)
        return job

    def _gate(self, job):
        from backend.app.services.background_dispatch import BackgroundDispatchService

        return BackgroundDispatchService._file_gate(job)

    async def _clear(self, gate, storage="external", transport=None):
        from backend.app.services.background_dispatch import BackgroundDispatchService

        await BackgroundDispatchService._clear_remote_name(
            transport or SimpleNamespace(),
            storage,
            printer_ip="b-door-dispatch",
            access_code="secret",
            remote_path="/A.3mf",
            remote_filename="A.3mf",
            ftp_timeout=None,
            printer_model=None,
            gate=gate,
        )

    async def test_the_same_named_file_of_a_running_print_ends_the_upload(self, printer):
        from backend.app.services import background_dispatch
        from backend.app.services.background_dispatch import UploadFailed

        printer.start("A", state="RUNNING")
        with (
            patch.object(background_dispatch, "delete_file_async", new=AsyncMock()) as delete,
            pytest.raises(UploadFailed, match="belongs to another print"),
        ):
            await self._clear(self._gate(self._job()))
        delete.assert_not_awaited()

    async def test_a_print_that_began_while_the_clear_waited_ends_the_upload(self, printer):
        from backend.app.services import background_dispatch
        from backend.app.services.background_dispatch import UploadFailed

        async def delete(*_a, may_run=None, **_k):
            printer.start("A")  # B is observed while J waits for the lane
            return DeleteResult.DELETED if may_run() else DeleteResult.FAILED

        with (
            patch.object(background_dispatch, "delete_file_async", new=delete),
            pytest.raises(UploadFailed, match="belongs to another print"),
        ):
            await self._clear(self._gate(self._job()))

    async def test_a_free_name_is_cleared_as_before(self, printer):
        from backend.app.services import background_dispatch

        with patch.object(
            background_dispatch, "delete_file_async", new=AsyncMock(return_value=DeleteResult.NOT_FOUND)
        ) as delete:
            await self._clear(self._gate(self._job()))
        assert delete.await_args.kwargs["may_run"]() is True

    async def test_a_silent_printer_ends_the_upload(self, printer, monkeypatch):
        """Review B2: a cached IDLE proves nothing about a print started since the connection dropped."""
        from backend.app.services import background_dispatch
        from backend.app.services.background_dispatch import UploadFailed

        idle = SimpleNamespace(connected=False, state="FINISH", gcode_file="", subtask_name="")
        monkeypatch.setattr(guard.printer_manager, "peek_status", lambda _pid: (idle, None, True))
        with (
            patch.object(background_dispatch, "delete_file_async", new=AsyncMock()) as delete,
            pytest.raises(UploadFailed, match="state is not known"),
        ):
            await self._clear(self._gate(self._job()))
        delete.assert_not_awaited()

    @pytest.mark.parametrize(
        "observed",
        [
            (SimpleNamespace(connected=False, state="IDLE", gcode_file="", subtask_name=""), 1.0, False),
            (SimpleNamespace(connected=True, state="IDLE", gcode_file="", subtask_name=""), 1.0, True),
            (SimpleNamespace(connected=True, state="IDLE", gcode_file="", subtask_name=""), None, False),
            (SimpleNamespace(connected=True, state="unknown", gcode_file="", subtask_name=""), 1.0, False),
            (None, None, False),
        ],
        ids=["disconnected", "stale", "never-reported", "unknown-state", "no-client"],
    )
    def test_no_destructive_site_runs_without_a_fresh_safe_state(self, printer, monkeypatch, observed):
        """The same gate serves the pre-upload clear, the /cache sweep, the upload and the failure cleanup."""
        monkeypatch.setattr(guard.printer_manager, "peek_status", lambda _pid: observed)
        gate = self._gate(self._job())
        for path in ("/A.3mf", "/cache/A.3mf", "/cache/1_A.bbl"):
            assert gate(path)() is False
        assert gate.refused == "printer_state_unknown"

    async def test_a_state_that_goes_stale_during_the_wait_stops_the_command(self, printer, monkeypatch):
        from backend.app.services import bambu_ftp

        reached, release = threading.Event(), threading.Event()
        client = MagicMock()

        def connect():
            reached.set()
            assert release.wait(5)
            return True

        client.connect.side_effect = connect
        monkeypatch.setattr(bambu_ftp, "BambuFTPClient", MagicMock(return_value=client))
        gate = self._gate(self._job())
        task = asyncio.create_task(
            bambu_ftp.delete_file_async("b-door-stale", "secret", "/cache/A.3mf", may_run=gate("/cache/A.3mf"))
        )
        try:
            assert await asyncio.to_thread(reached.wait, 5)
            printer.state.connected = False  # fresh and safe on entry, silent by the time the lane is ours
        finally:
            release.set()
        assert await task == DeleteResult.FAILED
        client.delete_file.assert_not_called()
        assert gate.refused == "printer_state_unknown"

    async def test_a_stale_state_that_named_the_file_as_loaded_still_keeps_it(self, printer, monkeypatch):
        from backend.app.services.background_dispatch import UploadFailed

        stale = SimpleNamespace(connected=False, state="RUNNING", gcode_file="A.3mf", subtask_name="A")
        monkeypatch.setattr(guard.printer_manager, "peek_status", lambda _pid: (stale, 1.0, True))
        with pytest.raises(UploadFailed):
            await self._clear(self._gate(self._job()))

    def test_another_dispatchs_expected_file_is_kept_and_its_own_is_not(self, printer):
        printer.expected = {"A.3mf": 50}
        assert self._gate(self._job(archive_id=50))("/A.3mf")() is True
        assert self._gate(self._job(archive_id=51))("/A.3mf")() is False

    def test_a_printer_record_that_was_replaced_is_not_touched(self, printer, monkeypatch):
        job = self._job()
        job.printer_life = 2
        from backend.app.services import background_dispatch

        monkeypatch.setattr(background_dispatch.printer_manager, "printer_life_is_current", lambda _pid, life: False)
        assert self._gate(job)("/Other.3mf")() is False

    async def test_the_working_copies_in_cache_are_kept_while_a_job_is_loaded(self, printer):
        printer.start("B", state="RUNNING")
        gate = self._gate(self._job())
        assert gate("/cache/A.3mf")() is True  # a stale copy of ours, B does not read it
        assert gate("/cache/1_B.bbl")() is False
        assert gate("/cache/B.3mf")() is False

    async def test_the_failure_cleanup_passes_its_gate_to_the_delete(self, printer):
        from backend.app.services import background_dispatch
        from backend.app.services.background_dispatch import BackgroundDispatchService

        printer.start("A", state="RUNNING")
        gate = self._gate(self._job())("/A.3mf")
        with patch.object(background_dispatch, "delete_file_async", new=AsyncMock()) as delete:
            await BackgroundDispatchService._cleanup_sd_card_file("ip", "code", "/A.3mf", None, may_run=gate)
        assert delete.await_args.kwargs["may_run"]() is False


class TestThePreUploadClearOnInternalStorage:
    """Review B1: nothing returns normally with the name still protected or still there."""

    def _gate(self):
        from backend.app.services.background_dispatch import BackgroundDispatchService, PrintDispatchJob

        job = MagicMock(spec=PrintDispatchJob)
        job.id, job.printer_id, job.printer_life, job.execution_archive_id = 1, 1, None, 50
        return BackgroundDispatchService._file_gate(job)

    async def _clear(self, transport, gate):
        from backend.app.services.background_dispatch import BackgroundDispatchService

        await BackgroundDispatchService._clear_remote_name(
            transport,
            "internal",
            printer_ip="b-door-internal",
            access_code="secret",
            remote_path="/A.3mf",
            remote_filename="A.3mf",
            ftp_timeout=None,
            printer_model=None,
            gate=gate,
        )

    def _transport(self, delete, entries=None):
        listed = [SimpleNamespace(name="A.3mf", path="/userdata/model/history/A.3mf")] if entries is None else entries
        return SimpleNamespace(list_files=AsyncMock(return_value=listed), delete=delete)

    async def test_a_print_that_begins_during_the_delete_ends_the_upload(self, printer):
        from backend.app.services.background_dispatch import UploadFailed

        async def delete(path, *, may_run):
            printer.start("A", state="RUNNING")
            return DeleteResult.DELETED if may_run() else DeleteResult.FAILED

        with pytest.raises(UploadFailed, match="belongs to another print"):
            await self._clear(self._transport(delete), self._gate())

    async def test_the_refusal_survives_a_state_that_looks_safe_again(self, printer):
        """The print that kept the file has ended by the time the guard is read again."""
        from backend.app.services.background_dispatch import UploadFailed

        async def delete(path, *, may_run):
            printer.start("A", state="RUNNING")
            allowed = may_run()
            printer.state.state = "FINISH"
            return DeleteResult.DELETED if allowed else DeleteResult.FAILED

        gate = self._gate()
        with pytest.raises(UploadFailed, match="belongs to another print"):
            await self._clear(self._transport(delete), gate)
        assert gate("/A.3mf")() is True  # a fresh read would have let the upload through

    async def test_a_file_the_printer_did_not_delete_ends_the_upload(self, printer):
        from backend.app.services.background_dispatch import UploadFailed

        for answer in (DeleteResult.FAILED, DeleteResult.NOT_FOUND, None):
            with pytest.raises(UploadFailed, match="did not delete it"):
                await self._clear(self._transport(AsyncMock(return_value=answer)), self._gate())

    async def test_a_delete_that_raises_ends_the_upload(self, printer):
        from backend.app.services.background_dispatch import UploadFailed

        with pytest.raises(UploadFailed, match="did not delete it"):
            await self._clear(self._transport(AsyncMock(side_effect=OSError("tunnel reset"))), self._gate())

    async def test_a_name_that_is_not_there_lets_the_upload_go_on(self, printer):
        delete = AsyncMock()
        await self._clear(self._transport(delete, entries=[SimpleNamespace(name="Other.3mf", path="/x")]), self._gate())
        delete.assert_not_awaited()

    async def test_a_deleted_file_lets_the_upload_go_on(self, printer):
        delete = AsyncMock(return_value=DeleteResult.DELETED)
        await self._clear(self._transport(delete), self._gate())
        assert delete.await_args.kwargs["may_run"]() is True

    async def test_a_printer_record_that_was_replaced_ends_the_upload(self, printer, monkeypatch):
        from backend.app.services import background_dispatch
        from backend.app.services.background_dispatch import UploadFailed

        gate = self._gate()
        gate._job.printer_life = 2
        monkeypatch.setattr(background_dispatch.printer_manager, "printer_life_is_current", lambda _pid, _life: False)
        delete = AsyncMock()
        with pytest.raises(UploadFailed):
            await self._clear(self._transport(delete), gate)
        delete.assert_not_awaited()

    async def test_the_lookup_tells_missing_from_kept(self, printer):
        from backend.app.services.background_dispatch import (
            INTERNAL_DELETED,
            INTERNAL_KEPT,
            INTERNAL_MISSING,
            INTERNAL_UNLISTED,
            clear_internal_by_name,
            delete_internal_by_name,
        )

        found = [SimpleNamespace(name="A.3mf", path="/h/A.3mf")]

        def transport(answer=None, entries=found, listing_error=None):
            listing = AsyncMock(side_effect=listing_error) if listing_error else AsyncMock(return_value=entries)
            return SimpleNamespace(list_files=listing, delete=AsyncMock(return_value=answer))

        assert await clear_internal_by_name(transport(DeleteResult.DELETED), "A.3mf") == INTERNAL_DELETED
        assert await clear_internal_by_name(transport(DeleteResult.FAILED), "A.3mf") == INTERNAL_KEPT
        assert await clear_internal_by_name(transport(None), "A.3mf") == INTERNAL_KEPT
        assert await clear_internal_by_name(transport(entries=[]), "A.3mf") == INTERNAL_MISSING
        assert await clear_internal_by_name(transport(listing_error=OSError("x")), "A.3mf") == INTERNAL_UNLISTED
        # The old bool: only a real deletion is "removed".
        assert await delete_internal_by_name(transport(DeleteResult.DELETED), "A.3mf") is True
        for answer in (DeleteResult.FAILED, DeleteResult.NOT_FOUND, None):
            assert await delete_internal_by_name(transport(answer), "A.3mf") is False


class TestTheUploadAsksTheSameGate:
    """Writing over a name destroys the file under it exactly as a delete would."""

    async def test_the_card_upload_sends_nothing_once_a_print_has_begun(self, printer, monkeypatch, tmp_path):
        from backend.app.services import bambu_ftp

        reached, release = threading.Event(), threading.Event()
        client = MagicMock()

        def connect():
            reached.set()
            assert release.wait(5)
            return True

        client.connect.side_effect = connect
        monkeypatch.setattr(bambu_ftp, "BambuFTPClient", MagicMock(return_value=client))
        source = tmp_path / "A.3mf"
        source.write_bytes(b"x")
        task = asyncio.create_task(
            bambu_ftp.upload_file_async(
                "b-door-upload", "secret", source, "/A.3mf", may_run=guard.may_delete(1, "/A.3mf")
            )
        )
        try:
            assert await asyncio.to_thread(reached.wait, 5)
            printer.start("A")
        finally:
            release.set()
        assert await task is False
        client.upload_file.assert_not_called()
        client.disconnect.assert_called_once()

    async def test_the_card_upload_does_not_even_connect_when_refused_on_entry(self, printer, monkeypatch, tmp_path):
        from backend.app.services import bambu_ftp

        made = MagicMock()
        monkeypatch.setattr(bambu_ftp, "BambuFTPClient", made)
        printer.start("A", state="RUNNING")
        source = tmp_path / "A.3mf"
        source.write_bytes(b"x")
        assert (
            await bambu_ftp.upload_file_async(
                "b-door-upload-2", "secret", source, "/A.3mf", may_run=guard.may_delete(1, "/A.3mf")
            )
            is False
        )
        made.assert_not_called()

    async def test_the_internal_upload_sends_nothing_once_a_print_has_begun(self, printer, monkeypatch, tmp_path):
        from backend.app.services.printer_files.tunnel import TunnelTransport

        client = SimpleNamespace(upload_file=AsyncMock())

        @asynccontextmanager
        async def connection():
            printer.start("A")
            yield client

        transport = TunnelTransport(SimpleNamespace(ip_address="b-door-tunnel-up", access_code="secret"))
        monkeypatch.setattr(transport, "_client", connection)
        assert await transport.upload(tmp_path / "A.3mf", "A.3mf", may_run=guard.may_delete(1, "/A.3mf")) is False
        client.upload_file.assert_not_awaited()

    async def test_a_refused_upload_is_not_retried_as_a_failure(self, printer):
        from backend.app.services.background_dispatch import BackgroundDispatchService, PrintDispatchJob, UploadFailed
        from backend.app.services.bambu_ftp import with_ftp_retry

        job = MagicMock(spec=PrintDispatchJob)
        job.id, job.printer_id, job.printer_life, job.execution_archive_id = 1, 1, None, 50
        gate = BackgroundDispatchService._file_gate(job)
        attempts = []

        async def upload(*_args, may_run=None, **_kwargs):
            attempts.append(1)
            printer.start("A", state="RUNNING")
            return bool(may_run())

        with pytest.raises(UploadFailed, match="belongs to another print"):
            await with_ftp_retry(
                gate.unless_refused(upload),
                max_retries=3,
                retry_delay=0.01,
                non_retry_exceptions=(UploadFailed,),
                may_run=gate("/A.3mf"),
            )
        assert attempts == [1]

    async def test_a_plain_failure_is_still_retried(self, printer):
        from backend.app.services.background_dispatch import BackgroundDispatchService, PrintDispatchJob, UploadFailed
        from backend.app.services.bambu_ftp import with_ftp_retry

        job = MagicMock(spec=PrintDispatchJob)
        job.id, job.printer_id, job.printer_life, job.execution_archive_id = 1, 1, None, 50
        gate = BackgroundDispatchService._file_gate(job)
        attempts = []

        async def upload(*_args, **_kwargs):
            attempts.append(1)
            return len(attempts) == 3

        assert await with_ftp_retry(
            gate.unless_refused(upload), max_retries=3, retry_delay=0.01, non_retry_exceptions=(UploadFailed,)
        )
        assert len(attempts) == 3


class TestTheCleanupSaysWhatHappened:
    """Review B3: a kept or failed step is not logged or counted as done."""

    async def test_an_answer_of_nothing_is_not_a_removal(self):
        from backend.app.services.printer_cleanup import remove_verified_copies

        data = b"x"
        removed = await remove_verified_copies(
            entries=[{"name": "A.gcode.3mf", "path": "/cache/A.gcode.3mf"}],
            wanted={"A.gcode.3mf"},
            expected_hashes={hashlib.sha256(data).hexdigest()},
            read_bytes=AsyncMock(return_value=data),
            delete=AsyncMock(return_value=None),
            label="test",
        )
        assert removed == 0

    async def test_the_failure_cleanup_says_when_the_file_may_still_be_there(self, caplog):
        from backend.app.services import background_dispatch
        from backend.app.services.background_dispatch import BackgroundDispatchService

        for answer, logged in (
            (DeleteResult.FAILED, True),
            (DeleteResult.DELETED, False),
            (DeleteResult.NOT_FOUND, False),
        ):
            caplog.clear()
            with (
                caplog.at_level("INFO", logger=background_dispatch.logger.name),
                patch.object(background_dispatch, "delete_file_async", new=AsyncMock(return_value=answer)),
            ):
                await BackgroundDispatchService._cleanup_sd_card_file("ip", "code", "/A.3mf", None)
            assert ("may still be there" in caplog.text) is logged, answer
