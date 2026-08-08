"""The env-managed OIDC provider is read-only through the API (upstream #2593).

Startup rewrites the row from ``BAMDUDE_OIDC_*`` on every boot, so a write
accepted here would be reverted at the next restart. Being told no is better
than being told yes and then having it quietly undone.

``BAMDUDE_LOCAL_LOGIN`` remains the recovery path if the provider becomes
unusable, so refusing outright cannot lock anyone out.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.models.oidc_provider import OIDCProvider


async def _provider(db_session, *, env_managed: bool) -> OIDCProvider:
    row = OIDCProvider(
        name="Authentik" if env_managed else "Hand made",
        issuer_url="https://id.example.com",
        client_id="bamdude",
        is_env_managed=env_managed,
    )
    row.client_secret = "s3cr3t"
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestWritesAreRefused:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_is_refused_with_409(self, async_client: AsyncClient, db_session):
        row = await _provider(db_session, env_managed=True)
        resp = await async_client.put(
            f"/api/v1/auth/oidc/providers/{row.id}",
            json={"is_enabled": False},
        )
        assert resp.status_code == 409
        assert "environment" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_is_refused_with_409(self, async_client: AsyncClient, db_session):
        row = await _provider(db_session, env_managed=True)
        resp = await async_client.delete(f"/api/v1/auth/oidc/providers/{row.id}")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_icon_delete_is_refused_too(self, async_client: AsyncClient, db_session):
        # The icon is part of the row the environment owns; clearing it here
        # would be undone on the next boot like any other field.
        row = await _provider(db_session, env_managed=True)
        resp = await async_client.delete(f"/api/v1/auth/oidc/providers/{row.id}/icon")
        assert resp.status_code == 409


class TestOrdinaryProvidersAreUnaffected:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_ui_created_provider_is_still_editable(self, async_client: AsyncClient, db_session):
        """The guard must key on the flag, not on "an OIDC provider exists"."""
        row = await _provider(db_session, env_managed=False)
        resp = await async_client.put(
            f"/api/v1/auth/oidc/providers/{row.id}",
            json={"is_enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["is_enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_ui_created_provider_is_still_deletable(self, async_client: AsyncClient, db_session):
        row = await _provider(db_session, env_managed=False)
        assert (await async_client.delete(f"/api/v1/auth/oidc/providers/{row.id}")).status_code == 200


class TestTheFlagIsOnTheWire:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_listing_says_which_provider_is_env_managed(self, async_client: AsyncClient, db_session):
        # The browser cannot see the environment, so the server has to say so —
        # otherwise the UI has no way to know which row to lock.
        await _provider(db_session, env_managed=True)
        await _provider(db_session, env_managed=False)
        rows = {r["name"]: r for r in (await async_client.get("/api/v1/auth/oidc/providers/all")).json()}
        assert rows["Authentik"]["is_env_managed"] is True
        assert rows["Hand made"]["is_env_managed"] is False
