"""Presence-selected API keys cover the registered HTTP gateway, not a list of GETs."""

import re
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from starlette.requests import Request

from backend.app.core import auth
from backend.app.main import PUBLIC_API_PATTERNS, PUBLIC_API_PREFIXES, PUBLIC_API_ROUTES, app
from backend.app.models.api_key import APIKey
from backend.app.models.user import User
from backend.tests.auth_helpers import bind_test_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("credential", ["", "bd_not_a_real_key"])
async def test_every_protected_http_route_refuses_selected_key_before_validation(async_client, credential):
    checked = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        if path in PUBLIC_API_ROUTES or any(path.startswith(prefix) for prefix in PUBLIC_API_PREFIXES):
            continue
        if any(pattern.fullmatch(path) for pattern in PUBLIC_API_PATTERNS):
            continue
        for method in route.methods - {"OPTIONS", "HEAD"}:
            response = await async_client.request(method, path, headers={"X-API-Key": credential})
            assert response.status_code == 401, (method, route.path, response.status_code, response.text)
            checked.add((method, route.path))
    assert len(checked) > 500  # generated coverage includes future non-public routes
    assert ("GET", "/api/v1/settings/ui-preferences") in checked
    assert ("GET", "/api/v1/auth/ldap/status") in checked


async def test_valid_key_reuses_one_hash_and_cannot_borrow_browser_admin(async_client, db_session, monkeypatch):
    owner = await db_session.scalar(select(User).where(User.username == "test_admin"))
    raw, key_hash, prefix = auth.generate_api_key()
    key = APIKey(name="gateway", key_hash=key_hash, key_prefix=prefix, can_read_status=True, enabled=True)
    await bind_test_key(db_session, key, owner)
    db_session.add(key)
    await db_session.commit()
    original = auth._scan_api_key
    scans = []

    async def scan(credential, candidates):
        scans.append(credential)
        return await original(credential, candidates)

    monkeypatch.setattr(auth, "_scan_api_key", scan)
    response = await async_client.get("/api/v1/printers/", headers={"X-API-Key": raw})
    assert response.status_code == 200, response.text
    assert scans == [raw]
    # The fixture also sends a valid administrator JWT. The selected key cannot
    # use it to answer a browser-only door or exceed its own permission ceiling.
    response = await async_client.get("/api/v1/auth/smtp", headers={"X-API-Key": raw})
    assert response.status_code == 401
    response = await async_client.get("/api/v1/users", headers={"X-API-Key": raw})
    assert response.status_code in {401, 403}
    assert len(scans) == 3  # no grant cache crosses request boundaries


async def test_optional_invalid_jwt_remains_optional():
    from fastapi.security import HTTPAuthorizationCredentials

    assert (
        await auth.get_current_user_optional(HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid"))
        is None
    )


async def test_ownership_bearer_branch_still_refuses_invalid_key_without_selector(monkeypatch, db_session):
    """Defense in depth: a later selector refactor must never turn invalid into ALL."""
    monkeypatch.setattr(auth, "selected_api_key", lambda *args: None)
    monkeypatch.setattr(auth, "_api_key_authority_or_none", AsyncMock(return_value=None))
    checker = auth.require_ownership_permission("archives:read_all", "archives:read_own")
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    with pytest.raises(HTTPException) as caught:
        await checker(
            request=request,
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="bd_invalid"),
            x_api_key=None,
        )
    assert caught.value.status_code == 401


@pytest.mark.parametrize(
    "scope,path",
    [
        ("camwall", "/api/v1/camwall/printers"),
        ("overlay", "/api/v1/printers/999/overlay-status"),
        ("monitor", "/api/v1/monitor/kiosk/snapshot"),
    ],
)
async def test_token_only_poll_facades_do_not_rescue_selected_key(async_client, scope, path):
    created = await async_client.post("/api/v1/auth/tokens", json={"name": scope, "scope": scope, "expires_in_days": 1})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    if scope == "monitor":
        header = {"Authorization": f"Bearer {token}"}
        query = ""
    else:
        header = {}
        query = f"?token={token}"
    for key in ("", "bd_invalid"):
        response = await async_client.get(path + query, headers={**header, "X-API-Key": key})
        assert response.status_code == 401, response.text
    # Header-free ordinary token behavior is preserved, including the missing
    # addressed printer's 404 after overlay authentication succeeded.
    response = await async_client.get(path + query, headers=header)
    assert response.status_code == (404 if scope == "overlay" else 200), response.text


@pytest.mark.parametrize(
    "kind,segment",
    [("archive", "archives/1/dl"), ("source", "archives/1/source-dl"), ("library", "library/files/1/dl")],
)
async def test_slicer_facades_refuse_selected_key_without_consuming_url_token(async_client, db_session, kind, segment):
    from backend.tests.auth_helpers import mint_test_authority

    capture = await mint_test_authority(db_session)
    token = await auth.create_slicer_download_token(kind, 1, authority=capture)
    for key in ("", "bd_invalid"):
        response = await async_client.get(f"/api/v1/{segment}/{token}/part.3mf", headers={"X-API-Key": key})
        assert response.status_code == 401, response.text
    assert await auth.verify_slicer_download_token(token, kind, 1, single_use=False)
