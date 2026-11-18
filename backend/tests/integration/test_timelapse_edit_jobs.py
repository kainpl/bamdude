"""The timelapse editor in the background (G10 E, M23–M26).

A real ffmpeg encodes real (tiny) videos through the real application: what is
proved is what lands on disk and in the registry, and what never does. The
cases that need an encoder that does NOT finish use a stand-in child — this
interpreter printing ffmpeg's progress lines — so a cancel, a revoke and a
restore meet a process that is really running.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.api.routes import media_downloads as download_routes
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_media_asset import ROLE_PRIMARY, ROLE_VARIANT, ArchiveMediaAsset
from backend.app.services import media_child, timelapse_editor, timelapse_processor
from backend.app.services.timelapse_editor import timelapse_edit_runtime
from backend.tests.integration.test_media_download_jobs import _patient_authority  # noqa: F401 -- autouse here too
from backend.tests.integration.test_workshop_library_rights import _jwt, _user

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg / ffprobe are not installed"),
]

TERMINAL = ("done", "failed", "cancelled")


def _make_video(path: Path, *, seconds: float = 2.0, audio: bool = False) -> None:
    command = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if audio:
        command += ["-c:a", "aac", "-shortest"]
    subprocess.run([*command, str(path)], check=True, timeout=120)


def _make_music(path: Path, *, seconds: float = 0.5) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}", str(path)],
        check=True,
        timeout=120,
    )


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    data = json.loads(out)
    kinds = [stream["codec_type"] for stream in data["streams"]]
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    return {
        "duration": float(data["format"]["duration"]),
        "audio": "audio" in kinds,
        "size": (video["width"], video["height"]),
    }


def _mean_volume(path: Path) -> float:
    """The mean volume of the file's audio in dB, read by ffmpeg's own ``volumedetect``."""
    err = subprocess.run(
        [FFMPEG, "-v", "info", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True,
        timeout=60,
    ).stderr.decode("utf-8", "replace")
    line = next(line for line in err.splitlines() if "mean_volume" in line)
    return float(line.split("mean_volume:")[1].split("dB")[0])


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    root = tmp_path_factory.mktemp("clips")
    silent, voiced, music = root / "silent.mp4", root / "voiced.mp4", root / "music.wav"
    _make_video(silent)
    _make_video(voiced, audio=True)
    _make_music(music)
    return SimpleNamespace(silent=silent, voiced=voiced, music=music)


@pytest.fixture
async def edit(monkeypatch, tmp_path, async_client, printer_factory, archive_factory, db_session, clips):
    """An archive showing a real video, and the editor's runtime rooted in the test's own directory."""
    monkeypatch.setattr(download_routes.settings, "base_dir", tmp_path)
    monkeypatch.setattr(timelapse_processor, "get_ffmpeg_path", lambda: FFMPEG)
    timelapse_edit_runtime.reopen()
    timelapse_edit_runtime.root = tmp_path / "timelapse-edits"
    printer = await printer_factory()

    async def make(source: Path = clips.silent, *, name: str = "Lamp"):
        relative = f"archive/{uuid.uuid4().hex[:8]}/timelapse/video.mp4"
        (tmp_path / relative).parent.mkdir(parents=True)
        shutil.copyfile(source, tmp_path / relative)
        archive = await archive_factory(printer.id, print_name=name, timelapse_path=relative)
        db_session.add(
            ArchiveMediaAsset(
                asset_uuid=str(uuid.uuid4()),
                archive_id=archive.id,
                archive_identity=archive.recovery_identity,
                role=ROLE_PRIMARY,
                revision=1,
                relative_path=relative,
                origin="upload",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        await db_session.commit()
        return SimpleNamespace(
            archive=archive,
            archive_id=archive.id,
            identity=archive.recovery_identity,
            relative=relative,
            file=tmp_path / relative,
            base=f"/api/v1/archives/{archive.id}/timelapse",
        )

    box = await make()
    box.make = make
    box.root = tmp_path
    try:
        yield box
    finally:
        assert await timelapse_edit_runtime.quiesce(20)
        timelapse_edit_runtime.reopen()
        timelapse_edit_runtime.root = None


async def _until(client, url, phases=TERMINAL, headers=None, seconds=60.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        response = await client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["phase"] in phases:
            return body
        assert asyncio.get_running_loop().time() < deadline, body
        await asyncio.sleep(0.05)


async def _start(client, box, *, content: bytes | None = None, headers=None, expect=202, **params):
    response = await client.post(f"{box.base}/process", params=params, content=content, headers=headers)
    assert response.status_code == expect, response.text
    return response.json()


async def _ended(client, url, job_id, headers=None, seconds=60.0):
    """The job's final status, read once after its own ``done`` event.

    ⚠️ Not polled over HTTP while it ends: every session of a test shares ONE
    connection, so a status request's own commit would release the savepoint
    the publication is inside ("no such savepoint"). In production each session
    has a connection of its own.
    """
    own = timelapse_edit_runtime.jobs[job_id]
    await asyncio.wait_for(own.done.wait(), seconds)
    response = await client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _finish(client, box, **kwargs):
    job = await _start(client, box, **kwargs)
    return await _ended(client, f"{box.base}/edit-jobs/{job['job_id']}", job["job_id"])


async def _rows(db, box):
    """The archive's registry rows as plain tuples, read fresh."""
    found = await db.execute(
        select(
            ArchiveMediaAsset.role,
            ArchiveMediaAsset.origin,
            ArchiveMediaAsset.relative_path,
            ArchiveMediaAsset.asset_uuid,
            ArchiveMediaAsset.content_sha256,
            ArchiveMediaAsset.size_bytes,
        )
        .where(ArchiveMediaAsset.archive_id == box.archive_id)
        .order_by(ArchiveMediaAsset.revision)
    )
    return [SimpleNamespace(**row._mapping) for row in found.all()]


