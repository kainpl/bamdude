"""Happy-path integration tests for 2FA API endpoints.

Ported from upstream Bambuddy v0.2.3 ``test_mfa_api.py`` (3265 LOC) — this
file keeps the high-value happy-path subset (~10 tests) that exercises the
full 2FA lifecycle. Critical security guards (JTI revocation, challenge-id
cookie binding, OIDC exchange replay, email OTP max attempts, rate limiting)
already live in ``test_security.py``; this file focuses on setup→enable→
verify→disable flows for TOTP, backup codes, and email OTP.

Covered endpoints:

- GET  /api/v1/auth/2fa/status
- POST /api/v1/auth/2fa/totp/setup
- POST /api/v1/auth/2fa/totp/enable
- POST /api/v1/auth/2fa/totp/disable
- POST /api/v1/auth/2fa/email/enable/confirm
- POST /api/v1/auth/2fa/verify   (TOTP + backup paths)

BamDude divergence from upstream:
- Tests use the pre-seeded ``test_admin`` token (conftest) to create each
  test user via POST /users/, then log in with an empty Authorization
  header — upstream used POST /auth/setup which is 403 once an admin exists.
- TOTP issuer is ``BamDude`` (not ``Bambuddy``).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from httpx import AsyncClient
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auth_ephemeral import AuthEphemeralToken
from backend.app.models.user import User

_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

LOGIN_URL = "/api/v1/auth/login"


def _norm_pw(password: str) -> str:
    """Ensure password meets complexity requirements."""
    if not any(c.isupper() for c in password):
        password = password[0].upper() + password[1:]
    if not any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" for c in password):
        password = password + "!"
    return password


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup_and_login(client: AsyncClient, username: str, password: str) -> str:
    """Create an admin user via the pre-seeded test_admin token, then login.

    Mirrors the helper in ``test_security.py``. The created user is added to
    the Administrators group so downstream admin-guarded endpoints work.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.core.database import async_session
    from backend.app.models.group import Group

    password = _norm_pw(password)
    create_resp = await client.post("/api/v1/users/", json={"username": username, "password": password})
    if create_resp.status_code not in (200, 201, 409):
        raise AssertionError(f"failed to create test user {username!r}: {create_resp.status_code} {create_resp.text}")

    async with async_session() as db:
        user = (
            await db.execute(select(User).options(selectinload(User.groups)).where(User.username == username))
        ).scalar_one()
        admin_group = (await db.execute(select(Group).where(Group.name == "Administrators"))).scalar_one_or_none()
        if admin_group is not None and admin_group not in user.groups:
            user.groups.append(admin_group)
        user.role = "admin"
        await db.commit()

    resp = await client.post(
        LOGIN_URL,
        json={"username": username, "password": password},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _login_get_pre_auth_token(client: AsyncClient, username: str, password: str) -> str:
    """Login a user who has 2FA enabled; return the pre_auth_token from the response."""
    password = _norm_pw(password)
    resp = await client.post(
        LOGIN_URL,
        json={"username": username, "password": password},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["requires_2fa"] is True, f"Expected requires_2fa=True, got {data}"
    assert data["pre_auth_token"] is not None
    return data["pre_auth_token"]


# ===========================================================================
# 2FA Status
# ===========================================================================


class TestTwoFAStatus:
    """GET /api/v1/auth/2fa/status."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_default_disabled(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "statususer", "statuspass123")
        response = await async_client.get("/api/v1/auth/2fa/status", headers=_auth_header(token))
        assert response.status_code == 200
        data = response.json()
        assert data["totp_enabled"] is False
        assert data["email_otp_enabled"] is False
        assert data["backup_codes_remaining"] == 0


# ===========================================================================
# TOTP Setup
# ===========================================================================


class TestTOTPSetup:
    """POST /api/v1/auth/2fa/totp/setup."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_setup_returns_secret_and_qr(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "totpsetup", "totpsetup123")
        response = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        assert response.status_code == 200
        data = response.json()
        assert "secret" in data
        assert len(data["secret"]) > 0
        assert "qr_code_b64" in data
        # BamDude divergence: issuer is "BamDude" (upstream uses "Bambuddy").
        assert data["issuer"] == "BamDude"
        # pyotp accepts the secret
        totp = pyotp.TOTP(data["secret"])
        assert len(totp.now()) == 6


# ===========================================================================
# TOTP Enable
# ===========================================================================


class TestTOTPEnable:
    """POST /api/v1/auth/2fa/totp/enable."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_enable_with_valid_code_returns_backup_codes(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "enableok", "enableok123")
        setup_resp = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        secret = setup_resp.json()["secret"]
        valid_code = pyotp.TOTP(secret).now()

        response = await async_client.post(
            "/api/v1/auth/2fa/totp/enable",
            json={"code": valid_code},
            headers=_auth_header(token),
        )
        assert response.status_code == 200
        data = response.json()
        assert "backup_codes" in data
        assert len(data["backup_codes"]) == 10
        for code in data["backup_codes"]:
            assert len(code) == 8

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_reflects_enabled_totp(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "statustotp", "statustotp1")
        setup_resp = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        secret = setup_resp.json()["secret"]
        valid_code = pyotp.TOTP(secret).now()
        await async_client.post(
            "/api/v1/auth/2fa/totp/enable",
            json={"code": valid_code},
            headers=_auth_header(token),
        )

        status_resp = await async_client.get("/api/v1/auth/2fa/status", headers=_auth_header(token))
        data = status_resp.json()
        assert data["totp_enabled"] is True
        assert data["backup_codes_remaining"] == 10


# ===========================================================================
# TOTP Disable
# ===========================================================================


class TestTOTPDisable:
    """POST /api/v1/auth/2fa/totp/disable."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_disable_with_valid_code(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "disableok", "disableok123")
        setup_resp = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        secret = setup_resp.json()["secret"]
        valid_code = pyotp.TOTP(secret).now()
        await async_client.post(
            "/api/v1/auth/2fa/totp/enable",
            json={"code": valid_code},
            headers=_auth_header(token),
        )

        disable_code = pyotp.TOTP(secret).now()
        response = await async_client.post(
            "/api/v1/auth/2fa/totp/disable",
            json={"code": disable_code},
            headers=_auth_header(token),
        )
        assert response.status_code == 200

        status_resp = await async_client.get("/api/v1/auth/2fa/status", headers=_auth_header(token))
        assert status_resp.json()["totp_enabled"] is False


# ===========================================================================
# Email OTP Enable
# ===========================================================================


class TestEmailOTPEnable:
    """POST /api/v1/auth/2fa/email/enable/confirm."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_confirm_enable_email_otp_happy_path(self, async_client: AsyncClient, db_session: AsyncSession):
        """Confirm step activates email OTP when setup_token + code are valid."""
        from sqlalchemy import select as sa_select

        token = await _setup_and_login(async_client, "confirmenable", "confirmenable1")

        # Give user an email address directly (SMTP not available in tests)
        user = (await db_session.execute(sa_select(User).where(User.username == "confirmenable"))).scalar_one()
        user.email = "confirmenable@example.com"
        await db_session.commit()

        # Inject a known setup token directly into the DB (bypasses SMTP)
        code = "123456"
        setup_token = secrets.token_urlsafe(32)
        db_session.add(
            AuthEphemeralToken(
                token=setup_token,
                token_type="email_otp_setup",
                username="confirmenable",
                nonce=_pwd_context.hash(code),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        await db_session.commit()

        resp = await async_client.post(
            "/api/v1/auth/2fa/email/enable/confirm",
            json={"setup_token": setup_token, "code": code},
            headers=_auth_header(token),
        )
        assert resp.status_code == 200

        status_resp = await async_client.get("/api/v1/auth/2fa/status", headers=_auth_header(token))
        assert status_resp.json()["email_otp_enabled"] is True


# ===========================================================================
# 2FA Verify — TOTP path
# ===========================================================================


class TestTwoFAVerifyTOTP:
    """POST /api/v1/auth/2fa/verify (method=totp)."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_verify_with_invalid_pre_auth_token(self, async_client: AsyncClient):
        response = await async_client.post(
            "/api/v1/auth/2fa/verify",
            json={"pre_auth_token": "bogus", "method": "totp", "code": "123456"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_verify_totp_issues_jwt(self, async_client: AsyncClient):
        """Full flow: setup → enable → login → pre_auth_token → verify → JWT."""
        token = await _setup_and_login(async_client, "verifytotpok", "verifytotpok1")
        setup_resp = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        secret = setup_resp.json()["secret"]
        valid_code = pyotp.TOTP(secret).now()
        await async_client.post(
            "/api/v1/auth/2fa/totp/enable",
            json={"code": valid_code},
            headers=_auth_header(token),
        )

        pre_auth_token = await _login_get_pre_auth_token(async_client, "verifytotpok", "verifytotpok1")
        verify_resp = await async_client.post(
            "/api/v1/auth/2fa/verify",
            json={
                "pre_auth_token": pre_auth_token,
                "method": "totp",
                "code": pyotp.TOTP(secret).now(),
            },
        )
        assert verify_resp.status_code == 200
        data = verify_resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["username"] == "verifytotpok"


# ===========================================================================
# 2FA Verify — Backup code path
# ===========================================================================


class TestTwoFAVerifyBackup:
    """POST /api/v1/auth/2fa/verify (method=backup)."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_verify_with_backup_code(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "backupcodeok", "backupcodeok1")
        setup_resp = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        secret = setup_resp.json()["secret"]
        valid_code = pyotp.TOTP(secret).now()
        enable_resp = await async_client.post(
            "/api/v1/auth/2fa/totp/enable",
            json={"code": valid_code},
            headers=_auth_header(token),
        )
        backup_code = enable_resp.json()["backup_codes"][0]

        pre_auth_token = await _login_get_pre_auth_token(async_client, "backupcodeok", "backupcodeok1")
        verify_resp = await async_client.post(
            "/api/v1/auth/2fa/verify",
            json={"pre_auth_token": pre_auth_token, "method": "backup", "code": backup_code},
        )
        assert verify_resp.status_code == 200
        assert "access_token" in verify_resp.json()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_backup_code_count_decrements(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "backupcount", "backupcount1")
        setup_resp = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        secret = setup_resp.json()["secret"]
        valid_code = pyotp.TOTP(secret).now()
        enable_resp = await async_client.post(
            "/api/v1/auth/2fa/totp/enable",
            json={"code": valid_code},
            headers=_auth_header(token),
        )
        backup_code = enable_resp.json()["backup_codes"][0]

        pre_auth_token = await _login_get_pre_auth_token(async_client, "backupcount", "backupcount1")
        await async_client.post(
            "/api/v1/auth/2fa/verify",
            json={"pre_auth_token": pre_auth_token, "method": "backup", "code": backup_code},
        )

        status_resp = await async_client.get("/api/v1/auth/2fa/status", headers=_auth_header(token))
        assert status_resp.json()["backup_codes_remaining"] == 9


# ===========================================================================
# pre_auth_token single-use (happy path)
# ===========================================================================


class TestPreAuthTokenSingleUse:
    """A successfully-used pre_auth_token cannot be reused."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pre_auth_token_is_single_use(self, async_client: AsyncClient):
        token = await _setup_and_login(async_client, "singleusepat", "singleusepat1")
        setup_resp = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=_auth_header(token))
        secret = setup_resp.json()["secret"]
        valid_code = pyotp.TOTP(secret).now()
        await async_client.post(
            "/api/v1/auth/2fa/totp/enable",
            json={"code": valid_code},
            headers=_auth_header(token),
        )

        pre_auth_token = await _login_get_pre_auth_token(async_client, "singleusepat", "singleusepat1")
        first = await async_client.post(
            "/api/v1/auth/2fa/verify",
            json={"pre_auth_token": pre_auth_token, "method": "totp", "code": pyotp.TOTP(secret).now()},
        )
        assert first.status_code == 200

        # Second use of the same token must fail
        second = await async_client.post(
            "/api/v1/auth/2fa/verify",
            json={"pre_auth_token": pre_auth_token, "method": "totp", "code": pyotp.TOTP(secret).now()},
        )
        assert second.status_code == 401
