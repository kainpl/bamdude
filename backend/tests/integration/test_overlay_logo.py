"""The installation's logo on the stream overlay (G09; upstream 67d3e7e86, owner's decision 2026-11-15).

One logo per installation. It is uploaded, replaced and removed under ``settings:update``, never stored as
sent (decoded, turned upright, saved again as a PNG without metadata), and read by the overlay OBS opens
through its own route under the long-lived ``overlay`` token — the one picture such a token opens, because it
is the installation's branding and not a user's resource.
"""

from __future__ import annotations

import io
import os
import time

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import update

from backend.app.core.config import settings
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.services import overlay_logo
from backend.tests.integration.test_long_lived_tokens_api import _create_user, _login, _user_headers

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

UPLOAD = "/api/v1/settings/overlay-logo"
INFO = "/api/v1/settings/overlay-logo/info"
STREAM_LOGO = "/api/v1/overlay-branding/logo"


def _image(fmt: str = "PNG", size: tuple[int, int] = (64, 32), colour="red", **save) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, fmt, **save)
    return buffer.getvalue()


def _file(content: bytes, name: str = "logo.png", mime: str = "image/png") -> dict:
    return {"file": (name, content, mime)}


async def _token(async_client: AsyncClient, scope: str = "overlay") -> dict:
    answer = await async_client.post("/api/v1/auth/tokens", json={"name": scope, "expires_in_days": 7, "scope": scope})
    assert answer.status_code in (200, 201), answer.text
    return answer.json()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    overlay_logo.forget()
    monkeypatch.setattr(overlay_logo, "_unreadable_logged", False)
    yield
    overlay_logo.forget()


# ---------------------------------------------------------------- upload, replace, remove


