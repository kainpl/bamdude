"""Credential lifetimes are immutable identities, not usernames or timestamps."""

from datetime import timedelta

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core import auth
from backend.app.core.auth_lifetime import (
    FrozenAuthority,
    VerifiedOutcome,
    capture_account,
    capture_key,
    capture_long_lived,
    guard_accounts,
    stage_end_sessions,
    stage_revoke_session,
    stage_session,
    utcnow,
    validate_captured,
)
from backend.app.core.auth_runtime import AuthRuntime, auth_runtime
from backend.app.models.api_key import APIKey
from backend.app.models.auth_ephemeral import AuthEphemeralToken
from backend.app.models.auth_session import AuthSession
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.settings import Settings
from backend.app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sessions(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(auth, "async_session", factory)
    monkeypatch.setattr(auth_runtime, "admitting", True)
    return factory


async def owner_and_session(factory, name="identity-owner", *, source="local", remember=False):
    async with factory() as db:
        user = User(username=name, role="admin", is_active=True, auth_source=source)
        db.add(user)
        await db.flush()
        issued = await stage_session(db, VerifiedOutcome(capture_account(user), source), remember_me=remember)
        await db.commit()
        return user, issued


async def independent_credentials(factory, user):
    async with factory() as db:
        key = APIKey(
            name="independent",
            key_hash="hash",
            key_prefix="bd_test",
            user_id=user.id,
            owner_identity=user.account_identity,
            contract_version=1,
            enabled=True,
        )
        token = LongLivedToken(
            user_id=user.id,
            owner_identity=user.account_identity,
            contract_version=1,
            name="wall",
            lookup_prefix="12345678",
            secret_hash="hash",
            scope="camera_stream",
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add_all([key, token])
        await db.commit()
        return capture_key(key), capture_long_lived(token)


async def test_sub_only_and_recycled_username_never_establish_identity(sessions):
    user, issued = await owner_and_session(sessions)
    legacy = auth.create_access_token({"sub": user.username})
    with pytest.raises(auth.JWTValidationFailure):
        await auth._validate_jwt_authority(legacy)
    async with sessions() as db:
        await db.execute(delete(User).where(User.id == user.id))
        replacement = User(id=user.id, username=user.username, role="admin", is_active=True)
        db.add(replacement)
        await db.commit()
        assert replacement.account_identity != user.account_identity
    with pytest.raises(auth.JWTValidationFailure):
        await auth._validate_jwt_authority(issued.access_token)


async def test_same_second_end_sessions_rejects_old_outcome_and_all_derivatives(sessions):
    user, issued = await owner_and_session(sessions)
    stale = VerifiedOutcome(capture_account(user), "local")
    media = await auth.create_media_token(authority=issued.capture)
    async with sessions() as db:
        await stage_end_sessions(db, stale.account)
        await db.commit()
    with pytest.raises(auth.JWTValidationFailure):
        await auth._validate_jwt_authority(issued.access_token)
    with pytest.raises(HTTPException) as caught:
        await auth._media_token_authority(media)
    assert caught.value.status_code == 401
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await stage_session(db, stale, remember_me=False)
        await db.rollback()
        current = await db.get(User, user.id)
        assert current.session_generation == stale.account.generation + 1
        fresh = await stage_session(db, VerifiedOutcome(capture_account(current), "local"), remember_me=False)
        await db.commit()
    assert (await auth._validate_jwt_authority(fresh.access_token)).capture == fresh.capture


async def test_logout_one_sid_preserves_other_sid_and_independent_credentials(sessions):
    user, first = await owner_and_session(sessions)
    async with sessions() as db:
        current = await db.get(User, user.id)
        second = await stage_session(db, VerifiedOutcome(capture_account(current), "local"), remember_me=False)
        await db.commit()
    key, token = await independent_credentials(sessions, user)
    async with sessions() as db:
        await stage_revoke_session(db, first.capture)
        await db.commit()
    with pytest.raises(auth.JWTValidationFailure):
        await auth._validate_jwt_authority(first.access_token)
    assert (await auth._validate_jwt_authority(second.access_token)).capture.sid == second.sid
    async with sessions() as db:
        assert (await validate_captured(db, key)).api_key is not None
        assert (await validate_captured(db, token)).long_lived_token is not None


async def test_reactivation_resumes_independent_credentials_but_never_browser_sid(sessions):
    user, issued = await owner_and_session(sessions)
    key, token = await independent_credentials(sessions, user)
    async with sessions() as db:
        current = await db.get(User, user.id)
        await stage_end_sessions(db, capture_account(current), "deactivated")
        current.is_active = False
        await db.commit()
    async with sessions() as db:
        for capture in [issued.capture, key, token]:
            with pytest.raises(HTTPException):
                await validate_captured(db, capture)
        current = await db.get(User, user.id)
        current.is_active = True
        await db.commit()
    async with sessions() as db:
        assert (await validate_captured(db, key)).user.account_identity == user.account_identity
        assert (await validate_captured(db, token)).user.account_identity == user.account_identity
        with pytest.raises(HTTPException):
            await validate_captured(db, issued.capture)


@pytest.mark.parametrize("family", ["key", "long_lived"])
async def test_retirement_is_not_reversed_by_enabling_or_extending_expiry(sessions, family):
    user, _ = await owner_and_session(sessions)
    key, token = await independent_credentials(sessions, user)
    capture = key if family == "key" else token
    async with sessions() as db:
        principal = await validate_captured(db, capture)
        row = principal.api_key if family == "key" else principal.long_lived_token
        row.retired_reason, row.retired_at = "legacy_credential_reissue_required", utcnow()
        row.contract_version = 1
        if family == "key":
            row.enabled = True
        else:
            row.revoked_at = None
        row.expires_at = utcnow() + timedelta(days=10)
        await db.commit()
    async with sessions() as db:
        with pytest.raises(HTTPException) as caught:
            await validate_captured(db, capture)
        assert caught.value.status_code == 401


async def test_refresh_race_preserves_family_and_true_replay_retires_access(sessions, monkeypatch):
    user, issued = await owner_and_session(sessions)
    async with sessions() as db:
        assert (await auth.verify_and_consume_refresh_token(db, issued.raw_refresh))[2] == "ok"
        principal = await validate_captured(db, issued.capture)
        rotated = await stage_session(
            db, VerifiedOutcome(capture_account(principal.user), "local"), remember_me=False, existing_sid=issued.sid
        )
        await db.commit()
    async with sessions() as db:
        assert (await auth.verify_and_consume_refresh_token(db, issued.raw_refresh))[2] == "race"
        await db.commit()
        assert (await validate_captured(db, rotated.capture)).user.id == user.id
    monkeypatch.setattr(auth, "REFRESH_REUSE_GRACE_SECONDS", -1)
    async with sessions() as db:
        assert (await auth.verify_and_consume_refresh_token(db, issued.raw_refresh))[2] == "reuse"
        await db.commit()
    with pytest.raises(auth.JWTValidationFailure):
        await auth._validate_jwt_authority(rotated.access_token)


async def test_refresh_remains_sliding_and_preserves_explicit_preference_under_short_ceiling(sessions):
    async with sessions() as db:
        db.add(Settings(key="session_max_hours", value="2"))
        await db.commit()
    user, issued = await owner_and_session(sessions, remember=True)
    async with sessions() as db:
        record = await db.get(AuthSession, issued.sid)
        record.created_at = utcnow() - timedelta(days=90)
        await db.commit()
        current = await db.get(User, user.id)
        rotated = await stage_session(
            db, VerifiedOutcome(capture_account(current), "local"), remember_me=True, existing_sid=issued.sid
        )
        await db.commit()
        record = await db.get(AuthSession, issued.sid)
        assert record.remember_me is True
        assert timedelta(hours=1, minutes=59) < rotated.expires_at - utcnow() <= timedelta(hours=2)


async def test_guard_proof_cannot_be_reused_after_commit_or_on_another_session(sessions):
    _, issued = await owner_and_session(sessions)
    async with sessions() as first, sessions() as second:
        proof = await guard_accounts(first, [issued.capture])
        with pytest.raises(RuntimeError):
            await validate_captured(second, issued.capture, proof=proof)
        await first.commit()
        with pytest.raises(RuntimeError):
            await validate_captured(first, issued.capture, proof=proof)


async def test_restore_drain_waits_until_actual_outer_transaction_ends(sessions):
    runtime = AuthRuntime()
    async with sessions() as db:
        runtime.track_transaction(db)
        await db.execute(select(User.id))
        assert not await runtime.quiesce_for_restore(timeout=0)
        await db.rollback()
        assert await runtime.quiesce_for_restore(timeout=0.1)


async def test_setup_access_only_has_real_session_without_new_refresh_cookie(sessions):
    async with sessions() as db:
        user = User(username="setup", role="admin", is_active=True)
        db.add(user)
        await db.flush()
        issued = await stage_session(
            db, VerifiedOutcome(capture_account(user), "local"), remember_me=False, issue_refresh=False
        )
        await db.commit()
        claims = jwt.decode(issued.access_token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        assert claims["sid"] == issued.sid
        assert issued.raw_refresh is None
        assert await db.get(AuthSession, issued.sid) is not None
        assert not list((await db.execute(select(AuthEphemeralToken))).scalars())


async def test_key_revoke_between_hash_and_authority_reread_never_returns_old_grant(sessions, monkeypatch):
    import asyncio

    from sqlalchemy import update

    user, issued = await owner_and_session(sessions)
    key_capture, _ = await independent_credentials(sessions, user)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_hash(_credential, candidates):
        started.set()
        await release.wait()
        return candidates[0]

    monkeypatch.setattr(auth, "_scan_api_key", slow_hash)
    attempt = asyncio.create_task(auth.resolve_api_key_authority(None, "bd_raw"))
    await asyncio.wait_for(started.wait(), 2)
    assert not auth_runtime._transactions, "hash work must not retain SQL transaction"
    async with sessions() as db:
        await db.execute(
            update(APIKey).where(APIKey.connector_identity == key_capture.key_identity).values(enabled=False)
        )
        await db.commit()
    release.set()
    with pytest.raises(auth.APIKeyValidationFailure):
        await asyncio.wait_for(attempt, 2)


async def test_long_lived_hash_outcome_cannot_transfer_to_recreated_numeric_owner(sessions, monkeypatch):
    import asyncio

    from backend.app.services import long_lived_tokens as ll

    user, issued = await owner_and_session(sessions)
    _, captured = await independent_credentials(sessions, user)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_hash(_credential, candidates):
        started.set()
        await release.wait()
        return candidates[0]

    monkeypatch.setattr(ll, "async_session", sessions)
    monkeypatch.setattr(ll, "_scan_api_key", slow_hash)
    async with sessions() as db:
        token = await db.scalar(select(LongLivedToken).where(LongLivedToken.token_identity == captured.token_identity))
        token.lookup_prefix = "12345678"
        await db.commit()
    async with sessions() as unused:
        attempt = asyncio.create_task(ll.verify_token(unused, "bblt_12345678_secret"))
        await asyncio.wait_for(started.wait(), 2)
        assert not auth_runtime._transactions
        async with sessions() as db:
            await db.execute(delete(User).where(User.id == user.id))
            db.add(User(id=user.id, username=user.username, role="admin", is_active=True))
            await db.commit()
        release.set()
        assert await asyncio.wait_for(attempt, 2) is None


async def test_usage_audit_failures_are_throttled_separately_without_fabricating_authority(sessions, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import OperationalError

    from backend.app.services import long_lived_tokens as ll

    user, issued = await owner_and_session(sessions)
    key, token = await independent_credentials(sessions, user)
    calls = 0

    @asynccontextmanager
    async def failed_factory():
        nonlocal calls
        calls += 1
        raise OperationalError("audit", {}, RuntimeError("offline"))
        yield

    monkeypatch.setattr(auth, "async_session", failed_factory)
    monkeypatch.setattr(ll, "async_session", failed_factory)
    auth._key_audit_times.clear()
    ll._audit_after.clear()
    await auth._audit_key_use(key, "hash")
    await auth._audit_key_use(key, "hash")
    await ll._audit_use(token)
    await ll._audit_use(token)
    assert calls == 2, "failure backoff must prevent per-request audit storms"
    # The side-effect-free validator still requires a fresh current principal.
    async with sessions() as db:
        await stage_end_sessions(db, capture_account(user))
        await db.commit()
        with pytest.raises(HTTPException):
            await validate_captured(db, issued.capture)
        assert (await validate_captured(db, key)).api_key is not None


async def test_cancelled_password_hash_retains_real_thread_until_restore_drain(sessions, monkeypatch):
    import asyncio
    import threading

    started, release = threading.Event(), threading.Event()

    def blocked_hash(_):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test hash barrier was not released")
        return "hashed"

    monkeypatch.setattr(auth, "get_password_hash", blocked_hash)
    caller = asyncio.create_task(auth.hash_password_async("secret"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not await auth_runtime.quiesce_for_restore(0.01)
        assert auth_runtime._tasks
        release.set()
        assert await auth_runtime.quiesce_for_restore(2)
    finally:
        release.set()
        auth_runtime.open_admission()


async def test_caller_session_authority_database_outage_is_retryable_without_masking_proof_bugs(sessions, monkeypatch):
    from sqlalchemy.exc import OperationalError

    user, issued = await owner_and_session(sessions)
    async with sessions() as db:

        async def unavailable(*_, **__):
            raise OperationalError("auth-read", {}, RuntimeError("offline"))

        monkeypatch.setattr(db, "execute", unavailable)
        with pytest.raises(HTTPException) as failed:
            await validate_captured(db, issued.capture)
        assert failed.value.status_code == 503
        assert failed.value.headers["Retry-After"] == "5"
        with pytest.raises(HTTPException) as failed:
            await guard_accounts(db, [issued.capture])
        assert failed.value.status_code == 503


async def test_key_derived_camera_token_keeps_current_printer_scope(sessions):
    user, issued = await owner_and_session(sessions)
    captured, _ = await independent_credentials(sessions, user)
    async with sessions() as db:
        key = await db.scalar(select(APIKey).where(APIKey.connector_identity == captured.key_identity))
        key.can_read_status = True
        key.printer_ids = [7]
        await db.commit()
    camera = await auth.create_camera_stream_token(authority=captured)
    assert await auth.verify_camera_stream_token(camera, printer_id=7)
    with pytest.raises(HTTPException) as refused_scope:
        await auth.verify_camera_stream_token(camera, printer_id=8)
    assert refused_scope.value.status_code == 403
    async with sessions() as db:
        await stage_end_sessions(db, capture_account(user))
        await db.commit()
    # Browser session events do not revoke the separate integration principal
    # or its own scoped bootstrap. Current key/owner policy remains mandatory.
    assert await auth.verify_camera_stream_token(camera, printer_id=7)
    async with sessions() as db:
        key = await db.scalar(select(APIKey).where(APIKey.connector_identity == captured.key_identity))
        key.enabled = False
        await db.commit()
    assert not await auth.verify_camera_stream_token(camera, printer_id=7)


async def test_caller_session_validator_does_not_autoflush_pending_domain_changes(sessions):
    user, issued = await owner_and_session(sessions)
    async with sessions() as db:
        pending = APIKey(name="unpublished", key_prefix="draft", key_hash="hash", user_id=user.id)
        db.add(pending)
        assert (await validate_captured(db, issued.capture)).user.id == user.id
        assert pending.id is None
        assert pending in db.new
        await db.rollback()


async def test_cli_reset_uses_same_epoch_without_revoking_independent_credentials(sessions, monkeypatch, capsys):
    from unittest.mock import AsyncMock

    from backend.app import cli

    user, issued = await owner_and_session(sessions)
    key, token = await independent_credentials(sessions, user)
    monkeypatch.setattr(cli, "async_session", sessions)
    monkeypatch.setattr(cli, "init_db", AsyncMock())
    monkeypatch.setattr(cli, "_prompt_for_password", lambda: "New_CliPass1!")
    assert await cli._reset_password(user.username, False, False) == 0
    assert "New_CliPass1!" not in capsys.readouterr().out
    with pytest.raises(auth.JWTValidationFailure):
        await auth._validate_jwt_authority(issued.access_token)
    async with sessions() as db:
        owner = await db.get(User, user.id)
        assert owner.session_generation == 1
        assert auth.verify_password("New_CliPass1!", owner.password_hash)
        assert (await validate_captured(db, key)).api_key is not None
        assert (await validate_captured(db, token)).long_lived_token is not None


@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_totp_replay_counter_records_actual_accepted_window_utc(offset, monkeypatch):
    from datetime import datetime, timezone

    import pyotp

    from backend.app.api.routes import mfa
    from backend.app.models.user_totp import UserTOTP

    fixed = datetime(2026, 11, 12, 12, 0, 10, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(mfa, "datetime", Clock)
    totp = pyotp.TOTP(pyotp.random_base32())
    accepted = totp.timecode(fixed) + offset
    record = UserTOTP(user_id=1, is_enabled=True)
    code = totp.generate_otp(accepted)
    mfa._assert_totp_not_replayed(totp, record, code)
    assert record.last_totp_counter == accepted
    with pytest.raises(HTTPException) as repeated:
        mfa._assert_totp_not_replayed(totp, record, code)
    assert repeated.value.status_code == 400


async def test_audit_failed_commit_keeps_full_minute_reservation_and_releases_sql(sessions, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import OperationalError

    from backend.app.services import long_lived_tokens as ll

    user, issued = await owner_and_session(sessions)
    key, token = await independent_credentials(sessions, user)
    attempts = 0

    @asynccontextmanager
    async def failed_commit_factory():
        nonlocal attempts
        attempts += 1
        async with sessions() as db:

            async def unavailable():
                raise OperationalError("commit", {}, RuntimeError("offline"))

            monkeypatch.setattr(db, "commit", unavailable)
            yield db

    monkeypatch.setattr(auth, "async_session", failed_commit_factory)
    monkeypatch.setattr(ll, "async_session", failed_commit_factory)
    auth._key_audit_times.clear()
    ll._audit_after.clear()
    before = asyncio.get_running_loop().time()
    await auth._audit_key_use(key, "hash")
    await ll._audit_use(token)
    assert auth._key_audit_times[key.key_identity] >= before + 60
    assert ll._audit_after[token.token_identity] >= before + 60
    await auth._audit_key_use(key, "hash")
    await ll._audit_use(token)
    assert attempts == 2
    assert not auth_runtime._transactions


async def test_audit_attempt_lru_uses_monotonic_sixty_seconds_and_bounded_eviction():
    from collections import OrderedDict

    cache = OrderedDict()
    assert auth.reserve_audit_attempt(cache, "first", 10, limit=2)
    assert not auth.reserve_audit_attempt(cache, "first", 69.999, limit=2)
    assert cache["first"] == 70
    assert auth.reserve_audit_attempt(cache, "second", 20, limit=2)
    assert not auth.reserve_audit_attempt(cache, "first", 30, limit=2)
    assert auth.reserve_audit_attempt(cache, "third", 30, limit=2)
    assert list(cache) == ["first", "third"]
    assert auth.reserve_audit_attempt(cache, "first", 70, limit=2)
    assert cache["first"] == 130
    assert len(cache) == 2


async def test_final_restore_actor_requires_proven_drain_and_never_reopens_normal_auth(sessions):
    from backend.app.core.auth_lifetime import validate_restore_actor

    user, issued = await owner_and_session(sessions)
    try:
        auth_runtime.close_admission()
        async with sessions() as db:
            with pytest.raises(HTTPException) as not_drained:
                await validate_restore_actor(db, issued.capture)
            assert not_drained.value.status_code == 503
        assert await auth_runtime.quiesce_for_restore(1)
        async with sessions() as db:
            principal = await validate_restore_actor(db, issued.capture)
            assert principal.user.account_identity == user.account_identity
            assert not auth_runtime.admitting
            with pytest.raises(HTTPException) as normal_refused:
                await stage_session(db, VerifiedOutcome(capture_account(principal.user), "local"), remember_me=False)
            assert normal_refused.value.status_code == 503
            await db.rollback()
        assert not auth_runtime._transactions
    finally:
        auth_runtime.open_admission()


async def test_final_restore_actor_rejects_session_revoked_during_preflight(sessions):
    from backend.app.core.auth_lifetime import validate_restore_actor

    user, issued = await owner_and_session(sessions)
    async with sessions() as db:
        await stage_end_sessions(db, capture_account(user))
        await db.commit()
    try:
        assert await auth_runtime.quiesce_for_restore(1)
        async with sessions() as db:
            with pytest.raises(HTTPException) as revoked:
                await validate_restore_actor(db, issued.capture)
            assert revoked.value.status_code == 401
        assert not auth_runtime.admitting
    finally:
        auth_runtime.open_admission()


@pytest.mark.parametrize("family", ["key", "long_lived"])
@pytest.mark.parametrize("change", ["disable", "permissions"])
async def test_failed_audit_commit_rereads_full_authority_after_rollback(sessions, monkeypatch, family, change):
    import asyncio
    from contextlib import asynccontextmanager

    from sqlalchemy import update
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.sql.dml import Update

    from backend.app.services import long_lived_tokens as ll

    user, issued = await owner_and_session(sessions)
    key, token = await independent_credentials(sessions, user)
    rolled_back, release = asyncio.Event(), asyncio.Event()
    audit_attempts = 0
    target_table = "api_keys" if family == "key" else "long_lived_tokens"

    @asynccontextmanager
    async def audit_failure_factory():
        nonlocal audit_attempts
        db = sessions()
        original_execute, original_commit = db.execute, db.commit
        auditing = False

        async def execute(statement, *args, **kwargs):
            nonlocal auditing
            if isinstance(statement, Update) and statement.table.name == target_table:
                auditing = True
            return await original_execute(statement, *args, **kwargs)

        async def commit():
            nonlocal audit_attempts
            if auditing:
                audit_attempts += 1
                raise OperationalError("usage-commit", {}, RuntimeError("audit unavailable"))
            await original_commit()

        monkeypatch.setattr(db, "execute", execute)
        monkeypatch.setattr(db, "commit", commit)
        try:
            yield db
        finally:
            await db.close()  # the failed writer really rolled back/released
            if auditing:
                rolled_back.set()
                await release.wait()

    async def hash_match(_credential, candidates):
        return candidates[0] if candidates else None

    monkeypatch.setattr(auth, "async_session", audit_failure_factory)
    monkeypatch.setattr(ll, "async_session", audit_failure_factory)
    monkeypatch.setattr(auth, "_scan_api_key", hash_match)
    monkeypatch.setattr(ll, "_scan_api_key", hash_match)
    auth._key_audit_times.clear()
    ll._audit_after.clear()
    async with sessions() as caller_db:
        attempt = asyncio.create_task(
            auth.resolve_api_key_authority(None, "bd_known")
            if family == "key"
            else ll.verify_token(caller_db, "bblt_12345678_known")
        )
        try:
            await asyncio.wait_for(rolled_back.wait(), 2)
            assert not auth_runtime._transactions
            async with sessions() as mutation:
                if change == "permissions":
                    await mutation.execute(
                        update(User).where(User.account_identity == user.account_identity).values(role="user")
                    )
                elif family == "key":
                    await mutation.execute(
                        update(APIKey).where(APIKey.connector_identity == key.key_identity).values(enabled=False)
                    )
                else:
                    await mutation.execute(
                        update(LongLivedToken)
                        .where(LongLivedToken.token_identity == token.token_identity)
                        .values(revoked_at=utcnow())
                    )
                await mutation.commit()
            release.set()
            if family == "key" and change == "disable":
                with pytest.raises(auth.APIKeyValidationFailure):
                    await asyncio.wait_for(attempt, 2)
            elif family == "key":
                authority = await asyncio.wait_for(attempt, 2)
                assert not authority.owner_is_admin
                with pytest.raises(HTTPException) as no_grant:
                    auth._check_apikey_permissions(authority.key, ["camera:view"], owner=authority.owner)
                assert no_grant.value.status_code == 403
            else:
                assert await asyncio.wait_for(attempt, 2) is None
            assert audit_attempts == 1
        finally:
            release.set()
            if not attempt.done():
                await asyncio.wait_for(attempt, 2)


@pytest.mark.parametrize("consume", [False, True])
async def test_ephemeral_expiry_rechecked_after_authority_await(sessions, monkeypatch, consume):
    user, issued = await owner_and_session(sessions)
    token_type = auth.TokenType.SLICER_DOWNLOAD if consume else auth.TokenType.MEDIA
    token = await auth._mint_ephemeral(issued.capture, token_type, timedelta(seconds=10))
    now = utcnow()
    clock = [now]
    original = auth.validate_captured

    async def delayed_authority(*args, **kwargs):
        result = await original(*args, **kwargs)
        clock[0] = now + timedelta(seconds=20)
        return result

    monkeypatch.setattr(auth, "utcnow", lambda: clock[0])
    monkeypatch.setattr(auth, "validate_captured", delayed_authority)
    with pytest.raises(HTTPException) as expired:
        await auth.resolve_ephemeral_authority(token, token_type, consume=consume)
    assert expired.value.status_code == 401
    async with sessions() as db:
        assert await db.scalar(select(AuthEphemeralToken.id).where(AuthEphemeralToken.token == token)) is not None


async def test_capture_request_accepts_case_insensitive_bearer_scheme(sessions):
    from starlette.requests import Request

    _, issued = await owner_and_session(sessions)
    request = Request({"type": "http", "headers": [(b"authorization", b"bearer " + issued.access_token.encode())]})
    assert await auth.capture_request_authority(request) == issued.capture
