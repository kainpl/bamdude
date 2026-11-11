"""Server-bound SSO persistence across real callback, factors and rotation.

Only IdP HTTP is mocked; ID-token signature/nonce, one-time DB tokens and
refresh cookies run through the actual routes. No live provider is contacted.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from backend.app.api.routes import mfa
from backend.app.core.auth import REFRESH_TOKEN_COOKIE_NAME
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.oidc_provider import OIDCProvider, UserOIDCLink
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.models.user_totp import UserTOTP

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
async def oidc_flow(async_client, db_session, monkeypatch, signing_key):
    user = await db_session.scalar(select(User).where(User.username == "test_admin"))
    user.auth_source = "oidc"
    provider = OIDCProvider(name="F IdP", issuer_url="https://idp.invalid", client_id="farm", client_secret="secret")
    db_session.add(provider)
    db_session.add(Settings(key="local_login_enabled", value="false"))
    await db_session.flush()
    db_session.add(UserOIDCLink(user_id=user.id, provider_id=provider.id, provider_user_id="f-sub"))
    await db_session.commit()
    nonce = None
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk["kid"] = "f-key"

    class IdP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            if url.endswith("/jwks"):
                data = {"keys": [jwk]}
            else:
                assert url == "https://idp.invalid/.well-known/openid-configuration"
                data = {
                    "issuer": "https://idp.invalid",
                    "authorization_endpoint": "https://idp.invalid/authorize",
                    "token_endpoint": "https://idp.invalid/token",
                    "jwks_uri": "https://idp.invalid/jwks",
                }
            return httpx.Response(200, json=data, request=httpx.Request("GET", url))

        async def post(self, url, **kwargs):
            assert url == "https://idp.invalid/token"
            token = jwt.encode(
                {
                    "iss": "https://idp.invalid",
                    "aud": "farm",
                    "sub": "f-sub",
                    "nonce": nonce,
                    "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                },
                signing_key,
                algorithm="RS256",
                headers={"kid": "f-key"},
            )
            return httpx.Response(200, json={"id_token": token}, request=httpx.Request("POST", url))

    monkeypatch.setattr(mfa.httpx, "AsyncClient", lambda **kwargs: IdP())

    async def start(choice=None, forged_callback=None, legacy_state=False):
        nonlocal nonce
        params = {} if choice is None else {"remember_me": str(choice).lower()}
        authorize = await async_client.get(f"/api/v1/auth/oidc/authorize/{provider.id}", params=params)
        assert authorize.status_code == 200, authorize.text
        query = parse_qs(urlsplit(authorize.json()["auth_url"]).query)
        nonce = query["nonce"][0]
        state = query["state"][0]
        if legacy_state:
            row = await db_session.scalar(select(AuthEphemeralToken).where(AuthEphemeralToken.token == state))
            row.remember_me = None
            await db_session.commit()
        callback_params = {"state": state, "code": "valid-code"}
        if forged_callback is not None:
            callback_params["remember_me"] = str(forged_callback).lower()
        callback = await async_client.get("/api/v1/auth/oidc/callback", params=callback_params, follow_redirects=False)
        assert callback.status_code == 302, callback.text
        location = callback.headers["location"]
        assert "#oidc_token=" in location, location
        token = parse_qs(urlsplit(location).fragment)["oidc_token"][0]
        return state, token

    return start, user


def assert_cookie(response, checked, ceiling=720):
    assert response.status_code == 200, response.text
    cookie = next(x for x in response.headers.get_list("set-cookie") if x.startswith(REFRESH_TOKEN_COOKIE_NAME + "="))
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    if checked:
        assert f"Max-Age={min(720, ceiling) * 3600}" in cookie
    else:
        assert "Max-Age" not in cookie


@pytest.mark.parametrize("checked,ceiling", [(False, 720), (True, 720), (True, 8), (False, 8)])
async def test_oidc_cookie_and_rotation_use_state_choice(checked, ceiling, oidc_flow, async_client, db_session):
    db_session.add(Settings(key="session_max_hours", value=str(ceiling)))
    await db_session.commit()
    start, _ = oidc_flow
    state, token = await start(checked, forged_callback=not checked)
    exchange = await async_client.post("/api/v1/auth/oidc/exchange", json={"oidc_token": token})
    assert_cookie(exchange, checked, ceiling)
    assert exchange.json()["user"]["auth_source"] == "oidc"
    rotated = await async_client.post("/api/v1/auth/refresh", headers={"Authorization": ""})
    assert_cookie(rotated, checked, ceiling)
    await db_session.rollback()
    rows = (
        await db_session.scalars(select(AuthEphemeralToken).where(AuthEphemeralToken.token_type == TokenType.REFRESH))
    ).all()
    assert len(rows) == 2
    assert all(row.remember_me is checked for row in rows)
    expected = min(720 if checked else 24, ceiling)
    assert all(abs((row.expires_at - row.created_at).total_seconds() / 3600 - expected) < 0.01 for row in rows)
    replay = await async_client.post("/api/v1/auth/oidc/exchange", json={"oidc_token": token})
    assert replay.status_code == 401
    callback_replay = await async_client.get(
        "/api/v1/auth/oidc/callback", params={"state": state, "code": "again"}, follow_redirects=False
    )
    assert "invalid_state" in callback_replay.headers["location"]


async def test_new_attempt_defaults_false_after_checked_attempt(oidc_flow, async_client):
    start, _ = oidc_flow
    await start(True)
    _, token = await start()
    exchange = await async_client.post("/api/v1/auth/oidc/exchange", json={"oidc_token": token})
    assert_cookie(exchange, False)


@pytest.mark.parametrize("checked", [False, True])
@pytest.mark.parametrize("method", ["totp", "backup", "email"])
async def test_oidc_choice_survives_factor_and_email_resend(
    checked, method, oidc_flow, async_client, db_session, monkeypatch
):
    start, user = oidc_flow
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = "JBSWY3DPEHPK3PXP"
    factor.backup_code_hashes = [mfa.pwd_context.hash("FBACKUP1")]
    db_session.add(factor)
    if method == "email":
        await mfa._set_email_2fa_enabled(db_session, user.id, True)
        user.email = "factor@example.invalid"
    await db_session.commit()
    _, token = await start(checked)
    exchange = await async_client.post("/api/v1/auth/oidc/exchange", json={"oidc_token": token})
    assert exchange.status_code == 200 and exchange.json()["requires_2fa"], exchange.text
    assert REFRESH_TOKEN_COOKIE_NAME not in exchange.cookies
    pre_auth = exchange.json()["pre_auth_token"]
    code = pyotp.TOTP(factor.secret).now() if method == "totp" else "FBACKUP1"
    if method == "email":
        captured = {}
        monkeypatch.setattr(mfa, "get_smtp_settings", AsyncMock(return_value={"host": "offline"}))
        monkeypatch.setattr(mfa, "send_email", lambda **kwargs: captured.update(kwargs))
        for _ in range(2):
            sent = await async_client.post("/api/v1/auth/2fa/email/send", json={"pre_auth_token": pre_auth})
            assert sent.status_code == 200, sent.text
            old = pre_auth
            pre_auth = sent.json()["pre_auth_token"]
            assert pre_auth != old
        code = captured["body_text"].split("code is: ")[1].split("\n")[0]
    verified = await async_client.post(
        "/api/v1/auth/2fa/verify",
        json={"pre_auth_token": pre_auth, "code": code, "method": method, "remember_me": not checked},
    )
    assert_cookie(verified, checked)
    assert verified.json()["user"]["auth_source"] == "oidc"
    replay = await async_client.post(
        "/api/v1/auth/2fa/verify", json={"pre_auth_token": pre_auth, "code": code, "method": method}
    )
    assert replay.status_code == 401
    rotated = await async_client.post("/api/v1/auth/refresh", headers={"Authorization": ""})
    assert_cookie(rotated, checked)


@pytest.mark.parametrize("legacy_state", [False, True])
async def test_legacy_oidc_null_defaults_false(legacy_state, oidc_flow, async_client, db_session):
    start, _ = oidc_flow
    _, token = await start(True, legacy_state=legacy_state)
    if not legacy_state:
        row = await db_session.scalar(select(AuthEphemeralToken).where(AuthEphemeralToken.token == token))
        row.remember_me = None
        await db_session.commit()
    exchange = await async_client.post("/api/v1/auth/oidc/exchange", json={"oidc_token": token})
    assert_cookie(exchange, False)


@pytest.mark.parametrize("checked", [False, True])
async def test_local_mfa_keeps_existing_client_preference(checked, async_client, db_session):
    user = await db_session.scalar(select(User).where(User.username == "test_admin"))
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = "JBSWY3DPEHPK3PXP"
    db_session.add(factor)
    await db_session.commit()
    login = await async_client.post(
        "/api/v1/auth/login", json={"username": "test_admin", "password": "Test_AdminPass1!"}
    )
    assert login.status_code == 200 and login.json()["requires_2fa"], login.text
    verified = await async_client.post(
        "/api/v1/auth/2fa/verify",
        json={
            "pre_auth_token": login.json()["pre_auth_token"],
            "code": pyotp.TOTP(factor.secret).now(),
            "method": "totp",
            "remember_me": checked,
        },
    )
    assert_cookie(verified, checked)
    assert verified.json()["user"]["auth_source"] == "local"


@pytest.mark.parametrize("legacy_hours,checked", [(8, False), (48, True)])
async def test_legacy_refresh_rows_keep_lifespan_inference(legacy_hours, checked, async_client, db_session):
    from backend.app.core.auth import _hash_refresh_token

    now = datetime.now(timezone.utc)
    db_session.add(
        AuthEphemeralToken.new_refresh(
            token_hash=_hash_refresh_token("f-legacy-refresh"),
            username="test_admin",
            family_id="f-legacy-family",
            expires_at=now + timedelta(hours=legacy_hours),
        )
    )
    await db_session.commit()
    rotated = await async_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": "", "Cookie": f"{REFRESH_TOKEN_COOKIE_NAME}=f-legacy-refresh"}
    )
    assert_cookie(rotated, checked)
