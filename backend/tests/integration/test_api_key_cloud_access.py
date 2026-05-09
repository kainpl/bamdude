"""Integration tests for API key ownership + cloud-access scope (#1182).

Covers two upstream Bambuddy v0.2.4b2 changes that shipped together:

- B.4 (``api_keys.user_id`` + ``can_access_cloud``): keys are stamped with
  the creating user's id; cloud-token spend is gated behind an explicit
  per-key opt-in that requires an owner.
- A.15 (slice + slicer-presets resolve cloud-token via key owner): when
  an API-key request hits ``/library/files/{id}/slice`` or
  ``/slicer/presets`` and the key has cloud access, the cloud-token
  resolution finds the owner's per-user token instead of falling through
  to "unauthenticated".

The tests run against the standard ``async_client`` fixture (pre-seeded
``test_admin`` JWT, full app + DB stack), and exercise the actual HTTP
boundary so the dep wiring + JSON shapes are also covered.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


def _admin_headers(token: str | None = None) -> dict:
    if token is None:
        # async_client already injects test_admin's JWT in default headers,
        # so an empty dict here means "use the fixture's auth".
        return {}
    return {"Authorization": f"Bearer {token}"}


async def _create_key(
    async_client: AsyncClient,
    *,
    name: str,
    can_access_cloud: bool = False,
    headers: dict | None = None,
) -> dict:
    """Create an API key via the public endpoint and return the response JSON."""
    response = await async_client.post(
        "/api/v1/api-keys/",
        headers=headers if headers is not None else {},
        json={"name": name, "can_access_cloud": can_access_cloud},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create_regular_user(async_client: AsyncClient, username: str = "cloudie") -> tuple[int, str]:
    """Create a non-admin user and return ``(user_id, jwt)``."""
    create_resp = await async_client.post(
        "/api/v1/users/",
        json={
            "username": username,
            "password": "RegularPass123!",
            "role": "user",
        },
    )
    assert create_resp.status_code in (200, 201), create_resp.text
    user_id = create_resp.json()["id"]
    login = await async_client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "RegularPass123!"},
    )
    assert login.status_code == 200, login.text
    return user_id, login.json()["access_token"]


# ---------------------------------------------------------------------------
# B.4: ownership + can_access_cloud at the API surface
# ---------------------------------------------------------------------------


class TestAPIKeyOwnership:
    @pytest.mark.asyncio
    async def test_create_stamps_user_id_from_jwt(self, async_client: AsyncClient):
        """A JWT-authenticated create stamps the caller's user_id on the key."""
        body = await _create_key(async_client, name="ui-key")
        assert body["user_id"] is not None, "JWT-created key must carry the creator's user_id"

    @pytest.mark.asyncio
    async def test_create_with_cloud_access_succeeds_when_authenticated(self, async_client: AsyncClient):
        """can_access_cloud=True is allowed when there's an authenticated user to own it."""
        body = await _create_key(async_client, name="cloud-key", can_access_cloud=True)
        assert body["can_access_cloud"] is True
        assert body["user_id"] is not None

    @pytest.mark.asyncio
    async def test_create_with_cloud_access_rejected_without_owner(self, async_client: AsyncClient):
        """API-key-authed flow has no current_user → can_access_cloud=True must 400."""
        # First, create a regular non-cloud key to authenticate the second call.
        seed = await _create_key(async_client, name="seed", can_access_cloud=False)
        raw = seed["key"]

        # Now post a create request using ONLY the API key (no JWT) → owner is None.
        del async_client.headers["Authorization"]
        try:
            response = await async_client.post(
                "/api/v1/api-keys/",
                headers={"X-API-Key": raw},
                json={"name": "ownerless-cloud", "can_access_cloud": True},
            )
        finally:
            # Restore JWT so subsequent tests in the same session don't fail.
            from backend.app.core.auth import create_access_token

            async_client.headers["Authorization"] = f"Bearer {create_access_token(data={'sub': 'test_admin'})}"
        assert response.status_code == 400, response.text
        assert "owning user" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_user_id_appears_in_list_and_response(self, async_client: AsyncClient):
        """list_api_keys + GET both surface user_id + can_access_cloud."""
        await _create_key(async_client, name="visible", can_access_cloud=True)
        listing = await async_client.get("/api/v1/api-keys/")
        assert listing.status_code == 200
        rows = listing.json()
        assert any(r["name"] == "visible" and r["can_access_cloud"] for r in rows)
        assert all("user_id" in r for r in rows)

    @pytest.mark.asyncio
    async def test_cannot_promote_legacy_ownerless_key_to_cloud(self, async_client: AsyncClient):
        """Direct DB insert simulates a legacy ownerless key; PATCH must 400 on cloud=True."""
        # Insert directly to bypass the create-time owner stamp.
        from sqlalchemy import insert, select

        from backend.app.core.auth import get_password_hash
        from backend.app.core.database import async_session
        from backend.app.models.api_key import APIKey

        async with async_session() as db:
            await db.execute(
                insert(APIKey).values(
                    name="legacy",
                    key_hash=get_password_hash("bb_legacy_dummy"),
                    key_prefix="bb_lega",
                    user_id=None,
                    can_access_cloud=False,
                )
            )
            await db.commit()
            row = (await db.execute(select(APIKey).where(APIKey.name == "legacy"))).scalar_one()
            legacy_id = row.id

        response = await async_client.patch(
            f"/api/v1/api-keys/{legacy_id}",
            json={"can_access_cloud": True},
        )
        assert response.status_code == 400, response.text

    @pytest.mark.asyncio
    async def test_update_cloud_access_succeeds_on_owned_key(self, async_client: AsyncClient):
        """A key with an owner can be flipped on/off via PATCH."""
        body = await _create_key(async_client, name="togglable", can_access_cloud=False)
        key_id = body["id"]

        on = await async_client.patch(
            f"/api/v1/api-keys/{key_id}",
            json={"can_access_cloud": True},
        )
        assert on.status_code == 200, on.text
        assert on.json()["can_access_cloud"] is True

        off = await async_client.patch(
            f"/api/v1/api-keys/{key_id}",
            json={"can_access_cloud": False},
        )
        assert off.status_code == 200, off.text
        assert off.json()["can_access_cloud"] is False

    @pytest.mark.asyncio
    async def test_user_delete_cascades_their_api_keys(self, async_client: AsyncClient):
        """Deleting the owning user removes the key (SQLite-safe explicit DELETE).

        The regular user role doesn't carry ``api_keys:create``, so seed the
        owned key directly in the DB instead of going through the public route
        — the goal here is the cascade on ``DELETE /users/{id}``, not the
        create-permission shape.
        """
        from sqlalchemy import select

        from backend.app.core.auth import generate_api_key
        from backend.app.core.database import async_session
        from backend.app.models.api_key import APIKey

        user_id, _user_token = await _create_regular_user(async_client, username="vanish")

        full_key, key_hash, key_prefix = generate_api_key()
        async with async_session() as db:
            db.add(
                APIKey(
                    name="vanish-key",
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    user_id=user_id,
                    can_access_cloud=True,
                )
            )
            await db.commit()
            row_id = (await db.execute(select(APIKey).where(APIKey.name == "vanish-key"))).scalar_one().id

        # Admin deletes the user.
        delete_resp = await async_client.delete(f"/api/v1/users/{user_id}")
        assert delete_resp.status_code in (200, 204), delete_resp.text

        # Key must be gone.
        async with async_session() as db:
            row = (await db.execute(select(APIKey).where(APIKey.id == row_id))).scalar_one_or_none()
        assert row is None, "Cascade should have deleted the user's key"
        # silence unused-binding warning
        del full_key


