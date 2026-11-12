"""Immutable principals and short caller-owned credential transactions (G01 C)."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeout
from sqlalchemy.orm import selectinload

from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.api_key import APIKey
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.auth_session import AuthSession
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.user import User

CREDENTIAL_CONTRACT_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def refused():
    return HTTPException(401, "Invalid or revoked credential", headers={"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class AccountCapture:
    account_identity: str
    generation: int | None
    user_id: int


@dataclass(frozen=True)
class FrozenAuthority:
    kind: str
    account_identity: str
    user_id: int
    generation: int | None = None
    sid: str | None = None
    key_identity: str | None = None
    token_identity: str | None = None


@dataclass(frozen=True)
class GuardProof:
    session_id: int
    transaction: object
    identities: frozenset[str]


@dataclass(frozen=True)
class FreshPrincipal:
    user: User
    capture: FrozenAuthority
    api_key: APIKey | None = None
    long_lived_token: LongLivedToken | None = None
    auth_source: str | None = None


@dataclass(frozen=True)
class VerifiedOutcome:
    account: AccountCapture
    auth_source: str


@dataclass(frozen=True)
class IssuedSession:
    access_token: str
    raw_refresh: str | None
    sid: str
    expires_at: datetime
    capture: FrozenAuthority
    user: User
    refresh_max_age: int | None = None


def capture_account(user: User, *, independent: bool = False) -> AccountCapture:
    identity, generation = user.account_identity, user.session_generation
    if not identity or not isinstance(generation, int) or isinstance(generation, bool):
        raise refused()
    return AccountCapture(identity, None if independent else generation, user.id)


def capture_key(key: APIKey) -> FrozenAuthority:
    if not key.connector_identity or not key.owner_identity or key.user_id is None:
        raise refused()
    return FrozenAuthority("key", key.owner_identity, key.user_id, key_identity=key.connector_identity)


def capture_long_lived(record: LongLivedToken) -> FrozenAuthority:
    if not record.token_identity or not record.owner_identity:
        raise refused()
    return FrozenAuthority("long_lived", record.owner_identity, record.user_id, token_identity=record.token_identity)


def capture_ephemeral(row: AuthEphemeralToken, *, pending: bool = False) -> FrozenAuthority:
    if row.contract_version != CREDENTIAL_CONTRACT_VERSION or not row.account_identity:
        raise refused()
    # user_id is a lookup hint; the immutable identity resolves the actual row.
    kind = "key" if row.key_identity else "long_lived" if row.token_identity else "pending" if pending else "session"
    return FrozenAuthority(kind, row.account_identity, 0, row.generation, row.sid, row.key_identity, row.token_identity)


def binding(capture: FrozenAuthority | AccountCapture) -> dict:
    return {
        "contract_version": CREDENTIAL_CONTRACT_VERSION,
        "account_identity": capture.account_identity,
        "generation": capture.generation,
        "sid": getattr(capture, "sid", None),
        "key_identity": getattr(capture, "key_identity", None),
        "token_identity": getattr(capture, "token_identity", None),
    }


def binding_conditions(capture):
    """SQL conditions for the exact captured incarnation, including NULLs."""
    return [getattr(AuthEphemeralToken, field) == value for field, value in binding(capture).items()]


def _database_boundary(function):
    @wraps(function)
    async def guarded(db, *args, **kwargs):
        try:
            # A caller-session validator/guard never flushes pending domain
            # changes merely because its SELECT needs current authority.
            with db.no_autoflush:
                return await function(db, *args, **kwargs)
        except (OperationalError, InterfaceError, DisconnectionError, SQLAlchemyTimeout, TimeoutError, OSError) as exc:
            raise HTTPException(503, "Authentication temporarily unavailable", headers={"Retry-After": "5"}) from exc

    return guarded


@_database_boundary
async def guard_accounts(db, captures, *, require_active=True) -> GuardProof:
    auth_runtime.track_transaction(db)
    return await _guard_accounts(db, captures, require_active=require_active)


async def _guard_accounts(db, captures, *, require_active=True) -> GuardProof:
    # Reject conflicting captures for the same identity instead of silently
    # choosing a newer generation from one of two participants.
    by_identity = {}
    for capture in captures:
        if not capture.account_identity:
            raise refused()
        old = by_identity.get(capture.account_identity)
        if old is not None and old.generation != capture.generation:
            if old.generation is not None and capture.generation is not None:
                raise refused()
            capture = old if old.generation is not None else capture
        by_identity[capture.account_identity] = capture
    for identity, capture in sorted(by_identity.items()):
        # Numeric hint is optional on derivatives. Reserve SQLite's writer via
        # an identity predicate before any authoritative read, even without it.
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(
                update(User)
                .where(User.account_identity == identity)
                .values(session_generation=User.session_generation, updated_at=User.updated_at)
            )
        user = (
            await db.execute(
                select(User)
                .where(User.account_identity == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None or (require_active and not user.is_active) or (capture.user_id and user.id != capture.user_id):
            raise refused()
        if capture.generation is not None and user.session_generation != capture.generation:
            raise refused()
    return GuardProof(id(db), db.sync_session.get_transaction(), frozenset(by_identity))


def check_proof(db, capture, proof: GuardProof):
    if (
        proof.session_id != id(db)
        or proof.transaction is not db.sync_session.get_transaction()
        or capture.account_identity not in proof.identities
        or not proof.transaction.is_active
    ):
        raise RuntimeError("Authentication lifetime guard is not held by this transaction")


@_database_boundary
async def validate_captured(db, capture: FrozenAuthority, *, required_permissions=(), printer_id=None, proof=None):
    if proof is not None:
        check_proof(db, capture, proof)
    else:
        auth_runtime.ensure_admitting()
    if not capture.account_identity:
        raise refused()
    user = (
        await db.execute(
            select(User)
            .where(User.account_identity == capture.account_identity)
            .options(selectinload(User.groups))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or (capture.user_id and capture.user_id != user.id):
        raise refused()
    key = record = None
    source = None
    now = utcnow()
    if capture.kind in {"session", "pending"}:
        if capture.generation is None or capture.generation != user.session_generation:
            raise refused()
        if capture.kind == "session":
            if not capture.sid:
                raise refused()
            session = (
                await db.execute(
                    select(AuthSession).where(AuthSession.sid == capture.sid).execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                session is None
                or session.account_identity != user.account_identity
                or session.generation != user.session_generation
                or session.revoked_at is not None
                or utc_naive(session.expires_at) <= now
            ):
                raise refused()
            source = session.auth_source
    elif capture.kind == "key":
        key = (
            await db.execute(
                select(APIKey)
                .where(APIKey.connector_identity == capture.key_identity)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            key is None
            or key.contract_version != CREDENTIAL_CONTRACT_VERSION
            or key.retired_at is not None
            or key.retired_reason is not None
            or not key.enabled
            or key.owner_identity != user.account_identity
            or key.user_id != user.id
            or (key.expires_at is not None and utc_naive(key.expires_at) <= now)
        ):
            raise refused()
        from backend.app.core.auth import _check_apikey_permissions, check_printer_access

        if required_permissions:
            _check_apikey_permissions(key, list(required_permissions), owner=user)
        if printer_id is not None:
            check_printer_access(key, printer_id)
    elif capture.kind == "long_lived":
        record = (
            await db.execute(
                select(LongLivedToken)
                .where(LongLivedToken.token_identity == capture.token_identity)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            record is None
            or record.contract_version != CREDENTIAL_CONTRACT_VERSION
            or record.retired_at is not None
            or record.retired_reason is not None
            or record.revoked_at is not None
            or record.owner_identity != user.account_identity
            or record.user_id != user.id
            or utc_naive(record.expires_at) <= now
        ):
            raise refused()
    else:
        raise refused()
    if key is None:
        for permission in required_permissions:
            if not user.has_permission(getattr(permission, "value", permission)):
                raise HTTPException(403, "Required permission is no longer granted")
    actual = FrozenAuthority(
        capture.kind,
        user.account_identity,
        user.id,
        capture.generation,
        capture.sid,
        capture.key_identity,
        capture.token_identity,
    )
    return FreshPrincipal(user, actual, key, record, source)


@_database_boundary
async def validate_restore_actor(db, capture: FrozenAuthority) -> FreshPrincipal:
    """Final restore-only authority read after drain, before any storage moves.

    The exclusive restore owner owns this SQL transaction and must release it
    before file I/O. Normal issuers/validators never use this closed-admission
    boundary. No raw secret resolution, audit, or global reopen occurs here.
    """
    auth_runtime.ensure_restore_drained()
    if capture.kind != "session":
        raise HTTPException(403, "A signed-in administrator is required")
    proof = await _guard_accounts(db, [capture])
    principal = await validate_captured(db, capture, required_permissions=("settings:restore",), proof=proof)
    if not principal.user.is_admin:
        raise HTTPException(403, "Administrator required")
    return principal


async def stage_session(db, verified: VerifiedOutcome, *, remember_me: bool, existing_sid=None, issue_refresh=True):
    from backend.app.core.auth import (
        ACCESS_TOKEN_EXPIRE_MINUTES,
        create_access_token,
        hash_client_secret,
        refresh_token_ttl,
        resolve_session_max_hours,
    )

    if verified.auth_source not in {"local", "ldap", "oidc"}:
        raise refused()
    proof = await guard_accounts(db, [verified.account])
    pending = FrozenAuthority(
        "pending", verified.account.account_identity, verified.account.user_id, verified.account.generation
    )
    principal = await validate_captured(db, pending, proof=proof)
    now = utcnow()
    expires = now + (
        refresh_token_ttl(remember_me, await resolve_session_max_hours(db))
        if issue_refresh
        else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    sid = existing_sid or secrets.token_hex(16)
    session = await db.get(AuthSession, sid, populate_existing=True)
    if existing_sid:
        if (
            session is None
            or session.revoked_at is not None
            or session.account_identity != pending.account_identity
            or session.generation != pending.generation
            or utc_naive(session.expires_at) <= now
        ):
            raise refused()
        # A refresh cannot change how this browser was originally authenticated.
        source = session.auth_source
        session.expires_at = expires
    else:
        source = verified.auth_source
        session = AuthSession(
            sid=sid,
            user_id=principal.user.id,
            account_identity=pending.account_identity,
            generation=pending.generation,
            auth_source=source,
            remember_me=remember_me,
            created_at=now,
            expires_at=expires,
        )
        db.add(session)
    capture = FrozenAuthority("session", pending.account_identity, principal.user.id, pending.generation, sid)
    raw = None
    if issue_refresh:
        raw = secrets.token_urlsafe(48)
        db.add(
            AuthEphemeralToken(
                token=hash_client_secret(raw),
                token_type=TokenType.REFRESH,
                username=principal.user.username,
                family_id=sid,
                remember_me=remember_me,
                auth_source=source,
                created_at=now,
                expires_at=expires,
                **binding(capture),
            )
        )
    token = create_access_token(
        {"sub": principal.user.username, "uid": capture.account_identity, "gen": capture.generation, "sid": sid}
    )
    await db.flush()
    principal.user._auth_capture = capture
    principal.user._auth_source = source
    return IssuedSession(
        token,
        raw,
        sid,
        expires,
        capture,
        principal.user,
        int((expires - now).total_seconds()) if issue_refresh else None,
    )


async def stage_revoke_session(db, capture: FrozenAuthority, reason="logout"):
    proof = await guard_accounts(db, [capture])
    check_proof(db, capture, proof)
    await db.execute(
        update(AuthSession)
        .where(AuthSession.sid == capture.sid, AuthSession.account_identity == capture.account_identity)
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    await db.execute(delete(AuthEphemeralToken).where(AuthEphemeralToken.sid == capture.sid))


async def stage_end_sessions(db, account: AccountCapture, reason="sessions_ended"):
    from backend.app.models.user_otp_code import UserOTPCode
    from backend.app.models.user_totp import UserTOTP

    await guard_accounts(db, [account], require_active=False)
    await db.execute(
        update(User)
        .where(User.account_identity == account.account_identity)
        .values(session_generation=User.session_generation + 1)
    )
    await db.execute(
        update(AuthSession)
        .where(AuthSession.account_identity == account.account_identity, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    await db.execute(
        delete(AuthEphemeralToken).where(
            AuthEphemeralToken.account_identity == account.account_identity,
            AuthEphemeralToken.key_identity.is_(None),
            AuthEphemeralToken.token_identity.is_(None),
        )
    )
    await db.execute(delete(UserOTPCode).where(UserOTPCode.account_identity == account.account_identity))
    await db.execute(delete(UserTOTP).where(UserTOTP.user_id == account.user_id, UserTOTP.is_enabled.is_(False)))


async def stage_restore_invalidation(db):
    """Restore orchestration owns the closed admission and caller transaction."""
    from backend.app.models.user_otp_code import UserOTPCode
    from backend.app.models.user_totp import UserTOTP

    await db.execute(update(User).values(session_generation=User.session_generation + 1))
    await db.execute(
        update(AuthSession)
        .where(AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason="restore")
    )
    await db.execute(delete(AuthEphemeralToken))
    await db.execute(delete(UserOTPCode))
    await db.execute(delete(UserTOTP).where(UserTOTP.is_enabled.is_(False)))