async def _pointer(db, box) -> str:
    return (await db.execute(select(PrintArchive.timelapse_path).where(PrintArchive.id == box.archive_id))).scalar_one()


def _hang(monkeypatch, *, progress: bool = True):
    """Replace the encoder with a child that reports progress and never finishes."""
    line = "print('out_time_us=500000', flush=True); " if progress else ""
    code = f"import time; {line}time.sleep(120)"
    monkeypatch.setattr(
        timelapse_processor.TimelapseProcessor,
        "edit_command",
        lambda self, output_path, **_kwargs: [sys.executable, "-c", code],
    )


def _no_leftovers(box) -> None:
    assert not list(box.root.rglob("*.editing.mp4")) and not list(box.root.rglob("output.mp4"))
    scratch = box.root / "timelapse-edits"
    assert not scratch.exists() or not list(scratch.iterdir())


class TestReplace:
    async def test_a_trim_and_a_speed_change_replace_the_video_through_the_seam(self, async_client, edit, db_session):
        done = await _finish(async_client, edit, trim_start=0.5, trim_end=1.5, speed=2.0, save_mode="replace")
        assert done["phase"] == "done" and done["progress"] == 1.0, done
        pointer = await _pointer(db_session, edit)
        assert pointer != edit.relative  # a name of its own; never written over the file readers followed
        info = _probe(edit.root / pointer)
        assert 0.3 <= info["duration"] <= 0.8  # one second, played twice as fast
        assert info["size"] == (160, 120)  # the source's resolution is kept (OD-6)
        assert not edit.file.exists()  # the replaced video went after the commit
        rows = await _rows(db_session, edit)
        assert [(row.role, row.origin) for row in rows] == [(ROLE_PRIMARY, "editor")]
        assert rows[0].content_sha256 and rows[0].size_bytes == (edit.root / pointer).stat().st_size
        _no_leftovers(edit)

    async def test_a_slowed_down_video_is_not_cut_back_to_its_old_length(self, async_client, edit, db_session):
        await _finish(async_client, edit, trim_start=0, trim_end=2.0, speed=0.5, save_mode="replace")
        info = _probe(edit.root / await _pointer(db_session, edit))
        assert info["duration"] >= 3.5  # two seconds at half speed are four

    async def test_progress_stays_below_one_until_the_publication(self, async_client, edit, monkeypatch):
        _hang(monkeypatch)
        job = await _start(async_client, edit, save_mode="replace")
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        seen = await _until(async_client, url, phases=("encoding",))
        await asyncio.sleep(0.6)
        seen = (await async_client.get(url)).json()
        # The stand-in reported half a second of a two-second video: progress moved, and is not 100 %.
        assert 0 < seen["progress"] < 1.0, seen
        await async_client.delete(url)
        assert (await _until(async_client, url))["phase"] == "cancelled"


