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
import os
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
from backend.app.services.encoder_slot import encoder_slot
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
    encoder_slot.reset()
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
        assert not encoder_slot.busy  # every edit gave the encoder back
        encoder_slot.reset()


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
    params.setdefault("save_mode", "replace")
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
        response = await async_client.post(
            f"/api/v1/archives/{bare.id}/timelapse/process", params={"save_mode": "replace"}
        )
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


class TestTheOldRequestIsRefused:
    """Codex E1: a form read as the new request had no parameters at all — every field took a default,
    and an old client's "save as new" replaced the original."""

    @pytest.mark.parametrize("encoding", ["multipart", "urlencoded"])
    async def test_a_form_is_refused_before_any_job_exists(self, async_client, edit, db_session, encoding):
        before = edit.file.read_bytes()
        fields = {"save_mode": "new", "speed": "2", "trim_start": "0"}
        options = (
            {"files": {name: (None, value) for name, value in fields.items()}}
            if encoding == "multipart"
            else {"data": fields}
        )
        response = await async_client.post(f"{edit.base}/process", **options)
        assert response.status_code == 415, response.text
        assert response.json()["detail"]["error"] == "legacy_request"
        assert [job for job in timelapse_edit_runtime.jobs.values() if job.archive_id == edit.archive_id] == []
        assert edit.file.read_bytes() == before and await _pointer(db_session, edit) == edit.relative
        _no_leftovers(edit)

    async def test_a_form_with_a_music_part_is_refused_too(self, async_client, edit, clips):
        response = await async_client.post(
            f"{edit.base}/process",
            data={"save_mode": "new"},
            files={"audio": ("track.wav", clips.music.read_bytes(), "audio/wav")},
        )
        assert response.status_code == 415
        assert timelapse_edit_runtime.jobs == {}

    async def test_a_request_that_does_not_say_how_to_save_keeps_the_original(self, async_client, edit, db_session):
        """The default is the mode that destroys nothing; replacing is always asked for in words."""
        response = await async_client.post(f"{edit.base}/process", params={"speed": 2})
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["save_mode"] == "new"
        ended = await _ended(async_client, f"{edit.base}/edit-jobs/{job['job_id']}", job["job_id"])
        assert ended["phase"] == "done", ended
        assert edit.file.is_file() and await _pointer(db_session, edit) == edit.relative

    async def test_the_current_request_still_passes(self, async_client, edit, db_session, clips):
        done = await _finish(
            async_client, edit, save_mode="new", audio_name="track.wav", content=clips.music.read_bytes()
        )
        assert done["phase"] == "done", done
        assert edit.file.is_file() and await _pointer(db_session, edit) == edit.relative


class TestOneEncoder:
    """Codex E2: an edit delayed while its directory was prepared started a second encoder beside the
    one that had overtaken it."""

    async def test_two_edits_prepared_at_different_speeds_never_encode_together(self, async_client, edit, monkeypatch):
        second = await edit.make()
        staged, go, unlock = asyncio.Event(), asyncio.Event(), asyncio.Event()
        running: list = []
        peak = 0
        original = timelapse_edit_runtime.prepare_directory

        async def prepare(job):
            await original(job)
            if job.archive_id == edit.archive_id:
                staged.set()
                await go.wait()  # the first edit is held here while the second overtakes it

        async def encode(job):
            nonlocal peak
            running.append(job.job_id)
            peak = max(peak, len(running))
            try:
                await unlock.wait()
            finally:
                running.remove(job.job_id)
            raise RuntimeError("the end of a controlled encode")

        monkeypatch.setattr(timelapse_edit_runtime, "prepare_directory", prepare)
        monkeypatch.setattr(timelapse_edit_runtime, "_encode", encode)
        first = asyncio.create_task(async_client.post(f"{edit.base}/process", params={"save_mode": "replace"}))
        try:
            await asyncio.wait_for(staged.wait(), 15)
            assert (
                await async_client.post(f"{second.base}/process", params={"save_mode": "replace"})
            ).status_code == 202
            for _ in range(100):
                if running:
                    break
                await asyncio.sleep(0.02)
            assert len(running) == 1
            go.set()
            assert (await first).status_code == 202
            await asyncio.sleep(0.6)  # the first edit has had every chance to start its encoder
            assert len(running) == 1 and peak == 1
        finally:
            go.set()
            unlock.set()
            await first
            await asyncio.gather(*(job.done.wait() for job in timelapse_edit_runtime.jobs.values() if job.task))
        assert peak == 1

    async def test_a_cancelled_waiter_gives_its_place_to_the_one_behind_it(
        self, async_client, edit, db_session, monkeypatch
    ):
        _hang(monkeypatch)
        holder = await _start(async_client, edit)
        await _until(async_client, f"{edit.base}/edit-jobs/{holder['job_id']}", phases=("encoding",))
        second_box, third_box = await edit.make(), await edit.make()
        second = await _start(async_client, second_box)
        # A second account: one person may have two edits at a time.
        await _user(db_session, "queue-editor", ["archives:read_all", "archives:update_all"])
        other = await _jwt("queue-editor")
        third = await _start(async_client, third_box, headers=other)
        own = timelapse_edit_runtime.jobs
        assert (own[second["job_id"]].phase, own[third["job_id"]].phase) == ("queued", "queued")
        # The one in the middle leaves the queue; then the holder ends.
        await async_client.delete(f"{second_box.base}/edit-jobs/{second['job_id']}")
        await asyncio.wait_for(own[second["job_id"]].done.wait(), 10)
        assert own[second["job_id"]].phase == "cancelled"
        await async_client.delete(f"{edit.base}/edit-jobs/{holder['job_id']}")
        await asyncio.wait_for(own[holder["job_id"]].done.wait(), 15)
        for _ in range(100):
            if own[third["job_id"]].phase == "encoding":
                break
            await asyncio.sleep(0.05)
        assert own[third["job_id"]].phase == "encoding"  # not stuck behind a waiter that left
        assert media_child.live() == 1
        await async_client.delete(f"{third_box.base}/edit-jobs/{third['job_id']}", headers=other)
        await asyncio.wait_for(own[third["job_id"]].done.wait(), 15)


