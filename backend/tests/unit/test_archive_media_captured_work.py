"""Late work never wins, and a claim, a path or a list proves only what it was taken for (G10 C, review r1).

One class per closure of the C2–C4 review:

* C2-1 — whoever starts captures identity, epoch and the video shown BEFORE its I/O;
* C2-2 — a printer recording's claim outlives the video made from it;
* C2-3 — the recording is deleted from the printer only as the very bytes an archive holds;
* C2-4 — a manual door's rights are asked again inside the publication;
* C2-5 — a stored path authorises no unlink outside the data directory;
* C3-1 — the run proof reaches the publication itself and the printer delete;
* C3-2 — a stored baseline is addressed, and a later capture never destroys a proven one.

A file-backed SQLite of its own: two sessions here must really be two connections.
"""

import asyncio
import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.services import archive_media, queue_sources, timelapse_baseline as tb
from backend.app.services.archive import ArchiveService
from backend.app.services.archive_media import RemoteSource
from backend.tests.schema_image import load_schema
from backend.tests.unit.services.test_printer_file_delete_doors import printer as guarded_printer  # noqa: F401
from backend.tests.unit.test_archive_media_publication import _archive, _pointer, _publish, _rows, _token_of

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def test_engine(tmp_path, monkeypatch):
    from backend.app.core import database
    from backend.app.core.credential_config import initialize_credential_config_anchor

    database.import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'media.db').as_posix()}")
    event.listen(engine.sync_engine, "connect", database.configure_sqlite_connection)
    await load_schema(engine)
    async with engine.begin() as conn:
        await initialize_credential_config_anchor(conn)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr("backend.app.core.auth.async_session", factory)
    monkeypatch.setattr("backend.app.services.long_lived_tokens.async_session", factory)
    yield engine
    await engine.dispose()


@pytest.fixture
def base(tmp_path, monkeypatch):
    (tmp_path / "archive").mkdir()
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    return tmp_path


def _fake_encoder(monkeypatch) -> list[Path]:
    """ffmpeg's double: writes the target it was given. Every launch is recorded."""
    from backend.app.services import camera

    monkeypatch.setattr(camera, "get_ffmpeg_path", lambda: "fake-ffmpeg")
    launched: list[Path] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        if "fake-ffmpeg" not in args:
            return await real_spawn(*args, **kwargs)
        source, target = Path(args[args.index("-i") + 1]), Path(args[-1])
        launched.append(source)

        class Process:
            returncode = 0

            async def communicate(self):
                target.write_bytes(source.read_bytes() + b"-converted")
                return b"", b""

        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return launched


REMOTE = RemoteSource("printer-identity", "external", "/timelapse/record.avi")


