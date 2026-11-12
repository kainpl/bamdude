"""Integration tests for long-lived camera-stream token routes (#1108).

Cover the auth gates, ownership rules, max-lifetime cap, token-shown-once
contract, and the camera-stream auth fall-through (the existing 60-min
ephemeral path still works AND a long-lived token is also accepted).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# Our ``async_client`` fixture comes pre-authenticated as ``test_admin`` (auth
# is always-on in BamDude — see CLAUDE.md). Tests below treat that client as
# admin and create non-admin users on demand.


async def _viewers_group_id(async_client: AsyncClient) -> int:
    """Look up the seeded Viewers group id so we can grant CAMERA_VIEW.

    Without that, regular users cannot create their own long-lived tokens
    (the same gate the existing 60-min ephemeral flow uses).
    """
    groups_resp = await async_client.get("/api/v1/groups/")
    assert groups_resp.status_code == 200, groups_resp.text
    viewers = next((g for g in groups_resp.json() if g["name"] == "Viewers"), None)
    assert viewers is not None, f"Viewers group not seeded: {groups_resp.text}"
    return viewers["id"]


async def _create_user(async_client: AsyncClient, username: str) -> int:
    """Create a non-admin user (seeded into the Viewers group). Returns user id."""
    viewers_id = await _viewers_group_id(async_client)
    response = await async_client.post(
        "/api/v1/users/",
        json={
            "username": username,
            "password": "UserPass1!",
            "role": "user",
            "group_ids": [viewers_id],
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


async def _login(async_client: AsyncClient, username: str) -> str:
    """Log in a freshly created user and return their JWT."""
    response = await async_client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "UserPass1!"},
    )
    body = response.json()
    token = body.get("access_token")
    assert token, f"login for {username!r} returned no access_token: {body}"
    return token


def _user_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreateLongLivedToken:
    async def test_create_returns_plaintext_token_exactly_once(self, async_client: AsyncClient):
        response = await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "Home Assistant", "expires_in_days": 30},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["token"].startswith("bblt_")
        assert body["name"] == "Home Assistant"
        assert body["scope"] == "camera_stream"
        assert body["lookup_prefix"]
        token_id = body["id"]

        # Listing must NOT include the plaintext (shown-once contract).
        listing = await async_client.get("/api/v1/auth/tokens")
        assert listing.status_code == 200
        listed = next((t for t in listing.json() if t["id"] == token_id), None)
        assert listed is not None
        assert listed["token"] is None  # plaintext gone forever

    async def test_create_rejects_expires_in_zero(self, async_client: AsyncClient):
        """Issue #1108: ``expire_in: 0`` (never) is explicitly forbidden."""
        response = await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "x", "expires_in_days": 0},
        )
        assert response.status_code == 400
        assert "positive" in response.json()["detail"].lower()

    async def test_create_rejects_above_max(self, async_client: AsyncClient):
        response = await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "x", "expires_in_days": 366},
        )
        assert response.status_code == 400
        assert "365" in response.json()["detail"]

    async def test_create_requires_auth(self, async_client: AsyncClient):
        # Strip the admin Authorization header for this single call.
        response = await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "x", "expires_in_days": 7},
            headers={"Authorization": ""},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListLongLivedTokens:
    async def test_list_returns_only_callers_tokens_by_default(self, async_client: AsyncClient):
        bob_id = await _create_user(async_client, "bob_list")
        bob_token = await _login(async_client, "bob_list")

        # Each user creates one token.
        await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "admins", "expires_in_days": 7},
        )
        await async_client.post(
            "/api/v1/auth/tokens",
            headers=_user_headers(bob_token),
            json={"name": "bobs", "expires_in_days": 7},
        )

        # Bob's listing should see only his.
        bob_listing = await async_client.get(
            "/api/v1/auth/tokens",
            headers=_user_headers(bob_token),
        )
        names = {t["name"] for t in bob_listing.json()}
        assert names == {"bobs"}
        assert bob_id == bob_listing.json()[0]["user_id"]

    async def test_admin_can_filter_by_user_id(self, async_client: AsyncClient):
        bob_id = await _create_user(async_client, "bob_filter")
        bob_token = await _login(async_client, "bob_filter")
        await async_client.post(
            "/api/v1/auth/tokens",
            headers=_user_headers(bob_token),
            json={"name": "bobs", "expires_in_days": 7},
        )

        admin_view = await async_client.get(f"/api/v1/auth/tokens?user_id={bob_id}")
        assert admin_view.status_code == 200
        names = {t["name"] for t in admin_view.json()}
        assert names == {"bobs"}

    async def test_non_admin_cannot_see_other_users_tokens(self, async_client: AsyncClient):
        await _create_user(async_client, "alice_see")
        bob_id = await _create_user(async_client, "bob_see")
        alice_token = await _login(async_client, "alice_see")

        forbidden = await async_client.get(
            f"/api/v1/auth/tokens?user_id={bob_id}",
            headers=_user_headers(alice_token),
        )
        assert forbidden.status_code == 403


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class TestRevokeLongLivedToken:
    async def test_api_key_cannot_remove_a_retired_owner_record(self, async_client, db_session):
        from datetime import datetime

        from sqlalchemy import update

        from backend.app.models.long_lived_token import LongLivedToken

        created = await async_client.post("/api/v1/auth/tokens", json={"name": "owner record", "expires_in_days": 7})
        assert created.status_code == 201, created.text
        token_id = created.json()["id"]
        await db_session.execute(
            update(LongLivedToken)
            .where(LongLivedToken.id == token_id)
            .values(
                contract_version=0,
                revoked_at=datetime.utcnow(),
                retired_reason="legacy_credential_reissue_required",
            )
        )
        await db_session.commit()
        issued_key = await async_client.post("/api/v1/api-keys/", json={"name": "status only", "can_read_status": True})
        assert issued_key.status_code == 200, issued_key.text
        refused = await async_client.delete(
            f"/api/v1/auth/tokens/{token_id}",
            headers={"Authorization": "", "X-API-Key": issued_key.json()["key"]},
            params={"expected_identity": created.json()["token_identity"], "retired_only": "true"},
        )
        assert refused.status_code == 403, refused.text
        assert await db_session.get(LongLivedToken, token_id) is not None

    @pytest.mark.parametrize("scope", ["camera_stream", "monitor"])
    async def test_owner_can_remove_retired_metadata_without_reviving_secret(self, async_client, db_session, scope):
        from datetime import datetime

        from sqlalchemy import update

        from backend.app.core.auth import verify_camera_stream_token
        from backend.app.models.credential_rollout import CredentialRolloutEpisode
        from backend.app.models.long_lived_token import LongLivedToken

        created = await async_client.post(
            "/api/v1/auth/tokens", json={"name": "retired entry", "scope": scope, "expires_in_days": 7}
        )
        assert created.status_code == 201, created.text
        token_id, raw = created.json()["id"], created.json()["token"]
        await db_session.execute(
            update(LongLivedToken)
            .where(LongLivedToken.id == token_id)
            .values(
                contract_version=0,
                revoked_at=datetime.utcnow(),
                retired_at=datetime.utcnow(),
                retired_reason="legacy_credential_reissue_required",
            )
        )
        db_session.add(
            CredentialRolloutEpisode(
                id="retired-metadata-episode", contract_key="g01-credential-contract-v1", long_lived_token_count=1
            )
        )
        await db_session.commit()
        assert any(row["id"] == token_id for row in (await async_client.get("/api/v1/auth/tokens")).json())
        intent = {"expected_identity": created.json()["token_identity"], "retired_only": "true"}
        # Ordinary revoke must not silently become a metadata-delete action.
        assert (await async_client.delete(f"/api/v1/auth/tokens/{token_id}")).status_code == 404
        response = await async_client.delete(f"/api/v1/auth/tokens/{token_id}", params=intent)
        assert response.status_code == 204, response.text
        assert all(row["id"] != token_id for row in (await async_client.get("/api/v1/auth/tokens")).json())
        assert await db_session.get(LongLivedToken, token_id) is None
        assert not await verify_camera_stream_token(raw)
        assert (await async_client.delete(f"/api/v1/auth/tokens/{token_id}", params=intent)).status_code == 404
        # Retirement facts remain historical even after a visible row is removed.
        status = await async_client.get("/api/v1/api-keys/rollout")
        assert status.status_code == 200, status.text
        assert status.json()["long_lived_token_count"] == 1

    async def test_other_user_cannot_remove_retired_metadata(self, async_client, db_session):
        from datetime import datetime

        from sqlalchemy import update

        from backend.app.models.long_lived_token import LongLivedToken

        created = await async_client.post("/api/v1/auth/tokens", json={"name": "private entry", "expires_in_days": 7})
        assert created.status_code == 201, created.text
        token_id = created.json()["id"]
        await db_session.execute(
            update(LongLivedToken)
            .where(LongLivedToken.id == token_id)
            .values(
                contract_version=0,
                revoked_at=datetime.utcnow(),
                retired_reason="legacy_credential_reissue_required",
            )
        )
        await db_session.commit()
        await _create_user(async_client, "retired_stranger")
        stranger = await _login(async_client, "retired_stranger")
        refused = await async_client.delete(
            f"/api/v1/auth/tokens/{token_id}",
            headers=_user_headers(stranger),
            params={"expected_identity": created.json()["token_identity"], "retired_only": "true"},
        )
        assert refused.status_code == 403, refused.text
        assert await db_session.get(LongLivedToken, token_id) is not None

    @pytest.mark.parametrize("replacement_retired", [False, True])
    @pytest.mark.parametrize("retired_intent", [False, True])
    async def test_stale_selected_identity_cannot_change_replacement(
        self, async_client, db_session, replacement_retired, retired_intent
    ):
        from datetime import datetime

        from sqlalchemy import update

        from backend.app.core.auth import verify_camera_stream_token
        from backend.app.models.long_lived_token import LongLivedToken

        old = await async_client.post("/api/v1/auth/tokens", json={"name": "selected old token", "expires_in_days": 7})
        assert old.status_code == 201, old.text
        selected = old.json()
        token_id = selected["id"]
        await db_session.execute(
            update(LongLivedToken)
            .where(LongLivedToken.id == token_id)
            .values(
                contract_version=0,
                revoked_at=datetime.utcnow(),
                retired_reason="legacy_credential_reissue_required",
            )
        )
        await db_session.commit()
        removed = await async_client.delete(
            f"/api/v1/auth/tokens/{token_id}",
            params={"expected_identity": selected["token_identity"], "retired_only": "true"},
        )
        assert removed.status_code == 204, removed.text
        new = await async_client.post("/api/v1/auth/tokens", json={"name": "replacement token", "expires_in_days": 7})
        assert new.status_code == 201, new.text
        replacement = new.json()
        assert replacement["id"] == token_id  # Real SQLite primary-key reuse.
        assert replacement["token_identity"] != selected["token_identity"]
        if replacement_retired:
            await db_session.execute(
                update(LongLivedToken)
                .where(LongLivedToken.id == token_id)
                .values(
                    contract_version=0,
                    revoked_at=datetime.utcnow(),
                    retired_reason="legacy_credential_reissue_required",
                )
            )
            await db_session.commit()
        stale = await async_client.delete(
            f"/api/v1/auth/tokens/{token_id}",
            params={
                "expected_identity": selected["token_identity"],
                "retired_only": str(retired_intent).lower(),
            },
        )
        assert stale.status_code == 404, stale.text
        surviving = await db_session.get(LongLivedToken, token_id, populate_existing=True)
        assert surviving is not None and surviving.token_identity == replacement["token_identity"]
        assert (surviving.revoked_at is not None) is replacement_retired
        assert await verify_camera_stream_token(replacement["token"]) is (not replacement_retired)

    async def test_metadata_intent_needs_identity_and_retirement(self, async_client, db_session):
        from backend.app.models.long_lived_token import LongLivedToken

        created = await async_client.post("/api/v1/auth/tokens", json={"name": "active", "expires_in_days": 7})
        token = created.json()
        for params in (
            {"retired_only": "true"},
            {"retired_only": "true", "expected_identity": token["token_identity"]},
        ):
            refused = await async_client.delete(f"/api/v1/auth/tokens/{token['id']}", params=params)
            assert refused.status_code == 404, refused.text
        assert (await db_session.get(LongLivedToken, token["id"], populate_existing=True)).revoked_at is None

    async def test_owner_can_revoke_own_token(self, async_client: AsyncClient):
        created = await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "x", "expires_in_days": 7},
        )
        token_id = created.json()["id"]

        revoke = await async_client.delete(f"/api/v1/auth/tokens/{token_id}")
        assert revoke.status_code == 204

        # Now gone from the listing.
        listing = await async_client.get("/api/v1/auth/tokens")
        assert all(t["id"] != token_id for t in listing.json())

    async def test_admin_can_revoke_any_users_token(self, async_client: AsyncClient):
        await _create_user(async_client, "bob_revoke")
        bob_token = await _login(async_client, "bob_revoke")
        created = await async_client.post(
            "/api/v1/auth/tokens",
            headers=_user_headers(bob_token),
            json={"name": "bobs", "expires_in_days": 7},
        )
        token_id = created.json()["id"]

        admin_revoke = await async_client.delete(f"/api/v1/auth/tokens/{token_id}")
        assert admin_revoke.status_code == 204

    async def test_non_owner_non_admin_cannot_revoke(self, async_client: AsyncClient):
        await _create_user(async_client, "alice_attack")
        await _create_user(async_client, "bob_target")
        bob_token = await _login(async_client, "bob_target")
        alice_token = await _login(async_client, "alice_attack")

        created = await async_client.post(
            "/api/v1/auth/tokens",
            headers=_user_headers(bob_token),
            json={"name": "bobs", "expires_in_days": 7},
        )
        token_id = created.json()["id"]

        forbidden = await async_client.delete(
            f"/api/v1/auth/tokens/{token_id}",
            headers=_user_headers(alice_token),
        )
        assert forbidden.status_code == 403

    async def test_revoke_unknown_id_404(self, async_client: AsyncClient):
        response = await async_client.delete("/api/v1/auth/tokens/99999")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Auth fall-through: ``verify_camera_stream_token`` accepts both kinds