# ---------------------------------------------------------------------------
# A.15: cloud-owner resolution at slicer routes
# ---------------------------------------------------------------------------


class TestResolveAPIKeyCloudOwner:
    @pytest.mark.asyncio
    async def test_returns_owner_for_cloud_flagged_key(self, async_client: AsyncClient):
        """Flagged key with active owner → resolver returns the owner's User."""
        from fastapi.security import HTTPAuthorizationCredentials
        from sqlalchemy import select

        from backend.app.api.routes.cloud import resolve_api_key_cloud_owner
        from backend.app.core.database import async_session
        from backend.app.models.api_key import APIKey

        body = await _create_key(async_client, name="rez-cloud", can_access_cloud=True)
        raw = body["key"]
        owner_id = body["user_id"]
        # Sanity: the response should round-trip the create payload.
        assert body["can_access_cloud"] is True
        assert owner_id is not None
        # Sanity: the key actually persisted with both flags set.
        async with async_session() as db:
            row = (await db.execute(select(APIKey).where(APIKey.name == "rez-cloud"))).scalar_one()
            assert row.can_access_cloud is True
            assert row.user_id == owner_id

        async with async_session() as db:
            user = await resolve_api_key_cloud_owner(
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw),
                x_api_key=None,
                db=db,
            )
        assert user is not None
        assert user.id == owner_id

    @pytest.mark.asyncio
    async def test_returns_none_for_non_cloud_key(self, async_client: AsyncClient):
        """can_access_cloud=False (default) → resolver returns None even with owner."""
        from backend.app.api.routes.cloud import resolve_api_key_cloud_owner
        from backend.app.core.database import async_session

        body = await _create_key(async_client, name="rez-default", can_access_cloud=False)
        raw = body["key"]

        async with async_session() as db:
            user = await resolve_api_key_cloud_owner(credentials=None, x_api_key=raw, db=db)
        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_for_legacy_ownerless_key(self, async_client: AsyncClient):
        """user_id IS NULL → resolver returns None even when can_access_cloud somehow True."""
        from backend.app.api.routes.cloud import resolve_api_key_cloud_owner
        from backend.app.core.auth import generate_api_key
        from backend.app.core.database import async_session
        from backend.app.models.api_key import APIKey

        full_key, key_hash, key_prefix = generate_api_key()

        async with async_session() as db:
            db.add(
                APIKey(
                    name="legacy-rez",
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    user_id=None,
                    # The route refuses this combo; we set it directly to assert
                    # the resolver still bails out as defence-in-depth.
                    can_access_cloud=True,
                )
            )
            await db.commit()

        async with async_session() as db:
            user = await resolve_api_key_cloud_owner(credentials=None, x_api_key=full_key, db=db)
        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_for_no_auth_headers(self, async_client: AsyncClient):
        """No credentials, no X-API-Key → resolver returns None (permissive)."""
        from backend.app.api.routes.cloud import resolve_api_key_cloud_owner
        from backend.app.core.database import async_session

        async with async_session() as db:
            user = await resolve_api_key_cloud_owner(credentials=None, x_api_key=None, db=db)
        assert user is None