class TestWhatItStartsFromIsCapturedBeforeTheIO:
    """C2-1."""

    @pytest.mark.parametrize("during_read", ["replace", "epoch"])
    async def test_an_upload_does_not_win_over_what_happened_while_it_was_read(
        self, db_session, base, monkeypatch, during_read
    ):
        from fastapi import HTTPException

        from backend.app.api.routes import archives as routes

        archive = await _archive(db_session)
        newer = None

        async def read():
            nonlocal newer
            if during_read == "replace":
                newer, _ = await _publish(db_session, base, archive, name="new.mp4", data=b"new")
                assert newer.ok
            else:
                queue_sources.invalidate_receipts()
            return b"late upload"

        upload = AsyncMock()
        upload.filename = "slow.mp4"
        upload.read.side_effect = read
        monkeypatch.setattr(routes.ws_manager, "send_archive_updated", AsyncMock())

        with pytest.raises(HTTPException) as refused:
            await routes.upload_timelapse(archive.id, upload, db=db_session, _=None)

        assert refused.value.status_code == 409
        assert refused.value.detail["error"] == ("source_changed" if newer else "target_gone")
        assert await _pointer(db_session, archive.id) == (newer.relative_path if newer else None)

    async def test_an_attach_that_started_first_cannot_replace_a_newer_commit(self, db_session, base, monkeypatch):
        """The ordinary caller hands no token in: the attach captures on entry, before it stages a byte."""
        archive = await _archive(db_session)
        entered, release = asyncio.Event(), asyncio.Event()
        real = asyncio.to_thread
        first = True

        async def stage_barrier(fn, *args, **kwargs):
            nonlocal first
            if getattr(fn, "__name__", "") == "_stage" and first:
                first = False
                entered.set()
                await release.wait()
            return await real(fn, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", stage_barrier)
        factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
        async with factory() as slow_db:
            service = ArchiveService(slow_db)
            slow = asyncio.create_task(service.attach_timelapse(archive.id, b"old-work", "slow.mp4"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                newer, _ = await _publish(db_session, base, archive, name="new.mp4", data=b"new-work")
                assert newer.ok
            finally:
                release.set()
            assert await asyncio.wait_for(slow, 5) is False
            assert service.media_refusal == archive_media.REFUSED_SOURCE_CHANGED

        assert await _pointer(db_session, archive.id) == newer.relative_path
        assert (base / newer.relative_path).read_bytes() == b"new-work"
        assert not list((base / "archive").rglob("*.part")), "the refused attach removed its staged file"

    async def test_the_seam_has_no_default_for_the_token(self, db_session, base):
        staged = base / "archive" / ".x.part"
        staged.write_bytes(b"v")
        with pytest.raises(TypeError, match="expected_token"):
            await archive_media.publish_primary(
                db_session,
                1,
                staged=staged,
                directory=base / "archive",
                filename="a.mp4",
                base_dir=base,
                origin="upload",
                expected_identity="I",
                expected_epoch=queue_sources.current_epoch(),
            )

    @pytest.mark.parametrize("change", ["replace", "remove"])
    async def test_a_conversion_queued_for_replaced_bytes_does_not_start(self, db_session, base, monkeypatch, change):
        """The old AVI can outlive its row — an open handle keeps it on disk. Its converter must not adopt
        whatever the archive shows when it finally runs."""
        from backend.app.services.archive import _convert_timelapse_to_mp4

        archive = await _archive(db_session)
        old, _ = await _publish(db_session, base, archive, name="old.avi", data=b"old")
        source = base / old.relative_path
        real_unlink = Path.unlink

        def busy_file(path, *args, **kwargs):
            if path == source:
                raise PermissionError("the old file is still open")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", busy_file)
        if change == "replace":
            result, _ = await _publish(db_session, base, archive, name="new.mp4", data=b"new")
        else:
            result = await archive_media.remove_primary(
                db_session,
                archive.id,
                base_dir=base,
                expected_identity=archive.recovery_identity,
                expected_epoch=queue_sources.current_epoch(),
            )
        assert result.ok and source.exists()
        shown = await _pointer(db_session, archive.id)
        launched = _fake_encoder(monkeypatch)

        await _convert_timelapse_to_mp4(
            archive.id,
            source,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )

        assert await _pointer(db_session, archive.id) == shown
        assert launched == [], "ffmpeg was not even started for bytes the archive no longer shows"

    async def test_a_conversion_is_bound_to_its_own_publication(self, db_session, base, monkeypatch):
        """Same path, another publication: removed and uploaded again under the same name."""
        from backend.app.services.archive import _convert_timelapse_to_mp4

        archive = await _archive(db_session)
        first, _ = await _publish(db_session, base, archive, name="v.avi", data=b"first")
        first_token = f"{first.revision}:{first.asset_uuid}"
        await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=base,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )
        second, _ = await _publish(db_session, base, archive, name="v.avi", data=b"second")
        assert second.relative_path == first.relative_path
        launched = _fake_encoder(monkeypatch)

        await _convert_timelapse_to_mp4(
            archive.id,
            base / first.relative_path,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
            started_from=first_token,
        )

        assert launched == []
        assert await _pointer(db_session, archive.id) == second.relative_path


class TestAClaimOutlivesTheVideoMadeFromIt:
    """C2-2."""

    async def _claimed(self, db, base, archive, **over):
        return (
            await _publish(
                db,
                base,
                archive,
                name="record.avi",
                origin="printer",
                remote=REMOTE,
                content_sha256="downloaded",
                **over,
            )
        )[0]

    async def _other_may_claim(self, db, base):
        other = await _archive(db)
        result, _ = await _publish(db, base, other, remote=REMOTE, content_sha256="downloaded", origin="printer")
        return result

    async def test_the_conversion_keeps_it(self, db_session, base, monkeypatch):
        from backend.app.services.archive import _convert_timelapse_to_mp4

        archive = await _archive(db_session)
        original = await self._claimed(db_session, base, archive)
        _fake_encoder(monkeypatch)

        await _convert_timelapse_to_mp4(
            archive.id,
            base / original.relative_path,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )

        rows = {row.role: row for row in await _rows(db_session, archive.id)}
        assert rows["primary"].origin == "conversion"
        assert rows["primary"].remote_path is None, "the MP4 was not read off the printer"
        receipt = rows["receipt"]
        assert (receipt.remote_printer_identity, receipt.remote_path, receipt.content_sha256) == (
            "printer-identity",
            "/timelapse/record.avi",
            "downloaded",  # the bytes that were downloaded, never the derived file's hash
        )
        assert receipt.asset_uuid == original.asset_uuid
        refused = await self._other_may_claim(db_session, base)
        assert (refused.ok, refused.refusal) == (False, archive_media.REFUSED_SOURCE_CLAIMED)

    async def test_an_upload_over_it_keeps_it(self, db_session, base):
        archive = await _archive(db_session)
        await self._claimed(db_session, base, archive)
        replaced, _ = await _publish(db_session, base, archive, name="mine.mp4", data=b"upload")
        assert replaced.ok

        refused = await self._other_may_claim(db_session, base)
        assert refused.refusal == archive_media.REFUSED_SOURCE_CLAIMED

    async def test_a_reprint_reset_keeps_it(self, db_session, base):
        archive = await _archive(db_session)
        await self._claimed(db_session, base, archive)
        row = await db_session.get(PrintArchive, archive.id, populate_existing=True)
        released = await archive_media.detach_primary_in(db_session, row)
        await db_session.commit()
        assert released and await _pointer(db_session, archive.id) is None

        refused = await self._other_may_claim(db_session, base)
        assert refused.refusal == archive_media.REFUSED_SOURCE_CLAIMED

    async def test_the_same_recording_comes_back_to_its_own_archive(self, db_session, base):
        """Its own receipt gives way; the claim is held once."""
        archive = await _archive(db_session)
        await self._claimed(db_session, base, archive)
        await _publish(db_session, base, archive, name="mine.mp4", data=b"upload")

        again = await self._claimed(db_session, base, archive)

        assert again.ok
        claims = [row for row in await _rows(db_session, archive.id) if row.remote_path]
        assert [(row.role, row.content_sha256) for row in claims] == [("primary", "downloaded")]

    async def test_the_operators_removal_releases_it(self, db_session, base):
        """The operator said this video is not wanted here: the recording may be attached where it belongs."""
        archive = await _archive(db_session)
        await self._claimed(db_session, base, archive)
        await _publish(db_session, base, archive, name="mine.mp4", data=b"upload")  # the claim is a receipt now
        removed = await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=base,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )
        assert removed.ok and [row.role for row in await _rows(db_session, archive.id)] == ["retired"]

        assert (await self._other_may_claim(db_session, base)).ok


class TestOnlyTheVeryBytesAreDeletedFromThePrinter:
    """C2-3 — against the real registry and the real file guard (a printer freshly seen finished)."""

    PATH = "/timelapse/record.mp4"

    async def _run(self, db_session, base, monkeypatch, *, copied: bytes, on_printer: bytes, identity="printer-life"):
        from backend.app.models.settings import Settings
        from backend.app.services import timelapse_files
        from backend.app.services.bambu_ftp import DeleteResult
        from backend.app.services.printer_files import factory

        device = SimpleNamespace(id=1, name="probe", connector_identity=identity)
        archive = await _archive(db_session)
        published, _ = await _publish(
            db_session,
            base,
            archive,
            data=copied,
            origin="printer",
            remote=RemoteSource("printer-life", "external", self.PATH, len(copied), "old"),
            content_sha256=hashlib.sha256(copied).hexdigest(),
        )
        assert published.ok
        db_session.add(Settings(key="delete_timelapse_after_attach", value="true"))
        await db_session.commit()
        files = {self.PATH: on_printer}
        deleted = []

        class Transport:
            async def delete_verified(self, path, *, ready, verify, may_run=None):
                if not ready() or not verify(files.get(path)) or not may_run():
                    return DeleteResult.FAILED
                deleted.append(files.pop(path))
                return DeleteResult.DELETED

        monkeypatch.setattr(factory, "transport_for", lambda *a: Transport())
        answer = await timelapse_files.remove_recording_after_attach(device, self.PATH)
        return answer, deleted

    async def test_the_copied_recording_is_deleted(self, db_session, base, monkeypatch, guarded_printer):
        answer, deleted = await self._run(db_session, base, monkeypatch, copied=b"video-A", on_printer=b"video-A")
        assert answer is True and deleted == [b"video-A"]

    async def test_another_video_under_the_same_name_stays(self, db_session, base, monkeypatch, guarded_printer):
        answer, deleted = await self._run(db_session, base, monkeypatch, copied=b"video-A", on_printer=b"video-B")
        assert answer is False and deleted == []

    async def test_another_printers_copy_proves_nothing(self, db_session, base, monkeypatch, guarded_printer):
        answer, deleted = await self._run(
            db_session, base, monkeypatch, copied=b"video-A", on_printer=b"video-A", identity="another-printer"
        )
        assert answer is False and deleted == []

    async def test_a_printer_without_an_identity_has_nothing_deleted(
        self, db_session, base, monkeypatch, guarded_printer
    ):
        answer, deleted = await self._run(
            db_session, base, monkeypatch, copied=b"video-A", on_printer=b"video-A", identity=None
        )
        assert answer is False and deleted == []


class TestRightsAreAskedAgainInsideThePublication:
    """C2-4."""

    async def test_an_account_disabled_while_its_upload_was_read_publishes_nothing(
        self, async_client, db_session, base, monkeypatch
    ):
        from starlette.datastructures import UploadFile

        from backend.app.models.user import User

        archive = await _archive(db_session)
        real_read = UploadFile.read
        revoked = []

        async def read_then_revoke(file, *args, **kwargs):
            data = await real_read(file, *args, **kwargs)
            if file.filename == "authority.mp4":
                await db_session.execute(update(User).where(User.username == "test_admin").values(is_active=False))
                await db_session.commit()
                revoked.append(True)
            return data

        monkeypatch.setattr(UploadFile, "read", read_then_revoke)

        response = await async_client.post(
            f"/api/v1/archives/{archive.id}/timelapse/upload", files={"file": ("authority.mp4", b"video", "video/mp4")}
        )

        assert revoked
        assert response.status_code == 401
        assert await _pointer(db_session, archive.id) is None
        assert await _rows(db_session, archive.id) == []
        assert not list((base / "archive").rglob("*.mp4")) and not list((base / "archive").rglob("*.part"))

    async def test_an_active_account_still_uploads(self, async_client, db_session, base):
        archive = await _archive(db_session)
        response = await async_client.post(
            f"/api/v1/archives/{archive.id}/timelapse/upload", files={"file": ("mine.mp4", b"video", "video/mp4")}
        )
        assert response.status_code == 200, response.text
        assert await _pointer(db_session, archive.id) is not None

    async def test_a_removal_asks_too(self, db_session, base):
        from fastapi import HTTPException

        from backend.app.core.auth_lifetime import FrozenAuthority

        archive = await _archive(db_session)
        published, _ = await _publish(db_session, base, archive)
        nobody = FrozenAuthority("session", "no-such-account", 0, 1, "no-such-session")

        with pytest.raises(HTTPException) as refused:
            await archive_media.remove_primary(
                db_session,
                archive.id,
                base_dir=base,
                expected_identity=archive.recovery_identity,
                expected_epoch=queue_sources.current_epoch(),
                authority=nobody,
                permissions=("archives:delete_all",),
            )

        assert refused.value.status_code == 401
        await db_session.rollback()
        assert await _pointer(db_session, archive.id) == published.relative_path
        assert (base / published.relative_path).exists()


class TestAStoredPathAuthorisesNoUnlinkOutsideTheDataDirectory:
    """C2-5."""

    @pytest.mark.parametrize("spelling", ["../outside.mp4", "archive/../../outside.mp4", "ABSOLUTE"])
    async def test_a_replaced_pointer_that_escapes_is_left_alone(self, db_session, tmp_path, spelling):
        data = tmp_path / "data"
        (data / "archive").mkdir(parents=True)
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"not the archive storage's")
        pointer = str(outside) if spelling == "ABSOLUTE" else spelling
        archive = await _archive(db_session, timelapse_path=pointer)

        replaced, _ = await _publish(db_session, data, archive, name="new.mp4")
        assert replaced.ok
        removed = await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=data,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )

        assert removed.ok and outside.read_bytes() == b"not the archive storage's"

    async def test_a_removed_pointer_that_escapes_is_left_alone(self, db_session, tmp_path):
        data = tmp_path / "data"
        (data / "archive").mkdir(parents=True)
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"x")
        archive = await _archive(db_session, timelapse_path="../outside.mp4")

        removed = await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=data,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )

        assert removed.ok and outside.exists()
        assert await _pointer(db_session, archive.id) is None, "the row is put right; only the unlink is refused"

    async def test_a_link_that_leads_out_is_left_alone(self, db_session, tmp_path):
        data = tmp_path / "data"
        (data / "archive").mkdir(parents=True)
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"x")
        try:
            (data / "archive" / "link.mp4").symlink_to(outside)
        except OSError:
            pytest.skip("this account may not create symbolic links")
        assert archive_media.contained(data, "archive/link.mp4") is None

    def test_a_path_inside_is_contained(self, tmp_path):
        (tmp_path / "archive").mkdir()
        assert archive_media.contained(tmp_path, "archive/v.mp4") == (tmp_path / "archive" / "v.mp4").resolve()
        assert archive_media.contained(tmp_path, "") is None