# ---------------------------------------------------------------------------
# The full /camera/stream HTTP integration would need a real ffmpeg / printer
# socket to keep the StreamingResponse alive. Verifying the auth dependency
# directly is a stronger check anyway: the route's only auth job is to call
# ``verify_camera_stream_token``, which is what these tests exercise.


class TestCameraStreamTokenVerification:
    async def test_long_lived_token_verifies_via_camera_stream_path(self, async_client: AsyncClient):
        """A freshly minted long-lived token must pass the same dependency
        the camera-stream route uses, after the ephemeral path would have
        rejected it.
        """
        from backend.app.core.auth import verify_camera_stream_token

        created = await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "kiosk", "expires_in_days": 90},
        )
        long_lived = created.json()["token"]

        assert await verify_camera_stream_token(long_lived) is True

    async def test_revoked_long_lived_token_fails_camera_stream_check(self, async_client: AsyncClient):
        from backend.app.core.auth import verify_camera_stream_token

        created = await async_client.post(
            "/api/v1/auth/tokens",
            json={"name": "kiosk", "expires_in_days": 30},
        )
        long_lived = created.json()["token"]
        token_id = created.json()["id"]

        await async_client.delete(f"/api/v1/auth/tokens/{token_id}")
        assert await verify_camera_stream_token(long_lived) is False

    async def test_garbage_token_fails_camera_stream_check(self, async_client: AsyncClient):
        from backend.app.core.auth import verify_camera_stream_token

        assert await verify_camera_stream_token("bblt_aaaaaaaa_garbage") is False
        assert await verify_camera_stream_token("not-a-real-token") is False