class TestSaveAsNew:
    async def test_the_original_stays_and_the_new_video_has_a_row_of_its_own(self, async_client, edit, db_session):
        done = await _finish(async_client, edit, trim_start=0, trim_end=1.0, save_mode="new")
        assert done["phase"] == "done", done
        assert await _pointer(db_session, edit) == edit.relative
        assert edit.file.exists()
        rows = await _rows(db_session, edit)
        assert [row.role for row in rows] == [ROLE_PRIMARY, ROLE_VARIANT]
        saved = edit.root / rows[1].relative_path
        assert saved.is_file() and 0.7 <= _probe(saved)["duration"] <= 1.3
        assert done["result"] == {"asset_uuid": rows[1].asset_uuid, "save_mode": "new"}

        # It can be found, downloaded through the chooser's list, and removed.
        listed = (await async_client.get(f"{edit.base}/variants")).json()["variants"]
        assert [item["asset"] for item in listed] == [rows[1].asset_uuid]
        sources = (await async_client.get(f"/api/v1/archives/{edit.archive_id}/media-sources")).json()
        assert [item["variant"] for item in sources["local"]["items"]] == [False, True]
        gone = await async_client.delete(f"{edit.base}/variants/{rows[1].asset_uuid}")
        assert gone.status_code == 200, gone.text
        assert not saved.exists() and edit.file.exists()
        assert [row.role for row in await _rows(db_session, edit)] == [ROLE_PRIMARY]
        again = await async_client.delete(f"{edit.base}/variants/{rows[1].asset_uuid}")
        assert again.status_code == 404


class TestSound:
    async def test_music_is_laid_over_a_silent_video_and_does_not_cut_it_short(
        self, async_client, edit, db_session, clips
    ):
        # The track is half a second, the video two: it is looped, the video keeps its length.
        await _finish(async_client, edit, save_mode="replace", audio_name="track.wav", content=clips.music.read_bytes())
        info = _probe(edit.root / await _pointer(db_session, edit))
        assert info["audio"] and info["duration"] >= 1.7

    async def test_the_volume_reaches_the_output(self, async_client, edit, db_session, clips):
        loud = await edit.make()
        quiet = await edit.make()
        for box, volume in ((loud, 1.0), (quiet, 0.1)):
            await _finish(
                async_client,
                box,
                save_mode="replace",
                audio_name="track.wav",
                audio_volume=volume,
                content=clips.music.read_bytes(),
            )
        loud_db = _mean_volume(edit.root / await _pointer(db_session, loud))
        quiet_db = _mean_volume(edit.root / await _pointer(db_session, quiet))
        assert loud_db - quiet_db > 12  # a tenth of the volume is 20 dB; the preview-only slider gave 0

    async def test_mute_gives_an_output_without_sound(self, async_client, edit, db_session, clips):
        voiced = await edit.make(clips.voiced)
        await _finish(async_client, voiced, save_mode="replace", mute="true")
        assert not _probe(edit.root / await _pointer(db_session, voiced))["audio"]
        with_music = await edit.make()
        await _finish(
            async_client,
            with_music,
            save_mode="replace",
            mute="true",
            audio_name="track.wav",
            content=clips.music.read_bytes(),
        )
        assert not _probe(edit.root / await _pointer(db_session, with_music))["audio"]

    async def test_a_source_with_its_own_sound_keeps_it_through_a_speed_change(
        self, async_client, edit, db_session, clips
    ):
        voiced = await edit.make(clips.voiced)
        await _finish(async_client, voiced, save_mode="replace", speed=2.0)
        info = _probe(edit.root / await _pointer(db_session, voiced))
        assert info["audio"] and info["duration"] <= 1.4

    async def test_a_silent_source_at_another_speed_encodes(self, async_client, edit, db_session):
        done = await _finish(async_client, edit, save_mode="replace", speed=0.5)
        assert done["phase"] == "done", done

    async def test_a_file_that_is_not_audio_is_refused_and_leaves_nothing(self, async_client, edit):
        refused = await _start(
            async_client, edit, save_mode="replace", audio_name="track.mp3", content=b"not audio at all", expect=400
        )
        assert refused["detail"]["error"] == "audio_invalid"
        assert (await async_client.get(f"{edit.base}/edit-jobs")).json() == {"jobs": []}
        _no_leftovers(edit)

    async def test_music_over_the_cap_is_refused_while_it_arrives(self, async_client, edit, monkeypatch):
        monkeypatch.setattr(timelapse_editor, "AUDIO_LIMIT", 64 * 1024)

        async def body():
            for _ in range(8):
                yield b"x" * (32 * 1024)  # no Content-Length: only the count can refuse it

        refused = await async_client.post(
            f"{edit.base}/process", params={"save_mode": "replace", "audio_name": "big.mp3"}, content=body()
        )
        assert refused.status_code == 413 and refused.json()["detail"]["error"] == "audio_too_large"
        _no_leftovers(edit)

    async def test_an_unknown_extension_is_refused(self, async_client, edit):
        refused = await _start(async_client, edit, audio_name="track.exe", content=b"MZ", expect=400)
        assert refused["detail"]["error"] == "audio_invalid"


