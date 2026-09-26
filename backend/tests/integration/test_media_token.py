"""Media routes take a signed-in user's media token, and check the resource (audit D9 a2, upstream 816f073a).

A browser cannot put an Authorization header on an ``<img src>`` or a ``<video
src>``, so pictures need a credential that fits in the URL. Until now there
were two answers, both wrong: most pictures were ANONYMOUS (anyone who could
reach the install could read any thumbnail, plate, QR code or timelapse by
guessing an id), and the rest borrowed the CAMERA stream token — which costs
``camera:view``, so a user granted the library but not the live camera got a
grid of broken images, and which names nobody, so no ownership could be checked.

The media token is minted by any signed-in user and records who they are; each
media route then applies the permission and ownership rules of the resource it
serves — the same ones its header-authenticated siblings use. Ordinary
``Authorization`` / ``X-API-Key`` headers work too (a ``fetch()``, an
integration), through the same checkers, so API-key scopes and printer lists are
unchanged. The camera routes keep the camera token and take nothing else; the
long-lived kiosk / wall / overlay tokens open no media route.

Stays anonymous, by decision: archive photos (notifications link them for
Discord, webhooks and ntfy, which fetch without credentials), external-link
icons, and the OIDC button icon on the login page.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from backend.app.core.auth import create_access_token, create_camera_stream_token

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret

# Every route that now takes a media token. Ids point at nothing: the gate runs
# before the handler looks anything up, so a refused credential is a 401 here
# and an accepted one is the handler's own 404.
MEDIA_ROUTES = [
    "/api/v1/archives/1/thumbnail",
    "/api/v1/archives/1/plate-thumbnail/1",
    "/api/v1/archives/1/plate-preview",
    "/api/v1/archives/1/project-image/Metadata/pick_1.png",
    "/api/v1/archives/1/qrcode",
    "/api/v1/archives/1/timelapse",
    "/api/v1/library/files/1/thumbnail",
    "/api/v1/library/files/1/plate-thumbnail/1",
    "/api/v1/library/files/1/card-file/Auxiliaries/pictures/a.png",
    "/api/v1/products/1/attachment-image/a.png",
    "/api/v1/products/1/cover-image",
    "/api/v1/projects/1/cover-image",
    "/api/v1/printers/1/camera-cover",
    "/api/v1/makerworld/imports/1/cover",
    "/api/v1/makerworld/imports/1/cover-variant",
    "/api/v1/makerworld/thumbnail?url=https://makerworld.bblmw.com/x.png",
]

STILL_ANONYMOUS = [
    "/api/v1/archives/1/photos/finish_1.jpg",
    "/api/v1/external-links/1/icon",
    "/api/v1/auth/oidc/providers/1/icon",
]


def _with_token(path: str, token: str) -> str:
    return f"{path}{'&' if '?' in path else '?'}token={token}"


async def _raw_get(client: AsyncClient, path: str):
    # httpx merges client headers into every request; build one without them.
    request = client.build_request("GET", path)
    request.headers.pop("Authorization", None)
    return await client.send(request)


async def _mint(client: AsyncClient, jwt: str | None = None) -> str:
    request = client.build_request("POST", "/api/v1/auth/media-token")
    if jwt is not None:
        request.headers["Authorization"] = f"Bearer {jwt}"
    response = await client.send(request)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _make_user(client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[str, int]:
    admin = {"Authorization": f"Bearer {create_access_token(data={'sub': 'test_admin'})}"}
    group = await client.post(
        "/api/v1/groups/", headers=admin, json={"name": f"mt_{username}", "permissions": permissions}
    )
    assert group.status_code == 201, group.text
    user = await client.post(
        "/api/v1/users/",
        headers=admin,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [group.json()["id"]]},
    )
    assert user.status_code == 201, user.text
    login = await client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return login.json()["access_token"], user.json()["id"]


async def _archive_with_thumbnail(archive_factory, printer_id: int, **kwargs):
    from backend.app.core.config import settings

    thumb = settings.base_dir / "archive" / f"thumb_{secrets.token_hex(4)}.png"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return await archive_factory(printer_id, thumbnail_path=str(thumb.relative_to(settings.base_dir)), **kwargs)


# -- minting -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_any_signed_in_user_can_mint_one_without_camera_view(async_client: AsyncClient):
    jwt, _ = await _make_user(async_client, username="mt_viewer", permissions=["archives:read_own"])
    assert await _mint(async_client, jwt)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_credentials_mint_nothing(async_client: AsyncClient):
    request = async_client.build_request("POST", "/api/v1/auth/media-token")
    request.headers.pop("Authorization", None)
    assert (await async_client.send(request)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_api_key_mints_nothing_it_uses_its_header(async_client: AsyncClient):
    key = (await async_client.post("/api/v1/api-keys/", json={"name": "mt-key", "can_read_status": True})).json()["key"]
    request = async_client.build_request("POST", "/api/v1/auth/media-token")
    request.headers.pop("Authorization", None)
    request.headers["X-API-Key"] = key
    assert (await async_client.send(request)).status_code == 401


# -- the boundary on every media route -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("path", MEDIA_ROUTES)
async def test_a_media_route_refuses_no_credential(async_client: AsyncClient, path: str):
    assert (await _raw_get(async_client, path)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("path", MEDIA_ROUTES)
async def test_a_media_route_refuses_the_camera_token(async_client: AsyncClient, path: str):
    """The camera token is handed to kiosks, walls and Home Assistant for video."""
    camera = await create_camera_stream_token()
    assert (await _raw_get(async_client, _with_token(path, camera))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("path", MEDIA_ROUTES)
async def test_a_media_route_takes_a_media_token(async_client: AsyncClient, path: str):
    token = await _mint(async_client)
    assert (await _raw_get(async_client, _with_token(path, token))).status_code not in (401, 403)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("path", STILL_ANONYMOUS)
async def test_the_decided_anonymous_media_stays_open(async_client: AsyncClient, path: str):
    assert (await _raw_get(async_client, path)).status_code not in (401, 403)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_camera_refuses_a_media_token(async_client: AsyncClient):
    token = await _mint(async_client)
    assert (await _raw_get(async_client, _with_token("/api/v1/printers/1/camera/snapshot", token))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unknown_token_is_refused(async_client: AsyncClient):
    assert (await _raw_get(async_client, "/api/v1/archives/1/thumbnail?token=nope")).status_code == 401


# -- the resource decides ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ownership_scopes_what_a_token_reaches(async_client: AsyncClient, printer_factory, archive_factory):
    jwt_a, uid_a = await _make_user(async_client, username="mt_own_a", permissions=["archives:read_own"])
    _, uid_b = await _make_user(async_client, username="mt_own_b", permissions=["archives:read_own"])
    printer = await printer_factory()
    own = await _archive_with_thumbnail(archive_factory, printer.id, created_by_id=uid_a)
    other = await _archive_with_thumbnail(archive_factory, printer.id, created_by_id=uid_b)
    token = await _mint(async_client, jwt_a)

    assert (await _raw_get(async_client, _with_token(f"/api/v1/archives/{own.id}/thumbnail", token))).status_code == 200
    # 404, not 403 — as on the header routes, an id's existence is not leaked.
    assert (
        await _raw_get(async_client, _with_token(f"/api/v1/archives/{other.id}/thumbnail", token))
    ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_trashed_own_archive_still_shows_its_thumbnail(
    async_client: AsyncClient, printer_factory, archive_factory, db_session
):
    """The trash lists deleted prints with their picture."""
    jwt, uid = await _make_user(async_client, username="mt_trash", permissions=["archives:read_own"])
    printer = await printer_factory()
    archive = await _archive_with_thumbnail(archive_factory, printer.id, created_by_id=uid)
    archive.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    token = await _mint(async_client, jwt)

    assert (
        await _raw_get(async_client, _with_token(f"/api/v1/archives/{archive.id}/thumbnail", token))
    ).status_code == 200


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_token_without_the_resources_permission_is_forbidden(async_client: AsyncClient):
    jwt, _ = await _make_user(async_client, username="mt_projects", permissions=["projects:read"])
    token = await _mint(async_client, jwt)
    assert (await _raw_get(async_client, _with_token("/api/v1/archives/1/thumbnail", token))).status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
async def test_headers_still_work_for_a_fetch(async_client: AsyncClient, printer_factory, archive_factory):
    printer = await printer_factory()
    archive = await _archive_with_thumbnail(archive_factory, printer.id)
    assert (await async_client.get(f"/api/v1/archives/{archive.id}/thumbnail")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_api_key_header_is_held_to_its_scope(async_client: AsyncClient, printer_factory, archive_factory):
    printer = await printer_factory()
    archive = await _archive_with_thumbnail(archive_factory, printer.id)
    path = f"/api/v1/archives/{archive.id}/thumbnail"

    reader = (await async_client.post("/api/v1/api-keys/", json={"name": "mt-read", "can_read_status": True})).json()
    blind = (await async_client.post("/api/v1/api-keys/", json={"name": "mt-blind", "can_read_status": False})).json()

    for key, expected in ((reader["key"], 200), (blind["key"], 403)):
        request = async_client.build_request("GET", path)
        request.headers.pop("Authorization", None)
        request.headers["X-API-Key"] = key
        assert (await async_client.send(request)).status_code == expected


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_password_change_ends_the_token(
    async_client: AsyncClient, db_session, printer_factory, archive_factory
):
    from backend.app.models.user import User

    jwt, uid = await _make_user(async_client, username="mt_pw", permissions=["archives:read_all"])
    printer = await printer_factory()
    archive = await _archive_with_thumbnail(archive_factory, printer.id)
    token = await _mint(async_client, jwt)
    path = _with_token(f"/api/v1/archives/{archive.id}/thumbnail", token)
    assert (await _raw_get(async_client, path)).status_code == 200

    await db_session.execute(
        update(User).where(User.id == uid).values(password_changed_at=datetime.now(timezone.utc) + timedelta(seconds=5))
    )
    await db_session.commit()

    assert (await _raw_get(async_client, path)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_deactivated_user_s_token_is_refused(async_client: AsyncClient, db_session):
    from backend.app.models.user import User

    jwt, uid = await _make_user(async_client, username="mt_gone", permissions=["archives:read_all"])
    token = await _mint(async_client, jwt)

    await db_session.execute(update(User).where(User.id == uid).values(is_active=False))
    await db_session.commit()

    assert (await _raw_get(async_client, _with_token("/api/v1/archives/1/thumbnail", token))).status_code == 401