class TestTheRunProofReachesThePublication:
    """C3-1."""

    async def _scan(
        self, db_session, monkeypatch, printer_factory, archive_factory, *, lose_at: str | None, proof=None
    ):
        from backend.app import main
        from backend.app.core import database

        device = await printer_factory()
        archive = await archive_factory(device.id, file_path="")
        archive_id = archive.id
        await db_session.commit()
        monkeypatch.setattr(main, "async_session", database.async_session)
        video = {"name": "ours.mp4", "path": "/timelapse/ours.mp4", "size": 5, "is_directory": False}
        monkeypatch.setattr(main, "_list_timelapse_videos", AsyncMock(return_value=([video], "/timelapse")))
        monkeypatch.setattr(main, "read_timelapse_video", AsyncMock(return_value=b"video"))
        monkeypatch.setattr(main, "_last_recording_path", lambda p: "")
        monkeypatch.setattr(main.ws_manager, "send_archive_updated", AsyncMock())
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        state = {"current": True, "reached": False}
        deletes = []

        async def remove(printer, path, *, may_run=None):
            deletes.append(may_run)

        monkeypatch.setattr(main, "remove_recording_after_attach", remove)
        real = asyncio.to_thread

        async def to_thread(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == lose_at:
                state["reached"] = True
                state["current"] = False  # the successor starts here
            return await real(fn, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", to_thread)
        await main._scan_for_timelapse_with_retries(
            archive_id, set(), baseline_trusted=True, may_run=proof or (lambda: state["current"])
        )
        if lose_at is not None:
            assert state["reached"], "the scan must reach the point where the next print starts"
        return archive_id, deletes

    async def test_a_print_that_starts_during_staging_stops_the_publication(
        self, db_session, base, monkeypatch, printer_factory, archive_factory
    ):
        archive_id, deletes = await self._scan(
            db_session, monkeypatch, printer_factory, archive_factory, lose_at="_stage"
        )
        assert await _pointer(db_session, archive_id) is None
        assert deletes == [], "nothing was published, so nothing may be removed from the printer"
        assert not list(base.rglob("*.mp4")) and not list(base.rglob("*.part"))

    async def test_a_print_that_starts_while_the_publication_holds_its_locks_stops_it(
        self, db_session, base, monkeypatch, printer_factory, archive_factory
    ):
        """After the publication's first look, before the bytes take their name: asked once more."""
        archive_id, deletes = await self._scan(
            db_session, monkeypatch, printer_factory, archive_factory, lose_at="_free_name"
        )
        assert await _pointer(db_session, archive_id) is None
        assert await _rows(db_session, archive_id) == []
        assert deletes == []
        assert not list(base.rglob("*.mp4")) and not list(base.rglob("*.part"))

    async def test_the_tidy_up_is_handed_the_same_proof(
        self, db_session, base, monkeypatch, printer_factory, archive_factory
    ):
        """Published while the run still owned the printer; the delete is asked with the run's own proof."""
        proof = lambda: True  # noqa: E731
        archive_id, handed = await self._scan(
            db_session, monkeypatch, printer_factory, archive_factory, lose_at=None, proof=proof
        )
        assert await _pointer(db_session, archive_id) is not None
        assert handed == [proof]

    async def test_a_printer_without_an_identity_is_left_to_the_operator(
        self, db_session, base, monkeypatch, printer_factory, archive_factory
    ):
        """C2-2: an automatic claim needs something to claim under."""
        from backend.app import main
        from backend.app.core import database
        from backend.app.models.printer import Printer

        device = await printer_factory()
        archive = await archive_factory(device.id, file_path="")
        archive_id = archive.id
        await db_session.execute(update(Printer).where(Printer.id == device.id).values(connector_identity=""))
        await db_session.commit()
        monkeypatch.setattr(main, "async_session", database.async_session)
        video = {"name": "ours.mp4", "path": "/timelapse/ours.mp4", "size": 5, "is_directory": False}
        monkeypatch.setattr(main, "_list_timelapse_videos", AsyncMock(return_value=([video], "/timelapse")))
        read = AsyncMock(return_value=b"video")
        monkeypatch.setattr(main, "read_timelapse_video", read)
        monkeypatch.setattr(main, "_last_recording_path", lambda p: "")
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await main._scan_for_timelapse_with_retries(archive_id, set(), baseline_trusted=True)

        read.assert_not_called()
        assert await _pointer(db_session, archive_id) is None


async def _printing(db, printer_id, **over):
    row = PrintArchive(filename="a.3mf", file_path="", file_size=1, status="printing", printer_id=printer_id, **over)
    db.add(row)
    await db.flush()
    plain = SimpleNamespace(id=row.id, recovery_identity=row.recovery_identity)
    await db.commit()
    return plain


async def _stored(db, archive_id):
    # Only this row is read afresh: the tests still hold their printer.
    return ((await db.get(PrintArchive, archive_id, populate_existing=True)).extra_data or {}).get(tb.KEY)


class TestAStoredBaselineIsAddressedAndNeverDestroyed:
    """C3-2."""

    @pytest.fixture
    def capture(self, monkeypatch):
        from backend.app import main
        from backend.app.core import database

        monkeypatch.setattr(main, "async_session", database.async_session)
        monkeypatch.setattr(main, "_timelapse_baselines", {})
        monkeypatch.setattr(main, "_untrusted_timelapse_baselines", set())
        monkeypatch.setattr(main, "_timelapse_baseline_owners", {})
        return main

    async def test_a_capture_is_stored_with_its_address_and_each_recording(
        self, db_session, capture, monkeypatch, printer_factory
    ):
        from backend.app.services import timelapse_files

        device = await printer_factory()
        archive = await _printing(db_session, device.id)
        listed = [
            {"name": "a.mp4", "path": "/timelapse/a.mp4", "size": 7, "mtime": "Nov 17 10:00"},
            {"name": "a.mp4", "path": "/userdata/media/timelapse/a.mp4", "size": 9},
        ]
        monkeypatch.setattr(capture, "_last_listing_answered", lambda pid: True)
        monkeypatch.setattr(capture, "_list_timelapse_videos", AsyncMock(return_value=(listed, "/timelapse")))
        monkeypatch.setattr(
            capture, "_last_listing_media", lambda pid: {"external": "answered", "internal": "not_asked"}
        )

        await capture._capture_timelapse_baseline_at_start(
            device,
            device.id,
            logging.getLogger(__name__),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
        )

        stored = await _stored(db_session, archive.id)
        assert (stored["v"], stored["archive_identity"], stored["printer_identity"]) == (
            tb.VERSION,
            archive.recovery_identity,
            device.connector_identity,
        )
        assert stored["media"] == {"external": "answered", "internal": "not_asked"}
        assert [(e["storage"], e["path"], e["size"]) for e in stored["entries"]] == [
            ("external", "/timelapse/a.mp4", 7),
            ("internal", "/userdata/media/timelapse/a.mp4", 9),
        ]
        address = tb.Address(archive.recovery_identity, device.connector_identity, await tb.storage_epoch(db_session))
        assert tb.read({tb.KEY: stored}, address=address) == (
            {timelapse_files.recording_key(entry) for entry in listed},
            True,
        )
        assert capture._timelapse_baseline_owners[device.id][0] == archive.recovery_identity

    async def test_a_list_overtaken_by_a_restore_is_not_written_into_the_new_storage(
        self, db_session, capture, monkeypatch, printer_factory
    ):
        device = await printer_factory()
        archive = await _printing(db_session, device.id)
        monkeypatch.setattr(capture, "_last_listing_answered", lambda pid: True)

        async def listing(*args, **kwargs):
            queue_sources.invalidate_receipts()  # the restore wins before the result is kept
            return [{"name": "old.mp4", "path": "/timelapse/old.mp4"}], "/timelapse"

        monkeypatch.setattr(capture, "_list_timelapse_videos", listing)

        await capture._capture_timelapse_baseline_at_start(
            device,
            device.id,
            logging.getLogger(__name__),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
        )

        assert await _stored(db_session, archive.id) is None
        assert device.id not in capture._timelapse_baselines, "nor is it kept in memory"

    async def test_a_start_that_lost_its_run_keeps_nothing(self, db_session, capture, monkeypatch, printer_factory):
        device = await printer_factory()
        archive = await _printing(db_session, device.id)
        monkeypatch.setattr(capture, "_last_listing_answered", lambda pid: True)
        monkeypatch.setattr(capture, "_list_timelapse_videos", AsyncMock(return_value=([], "/timelapse")))
        answers = iter([True, False])  # ours when the listing began; a newer print by the time it answered

        await capture._capture_timelapse_baseline_at_start(
            device,
            device.id,
            logging.getLogger(__name__),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            may_run=lambda: next(answers, False),
        )

        assert await _stored(db_session, archive.id) is None
        assert device.id not in capture._timelapse_baselines

    async def test_an_unread_reconnect_does_not_destroy_the_start_boundary(
        self, db_session, capture, monkeypatch, printer_factory
    ):
        """After a restart the card does not answer: the trusted list taken at the start stays as it was."""
        device = await printer_factory()
        initial = tb.record({"previous.mp4"}, trusted=True, taken="start")
        archive = await _printing(db_session, device.id, extra_data={tb.KEY: initial})
        monkeypatch.setattr(capture, "_last_listing_answered", lambda pid: False)
        monkeypatch.setattr(capture, "_list_timelapse_videos", AsyncMock(return_value=([], "/timelapse")))

        await capture._capture_timelapse_baseline_at_start(
            device,
            device.id,
            logging.getLogger(__name__),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            taken="late",
        )

        assert await _stored(db_session, archive.id) == initial

    async def test_after_a_restart_the_kept_boundary_is_taken_back_without_reading_the_card(
        self, db_session, capture, monkeypatch, printer_factory
    ):
        device = await printer_factory()
        archive = await _printing(db_session, device.id)
        address = tb.Address(archive.recovery_identity, device.connector_identity, await tb.storage_epoch(db_session))
        kept = tb.record(
            {"previous.mp4"},
            trusted=True,
            taken="start",
            entries=[{"name": "previous.mp4", "path": "/timelapse/previous.mp4"}],
            address=address,
        )
        row = await db_session.get(PrintArchive, archive.id)
        row.extra_data = {tb.KEY: kept}
        await db_session.commit()
        listing = AsyncMock(return_value=([], "/timelapse"))
        monkeypatch.setattr(capture, "_list_timelapse_videos", listing)

        await capture._capture_timelapse_baseline_at_start(
            device,
            device.id,
            logging.getLogger(__name__),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            taken="late",
        )

        listing.assert_not_called()
        assert capture._timelapse_baselines[device.id] == {"external:/timelapse/previous.mp4"}
        assert device.id not in capture._untrusted_timelapse_baselines
        assert await _stored(db_session, archive.id) == kept

    async def test_a_late_list_that_was_read_fills_an_unread_start(
        self, db_session, capture, monkeypatch, printer_factory
    ):
        device = await printer_factory()
        archive = await _printing(
            db_session, device.id, extra_data={tb.KEY: tb.record(set(), trusted=False, taken="start")}
        )
        monkeypatch.setattr(capture, "_last_listing_answered", lambda pid: True)
        monkeypatch.setattr(
            capture,
            "_list_timelapse_videos",
            AsyncMock(return_value=([{"name": "old.mp4", "path": "/timelapse/old.mp4"}], "/timelapse")),
        )

        await capture._capture_timelapse_baseline_at_start(
            device,
            device.id,
            logging.getLogger(__name__),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            taken="late",
        )

        stored = await _stored(db_session, archive.id)
        assert (stored["taken"], stored["trusted"], stored["names"]) == ("late", True, ["old.mp4"])

    async def test_a_finished_print_is_given_no_baseline(self, db_session, printer_factory):
        """Still printing, still current: a list is not written into an execution that is over."""
        device = await printer_factory()
        archive = await _archive(db_session, printer_id=device.id)  # completed
        assert not await tb.save(
            db_session, archive.id, archive.recovery_identity, tb.record(set(), trusted=True, taken="start")
        )
        assert await _stored(db_session, archive.id) is None

    async def test_a_reprint_drops_the_earlier_runs_list(self, db_session, printer_factory):
        device = await printer_factory()
        archive = await _printing(
            db_session, device.id, extra_data={"other": 1, tb.KEY: tb.record({"a.mp4"}, trusted=True, taken="start")}
        )
        row = await db_session.get(PrintArchive, archive.id)
        tb.forget_in(row)
        await db_session.commit()
        db_session.expire_all()
        assert (await db_session.get(PrintArchive, archive.id)).extra_data == {"other": 1}

    def test_memory_hands_a_list_only_to_the_print_it_was_taken_for(self, capture):
        epoch = queue_sources.current_epoch()
        capture._timelapse_baselines[7] = {"external:/timelapse/a.mp4"}
        capture._timelapse_baseline_owners[7] = ("ARCHIVE-A", epoch)
        assert capture._pop_timelapse_baseline(7, archive_identity="ARCHIVE-B") == (None, True)
        assert 7 not in capture._timelapse_baselines

        capture._timelapse_baselines[7] = {"external:/timelapse/a.mp4"}
        capture._timelapse_baseline_owners[7] = ("ARCHIVE-A", epoch - 1)  # taken before a restore
        assert capture._pop_timelapse_baseline(7, archive_identity="ARCHIVE-A") == (None, True)

        capture._timelapse_baselines[7] = {"external:/timelapse/a.mp4"}
        capture._timelapse_baseline_owners[7] = ("ARCHIVE-A", epoch)
        assert capture._pop_timelapse_baseline(7, archive_identity="ARCHIVE-A") == (
            {"external:/timelapse/a.mp4"},
            True,
        )


class TestARecoveryThatWasOvertakenLeavesNothing:
    """C3-2: the recovery's own proof reaches the capture — the archive AND memory."""

    async def test_a_list_read_for_a_run_that_was_reset_meanwhile_is_dropped(
        self, db_session, monkeypatch, printer_factory
    ):
        from datetime import datetime

        from backend.app import main
        from backend.app.core import database
        from backend.app.services import print_execution

        device = await printer_factory()
        archive = await _printing(
            db_session, device.id, extra_data={tb.KEY: tb.record(set(), trusted=False, taken="start")}
        )
        monkeypatch.setattr(main, "async_session", database.async_session)
        monkeypatch.setattr(main, "_timelapse_baselines", {})
        monkeypatch.setattr(main, "_untrusted_timelapse_baselines", set())
        monkeypatch.setattr(main, "_timelapse_baseline_owners", {})
        run = {"generation": 41, "reset": False}
        manager = main.printer_manager
        monkeypatch.setattr(manager, "accepts_client_callback_generation", lambda *a: True)
        monkeypatch.setattr(manager, "life_holds", lambda *a: True)
        monkeypatch.setattr(
            manager, "get_client", lambda *a: SimpleNamespace(journal_generation=lambda: run["generation"])
        )
        monkeypatch.setattr(manager, "get_status", lambda *a: SimpleNamespace(state="RUNNING"))
        monkeypatch.setattr(manager, "is_awaiting_plate_clear", lambda *a: False)
        monkeypatch.setattr(main, "_restore_usage_tracking_session", AsyncMock())
        monkeypatch.setattr(main, "_seed_runout_detector_from_journal", AsyncMock())
        live = SimpleNamespace(archive_id=archive.id, archive_identity=archive.recovery_identity, plate_index=1)
        monkeypatch.setattr(main, "_live_archive_for_running_print", AsyncMock(return_value=live))
        monkeypatch.setattr(
            print_execution, "decide_start", AsyncMock(return_value=SimpleNamespace(current_id=archive.id, old=False))
        )
        monkeypatch.setattr(main, "mark_queue_printing_for_printer", AsyncMock(return_value=live))
        monkeypatch.setattr(main, "_printer_reported_options", lambda *a: None)
        monkeypatch.setattr(main, "_bind_print_file_analysis_context", lambda *a: None)
        monkeypatch.setattr(main, "_last_listing_answered", lambda *a: True)
        monkeypatch.setattr(main, "_last_listing_media", lambda *a: {"external": "answered"})

        async def listing(*args, **kwargs):
            # The list was asked for run 41; before it answers, a reprint resets the row — the same
            # archive incarnation, still printing — and the run is 42.
            async with database.async_session() as db:
                row = await db.get(PrintArchive, archive.id)
                tb.forget_in(row)
                row.started_at = datetime(2026, 11, 17, 15, 0, 0)
                await db.commit()
            run.update(generation=42, reset=True)
            return [{"name": "old-run.mp4", "path": "/timelapse/old-run.mp4"}], "/timelapse"

        monkeypatch.setattr(main, "_list_timelapse_videos", listing)

        await main._recover_running_print(device.id, {"_journal_generation": 41})

        assert run["reset"], "the recovery must reach the listing"
        assert await _stored(db_session, archive.id) is None
        assert device.id not in main._timelapse_baselines
        assert device.id not in main._timelapse_baseline_owners

    async def test_a_list_the_archive_refused_does_not_stay_in_memory(self, db_session, monkeypatch, printer_factory):
        """An unread list against a trusted kept one: the archive keeps its own, and memory takes neither."""
        from backend.app import main
        from backend.app.core import database

        device = await printer_factory()
        archive = await _printing(
            db_session, device.id, extra_data={tb.KEY: tb.record({"previous.mp4"}, trusted=True, taken="start")}
        )
        monkeypatch.setattr(main, "async_session", database.async_session)
        monkeypatch.setattr(main, "_timelapse_baselines", {})
        monkeypatch.setattr(main, "_untrusted_timelapse_baselines", set())
        monkeypatch.setattr(main, "_timelapse_baseline_owners", {})
        monkeypatch.setattr(main, "_last_listing_answered", lambda pid: False)
        monkeypatch.setattr(main, "_list_timelapse_videos", AsyncMock(return_value=([], "/timelapse")))

        await main._capture_timelapse_baseline_at_start(
            device,
            device.id,
            logging.getLogger(__name__),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            taken="late",
        )

        assert device.id not in main._timelapse_baselines
        assert device.id not in main._untrusted_timelapse_baselines


class TestOnlyALiveCopyIsPinned:
    """C2-3: a receipt is history; what authorises a delete on the printer is a copy that exists."""

    async def _claimed(self, db_session, base):
        archive = await _archive(db_session)
        published, _ = await _publish(
            db_session,
            base,
            archive,
            origin="printer",
            remote=RemoteSource("printer-identity", "external", "//timelapse//record.avi"),
            content_sha256="downloaded",
        )
        return archive, published

    async def _pin(self, db_session, base, path="/timelapse/record.avi", identity="printer-identity"):
        return await archive_media.pin_live_copy(db_session, base, identity, "external", path)

    async def test_the_shown_copy_is_pinned_and_the_pin_is_released(self, db_session, base):
        _archive_row, published = await self._claimed(db_session, base)
        pin = await self._pin(db_session, base)
        assert (pin.asset_uuid, pin.relative_path, pin.content_sha256, pin.holds()) == (
            published.asset_uuid,
            published.relative_path,
            "downloaded",
            True,
        )
        archive_media.release_pin(pin)
        assert archive_media._pins == []

    async def test_a_removal_that_wins_while_the_copy_is_being_found_leaves_no_pin(self, db_session, base, monkeypatch):
        """The disk said "there" and, before the pin was registered, the video was removed: look again."""
        archive, published = await self._claimed(db_session, base)
        local = base / published.relative_path
        reached, release = asyncio.Event(), asyncio.Event()
        real = asyncio.to_thread
        stopped = False

        async def after_the_disk_answered(fn, *args, **kwargs):
            nonlocal stopped
            result = await real(fn, *args, **kwargs)
            if not stopped and getattr(fn, "__name__", "") == "is_file":
                stopped = True
                reached.set()
                await release.wait()
            return result

        monkeypatch.setattr(asyncio, "to_thread", after_the_disk_answered)
        factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
        async with factory() as other:
            pinning = asyncio.create_task(
                archive_media.pin_live_copy(other, base, "printer-identity", "external", "/timelapse/record.avi")
            )
            await asyncio.wait_for(reached.wait(), 5)
            removed = await archive_media.remove_primary(
                db_session,
                archive.id,
                base_dir=base,
                expected_identity=archive.recovery_identity,
                expected_epoch=queue_sources.current_epoch(),
            )
            assert removed.ok and not local.exists()
            release.set()
            assert await asyncio.wait_for(pinning, 5) is None
        assert archive_media._pins == []

    async def test_the_hard_delete_of_the_archive_revokes_the_pin(self, db_session, base):
        archive, _ = await self._claimed(db_session, base)
        pin = await self._pin(db_session, base)
        try:
            await archive_media.forget_archives(db_session, [archive.id])
            await db_session.rollback()  # revoked before the commit, and it stays revoked
            assert pin.revoked and not pin.holds()
        finally:
            archive_media.release_pin(pin)

    async def test_a_pin_asks_the_disk_itself(self, db_session, base):
        """A door that takes the file without revoking still gets nothing deleted from the printer."""
        _archive_row, published = await self._claimed(db_session, base)
        pin = await self._pin(db_session, base)
        try:
            assert pin.holds()
            (base / published.relative_path).unlink()
            assert not pin.revoked and not pin.holds() and not pin.matches(b"anything")
        finally:
            archive_media.release_pin(pin)

    async def test_the_unlink_after_a_commit_revokes_by_path(self, db_session, base):
        _archive_row, published = await self._claimed(db_session, base)
        pin = await self._pin(db_session, base)
        try:
            await archive_media._unlink(base, published.relative_path)
            assert pin.revoked
        finally:
            archive_media.release_pin(pin)

    async def test_another_path_or_printer_has_no_copy(self, db_session, base):
        await self._claimed(db_session, base)
        assert await self._pin(db_session, base, path="/timelapse/other.avi") is None
        assert await self._pin(db_session, base, identity="another-printer") is None

    async def test_a_receipt_is_no_copy(self, db_session, base):
        archive, _ = await self._claimed(db_session, base)
        await _publish(db_session, base, archive, name="mine.mp4", data=b"upload")  # the claim is a receipt now
        assert await self._pin(db_session, base) is None

    async def test_a_copy_whose_file_is_gone_is_no_copy(self, db_session, base):
        _archive_row, published = await self._claimed(db_session, base)
        (base / published.relative_path).unlink()
        assert await self._pin(db_session, base) is None

    @pytest.mark.parametrize("change", ["replace", "remove", "reprint"])
    async def test_taking_the_video_away_revokes_the_pin(self, db_session, base, change):
        archive, _ = await self._claimed(db_session, base)
        pin = await self._pin(db_session, base)
        try:
            if change == "replace":
                await _publish(db_session, base, archive, name="mine.mp4", data=b"upload")
            elif change == "remove":
                await archive_media.remove_primary(
                    db_session,
                    archive.id,
                    base_dir=base,
                    expected_identity=archive.recovery_identity,
                    expected_epoch=queue_sources.current_epoch(),
                )
            else:
                row = await db_session.get(PrintArchive, archive.id, populate_existing=True)
                await archive_media.detach_primary_in(db_session, row)
                await db_session.commit()
            assert not pin.holds()
            assert not pin.matches(b"anything")
        finally:
            archive_media.release_pin(pin)