class TestNumbers:
    @pytest.mark.parametrize(
        "params",
        [
            {"speed": 8},
            {"speed": 0.1},
            {"trim_start": -1},
            {"trim_start": 1.5, "trim_end": 1.0},
            {"trim_start": 5},
            {"trim_end": 60},
            {"audio_volume": 1.5},
            {"save_mode": "sideways"},
            {"speed": "nan"},
            {"trim_end": "inf"},
        ],
    )
    async def test_numbers_outside_the_video_are_refused(self, async_client, edit, params):
        response = await async_client.post(f"{edit.base}/process", params={"save_mode": "replace", **params})
        assert response.status_code in (400, 422), response.text
        assert (await async_client.get(f"{edit.base}/edit-jobs")).json() == {"jobs": []}

    async def test_an_archive_without_a_video_is_404(self, async_client, edit, archive_factory, printer_factory):
        printer = await printer_factory()
        bare = await archive_factory(printer.id, print_name="Bare")
        response = await async_client.post(f"/api/v1/archives/{bare.id}/timelapse/process")
        assert response.status_code == 404


class TestOwnership:
    async def test_one_edit_per_video_version(self, async_client, edit, monkeypatch):
        _hang(monkeypatch)
        job = await _start(async_client, edit, save_mode="replace")
        second = await _start(async_client, edit, save_mode="new", expect=409)
        assert second["detail"]["error"] == "edit_in_progress"
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await async_client.delete(url)
        assert (await _until(async_client, url))["phase"] == "cancelled"

    async def test_a_cancel_kills_the_encoder_and_removes_its_files(self, async_client, edit, db_session, monkeypatch):
        _hang(monkeypatch)
        job = await _start(async_client, edit, save_mode="replace")
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await _until(async_client, url, phases=("encoding",))
        assert media_child.live() == 1
        asked = (await async_client.delete(url)).json()
        assert asked["phase"] in ("cancelling", "cancelled")
        assert (await _until(async_client, url))["phase"] == "cancelled"
        assert media_child.live() == 0  # ``cancelled`` was said only after the reap
        assert await _pointer(db_session, edit) == edit.relative and edit.file.exists()
        _no_leftovers(edit)

    async def test_another_users_edit_is_the_same_404_as_none(self, async_client, edit, db_session, monkeypatch):
        _hang(monkeypatch)
        job = await _start(async_client, edit, save_mode="replace")
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await _user(db_session, "other-editor", ["archives:read_all", "archives:update_all"])
        other = await _jwt("other-editor")
        assert (await async_client.get(url, headers=other)).status_code == 404
        assert (await async_client.delete(url, headers=other)).status_code == 404
        assert (await async_client.get(f"{edit.base}/edit-jobs", headers=other)).json() == {"jobs": []}
        assert (await async_client.get(f"{edit.base}/edit-jobs/{uuid.uuid4().hex}")).status_code == 404
        await async_client.delete(url)
        assert (await _until(async_client, url))["phase"] == "cancelled"

    async def test_reading_alone_does_not_start_an_edit(self, async_client, edit, db_session):
        await _user(db_session, "reader", ["archives:read_all"])
        response = await async_client.post(
            f"{edit.base}/process", params={"save_mode": "replace"}, headers=await _jwt("reader")
        )
        assert response.status_code == 403
        assert (await async_client.get(f"{edit.base}/edit-jobs")).json() == {"jobs": []}

    async def test_a_dialog_opened_again_finds_its_edit(self, async_client, edit, monkeypatch):
        _hang(monkeypatch)
        job = await _start(async_client, edit, save_mode="replace")
        listed = (await async_client.get(f"{edit.base}/edit-jobs")).json()["jobs"]
        assert [item["job_id"] for item in listed] == [job["job_id"]]
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await async_client.delete(url)
        await _until(async_client, url)