class TestCancelAndThePublication:
    """Codex E3: a cancel answered 200 while the result was being hashed, and the original was replaced anyway."""

    async def test_a_cancel_accepted_while_the_result_is_hashed_keeps_the_original(
        self, async_client, edit, db_session, monkeypatch
    ):
        import threading

        real = timelapse_editor._sha256
        entered, release = threading.Event(), threading.Event()

        def digest(path):
            entered.set()
            assert release.wait(20)
            return real(path)

        monkeypatch.setattr(timelapse_editor, "_sha256", digest)
        before = edit.file.read_bytes()
        job = await _start(async_client, edit)
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        try:
            assert await asyncio.to_thread(entered.wait, 30)
            response = await async_client.delete(url)
        finally:
            release.set()
        assert response.status_code == 200, response.text
        ended = await _ended(async_client, url, job["job_id"])
        assert ended["phase"] == "cancelled", ended
        assert edit.file.read_bytes() == before and await _pointer(db_session, edit) == edit.relative
        _no_leftovers(edit)

    @pytest.mark.parametrize("mode", ["replace", "new"])
    async def test_a_cancel_accepted_while_the_publication_waits_for_its_locks_publishes_nothing(
        self, async_client, edit, db_session, monkeypatch, mode
    ):
        """The seam asks the job once more under its own locks; the cancel arrived before that."""
        from backend.app.services import archive_media

        waiting, go = asyncio.Event(), asyncio.Event()
        real = archive_media._load_target

        async def held(db, archive_id, identity, epoch):
            waiting.set()
            await go.wait()
            return await real(db, archive_id, identity, epoch)

        monkeypatch.setattr(archive_media, "_load_target", held)
        job = await _start(async_client, edit, save_mode=mode)
        own = timelapse_edit_runtime.jobs[job["job_id"]]
        try:
            await asyncio.wait_for(waiting.wait(), 60)
            assert timelapse_edit_runtime.request_cancel(own) is True  # under the locks, not yet past the last word
        finally:
            go.set()
        await asyncio.wait_for(own.done.wait(), 30)
        assert (own.phase, own.no_return) == ("cancelled", False)
        assert edit.file.is_file() and await _pointer(db_session, edit) == edit.relative
        assert [row.role for row in await _rows(db_session, edit)] == [ROLE_PRIMARY]
        _no_leftovers(edit)

    async def test_past_the_point_of_no_return_the_cancel_is_refused_and_the_edit_completes(
        self, async_client, edit, db_session, monkeypatch
    ):
        from backend.app.services import archive_media

        past, go = asyncio.Event(), asyncio.Event()
        real = archive_media._next_revision
        own_job: dict = {}

        async def held(db, current):
            # Called by the seam right after it asked the job its last word.
            if own_job and own_job["job"].no_return:
                past.set()
                await go.wait()
            return await real(db, current)

        monkeypatch.setattr(archive_media, "_next_revision", held)
        job = await _start(async_client, edit)
        own_job["job"] = timelapse_edit_runtime.jobs[job["job_id"]]
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        try:
            await asyncio.wait_for(past.wait(), 60)
            refused = timelapse_edit_runtime.request_cancel(own_job["job"])
        finally:
            go.set()
        assert refused is False
        await asyncio.wait_for(own_job["job"].done.wait(), 30)
        assert own_job["job"].phase == "done"
        assert await _pointer(db_session, edit) != edit.relative  # it completed, as the refusal said it would
        # And the door says so: 409, never a 200 the result contradicts.
        again = await async_client.delete(url)
        assert again.status_code == 409 and again.json()["detail"]["error"] == "cancel_too_late"


