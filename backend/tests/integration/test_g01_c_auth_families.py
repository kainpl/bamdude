"""Real HTTP boundaries for current identity-bearing login and MFA families."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pyotp
import pytest
from sqlalchemy import select

from backend.app.api.routes import auth as auth_routes, mfa
from backend.app.core import auth
from backend.app.core.auth_lifetime import (
    VerifiedOutcome,
    binding,
    capture_account,
    stage_end_sessions,
    stage_session,
    utcnow,
)
from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.auth_session import AuthSession
from backend.app.models.user import User
from backend.app.models.user_totp import UserTOTP

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def mint(db, name, *, source="local", metadata="local", password=None):
    user = User(
        username=name,
        role="admin",
        is_active=True,
        auth_source=metadata,
        password_hash=auth.get_password_hash(password) if password else None,
    )
    db.add(user)
    await db.flush()
    issued = await stage_session(db, VerifiedOutcome(capture_account(user), source), remember_me=False)
    await db.commit()
    return user, issued


def headers(issued):
    return {"Authorization": f"Bearer {issued.access_token}"}


@pytest.mark.parametrize(
    "source,metadata,password,submitted,expected",
    [
        ("local", "oidc", None, None, 401),
        ("ldap", "local", None, None, 200),
        ("oidc", "local", None, None, 200),
        ("oidc", "oidc", "OwnerPass1!", "wrong", 401),
        ("oidc", "oidc", "OwnerPass1!", "OwnerPass1!", 200),
    ],
)
async def test_admin_mfa_reset_uses_recorded_session_source_and_local_password_if_present(
    async_client, db_session, source, metadata, password, submitted, expected
):
    actor, issued = await mint(db_session, "mfa-actor", source=source, metadata=metadata, password=password)
    target, target_session = await mint(db_session, "mfa-target")
    response = await async_client.request(
        "DELETE", f"/api/v1/auth/2fa/admin/{target.id}", headers=headers(issued), json={"admin_password": submitted}
    )
    assert response.status_code == expected, response.text
    await db_session.rollback()
    target = await db_session.get(User, target.id, populate_existing=True)
    assert target.session_generation == (1 if expected == 200 else 0)
    old = await async_client.get("/api/v1/auth/me", headers=headers(target_session))
    assert old.status_code == (401 if expected == 200 else 200)


async def test_pending_totp_setup_belongs_to_exact_browser_session(async_client, db_session):
    user, first = await mint(db_session, "totp-browser")
    second = await stage_session(db_session, VerifiedOutcome(capture_account(user), "local"), remember_me=False)
    await db_session.commit()
    setup = await async_client.post("/api/v1/auth/2fa/totp/setup", headers=headers(first))
    assert setup.status_code == 200, setup.text
    code = pyotp.TOTP(setup.json()["secret"]).now()
    crossed = await async_client.post("/api/v1/auth/2fa/totp/enable", headers=headers(second), json={"code": code})
    assert crossed.status_code == 400, crossed.text
    own = await async_client.post("/api/v1/auth/2fa/totp/enable", headers=headers(first), json={"code": code})
    assert own.status_code == 200, own.text


async def test_end_sessions_invalidates_pre_auth_and_reset_without_changing_password(async_client, db_session):
    user, issued = await mint(db_session, "pending-owner", password="OldPass123!")
    original_hash = user.password_hash
    pending = await mfa.create_pre_auth_token(
        db_session, user.username, verified=VerifiedOutcome(capture_account(user), "local")
    )
    reset = await auth_routes.create_password_reset_token(db_session, user.username)
    await stage_end_sessions(db_session, capture_account(user))
    await db_session.commit()
    factor = await async_client.post(
        "/api/v1/auth/2fa/verify", json={"pre_auth_token": pending, "method": "email", "code": "123456"}
    )
    assert factor.status_code == 401
    reset_result = await async_client.post(
        "/api/v1/auth/forgot-password/confirm", json={"token": reset, "new_password": "NewPass123!"}
    )
    assert reset_result.status_code == 400
    await db_session.rollback()
    assert (await db_session.get(User, user.id, populate_existing=True)).password_hash == original_hash


async def test_legacy_username_only_pre_auth_cannot_consume_factor_or_issue_session(async_client, db_session):
    user, issued = await mint(db_session, "legacy-pending")
    secret = pyotp.random_base32()
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = secret
    db_session.add_all(
        [
            factor,
            AuthEphemeralToken(
                token="legacy-pre-auth",
                token_type=TokenType.PRE_AUTH,
                username=user.username,
                expires_at=utcnow() + timedelta(minutes=5),
            ),
        ]
    )
    await db_session.commit()
    factor_id = factor.id
    before = len((await db_session.scalars(select(AuthSession))).all())
    response = await async_client.post(
        "/api/v1/auth/2fa/verify",
        json={"pre_auth_token": "legacy-pre-auth", "method": "totp", "code": pyotp.TOTP(secret).now()},
    )
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    await db_session.rollback()
    assert len((await db_session.scalars(select(AuthSession))).all()) == before
    assert (await db_session.get(UserTOTP, factor_id, populate_existing=True)).last_totp_counter is None


async def test_oidc_delayed_remote_attempt_is_drained_before_restore_can_reopen(async_client, db_session, monkeypatch):
    # A consumed state is no longer in the DB when IdP I/O starts. The route
    # operation must remain owned after its short SQL transaction has ended.
    from backend.app.models.oidc_provider import OIDCProvider

    provider = OIDCProvider(name="Delayed", issuer_url="https://idp.invalid", client_id="farm", client_secret="opaque")
    db_session.add(provider)
    await db_session.flush()
    db_session.add(
        AuthEphemeralToken(
            token="owned-state",
            token_type=TokenType.OIDC_STATE,
            provider_id=provider.id,
            nonce="nonce",
            code_verifier="verifier",
            contract_version=1,
            expires_at=utcnow() + timedelta(minutes=5),
        )
    )
    await db_session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    class IdP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, _):
            started.set()
            await release.wait()
            raise RuntimeError("remote unavailable")

    monkeypatch.setattr(mfa.httpx, "AsyncClient", lambda **_: IdP())
    request = asyncio.create_task(async_client.get("/api/v1/auth/oidc/callback?code=code&state=owned-state"))
    await asyncio.wait_for(started.wait(), 2)
    assert not await auth_runtime.quiesce_for_restore(0.01)
    assert not auth_runtime.admitting
    # No transaction/checkout is kept across the IdP wait.
    assert not auth_runtime._transactions
    release.set()
    response = await asyncio.wait_for(request, 2)
    assert response.status_code == 302
    assert await auth_runtime.quiesce_for_restore(1)
    auth_runtime.open_admission()
    await db_session.rollback()
    assert not (
        await db_session.scalars(
            select(AuthEphemeralToken).where(AuthEphemeralToken.token_type == TokenType.OIDC_EXCHANGE)
        )
    ).all()


@pytest.mark.parametrize("alias", [False, True])
async def test_ldap_verified_outcome_cannot_cross_account_generation_before_sync(
    async_client, db_session, monkeypatch, alias
):
    import threading
    from types import SimpleNamespace

    from backend.app.services import ldap_service

    user, old = await mint(db_session, "ldap-canonical", source="ldap", metadata="ldap")
    started, release = threading.Event(), threading.Event()
    calls = 0
    expected_calls = 2 if alias else 1

    def directory_verify(*_):
        nonlocal calls
        calls += 1
        if calls == expected_calls:
            started.set()
            if not release.wait(5):
                raise RuntimeError("LDAP test barrier not released")
        return SimpleNamespace(username="ldap-canonical", email=None, groups=[], groups_complete=True)

    monkeypatch.setattr(auth_routes, "_get_ldap_settings", AsyncMock(return_value={"ldap_enabled": "true"}))
    monkeypatch.setattr(ldap_service, "parse_ldap_config", lambda _: SimpleNamespace(auto_provision=True))
    monkeypatch.setattr(ldap_service, "authenticate_ldap_user", directory_verify)
    sync = AsyncMock()
    monkeypatch.setattr(auth_routes, "_sync_ldap_user", sync)
    request = asyncio.create_task(
        async_client.post(
            "/api/v1/auth/login",
            json={"username": "typed-directory-alias" if alias else "ldap-canonical", "password": "directory-proof"},
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert not auth_runtime._transactions, "LDAP network proof must not own a DB checkout"
        await stage_end_sessions(db_session, capture_account(user))
        await db_session.commit()
        release.set()
        response = await asyncio.wait_for(request, 2)
        assert response.status_code == 401, response.text
        assert "set-cookie" not in response.headers
        assert calls == expected_calls
        sync.assert_not_awaited()
    finally:
        release.set()
        if not request.done():
            await asyncio.wait_for(request, 2)


async def test_forgot_password_confirm_changes_hash_and_epoch_atomically(async_client, db_session):
    user, issued = await mint(db_session, "reset-current-owner", password="Old_Password1!")
    raw = await auth_routes.create_password_reset_token(db_session, user.username)
    response = await async_client.post(
        "/api/v1/auth/forgot-password/confirm", json={"token": raw, "new_password": "New_Password1!"}
    )
    assert response.status_code == 200, response.text
    await db_session.rollback()
    owner = await db_session.get(User, user.id, populate_existing=True)
    assert owner.session_generation == 1
    assert auth.verify_password("New_Password1!", owner.password_hash)
    assert not auth.verify_password("Old_Password1!", owner.password_hash)
    assert (await async_client.get("/api/v1/auth/me", headers=headers(issued))).status_code == 401
    replay = await async_client.post(
        "/api/v1/auth/forgot-password/confirm", json={"token": raw, "new_password": "Other_Password1!"}
    )
    assert replay.status_code == 400


@pytest.mark.parametrize("door", ["search", "provision"])
async def test_manual_ldap_door_rechecks_actor_after_owned_network_without_checkout(
    async_client, db_session, monkeypatch, door
):
    import threading
    from types import SimpleNamespace

    from backend.app.services import ldap_service

    user, issued = await mint(db_session, "ldap-manual-admin")
    started, release = threading.Event(), threading.Event()
    result = SimpleNamespace(
        username="manual-new-user",
        email=None,
        groups=[],
        groups_complete=True,
        display_name="Directory user",
        dn="uid=manual-new-user,dc=example",
    )

    def remote(*_, **__):
        started.set()
        if not release.wait(5):
            raise RuntimeError("manual LDAP barrier not released")
        return [result] if door == "search" else result

    monkeypatch.setattr(auth_routes, "_get_ldap_settings", AsyncMock(return_value={"ldap_enabled": "true"}))
    monkeypatch.setattr(
        ldap_service,
        "parse_ldap_config",
        lambda _: SimpleNamespace(auto_provision=True, group_mapping={}, default_group=None),
    )
    monkeypatch.setattr(ldap_service, "search_ldap_users" if door == "search" else "lookup_ldap_user", remote)
    request = asyncio.create_task(
        async_client.get("/api/v1/auth/ldap/search?q=user", headers=headers(issued))
        if door == "search"
        else async_client.post(
            "/api/v1/auth/ldap/provision", headers=headers(issued), json={"username": "manual-new-user"}
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert not auth_runtime._transactions
        await stage_end_sessions(db_session, capture_account(user))
        await db_session.commit()
        release.set()
        response = await asyncio.wait_for(request, 2)
        assert response.status_code == 401, response.text
        assert "Directory user" not in response.text
        await db_session.rollback()
        assert await db_session.scalar(select(User).where(User.username == "manual-new-user")) is None
    finally:
        release.set()
        if not request.done():
            await asyncio.wait_for(request, 2)


@pytest.mark.parametrize("family", ["oidc", "reset", "pre_auth"])
async def test_single_use_token_cannot_consume_replacement_with_reused_numeric_id(
    async_client, db_session, monkeypatch, family
):
    from sqlalchemy import delete

    from backend.app.core.auth_lifetime import capture_ephemeral

    user, issued = await mint(db_session, "one-shot-owner", password="OriginalPass1!")
    if family == "reset":
        raw = await auth_routes.create_password_reset_token(db_session, user.username)
        lookup = auth.hash_client_secret(raw)
        module = auth_routes
    else:
        raw = "one-shot-original"
        lookup = raw
        module = mfa
        db_session.add(
            AuthEphemeralToken(
                token=raw,
                token_type=TokenType.OIDC_EXCHANGE if family == "oidc" else TokenType.PRE_AUTH,
                auth_source="oidc" if family == "oidc" else "local",
                remember_me=False,
                username=user.username,
                expires_at=utcnow() + timedelta(minutes=5),
                **binding(capture_account(user)),
            )
        )
        await db_session.commit()
    original_row = await db_session.scalar(select(AuthEphemeralToken).where(AuthEphemeralToken.token == lookup))
    row_id = original_row.id
    await db_session.rollback()
    original_guard = module.guard_accounts
    replaced = False

    async def reuse_pk(db, captures, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            # Simulates a first concurrent consumer committing its refresh at
            # the same SQLite max PK before this request acquires the guard.
            from backend.app.core import database

            async with database.async_session() as mutation:
                await mutation.execute(delete(AuthEphemeralToken).where(AuthEphemeralToken.id == row_id))
                mutation.add(
                    AuthEphemeralToken(
                        id=row_id,
                        token="replacement-refresh",
                        token_type=TokenType.REFRESH,
                        username="one-shot-owner",
                        family_id=issued.sid,
                        expires_at=utcnow() + timedelta(days=1),
                        **binding(issued.capture),
                    )
                )
                await mutation.commit()
        return await original_guard(db, captures, **kwargs)

    monkeypatch.setattr(module, "guard_accounts", reuse_pk)
    if family == "oidc":
        response = await async_client.post("/api/v1/auth/oidc/exchange", json={"oidc_token": raw})
        assert response.status_code == 401, response.text
        assert "set-cookie" not in response.headers
    elif family == "reset":
        response = await async_client.post(
            "/api/v1/auth/forgot-password/confirm", json={"token": raw, "new_password": "ReplacementPass1!"}
        )
        assert response.status_code == 400, response.text
    else:
        async with auth.async_session() as consuming:
            assert await mfa.consume_pre_auth_token(consuming, raw) is None
    await db_session.rollback()
    replacement = await db_session.get(AuthEphemeralToken, row_id)
    assert replacement.token == "replacement-refresh"
    assert replacement.token_type == TokenType.REFRESH
    assert replaced
