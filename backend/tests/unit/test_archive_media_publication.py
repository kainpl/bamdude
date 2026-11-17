"""The one seam that gives an archive a video, replaces it or removes it (G10 C2, M12/M13, review N1)."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_media_asset import ArchiveMediaAsset
from backend.app.services import archive_media, queue_sources
from backend.app.services.archive_media import RemoteSource


@pytest.fixture
def base(tmp_path):
    (tmp_path / "archive").mkdir()
    return tmp_path


async def _archive(db, **over):
    """A committed archive, as plain values: the ORM row expires at every commit the seam makes."""
    row = PrintArchive(filename="a.3mf", file_path="", file_size=1, status="completed", **over)
    db.add(row)
    await db.flush()
    plain = SimpleNamespace(id=row.id, recovery_identity=row.recovery_identity)
    await db.commit()
    return plain


async def _live(db, archive) -> PrintArchive:
    db.expire_all()
    return (await db.execute(select(PrintArchive).where(PrintArchive.id == archive.id))).scalar_one()


async def _token_of(db, archive) -> str:
    return await archive_media.primary_token(db, await _live(db, archive))


def _staged(base, data=b"video") -> object:
    path = base / "archive" / f".{uuid4().hex}.part"
    path.write_bytes(data)
    return path


async def _publish(db, base, archive, *, name="v.mp4", data=b"video", **over):
    directory = base / "archive" / "timelapse" / archive.recovery_identity
    directory.mkdir(parents=True, exist_ok=True)
    if "expected_token" not in over:
        # The seam has no default: a caller captures the video it starts from.
        over["expected_token"] = await _token_of(db, archive)
    staged = _staged(base, data)
    result = await archive_media.publish_primary(
        db,
        archive.id,
        staged=staged,
        directory=directory,
        filename=name,
        base_dir=base,
        origin=over.pop("origin", "upload"),
        expected_identity=over.pop("expected_identity", archive.recovery_identity),
        expected_epoch=over.pop("expected_epoch", queue_sources.current_epoch()),
        **over,
    )
    return result, staged


async def _rows(db, archive_id):
    db.expire_all()
    return list(
        (
            await db.execute(
                select(ArchiveMediaAsset)
                .where(ArchiveMediaAsset.archive_id == archive_id)
                .order_by(ArchiveMediaAsset.id)
            )
        )
        .scalars()
        .all()
    )


async def _pointer(db, archive_id):
    db.expire_all()
    return (await db.execute(select(PrintArchive.timelapse_path).where(PrintArchive.id == archive_id))).scalar_one()


class TestPublishing:
    async def test_the_row_and_the_pointer_are_written_together(self, db_session, base):
        archive = await _archive(db_session)
        result, staged = await _publish(db_session, base, archive, content_sha256="h", size_bytes=5)

        assert result.ok and result.revision == 1
        assert await _pointer(db_session, archive.id) == result.relative_path
        (row,) = await _rows(db_session, archive.id)
        assert (row.role, row.relative_path, row.origin, row.content_sha256, row.size_bytes) == (
            "primary",
            result.relative_path,
            "upload",
            "h",
            5,
        )
        assert (base / result.relative_path).read_bytes() == b"video"
        assert not staged.exists()

    async def test_a_new_video_replaces_the_old_one_and_removes_its_file_after_the_commit(self, db_session, base):
        archive = await _archive(db_session)
        first, _ = await _publish(db_session, base, archive, name="a.avi", data=b"old")
        second, _ = await _publish(db_session, base, archive, name="a.mp4", data=b"new")

        assert second.ok and second.revision == 2
        (row,) = await _rows(db_session, archive.id)
        assert row.relative_path == second.relative_path
        assert not (base / first.relative_path).exists()
        assert (base / second.relative_path).read_bytes() == b"new"

    async def test_a_publication_never_writes_over_a_file_of_the_same_name(self, db_session, base):
        archive = await _archive(db_session)
        first, _ = await _publish(db_session, base, archive, name="v.mp4", data=b"old")
        live = base / first.relative_path
        seen = {}
        real = archive_media._free_name

        def spy(directory, filename):
            seen["during"] = live.read_bytes()  # the live file is untouched while the new one is placed
            return real(directory, filename)

        archive_media._free_name = spy
        try:
            second, _ = await _publish(db_session, base, archive, name="v.mp4", data=b"new")
        finally:
            archive_media._free_name = real
        assert seen["during"] == b"old"
        assert second.relative_path != first.relative_path
        assert (base / second.relative_path).read_bytes() == b"new"


class TestTheTargetMustStillBeTheSameArchive:
    @pytest.mark.parametrize("case", ["identity", "epoch", "noncurrent", "gone"])
    async def test_a_changed_target_is_refused_and_nothing_is_left(self, db_session, base, case):
        archive = await _archive(db_session)
        over = {}
        if case == "identity":
            over["expected_identity"] = "an-earlier-archive-of-this-id"
        elif case == "epoch":
            over["expected_epoch"] = queue_sources.current_epoch() - 1
        elif case == "noncurrent":
            from backend.app.services.print_execution import NONCURRENT

            (await _live(db_session, archive)).extra_data = {NONCURRENT: True}
            await db_session.commit()
        else:
            (await _live(db_session, archive)).deleted_at = datetime(2026, 1, 1)
            await db_session.commit()
        result, staged = await _publish(db_session, base, archive, **over)

        assert (result.ok, result.refusal) == (False, archive_media.REFUSED_TARGET_GONE)
        assert await _pointer(db_session, archive.id) is None
        assert await _rows(db_session, archive.id) == []
        assert not staged.exists()
        assert list((base / "archive" / "timelapse" / archive.recovery_identity).iterdir()) == []


class TestALatePublicationDoesNotWinByBeingLast:
    async def test_a_video_replaced_meanwhile_refuses_the_late_one(self, db_session, base):
        archive = await _archive(db_session)
        await _publish(db_session, base, archive, name="a.avi", data=b"avi")
        token = await _token_of(db_session, archive)  # a conversion starts from this video
        newer, _ = await _publish(db_session, base, archive, name="b.mp4", data=b"newer")  # the operator uploads

        late, staged = await _publish(db_session, base, archive, name="a.mp4", data=b"converted", expected_token=token)

        assert (late.ok, late.refusal) == (False, archive_media.REFUSED_SOURCE_CHANGED)
        assert await _pointer(db_session, archive.id) == newer.relative_path
        assert not staged.exists()

    async def test_no_video_is_a_state_too(self, db_session, base):
        archive = await _archive(db_session)
        token = await _token_of(db_session, archive)
        assert token == archive_media.NO_VIDEO
        await _publish(db_session, base, archive, data=b"somebody else")
        late, _ = await _publish(db_session, base, archive, data=b"mine", expected_token=token)
        assert late.refusal == archive_media.REFUSED_SOURCE_CHANGED

    async def test_removed_and_attached_again_under_the_same_name_is_a_different_state(self, db_session, base):
        archive = await _archive(db_session)
        await _publish(db_session, base, archive, name="v.mp4")
        token = await _token_of(db_session, archive)
        await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=base,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )
        again, _ = await _publish(db_session, base, archive, name="v.mp4")
        assert again.relative_path.endswith("v.mp4")  # the same path as before
        assert await _token_of(db_session, archive) != token

    async def test_an_unchanged_video_lets_the_publication_through(self, db_session, base):
        archive = await _archive(db_session)
        await _publish(db_session, base, archive, name="a.avi")
        token = await _token_of(db_session, archive)
        result, _ = await _publish(db_session, base, archive, name="a.mp4", expected_token=token)
        assert result.ok


class TestARecordingBelongsToOneArchive:
    REMOTE = RemoteSource("P-1", "external", "/timelapse/v.mp4", size=5, mtime_raw="Nov 16 10:00")

    async def test_the_second_archive_is_refused_and_keeps_what_it_had(self, db_session, base):
        first, second = await _archive(db_session), await _archive(db_session)
        won, _ = await _publish(db_session, base, first, origin="printer", content_sha256="h", remote=self.REMOTE)
        kept, _ = await _publish(db_session, base, second, name="own.mp4", data=b"own")

        lost, staged = await _publish(
            db_session, base, second, origin="printer", content_sha256="h", remote=self.REMOTE
        )

        assert won.ok
        assert (lost.ok, lost.refusal) == (False, archive_media.REFUSED_SOURCE_CLAIMED)
        assert await _pointer(db_session, second.id) == kept.relative_path
        assert (base / kept.relative_path).read_bytes() == b"own"
        assert not staged.exists()
        (row,) = await _rows(db_session, first.id)
        assert (row.remote_printer_identity, row.remote_storage, row.remote_path, row.remote_size) == (
            "P-1",
            "external",
            "/timelapse/v.mp4",
            5,
        )

    @pytest.mark.parametrize(
        "other",
        [
            RemoteSource("P-1", "internal", "/timelapse/v.mp4"),
            RemoteSource("P-2", "external", "/timelapse/v.mp4"),
            RemoteSource("P-1", "external", "/timelapse/other.mp4"),
        ],
        ids=["another-medium", "another-printer", "another-path"],
    )
    async def test_the_same_name_elsewhere_is_another_recording(self, db_session, base, other):
        first, second = await _archive(db_session), await _archive(db_session)
        await _publish(db_session, base, first, origin="printer", content_sha256="h", remote=self.REMOTE)
        result, _ = await _publish(db_session, base, second, origin="printer", content_sha256="h", remote=other)
        assert result.ok

    async def test_other_bytes_at_the_same_path_are_another_recording(self, db_session, base):
        first, second = await _archive(db_session), await _archive(db_session)
        await _publish(db_session, base, first, origin="printer", content_sha256="h1", remote=self.REMOTE)
        result, _ = await _publish(db_session, base, second, origin="printer", content_sha256="h2", remote=self.REMOTE)
        assert result.ok

    async def test_the_path_is_stored_in_one_spelling(self, db_session, base):
        first, second = await _archive(db_session), await _archive(db_session)
        await _publish(db_session, base, first, origin="printer", content_sha256="h", remote=self.REMOTE)
        sloppy = RemoteSource("P-1", "external", "timelapse//v.mp4/")
        result, _ = await _publish(db_session, base, second, origin="printer", content_sha256="h", remote=sloppy)
        assert result.refusal == archive_media.REFUSED_SOURCE_CLAIMED

    async def test_the_same_archive_may_take_its_own_recording_again(self, db_session, base):
        archive = await _archive(db_session)
        await _publish(db_session, base, archive, origin="printer", content_sha256="h", remote=self.REMOTE)
        again, _ = await _publish(db_session, base, archive, origin="printer", content_sha256="h", remote=self.REMOTE)
        assert again.ok and again.revision == 2


class TestRowsFromBeforeTheSeam:
    """Review N1: after m237 an old writer could still replace or remove a video without touching its row."""

    async def _seeded(self, db, base, *, pointer, row_path):
        archive = await _archive(db, timelapse_path=pointer)
        if row_path is not None:
            db.add(
                ArchiveMediaAsset(
                    asset_uuid=str(uuid4()),
                    archive_id=archive.id,
                    archive_identity=archive.recovery_identity,
                    role="primary",
                    revision=1,
                    relative_path=row_path,
                    origin="legacy",
                    created_at=datetime(2026, 1, 1),
                )
            )
            await db.commit()
        for path in {pointer, row_path} - {None}:
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.encode())
        return archive

    async def test_attach_after_the_seed_describes_the_pointer_as_legacy_first(self, db_session, base):
        """Attached by an old writer after m237: a pointer, and no row at all."""
        archive = await self._seeded(db_session, base, pointer="archive/old/attached.mp4", row_path=None)
        assert await _token_of(db_session, archive) == "legacy:archive/old/attached.mp4"

        result, _ = await _publish(db_session, base, archive, name="new.mp4")

        assert result.ok and result.revision == 2  # the legacy description took revision 1
        (row,) = await _rows(db_session, archive.id)
        assert row.relative_path == result.relative_path and row.origin == "upload"
        assert not (base / "archive/old/attached.mp4").exists()

    async def test_replace_after_the_seed_does_not_put_the_seeded_video_back(self, db_session, base):
        """The seeded row names the video m237 saw; an old writer has since replaced it."""
        archive = await self._seeded(
            db_session, base, pointer="archive/old/newer.mp4", row_path="archive/old/seeded.mp4"
        )
        token = await _token_of(db_session, archive)
        assert token == "legacy:archive/old/newer.mp4"

        removed = await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=base,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
            expected_token=token,
        )

        assert removed.ok
        assert await _pointer(db_session, archive.id) is None
        assert [row.role for row in await _rows(db_session, archive.id)] == ["retired"]  # the removal's mark
        assert not (base / "archive/old/newer.mp4").exists()
        # The stale row's file is not this archive's video any more; it is not touched and not brought back.
        assert (base / "archive/old/seeded.mp4").exists()

    async def test_delete_after_the_seed_does_not_bring_the_video_back(self, db_session, base):
        """An old writer removed the video: the pointer is NULL and the seeded row is still there."""
        archive = await self._seeded(db_session, base, pointer=None, row_path="archive/old/seeded.mp4")
        # The emptiness an old writer left has the stale row's revision, not the never-had-a-video one.
        token = await _token_of(db_session, archive)
        assert token.startswith("none:") and token != archive_media.NO_VIDEO

        result, _ = await _publish(db_session, base, archive, name="new.mp4", expected_token=token)

        assert result.ok
        (row,) = [r for r in await _rows(db_session, archive.id) if r.role == "primary"]
        assert row.relative_path == result.relative_path
        assert await _pointer(db_session, archive.id) == result.relative_path

    async def test_a_legacy_description_never_carries_a_source(self, db_session, base):
        archive = await self._seeded(db_session, base, pointer="archive/old/attached.mp4", row_path=None)
        async with queue_sources.storage_mutation():
            row = await archive_media._reconcile(db_session, await _live(db_session, archive))
            await db_session.flush()
            seen = (row.origin, row.content_sha256, row.remote_printer_identity, row.remote_path)
        assert seen == ("legacy", None, None, None)

    async def test_rows_of_an_earlier_archive_of_this_id_are_dropped(self, db_session, base):
        archive = await _archive(db_session)
        db_session.add(
            ArchiveMediaAsset(
                asset_uuid=str(uuid4()),
                archive_id=archive.id,
                archive_identity="an-earlier-archive-of-this-id",
                role="primary",
                revision=9,
                relative_path="archive/dead.mp4",
                origin="legacy",
                created_at=datetime(2026, 1, 1),
            )
        )
        await db_session.commit()
        result, _ = await _publish(db_session, base, archive)
        assert result.ok and result.revision == 1
        assert [r.archive_identity for r in await _rows(db_session, archive.id)] == [archive.recovery_identity]


class TestRemoving:
    async def test_the_row_the_pointer_and_then_the_file_go(self, db_session, base):
        archive = await _archive(db_session)
        published, _ = await _publish(db_session, base, archive)
        result = await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=base,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )
        assert result.ok
        assert await _pointer(db_session, archive.id) is None
        (mark,) = await _rows(db_session, archive.id)
        assert (mark.role, mark.revision) == ("retired", published.revision + 1)
        assert not (base / published.relative_path).exists()

    async def test_the_emptiness_after_a_removal_is_not_the_emptiness_before_it(self, db_session, base):
        """C2-1: an upload that captured "no video" and slept through attach-then-remove is refused."""
        archive = await _archive(db_session)
        before = await _token_of(db_session, archive)
        published, _ = await _publish(db_session, base, archive)
        await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=base,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )
        after = await _token_of(db_session, archive)
        assert (before, after) == ("none:0", "none:2")

        late, _ = await _publish(db_session, base, archive, name="late.mp4", expected_token=before)
        assert (late.ok, late.refusal) == (False, archive_media.REFUSED_SOURCE_CHANGED)
        assert await _pointer(db_session, archive.id) is None

        fresh, _ = await _publish(db_session, base, archive, name="fresh.mp4")
        assert fresh.ok and fresh.revision == 3, "revisions go on from the removal's, never back"
        assert [row.role for row in await _rows(db_session, archive.id)] == ["retired", "primary"]

    async def test_a_reprint_reset_marks_the_emptiness_too(self, db_session, base):
        archive = await _archive(db_session)
        await _publish(db_session, base, archive)
        row = await _live(db_session, archive)
        await archive_media.detach_primary_in(db_session, row)
        await db_session.commit()
        assert await _token_of(db_session, archive) == "none:2"

    async def test_a_file_another_archive_still_shows_is_kept(self, db_session, base):
        """A 3MF folder can be shared by content dedup; an old-layout video may sit in it."""
        shared = "archive/shared/timelapse.mp4"
        (base / "archive/shared").mkdir(parents=True)
        (base / shared).write_bytes(b"shared")
        one = await _archive(db_session, timelapse_path=shared)
        other = await _archive(db_session, timelapse_path=shared)
        result = await archive_media.remove_primary(
            db_session,
            one.id,
            base_dir=base,
            expected_identity=one.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
        )
        assert result.ok
        assert (base / shared).exists()
        assert await _pointer(db_session, other.id) == shared

    async def test_a_removal_started_from_another_video_is_refused(self, db_session, base):
        archive = await _archive(db_session)
        await _publish(db_session, base, archive, name="a.mp4")
        token = await _token_of(db_session, archive)
        newer, _ = await _publish(db_session, base, archive, name="b.mp4")
        result = await archive_media.remove_primary(
            db_session,
            archive.id,
            base_dir=base,
            expected_identity=archive.recovery_identity,
            expected_epoch=queue_sources.current_epoch(),
            expected_token=token,
        )
        assert result.refusal == archive_media.REFUSED_SOURCE_CHANGED
        assert await _pointer(db_session, archive.id) == newer.relative_path
        assert (base / newer.relative_path).exists()


class TestAFailedPublicationLeavesNothing:
    async def test_a_commit_that_fails_removes_the_file_it_placed(self, db_session, base, monkeypatch):
        archive = await _archive(db_session)
        directory = base / "archive" / "timelapse" / archive.recovery_identity
        real = db_session.commit
        calls = {"n": 0}

        async def commit():
            calls["n"] += 1
            if calls["n"] == 2:  # the publication's own commit; the first only ends the caller's transaction
                raise RuntimeError("connection lost")
            return await real()

        monkeypatch.setattr(db_session, "commit", commit)
        with pytest.raises(RuntimeError, match="connection lost"):
            await _publish(db_session, base, archive)
        monkeypatch.undo()
        await db_session.rollback()

        assert list(directory.iterdir()) == []
        assert [p for p in (base / "archive").iterdir() if p.is_file()] == []
        assert await _pointer(db_session, archive.id) is None