class TestLateResults:
    async def test_a_video_replaced_during_the_encode_refuses_the_result(self, async_client, edit, db_session, clips):
        """The encode finishes; what it started from is no longer what the archive shows."""
        from backend.app.core.config import settings
        from backend.app.services import archive_media, queue_sources

        gate, encoded = asyncio.Event(), asyncio.Event()
        real = timelapse_edit_runtime._publish

        async def held(job):
            encoded.set()  # the encoder has ended and its output was accepted
            await gate.wait()
            return await real(job)

        timelapse_edit_runtime._publish = held
        try:
            job = await _start(async_client, edit, save_mode="replace")
            url = f"{edit.base}/edit-jobs/{job['job_id']}"
            await asyncio.wait_for(encoded.wait(), 60)
            # Somebody uploads another video meanwhile.
            staged = edit.file.parent / ".upload.part"
            shutil.copyfile(clips.voiced, staged)
            from backend.app.core import database

            async with database.async_session() as other:
                shown = (
                    await other.execute(select(PrintArchive).where(PrintArchive.id == edit.archive_id))
                ).scalar_one()
                token = await archive_media.primary_token(other, shown)
            published = await archive_media.publish_primary(
                db_session,
                edit.archive_id,
                staged=staged,
                directory=edit.file.parent,
                filename="uploaded.mp4",
                base_dir=settings.base_dir,
                origin="upload",
                expected_identity=edit.identity,
                expected_epoch=queue_sources.current_epoch(),
                expected_token=token,
            )
            assert published.ok
            gate.set()
            ended = await _ended(async_client, url, job["job_id"])
        finally:
            timelapse_edit_runtime._publish = real
            gate.set()
        assert (ended["phase"], ended["error"]) == ("failed", "source_changed")
        assert await _pointer(db_session, edit) == published.relative_path  # the upload stands
        assert _probe(edit.root / published.relative_path)["audio"]
        _no_leftovers(edit)

    async def test_a_lost_right_stops_the_edit_and_publishes_nothing(self, async_client, edit, db_session, monkeypatch):
        from backend.app.models.group import Group

        _hang(monkeypatch)
        user = await _user(db_session, "revoked-editor", ["archives:read_all", "archives:update_all"])
        headers = await _jwt("revoked-editor")
        job = await _start(async_client, edit, save_mode="replace", headers=headers)
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await _until(async_client, url, phases=("encoding",), headers=headers)
        group = (await db_session.execute(select(Group).where(Group.name == "grp-revoked-editor"))).scalar_one()
        group.permissions = ["archives:read_all"]
        await db_session.commit()
        from backend.app.services.live_authority import live_authority

        live_authority.invalidate()
        # The job ends by itself, within the coordinator's lease; its owner can no longer ask about it.
        deadline = asyncio.get_running_loop().time() + 30
        own = timelapse_edit_runtime.jobs[job["job_id"]]
        while not own.done.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.1)
        assert (own.phase, own.error) == ("failed", "authority_lost")
        assert media_child.live() == 0
        assert await _pointer(db_session, edit) == edit.relative
        assert (await async_client.get(url, headers=headers)).status_code == 403
        assert user.id
        _no_leftovers(edit)