class TestTheAdmittedInput:
    """Codex E4: the job kept a PATH; another video put under it after admission became the result."""

    @staticmethod
    def _red(tmp_path: Path) -> Path:
        other = tmp_path / "other.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:size=160x120:duration=2:rate=10"]
            + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(other)],
            check=True,
            timeout=120,
        )
        return other

    @staticmethod
    def _first_pixel(video: Path) -> tuple:
        raw = subprocess.check_output(
            [FFMPEG, "-v", "error", "-i", str(video), "-frames:v", "1", "-vf", "scale=1:1"]
            + ["-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            timeout=60,
        )
        return tuple(raw[:3])

    async def test_a_video_swapped_in_after_admission_never_reaches_the_result(
        self, async_client, edit, db_session, tmp_path, monkeypatch
    ):
        other = self._red(tmp_path)
        size = max(edit.file.stat().st_size, other.stat().st_size)
        for path in (other, edit.file):
            with path.open("ab") as handle:
                handle.truncate(size)
        stamp = edit.file.stat()
        original = timelapse_edit_runtime.prepare_directory

        async def swap_after(job):
            await original(job)
            other.replace(edit.file)  # the same name, the same size, the same time — other bytes
            os.utime(edit.file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

        monkeypatch.setattr(timelapse_edit_runtime, "prepare_directory", swap_after)
        done = await _finish(async_client, edit, save_mode="new")
        assert done["phase"] == "done", done
        saved = edit.root / (await _rows(db_session, edit))[-1].relative_path
        red, green, blue = self._first_pixel(saved)
        assert not (red > 220 and green < 40 and blue < 40), (red, green, blue)

    async def test_a_video_swapped_in_before_the_pin_refuses_the_edit(
        self, async_client, edit, db_session, tmp_path, monkeypatch
    ):
        """Between the archive being read and the place being prepared: nothing is encoded."""
        other = self._red(tmp_path)
        real = timelapse_editor._make_directory

        def swap_then_make(root, job_id):
            other.replace(edit.file)
            return real(root, job_id)

        monkeypatch.setattr(timelapse_editor, "_make_directory", swap_then_make)
        refused = await _start(async_client, edit, expect=409)
        assert refused["detail"]["error"] == "source_changed"
        assert timelapse_edit_runtime.jobs == {}
        assert [row.role for row in await _rows(db_session, edit)] == [ROLE_PRIMARY]
        _no_leftovers(edit)

    async def test_the_pin_is_the_admitted_file_and_goes_with_the_job(self, async_client, edit, monkeypatch):
        _hang(monkeypatch)
        job = await _start(async_client, edit)
        url = f"{edit.base}/edit-jobs/{job['job_id']}"
        await _until(async_client, url, phases=("encoding",))
        own = timelapse_edit_runtime.jobs[job["job_id"]]
        assert own.pinned.parent == own.directory and own.pinned.read_bytes() == edit.file.read_bytes()
        await async_client.delete(url)
        assert (await _until(async_client, url))["phase"] == "cancelled"
        assert edit.file.is_file()  # removing the pin removed a name, not the archive's video
        _no_leftovers(edit)

    async def test_without_hard_links_the_pin_is_a_checked_copy(self, async_client, edit, db_session, monkeypatch):
        def no_links(_source, _target):
            raise OSError("this volume has no hard links")

        monkeypatch.setattr(timelapse_editor.os, "link", no_links)
        done = await _finish(async_client, edit, save_mode="new")
        assert done["phase"] == "done", done
        assert edit.file.is_file() and len(await _rows(db_session, edit)) == 2
