"""The installation's logo on the stream overlay (G09; upstream 67d3e7e86, owner's decision 2026-11-15).

One logo per installation. It is uploaded, replaced and removed under ``settings:update``, never stored as
sent (decoded, turned upright, saved again as a PNG without metadata), and read by the overlay OBS opens
through its own route under the long-lived ``overlay`` token — the one picture such a token opens, because it
is the installation's branding and not a user's resource.
"""

from __future__ import annotations

import io

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
def _fresh_cache():
    overlay_logo.forget()
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