class TestWhereTheEncoderWrites:
    async def test_the_half_encoded_file_stays_out_of_the_archive_tree(self, async_client, edit, monkeypatch):
        """On one volume the encoder writes in the job's own directory: a backup never meets it."""
        _hang(monkeypatch)
        job = await _start(async_client, edit, save_mode="replace")
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await _until(async_client, url, phases=("encoding",))
        own = timelapse_edit_runtime.jobs[job["job_id"]]
        assert own.staged == edit.root / "timelapse-edits" / job["job_id"] / "output.mp4"
        assert edit.root / "archive" not in own.staged.parents
        await async_client.delete(url)
        assert (await _until(async_client, url))["phase"] == "cancelled"
        _no_leftovers(edit)

    async def test_on_another_volume_it_writes_beside_the_video_and_names_the_file(
        self, async_client, edit, db_session, monkeypatch
    ):
        """The archive on a volume of its own: the rename needs the staged file there."""
        noted: dict = {}
        beside = lambda job: job.source.with_name(timelapse_editor.staged_name(job.job_id))  # noqa: E731
        monkeypatch.setattr(timelapse_editor, "_staging_place", beside)
        real = timelapse_edit_runtime._publish

        async def looking(job):
            note = job.directory / timelapse_editor.OUTPUT_NOTE
            noted["note"] = note.read_text(encoding="utf-8")
            noted["staged"] = job.staged
            return await real(job)

        monkeypatch.setattr(timelapse_edit_runtime, "_publish", looking)
        done = await _finish(async_client, edit, save_mode="replace")
        assert done["phase"] == "done", done
        assert noted["staged"].parent == edit.file.parent
        # What a crash would have left is named, relative to the data directory, for the next startup.
        assert (edit.root / noted["note"]) == noted["staged"]
        assert (edit.root / await _pointer(db_session, edit)).is_file()
        _no_leftovers(edit)


class TestRestoreAndRestart:
    async def test_a_restore_stops_the_encoder_and_refuses_new_edits(self, async_client, edit, db_session, monkeypatch):
        _hang(monkeypatch)
        job = await _start(async_client, edit, save_mode="replace")
        await _until(async_client, f"{edit.base}/edit-jobs/{job['job_id']}", phases=("encoding",))
        assert await timelapse_edit_runtime.quiesce(20) is True
        own = timelapse_edit_runtime.jobs[job["job_id"]]
        assert (own.phase, own.error) == ("failed", "interrupted")
        assert media_child.live() == 0
        refused = await _start(async_client, edit, save_mode="replace", expect=503)
        assert refused["detail"]["error"] == "restoring"
        assert await _pointer(db_session, edit) == edit.relative
        _no_leftovers(edit)

    async def test_an_unreaped_encoder_refuses_the_drain_and_keeps_its_output(self, async_client, edit, monkeypatch):
        _hang(monkeypatch)
        monkeypatch.setattr(media_child, "TERMINATE_GRACE", 0.2)
        monkeypatch.setattr(media_child, "REAP_WAIT", 0.4)
        spared: list = []
        monkeypatch.setattr(
            media_child._Watcher, "_signal", staticmethod(lambda process, *, kill: spared.append(process))
        )
        job = await _start(async_client, edit, save_mode="replace")
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await _until(async_client, url, phases=("encoding",))
        try:
            assert await timelapse_edit_runtime.quiesce(3) is False  # a restore asking now is refused
            own = timelapse_edit_runtime.jobs[job["job_id"]]
            assert (own.phase, own.error) == ("failed", "process_unreaped")
            # Its scratch directory is still there: nothing of a possibly-live writer was touched.
            assert (edit.root / "timelapse-edits" / job["job_id"] / timelapse_editor.MARKER).is_file()
        finally:
            for process in spared:
                process.kill()
        deadline = asyncio.get_running_loop().time() + 15
        while media_child.live():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        # What the next startup does with what such a run leaves.
        removed, kept = timelapse_editor.clean_scratch(edit.root / "timelapse-edits", edit.root)
        assert (removed, kept) == (1, 0)

    async def test_startup_removes_a_crashed_runs_scratch_and_its_named_output_only(self, edit):
        root = edit.root / "timelapse-edits"
        job_id = uuid.uuid4().hex
        directory = timelapse_editor._make_directory(root, job_id)
        staged = edit.file.with_name(timelapse_editor.staged_name(job_id))
        staged.write_bytes(b"half an encode")
        (directory / timelapse_editor.OUTPUT_NOTE).write_text(str(staged.relative_to(edit.root)), encoding="utf-8")
        # A directory that names somebody else's file, and a foreign directory without our marker.
        liar = timelapse_editor._make_directory(root, uuid.uuid4().hex)
        (liar / timelapse_editor.OUTPUT_NOTE).write_text(edit.relative, encoding="utf-8")
        foreign = root / "not-ours"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("x", encoding="utf-8")

        removed, kept = timelapse_editor.clean_scratch(root, edit.root)
        assert (removed, kept) == (2, 1)
        assert not staged.exists() and not directory.exists() and not liar.exists()
        assert edit.file.exists()  # the archive's own video was named by a note and left alone
        assert (foreign / "keep.txt").is_file()