class TestUpload:
    async def test_an_uploaded_logo_is_saved_and_read_back(self, async_client: AsyncClient):
        answer = await async_client.post(UPLOAD, files=_file(_image()))

        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["exists"] is True and len(body["revision"]) >= 8
        assert (await async_client.get(INFO)).json() == body
        picture = await async_client.get(UPLOAD)
        assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
        with Image.open(io.BytesIO(picture.content)) as saved:
            assert saved.format == "PNG" and saved.size == (64, 32)

    @pytest.mark.parametrize("fmt", ["JPEG", "WEBP"])
    async def test_a_jpeg_or_webp_is_accepted_and_stored_as_png(self, async_client: AsyncClient, fmt):
        answer = await async_client.post(UPLOAD, files=_file(_image(fmt), f"logo.{fmt.lower()}", "image/jpeg"))

        assert answer.status_code == 200, answer.text
        assert (settings.base_dir / "overlay-branding" / "logo.png").read_bytes().startswith(b"\x89PNG")

    async def test_metadata_never_reaches_the_stored_logo(self, async_client: AsyncClient):
        """A phone photo carries GPS and the camera's serial in EXIF; a PNG carries text chunks. The logo is
        composited into a public broadcast: none of it is kept, and the orientation goes into the pixels."""
        exif = Image.Exif()
        exif[0x0112] = 6  # orientation: turn 90° clockwise to show upright
        exif[0x010F] = "SECRET-CAMERA-MAKER"
        jpeg = _image("JPEG", (64, 32), exif=exif.tobytes())
        assert b"SECRET-CAMERA-MAKER" in jpeg

        assert (await async_client.post(UPLOAD, files=_file(jpeg, "a.jpg", "image/jpeg"))).status_code == 200

        stored = (settings.base_dir / "overlay-branding" / "logo.png").read_bytes()
        assert b"SECRET-CAMERA-MAKER" not in stored
        with Image.open(io.BytesIO(stored)) as saved:
            assert saved.size == (32, 64), "the EXIF orientation was not applied to the pixels"
            assert not saved.getexif() and not getattr(saved, "text", {})

    async def test_a_large_logo_is_scaled_down(self, async_client: AsyncClient):
        assert (await async_client.post(UPLOAD, files=_file(_image(size=(1600, 400))))).status_code == 200

        with Image.open(settings.base_dir / "overlay-branding" / "logo.png") as saved:
            assert max(saved.size) == overlay_logo.MAX_SIDE and saved.size[0] == 4 * saved.size[1]

    @pytest.mark.parametrize(
        ("content", "name", "mime"),
        [
            (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "logo.svg", "image/svg+xml"),
            (b"GIF89a" + b"\x00" * 64, "logo.gif", "image/gif"),
            (b"not an image at all", "logo.png", "image/png"),
            (_image()[:40], "logo.png", "image/png"),  # a PNG cut short
            (b"", "logo.png", "image/png"),
        ],
        ids=["svg", "gif", "garbage", "truncated", "empty"],
    )
    async def test_what_is_not_a_png_jpeg_or_webp_is_refused(self, async_client: AsyncClient, content, name, mime):
        answer = await async_client.post(UPLOAD, files=_file(content, name, mime))

        assert answer.status_code == 422, answer.text
        assert (await async_client.get(INFO)).json() == {"exists": False, "revision": None}

    async def test_a_real_gif_is_refused(self, async_client: AsyncClient):
        assert (await async_client.post(UPLOAD, files=_file(_image("GIF"), "a.gif", "image/gif"))).status_code == 422

    async def test_an_animated_picture_is_refused(self, async_client: AsyncClient):
        buffer = io.BytesIO()
        frames = [Image.new("RGB", (16, 16), colour) for colour in ("red", "blue")]
        frames[0].save(buffer, "WEBP", save_all=True, append_images=frames[1:], duration=100)

        answer = await async_client.post(UPLOAD, files=_file(buffer.getvalue(), "a.webp", "image/webp"))

        assert answer.status_code == 422, answer.text

    async def test_too_many_bytes_are_refused(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.setattr(overlay_logo, "MAX_BYTES", 200)

        answer = await async_client.post(UPLOAD, files=_file(_image(size=(300, 300)) + b"\x00" * 300))

        assert answer.status_code == 413, answer.text

    async def test_too_many_pixels_are_refused(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.setattr(overlay_logo, "MAX_PIXELS", 1000)

        answer = await async_client.post(UPLOAD, files=_file(_image(size=(64, 32))))

        assert answer.status_code == 422, answer.text

    async def test_a_refused_replacement_leaves_the_saved_logo(self, async_client: AsyncClient):
        saved = (await async_client.post(UPLOAD, files=_file(_image()))).json()

        assert (await async_client.post(UPLOAD, files=_file(b"garbage"))).status_code == 422

        assert (await async_client.get(INFO)).json() == saved

    async def test_a_replacement_has_a_new_revision_and_the_same_bytes_the_same(self, async_client: AsyncClient):
        first = (await async_client.post(UPLOAD, files=_file(_image(colour="red")))).json()
        again = (await async_client.post(UPLOAD, files=_file(_image(colour="red")))).json()
        second = (await async_client.post(UPLOAD, files=_file(_image(colour="blue")))).json()

        assert again["revision"] == first["revision"]
        assert second["revision"] != first["revision"]

    async def test_removing_it_leaves_nothing_and_may_be_repeated(self, async_client: AsyncClient):
        await async_client.post(UPLOAD, files=_file(_image()))

        for _ in range(2):
            answer = await async_client.delete(UPLOAD)
            assert answer.status_code == 200 and answer.json() == {"exists": False, "revision": None}

        assert (await async_client.get(UPLOAD)).status_code == 404
        assert list((settings.base_dir / "overlay-branding").iterdir()) == []

    async def test_no_temporary_file_is_left_beside_the_logo(self, async_client: AsyncClient):
        await async_client.post(UPLOAD, files=_file(_image(colour="red")))
        await async_client.post(UPLOAD, files=_file(_image(colour="blue")))
        await async_client.post(UPLOAD, files=_file(b"garbage"))

        assert [path.name for path in (settings.base_dir / "overlay-branding").iterdir()] == ["logo.png"]


class TestWhoMayChangeIt:
    async def test_a_user_without_the_settings_right_changes_nothing_but_sees_it(self, async_client: AsyncClient):
        await async_client.post(UPLOAD, files=_file(_image()))
        await _create_user(async_client, "viewer_logo")
        viewer = _user_headers(await _login(async_client, "viewer_logo"))

        assert (await async_client.post(UPLOAD, files=_file(_image()), headers=viewer)).status_code == 403
        assert (await async_client.delete(UPLOAD, headers=viewer)).status_code == 403
        assert (await async_client.get(INFO, headers=viewer)).json()["exists"] is True
        assert (await async_client.get(UPLOAD, headers=viewer)).status_code == 200


# ---------------------------------------------------------------- the read OBS makes


class TestTheOverlayTokensRead:
    async def test_an_overlay_token_reads_the_logo(self, async_client: AsyncClient):
        revision = (await async_client.post(UPLOAD, files=_file(_image()))).json()["revision"]
        token = (await _token(async_client))["token"]

        answer = await async_client.get(STREAM_LOGO, params={"v": revision, "token": token})

        assert answer.status_code == 200
        assert answer.content == (settings.base_dir / "overlay-branding" / "logo.png").read_bytes()
        assert answer.headers["content-type"] == "image/png"
        assert answer.headers["x-content-type-options"] == "nosniff"
        cache = answer.headers["cache-control"]
        assert "private" in cache and "immutable" in cache and "public" not in cache

    async def test_an_address_that_names_another_revision_is_not_kept_by_a_cache(self, async_client: AsyncClient):
        await async_client.post(UPLOAD, files=_file(_image()))
        token = (await _token(async_client))["token"]

        for params in ({"token": token}, {"v": "an-older-one", "token": token}):
            answer = await async_client.get(STREAM_LOGO, params=params)
            assert answer.status_code == 200
            assert "no-cache" in answer.headers["cache-control"] and "private" in answer.headers["cache-control"]

    async def test_without_a_logo_it_is_404(self, async_client: AsyncClient):
        token = (await _token(async_client))["token"]

        assert (await async_client.get(STREAM_LOGO, params={"token": token})).status_code == 404

    @pytest.mark.parametrize("scope", ["camera_stream", "camwall", "monitor"])
    async def test_a_token_of_another_scope_does_not(self, async_client: AsyncClient, scope):
        await async_client.post(UPLOAD, files=_file(_image()))
        token = (await _token(async_client, scope))["token"]

        assert (await async_client.get(STREAM_LOGO, params={"token": token})).status_code == 401

    @pytest.mark.parametrize(
        "params", [{}, {"token": ""}, {"token": "bblt_not-a-token"}], ids=["none", "empty", "junk"]
    )
    async def test_no_token_or_a_made_up_one_does_not(self, async_client: AsyncClient, params):
        await async_client.post(UPLOAD, files=_file(_image()))

        assert (await async_client.get(STREAM_LOGO, params=params)).status_code == 401

    async def test_a_revoked_token_does_not(self, async_client: AsyncClient):
        await async_client.post(UPLOAD, files=_file(_image()))
        created = await _token(async_client)
        assert (await async_client.delete(f"/api/v1/auth/tokens/{created['id']}")).status_code == 204

        assert (await async_client.get(STREAM_LOGO, params={"token": created["token"]})).status_code == 401

    async def test_an_expired_token_does_not(self, async_client: AsyncClient, db_session):
        from datetime import datetime, timedelta

        await async_client.post(UPLOAD, files=_file(_image()))
        created = await _token(async_client)
        await db_session.execute(
            update(LongLivedToken)
            .where(LongLivedToken.id == created["id"])
            .values(expires_at=datetime.utcnow() - timedelta(days=1))
        )
        await db_session.commit()

        assert (await async_client.get(STREAM_LOGO, params={"token": created["token"]})).status_code == 401

    async def test_an_api_key_beside_the_token_is_refused(self, async_client: AsyncClient):
        await async_client.post(UPLOAD, files=_file(_image()))
        token = (await _token(async_client))["token"]

        answer = await async_client.get(STREAM_LOGO, params={"token": token}, headers={"X-API-Key": "bd_made-up"})

        assert answer.status_code in (401, 403)

    async def test_the_route_serves_one_file_and_takes_no_path(self, async_client: AsyncClient):
        await async_client.post(UPLOAD, files=_file(_image()))
        token = (await _token(async_client))["token"]

        for tail in ("/logo.png", "/../logo.png", "/x"):
            assert (await async_client.get(STREAM_LOGO + tail, params={"token": token})).status_code in (401, 404)


# ---------------------------------------------------------------- the feed says whether there is one


class TestTheFeed:
    async def test_the_feed_names_the_logos_revision(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        token = (await _token(async_client))["token"]
        feed = f"/api/v1/printers/{printer.id}/overlay-status"

        assert (await async_client.get(feed, params={"token": token})).json()["logo"] is None

        revision = (await async_client.post(UPLOAD, files=_file(_image()))).json()["revision"]
        assert (await async_client.get(feed, params={"token": token})).json()["logo"] == {"revision": revision}

        await async_client.delete(UPLOAD)
        assert (await async_client.get(feed, params={"token": token})).json()["logo"] is None

    async def test_a_logo_put_there_behind_our_back_is_noticed(self, async_client: AsyncClient, monkeypatch):
        """A restore replaces the directory without telling the writer: the answer is re-read, not remembered
        for the life of the process."""
        monkeypatch.setattr(overlay_logo, "CACHE_SECONDS", 0.0)
        assert (await async_client.get(INFO)).json()["exists"] is False

        directory = settings.base_dir / "overlay-branding"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "logo.png").write_bytes(_image())

        assert (await async_client.get(INFO)).json()["exists"] is True


# ---------------------------------------------------------------- a logo that cannot be read or written


class TestAFolderWeCannotUse:
    """A ``sudo`` restore or a changed Docker volume leaves ``overlay-branding/`` unreadable. Branding is never
    worth a broadcast: the feed keeps answering, without a logo."""

    @staticmethod
    def _unreadable_logo():
        # A directory where the file belongs: it is found, and reading it fails (PermissionError on Windows,
        # IsADirectoryError elsewhere) â€” what a root-owned file does, on every platform a test runs on.
        (settings.base_dir / "overlay-branding" / "logo.png").mkdir(parents=True)

    async def test_the_feed_answers_without_a_logo(self, async_client: AsyncClient, printer_factory, caplog):
        printer = await printer_factory()
        token = (await _token(async_client))["token"]
        self._unreadable_logo()

        with caplog.at_level("WARNING", logger="backend.app.services.overlay_logo"):
            answers = [
                await async_client.get(f"/api/v1/printers/{printer.id}/overlay-status", params={"token": token})
                for _ in range(2)
            ]

        assert [answer.status_code for answer in answers] == [200, 200], answers[0].text
        assert answers[0].json()["logo"] is None
        warnings_ = [record for record in caplog.records if record.name == "backend.app.services.overlay_logo"]
        assert len(warnings_) == 1, "one warning per episode, not one per poll"
        assert "overlay-branding" in warnings_[0].getMessage()
        assert str(settings.base_dir) not in warnings_[0].getMessage()

    async def test_the_feed_survives_any_failure_of_the_branding(
        self, async_client: AsyncClient, printer_factory, monkeypatch
    ):
        async def broken():
            raise RuntimeError("anything at all")

        monkeypatch.setattr(overlay_logo, "current", broken)
        printer = await printer_factory()
        token = (await _token(async_client))["token"]

        answer = await async_client.get(f"/api/v1/printers/{printer.id}/overlay-status", params={"token": token})

        assert answer.status_code == 200, answer.text
        assert answer.json()["logo"] is None

    async def test_the_reads_say_there_is_none(self, async_client: AsyncClient):
        token = (await _token(async_client))["token"]
        self._unreadable_logo()

        assert (await async_client.get(STREAM_LOGO, params={"token": token})).status_code == 404
        assert (await async_client.get(UPLOAD)).status_code == 404
        assert (await async_client.get(INFO)).json() == {"exists": False, "revision": None}

    async def test_a_readable_logo_ends_the_episode(self, async_client: AsyncClient, monkeypatch, caplog):
        monkeypatch.setattr(overlay_logo, "CACHE_SECONDS", 0.0)
        self._unreadable_logo()
        with caplog.at_level("WARNING", logger="backend.app.services.overlay_logo"):
            await async_client.get(INFO)
            (settings.base_dir / "overlay-branding" / "logo.png").rmdir()
            assert (await async_client.get(INFO)).json()["exists"] is False
            self._unreadable_logo()
            await async_client.get(INFO)

        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2

    async def test_an_upload_into_a_folder_that_cannot_be_written_is_refused(self, async_client: AsyncClient):
        # A plain file where the folder belongs: nothing can be created in it.
        (settings.base_dir / "overlay-branding").write_bytes(b"")

        answer = await async_client.post(UPLOAD, files=_file(_image()))

        assert answer.status_code == 507, answer.text
        assert "overlay-branding" in answer.json()["detail"]

    async def test_removing_a_logo_that_cannot_be_removed_is_refused(self, async_client: AsyncClient):
        self._unreadable_logo()

        answer = await async_client.delete(UPLOAD)

        assert answer.status_code == 507, answer.text


# ---------------------------------------------------------------- what a hard kill or a busy file leaves (M5, M6)


class TestTheFileIsRobust:
    @staticmethod
    def _leftover(age_seconds: float):
        folder = settings.base_dir / "overlay-branding"
        folder.mkdir(parents=True, exist_ok=True)
        leftover = folder / ".logo-killed.tmp"
        leftover.write_bytes(b"half a png")
        then = time.time() - age_seconds
        os.utime(leftover, (then, then))
        return leftover

    async def test_a_temporary_file_a_hard_kill_left_goes_at_the_next_write(self, async_client: AsyncClient):
        leftover = self._leftover(3600)

        assert (await async_client.post(UPLOAD, files=_file(_image()))).status_code == 200

        assert not leftover.exists()

    async def test_and_at_the_first_read_after_a_start(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.setattr(overlay_logo, "_reclaimed", False)
        leftover = self._leftover(3600)

        assert (await async_client.get(INFO)).status_code == 200

        assert not leftover.exists()

    async def test_a_write_in_progress_is_not_reclaimed(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.setattr(overlay_logo, "_reclaimed", False)
        being_written = self._leftover(1)

        await async_client.get(INFO)
        await async_client.post(UPLOAD, files=_file(_image()))

        assert being_written.exists()

    async def test_a_reader_holding_the_file_delays_the_replacement_and_does_not_fail_it(
        self, async_client: AsyncClient, monkeypatch
    ):
        """On Windows a concurrent read of the logo makes ``os.replace`` fail with a sharing violation for as
        long as the reader holds it."""
        real, calls = os.replace, []

        def busy_twice(source, target):
            calls.append(target)
            if len(calls) <= 2:
                raise PermissionError(13, "sharing violation")
            return real(source, target)

        monkeypatch.setattr(overlay_logo.os, "replace", busy_twice)

        answer = await async_client.post(UPLOAD, files=_file(_image()))

        assert answer.status_code == 200, answer.text
        assert len(calls) == 3
        assert [path.name for path in (settings.base_dir / "overlay-branding").iterdir()] == ["logo.png"]

    async def test_an_upload_that_declares_too_many_bytes_is_refused_before_it_is_read(
        self, async_client: AsyncClient, monkeypatch
    ):
        from starlette.requests import Request

        read = []

        async def form(self, *args, **kwargs):
            read.append(True)
            raise AssertionError("the body of an oversized upload was read")

        monkeypatch.setattr(Request, "form", form)
        monkeypatch.setattr(Request, "_get_form", form)

        answer = await async_client.post(UPLOAD, files=_file(b"\x00" * (overlay_logo.MAX_BYTES + 200_000)))

        assert answer.status_code == 413, answer.text
        assert read == []

    async def test_a_request_without_a_file_is_refused(self, async_client: AsyncClient):
        answer = await async_client.post(UPLOAD, data={"other": "field"})

        assert answer.status_code == 422, answer.text


# ---------------------------------------------------------------- backups carry it


async def test_the_backup_map_carries_the_logos_directory():
    from backend.app.services.backup_files import directories

    assert directories(settings)["overlay_branding"] == overlay_logo.directory()
    assert overlay_logo.directory() == settings.base_dir / "overlay-branding"


# ---------------------------------------------------------------- the header-less door is exactly one route


async def test_only_the_token_read_is_reachable_without_a_header():
    """The middleware steps aside for the overlay's read and for nothing else of the logo: the upload, the
    removal and the signed-in reads keep the blanket gate, and the token read carries the overlay-token gate
    (checked here by name as well — the table's own sweep stops at its first mismatch)."""
    from backend.app.main import PUBLIC_API_PATTERNS
    from backend.tests.test_auth_public_patterns import PUBLIC_ROUTES, _gate_dependencies, _intended

    def public(path: str) -> bool:
        return any(pattern.match(path) for pattern in PUBLIC_API_PATTERNS)

    assert public(STREAM_LOGO)
    for path in (UPLOAD, INFO, STREAM_LOGO + "/", STREAM_LOGO + "/logo.png", "/api/v1/overlay-branding"):
        assert not public(path), path
    assert PUBLIC_ROUTES[STREAM_LOGO][0] == "overlay-token"
    gates = _gate_dependencies(_intended(STREAM_LOGO))
    assert [name.split(".")[0] for name in gates] == ["require_overlay_token"]
