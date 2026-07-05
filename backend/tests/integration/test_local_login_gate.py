"""Integration tests for the local login gate + autologin (#1589 / G8-H1).

Covers the four contracts from the upstream issue, adapted to BamDude's
always-on-auth test harness (the ``async_client`` fixture already seeds an
admin ``test_admin`` / ``Test_AdminPass1!`` and carries its JWT, so there is
no ``/auth/setup`` step to run — an admin always exists):

1. POST /auth/login rejects local credentials when local_login_enabled=false
   AND the BAMDUDE_LOCAL_LOGIN env var is not set.
2. The BAMDUDE_LOCAL_LOGIN=true env var bypasses the gate (recovery path).
3. POST /auth/forgot-password is gated by the same flag (with the same bypass).
4. GET /auth/advanced-auth/status surfaces both new fields so the LoginPage
   can render the right UI in a single query.

LDAP keeps its own ``ldap_enabled`` switch and is unaffected by the gate — the
regression suite below guards the login refactor that made the local path
skippable (the naive cut wiped the LDAP-bound ``user`` variable).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings
from backend.app.services.ldap_service import LDAPUserInfo

# The admin seeded by the async_client fixture (see backend/tests/conftest.py).
ADMIN_USERNAME = "test_admin"
ADMIN_PASSWORD = "Test_AdminPass1!"


async def _set_setting(db: AsyncSession, key: str, value: str) -> None:
    result = await db.execute(select(Settings).where(Settings.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        db.add(Settings(key=key, value=value))
    else:
        row.value = value
    await db.commit()


class TestLocalLoginGate:
    """The `local_login_enabled` setting blocks /auth/login + /auth/forgot-password
    when the env-var recovery bypass is not in play."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_login_default_allows_local_credentials(self, async_client: AsyncClient, db_session: AsyncSession):
        """Default install (setting absent) keeps the pre-#1589 behaviour."""
        response = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_login_rejected_when_local_disabled(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """With local_login_enabled=false and no env bypass, valid creds are
        rejected with the same generic 401 as bad creds (no UI-stating leak)."""
        await _set_setting(db_session, "local_login_enabled", "false")
        monkeypatch.delenv("BAMDUDE_LOCAL_LOGIN", raising=False)

        response = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 401
        # Same wording as wrong-password 401 — never leaks whether local login
        # is disabled (would help credential stuffing prioritise).
        assert "Incorrect username or password" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_env_var_bypasses_local_disabled_gate(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """BAMDUDE_LOCAL_LOGIN=true opens the recovery path even when the DB
        setting forbids local login (SSO-broken admin recovery)."""
        await _set_setting(db_session, "local_login_enabled", "false")
        monkeypatch.setenv("BAMDUDE_LOCAL_LOGIN", "true")

        response = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_forgot_password_rejected_when_local_disabled(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """Forgot-password is a local-credentials flow — useless when local
        login is off (the reset wouldn't grant access anyway)."""
        await _set_setting(db_session, "local_login_enabled", "false")
        monkeypatch.delenv("BAMDUDE_LOCAL_LOGIN", raising=False)

        response = await async_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "x@example.com"},
        )
        assert response.status_code == 403
        assert "Local login is disabled" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_forgot_password_env_var_bypasses_gate(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """BAMDUDE_LOCAL_LOGIN=true also re-opens forgot-password — the gate on
        this route shares the same recovery bypass as /auth/login. (Advanced
        auth is off by default, so the request proceeds past the #1589 gate and
        is rejected by the pre-existing 400 'advanced auth not enabled' check —
        never the 403 local-login gate.)"""
        await _set_setting(db_session, "local_login_enabled", "false")
        monkeypatch.setenv("BAMDUDE_LOCAL_LOGIN", "true")

        response = await async_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "x@example.com"},
        )
        assert response.status_code != 403, response.text
        assert "Local login is disabled" not in response.text


class TestLdapLoginNotAffectedByGate:
    """LDAP keeps its own ldap_enabled switch and bypasses local_login_enabled
    entirely. This is the regression suite for the login refactor in #1589 —
    without it, an LDAP user could fail to log in when local login is disabled
    even though the gate is supposed to leave LDAP alone."""

    async def _enable_ldap(self, db: AsyncSession) -> None:
        for key, value in {
            "ldap_enabled": "true",
            "ldap_server_url": "ldaps://ldap.test",
            "ldap_bind_dn": "cn=svc,dc=test,dc=com",
            "ldap_bind_password": "x",
            "ldap_search_base": "dc=test,dc=com",
            "ldap_user_filter": "(uid={username})",
            "ldap_security": "ldaps",
            "ldap_group_mapping": "{}",
            "ldap_auto_provision": "true",
            "ldap_default_group": "",
        }.items():
            await _set_setting(db, key, value)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ldap_login_succeeds_when_local_disabled(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """LDAP-authenticated login must still issue a JWT even when the
        local-login gate is off and no env-var bypass is set. The naive cut of
        #1589 wiped the LDAP-bound `user` variable in this branch."""
        await self._enable_ldap(db_session)
        await _set_setting(db_session, "local_login_enabled", "false")
        monkeypatch.delenv("BAMDUDE_LOCAL_LOGIN", raising=False)

        fake_ldap = LDAPUserInfo(
            username="ldapuser",
            email="ldapuser@test.com",
            display_name="LDAP User",
            groups=[],
        )
        with patch(
            "backend.app.services.ldap_service.authenticate_ldap_user",
            return_value=fake_ldap,
        ):
            response = await async_client.post(
                "/api/v1/auth/login",
                json={"username": "ldapuser", "password": "anything"},
            )

        assert response.status_code == 200, response.text
        assert "access_token" in response.json()
        assert response.json()["user"]["username"] == "ldapuser"


class TestAdvancedAuthStatusSurfacesGate:
    """The /auth/advanced-auth/status endpoint feeds the LoginPage's render
    decisions in a single query — it must surface both new #1589 fields."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_includes_local_login_and_autologin(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("BAMDUDE_LOCAL_LOGIN", raising=False)
        response = await async_client.get("/api/v1/auth/advanced-auth/status")
        assert response.status_code == 200
        result = response.json()
        assert "local_login_enabled" in result
        assert "autologin_provider_id" in result
        # Default install: local on, no autologin provider.
        assert result["local_login_enabled"] is True
        assert result["autologin_provider_id"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_env_var_flips_local_login_back_true(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """When the DB setting disables local login but BAMDUDE_LOCAL_LOGIN is
        set, the status endpoint reports local_login_enabled=true so the SPA
        renders the credentials form to match what the route will accept."""
        await _set_setting(db_session, "local_login_enabled", "false")
        monkeypatch.setenv("BAMDUDE_LOCAL_LOGIN", "true")

        response = await async_client.get("/api/v1/auth/advanced-auth/status")
        assert response.status_code == 200
        assert response.json()["local_login_enabled"] is True
