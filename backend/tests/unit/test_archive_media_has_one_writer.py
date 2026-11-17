"""Only ``services/archive_media.py`` writes an archive's video pointer, and every door goes through it (G10 C2)."""

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.archive_media_asset import ArchiveMediaAsset
from backend.app.services import archive_media
from backend.app.services.archive_media import RemoteSource

APP = Path(__file__).resolve().parents[2] / "app"
WRITER = APP / "services" / "archive_media.py"


def test_nothing_else_assigns_the_video_pointer():
    """``<archive>.timelapse_path = …`` outside the seam is a second writer of the same fact."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path == WRITER or "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, (ast.AugAssign, ast.AnnAssign))
                else []
            )
            for target in targets:
                if not (isinstance(target, ast.Attribute) and target.attr == "timelapse_path"):
                    continue
                owner = ast.unparse(target.value)
                # ``state.timelapse_path`` is the printer's report of where it records, not an archive's video.
                if owner.endswith("state"):
                    continue
                offenders.append(f"{path.relative_to(APP).as_posix()}:{node.lineno} {owner}.timelapse_path")
    assert offenders == []


def test_no_route_or_service_unlinks_an_archive_video_by_hand():
    """The file goes after the seam's commit; an unlink beside a pointer read is the old order coming back."""
    source = (APP / "api" / "routes" / "archives.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    delete = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "delete_timelapse")
    text = ast.unparse(delete)
    assert "remove_primary" in text
    assert ".unlink(" not in text


@pytest.fixture
def media_env(monkeypatch, tmp_path):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
    monkeypatch.setattr("backend.app.services.archive.spawn_background_task", lambda coro, **_k: coro.close())
    return tmp_path


async def _rows(db):
    db.expire_all()
    return list((await db.execute(select(ArchiveMediaAsset).order_by(ArchiveMediaAsset.id))).scalars().all())


class TestTheServiceDoor:
    async def test_an_attach_writes_the_row_with_its_measured_content(
        self, db_session, archive_factory, printer_factory, media_env
    ):
        from backend.app.services.archive import ArchiveService

        archive = await archive_factory((await printer_factory()).id, file_path="")
        archive_id, identity = archive.id, archive.recovery_identity
        service = ArchiveService(db_session)

        assert await service.attach_timelapse(archive_id, b"video", "v.mp4", origin="upload")

        (row,) = await _rows(db_session)
        assert (row.archive_id, row.archive_identity, row.origin, row.size_bytes) == (archive_id, identity, "upload", 5)
        assert len(row.content_sha256) == 64
        assert (media_env / row.relative_path).read_bytes() == b"video"
        assert service.media_refusal is None
        # No staged file is left beside it.
        assert [p.name for p in (media_env / row.relative_path).parent.iterdir()] == ["v.mp4"]

    async def test_two_prints_of_one_file_do_not_share_the_recording(
        self, db_session, archive_factory, printer_factory, media_env
    ):
        """M12: the second archive neither takes the first one's video nor loses what it had."""
        from backend.app.services.archive import ArchiveService

        printer = await printer_factory()
        first = await archive_factory(printer.id, file_path="")
        second = await archive_factory(printer.id, file_path="")
        first_id, second_id = first.id, second.id
        remote = RemoteSource("P-1", "external", "/timelapse/video_1.mp4")

        assert await ArchiveService(db_session).attach_timelapse(
            first_id, b"recording", "video_1.mp4", origin="printer", remote=remote
        )
        service = ArchiveService(db_session)
        assert not await service.attach_timelapse(
            second_id, b"recording", "video_1.mp4", origin="printer", remote=remote
        )

        assert service.media_refusal == archive_media.REFUSED_SOURCE_CLAIMED
        rows = await _rows(db_session)
        assert [r.archive_id for r in rows] == [first_id]

    async def test_a_reused_archive_id_is_not_given_the_video(
        self, db_session, archive_factory, printer_factory, media_env
    ):
        from backend.app.services.archive import ArchiveService

        archive = await archive_factory((await printer_factory()).id, file_path="")
        service = ArchiveService(db_session)
        assert not await service.attach_timelapse(
            archive.id, b"video", "v.mp4", expected_identity="an-earlier-archive-of-this-id"
        )
        assert service.media_refusal == archive_media.REFUSED_TARGET_GONE
        assert await _rows(db_session) == []


class TestTheRoutes:
    async def test_an_upload_announces_itself_after_the_commit(
        self, db_session, archive_factory, printer_factory, media_env, monkeypatch
    ):
        from backend.app.api.routes import archives as routes

        announced = AsyncMock()
        monkeypatch.setattr(routes.ws_manager, "send_archive_updated", announced)
        archive = await archive_factory((await printer_factory()).id, file_path="")
        upload = AsyncMock()
        upload.filename = "clip.mp4"
        upload.read.return_value = b"video"

        result = await routes.upload_timelapse(archive_id=archive.id, file=upload, db=db_session, _=None)

        assert result["status"] == "attached"
        announced.assert_awaited_once()
        assert len(await _rows(db_session)) == 1

    @pytest.mark.parametrize(
        ("refusal", "status", "code"),
        [
            ("source_claimed", 409, "source_claimed"),
            ("source_changed", 409, "source_changed"),
            ("target_gone", 409, "target_gone"),
        ],
    )
    def test_a_refusal_is_answered_with_its_code(self, refusal, status, code):
        from backend.app.api.routes.archives import _refuse_media

        with pytest.raises(HTTPException) as raised:
            _refuse_media(refusal)
        assert raised.value.status_code == status
        assert raised.value.detail["error"] == code

    def test_an_unknown_failure_stays_a_server_error(self):
        from backend.app.api.routes.archives import _refuse_media

        with pytest.raises(HTTPException) as raised:
            _refuse_media(None)
        assert raised.value.status_code == 500


class TestTheSourceOfARecording:
    def test_it_names_the_printer_by_its_identity_and_the_medium_by_the_path(self):
        from types import SimpleNamespace

        from backend.app.services.timelapse_files import INTERNAL_TIMELAPSE_ROOT, remote_source

        printer = SimpleNamespace(id=7, connector_identity="P-IDENTITY")
        card = remote_source(printer, "/timelapse/v.mp4", {"size": 10, "mtime": "Nov 16 10:00"})
        assert (card.printer_identity, card.storage, card.path, card.size, card.mtime_raw) == (
            "P-IDENTITY",
            "external",
            "/timelapse/v.mp4",
            10,
            "Nov 16 10:00",
        )
        assert remote_source(printer, f"{INTERNAL_TIMELAPSE_ROOT}/v.mp4").storage == "internal"

    def test_a_printer_without_an_identity_claims_nothing(self):
        from types import SimpleNamespace

        from backend.app.services.timelapse_files import remote_source

        assert remote_source(SimpleNamespace(id=7, connector_identity=None), "/timelapse/v.mp4") is None
        assert remote_source(SimpleNamespace(id=7), "/timelapse/v.mp4") is None
