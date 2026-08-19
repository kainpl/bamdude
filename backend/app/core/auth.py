from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWTError as JWTError
from passlib.context import CryptContext
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.database import async_session, get_db
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.group import Group, user_groups
from backend.app.models.settings import Settings
from backend.app.models.user import User

logger = logging.getLogger(__name__)

# Password hashing
# Use pbkdf2_sha256 instead of bcrypt to avoid 72-byte limit and passlib initialization issues
# pbkdf2_sha256 is a secure password hashing algorithm without bcrypt's limitations
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


_JWT_SECRET_MIN_LEN = 32
"""Minimum length (characters) for an HS256 signing secret.

HS256 is HMAC-SHA256 — RFC 7518 §3.2 mandates a key at least as long as
the hash output (256 bits = 32 bytes). Below that the JWT signature
collapses to brute-force territory, which is what PYSEC-2025-183 /
CVE-2025-45768 flags pyjwt for (the CVE is disputed and the supplier
correctly places the responsibility on the application — that's here).

Applied to both code paths:

- ``JWT_SECRET_KEY`` env var: rejected at startup if shorter (hard fail
  with an actionable error so a self-hosted operator can fix it and
  restart).
- ``.jwt_secret`` file: rejected on read (already enforced); the
  generator uses ``secrets.token_urlsafe(64)`` which produces ~86 chars
  of base64url, well above the floor.
"""


def _get_jwt_secret() -> str:
    """Get the JWT secret key from environment, file, or generate a new one.

    Priority:
    1. JWT_SECRET_KEY environment variable
    2. .jwt_secret file in data directory
    3. Generate new random secret and save to file

    Returns:
        The JWT secret key
    """
    # 1. Check environment variable first
    env_secret = os.environ.get("JWT_SECRET_KEY")
    if env_secret:
        if len(env_secret) < _JWT_SECRET_MIN_LEN:
            raise RuntimeError(
                f"JWT_SECRET_KEY is too short ({len(env_secret)} chars; minimum {_JWT_SECRET_MIN_LEN}). "
                "HS256 requires a 256-bit key (RFC 7518 §3.2 / CVE-2025-45768). "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        logger.info("Using JWT secret from JWT_SECRET_KEY environment variable")
        return env_secret

    # 2. Check for secret file in data directory
    # Shared resolver in ``paths.py`` so DATA_DIR fallback stays in lockstep
    # with ``encryption.py`` (.mfa_encryption_key sibling file).
    from backend.app.core.paths import resolve_data_dir

    data_dir = resolve_data_dir()
    secret_file = data_dir / ".jwt_secret"

    if secret_file.exists():
        try:
            secret = secret_file.read_text().strip()
            if secret and len(secret) >= _JWT_SECRET_MIN_LEN:
                logger.info("Using JWT secret from %s", secret_file)
                return secret
        except OSError as e:
            logger.warning("Failed to read JWT secret file: %s", e)

    # 3. Generate new random secret
    new_secret = secrets.token_urlsafe(64)

    # Try to save it
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # Note: CodeQL flags this as "clear-text storage of sensitive information" but this is
        # intentional and secure - JWT secrets must be readable by the app, we set 0600 permissions,
        # and this is standard practice for self-hosted applications (same as .env files).
        secret_file.write_text(new_secret)  # nosec B105
        # Restrict permissions (owner read/write only)
        secret_file.chmod(0o600)
        logger.info("Generated new JWT secret and saved to %s", secret_file)
    except OSError as e:
        logger.warning(
            "Could not save JWT secret to file (%s). "
            "Secret will be regenerated on restart, invalidating existing tokens. "
            "Set JWT_SECRET_KEY environment variable for persistence.",
            e,
        )

    return new_secret


# JWT settings
SECRET_KEY = _get_jwt_secret()
ALGORITHM = "HS256"

# Access token TTL — short by design. Sliding-session refresh tokens cover the
# "stay logged in" UX so reducing the access-token exposure window is free.
# Previously 60 * 24 (24h, §18.4 M-2); dropped to 60 min once /auth/refresh
# landed (§18.14 sliding session) because a leaked access token now expires
# within an hour instead of a day.
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# Refresh token TTL — picks between two values based on the login-time
# `remember_me` flag. Without remember-me the refresh is a session cookie
# (no cookie Max-Age → dies when the browser closes), capped on the DB side
# to 12 h so an overnight closed-but-resumed session still needs re-login.
# With remember-me, 30 days matches OWASP recommended refresh TTL and both
# the cookie Max-Age and DB exp stretch to that.
REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER = 30
# A day rather than half of one. Without "remember me" the session is meant to
# end when the user stops using BamDude — but 12 h lands mid-shift for anyone
# who signs in at the start of a working day, which reads as a random logout
# rather than as the choice they made at the login form.
REFRESH_TOKEN_EXPIRE_HOURS_SESSION = 24

# How long after a refresh token is consumed a second presentation is still
# treated as one client racing itself rather than as a replay. Short on purpose:
# the collision it forgives happens within milliseconds (tabs sharing a token
# reach the same proactive-refresh deadline together), so seconds are already
# generous, and every second widens the window in which a stolen cookie escapes
# family revocation.
REFRESH_REUSE_GRACE_SECONDS = 10
REFRESH_TOKEN_COOKIE_NAME = "bamdude_refresh"
# The refresh cookie is only ever sent on these paths — narrows the CSRF
# surface and keeps unrelated routes from seeing the cookie in their logs.
REFRESH_TOKEN_COOKIE_PATH = "/api/v1/auth"

# Admin-configurable ceiling on effective session lifetime (#1706, adapted).
# Upstream clamps the 24 h ACCESS token; our access token is deliberately short
# (1 h) and auto-refreshes, so the real session lifetime is the REFRESH token
# TTL. The ceiling therefore clamps the refresh TTL (+ its cookie Max-Age) at
# login and rotation. 720 h (30 d) matches remember-me + the Pydantic le=720
# bound; default 720 preserves existing remember-me sessions on upgrade.
SESSION_MAX_HOURS_HARD_CEILING = 720
SESSION_MAX_HOURS_DEFAULT = 720


async def resolve_session_max_hours(db: AsyncSession) -> int:
    """Resolve the admin-set session-lifetime ceiling in hours (#1706).

    Reads ``session_max_hours`` from the settings table, clamps to
    [1, HARD_CEILING], and falls back to DEFAULT on a missing/blank/
    unparseable/<1 value. DB errors are NOT caught — a broken DB must abort
    login rather than silently change the session lifetime.
    """
    result = await db.execute(select(Settings).where(Settings.key == "session_max_hours"))
    row = result.scalar_one_or_none()
    if row is None or not row.value:
        return SESSION_MAX_HOURS_DEFAULT
    try:
        hours = int(row.value)
    except (TypeError, ValueError):
        return SESSION_MAX_HOURS_DEFAULT
    if hours < 1:
        return SESSION_MAX_HOURS_DEFAULT
    return min(hours, SESSION_MAX_HOURS_HARD_CEILING)


# HTTP Bearer token
security = HTTPBearer(auto_error=False)

# --- Slicer download tokens ---
# Short-lived, single-use tokens for slicer protocol handlers that can't send
# auth headers. Stored in AuthEphemeralToken (token_type=SLICER_DOWNLOAD) so
# they survive server restarts and work in multi-worker deployments (§18.4 M-3).
SLICER_TOKEN_EXPIRE_MINUTES = 5


async def create_slicer_download_token(resource_type: str, resource_id: int) -> str:
    """Create a short-lived, single-use download token for slicer protocol handlers."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=SLICER_TOKEN_EXPIRE_MINUTES)
    token = secrets.token_urlsafe(24)
    resource_key = f"{resource_type}:{resource_id}"
    async with async_session() as db:
        # Prune expired tokens opportunistically.
        await db.execute(
            delete(AuthEphemeralToken).where(
                AuthEphemeralToken.token_type == TokenType.SLICER_DOWNLOAD,
                AuthEphemeralToken.expires_at < now,
            )
        )
        db.add(
            AuthEphemeralToken(
                token=token,
                token_type=TokenType.SLICER_DOWNLOAD,
                nonce=resource_key,
                expires_at=expires_at,
            )
        )
        await db.commit()
    return token


async def verify_slicer_download_token(token: str, resource_type: str, resource_id: int) -> bool:
    """Verify and atomically consume a slicer download token.

    DELETE…RETURNING ensures the token is single-use even under concurrent
    requests. M-NEW-1 fix: ``nonce`` (resource key) is in the WHERE clause so
    the DELETE only succeeds for the correct resource — earlier versions
    consumed the row even on resource-mismatch, permanently invalidating it.
    """
    expected_key = f"{resource_type}:{resource_id}"
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(
            delete(AuthEphemeralToken)
            .where(
                AuthEphemeralToken.token == token,
                AuthEphemeralToken.token_type == TokenType.SLICER_DOWNLOAD,
                AuthEphemeralToken.nonce == expected_key,
                AuthEphemeralToken.expires_at > now,
            )
            .returning(AuthEphemeralToken.id)
        )
        if result.one_or_none() is None:
            return False
        await db.commit()
        return True


# --- Camera stream tokens ---
# Reusable (not single-use) tokens for MJPEG stream / snapshot endpoints that
# are loaded by <img>/<video> tags — those can't send Authorization headers,
# so the frontend obtains a token and appends ?token=... to the URL. Stored
# in AuthEphemeralToken (token_type=CAMERA_STREAM) for multi-worker safety
# and restart persistence (§18.4 M-3).
CAMERA_STREAM_TOKEN_EXPIRE_MINUTES = 60


async def create_camera_stream_token() -> str:
    """Create a reusable camera-stream token (valid for CAMERA_STREAM_TOKEN_EXPIRE_MINUTES)."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=CAMERA_STREAM_TOKEN_EXPIRE_MINUTES)
    token = secrets.token_urlsafe(24)
    async with async_session() as db:
        # Prune expired tokens opportunistically.
        await db.execute(
            delete(AuthEphemeralToken).where(
                AuthEphemeralToken.token_type == TokenType.CAMERA_STREAM,
                AuthEphemeralToken.expires_at < now,
            )
        )
        db.add(
            AuthEphemeralToken(
                token=token,
                token_type=TokenType.CAMERA_STREAM,
                expires_at=expires_at,
            )
        )
        await db.commit()
    return token


async def verify_camera_stream_token(token: str) -> bool:
    """Verify a camera stream token is valid (reusable — does not consume it).

    Tries the ephemeral 60-minute token first (the common, browser-bound case)
    and falls through to long-lived tokens (#1108) for HA / kiosk integrations
    that paste a token once and expect it to keep working for days.
    """
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(
            select(AuthEphemeralToken).where(
                AuthEphemeralToken.token == token,
                AuthEphemeralToken.token_type == TokenType.CAMERA_STREAM,
                AuthEphemeralToken.expires_at > now,
            )
        )
        if result.scalar_one_or_none() is not None:
            return True

        # Long-lived path. Imported lazily so the auth module stays importable
        # at startup before the long_lived_tokens model is registered.
        from backend.app.services.long_lived_tokens import STREAM_SCOPES, verify_token as verify_long_lived

        # An overlay token has to be able to pull the video its own view shows,
        # so the stream gate honours every scope in STREAM_SCOPES (upstream
        # #2613). It is still an explicit list — never "any scope".
        record = await verify_long_lived(db, token, scope=STREAM_SCOPES)
        return record is not None


# --- WebSocket connection tokens ---
# Short-lived, reusable tokens gating ``/api/v1/ws``. The Starlette
# ``@app.middleware("http")`` auth gate only sees the "http" scope, so it never
# intercepts the WebSocket upgrade — the endpoint was previously open to any
# client that could reach the HTTP port, fanning every printer/archive/inventory
# broadcast out to it (upstream Bambuddy GHSA-r2qv follow-up). Browsers can't set
# Authorization headers on a WebSocket, so the SPA mints a token via
# ``POST /auth/ws-token`` (behind ``Permission.WEBSOCKET_CONNECT``) and passes it
# as ``?token=`` — the same pattern as camera-stream tokens.
WEBSOCKET_TOKEN_EXPIRE_MINUTES = 60


async def create_websocket_token(username: str | None = None) -> str:
    """Create a reusable WebSocket connection token (60-minute window).

    ``username`` records which user the token was minted for so the connection
    can be tagged for per-user broadcasts (BamDude has no anonymous users). It is
    None only for API-key callers, whose connections fall back to global broadcast.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=WEBSOCKET_TOKEN_EXPIRE_MINUTES)
    token = secrets.token_urlsafe(24)
    async with async_session() as db:
        # Prune expired tokens opportunistically.
        await db.execute(
            delete(AuthEphemeralToken).where(
                AuthEphemeralToken.token_type == TokenType.WEBSOCKET,
                AuthEphemeralToken.expires_at < now,
            )
        )
        db.add(
            AuthEphemeralToken(
                token=token,
                token_type=TokenType.WEBSOCKET,
                username=username,
                expires_at=expires_at,
            )
        )
        await db.commit()
    return token


async def verify_websocket_token(token: str) -> bool:
    """Verify a WebSocket connection token is valid (reusable — not consumed)."""
    if not token:
        return False
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(
            select(AuthEphemeralToken).where(
                AuthEphemeralToken.token == token,
                AuthEphemeralToken.token_type == TokenType.WEBSOCKET,
                AuthEphemeralToken.expires_at > now,
            )
        )
        return result.scalar_one_or_none() is not None


async def resolve_websocket_token_user(token: str) -> int | None:
    """Return the user id a valid WS token was minted for, for per-user broadcast
    tagging. None if the token is invalid/expired or was minted without a user
    (API-key caller) — the caller must still gate on ``verify_websocket_token``."""
    if not token:
        return None
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        username = (
            await db.execute(
                select(AuthEphemeralToken.username).where(
                    AuthEphemeralToken.token == token,
                    AuthEphemeralToken.token_type == TokenType.WEBSOCKET,
                    AuthEphemeralToken.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if not username:
            return None
        return (await db.execute(select(User.id).where(User.username == username))).scalar_one_or_none()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash.

    Uses pbkdf2_sha256 which handles long passwords automatically.
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password.

    Uses pbkdf2_sha256 which is secure and has no password length limit.
    """
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token with ``jti`` (revocation) and ``iat`` (freshness) claims."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    jti = secrets.token_hex(16)
    to_encode.update({"exp": expire, "jti": jti, "iat": now})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# ---------------------------------------------------------------------------
# Refresh tokens (§18.14 sliding session)
# ---------------------------------------------------------------------------


def _hash_refresh_token(raw: str) -> str:
    """SHA-256 hex of the raw cookie value.

    Raw refresh tokens never touch the DB — only their hash. Stolen DB → rows
    can't be replayed against /auth/refresh because the raw value was only
    ever in the client's cookie. Uses hashlib (stdlib) intentionally — the
    existing MFA module uses the same primitive for TOTP shared-secret
    fingerprints, so dev dependencies stay flat.
    """
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_token_ttl(remember_me: bool, ceiling_hours: int = SESSION_MAX_HOURS_HARD_CEILING) -> timedelta:
    """Absolute DB-side TTL for a refresh token.

    Without ``remember_me`` the cookie is a session cookie (closes with the
    browser), but a session-cookie alone doesn't stop a server-side replay
    if the user just locks the screen and leaves the tab open — hence the
    separate 12 h DB-cap that kicks in even if the browser stays open.

    ``ceiling_hours`` clamps the resulting TTL to the admin-configured session
    ceiling (#1706). Defaults to the hard ceiling so callers that don't pass it
    keep the pre-#1706 behaviour.
    """
    base = (
        timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER)
        if remember_me
        else timedelta(hours=REFRESH_TOKEN_EXPIRE_HOURS_SESSION)
    )
    return min(base, timedelta(hours=ceiling_hours))


async def create_refresh_token(
    db,
    *,
    username: str,
    remember_me: bool,
    family_id: str | None = None,
    ceiling_hours: int | None = None,
) -> tuple[str, str, datetime]:
    """Mint a refresh token and persist its hash.

    Returns ``(raw_token, family_id, expires_at)``. Caller is responsible
    for committing the session and setting the cookie on the response.

    ``family_id`` links every rotation descended from one /login. Pass the
    existing id when rotating (so reuse detection can see the lineage);
    leave it None for fresh logins so a new family is created.

    ``ceiling_hours`` is the admin-configured session ceiling (#1706); when
    None it is resolved from settings here so every call path is clamped even
    if the caller forgets to thread it.
    """
    from backend.app.models.auth_ephemeral import AuthEphemeralToken

    if ceiling_hours is None:
        ceiling_hours = await resolve_session_max_hours(db)
    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_refresh_token(raw_token)
    if family_id is None:
        family_id = secrets.token_hex(16)
    expires_at = datetime.now(timezone.utc) + refresh_token_ttl(remember_me, ceiling_hours)

    row = AuthEphemeralToken.new_refresh(
        token_hash=token_hash,
        username=username,
        family_id=family_id,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return raw_token, family_id, expires_at


def _within_reuse_grace(used_at: datetime, now: datetime) -> bool:
    """True when ``used_at`` is recent enough to be a self-race, not a replay.

    Naive timestamps are read as UTC: the column is written from
    ``datetime.now(timezone.utc)`` but SQLite hands it back without a tzinfo,
    and comparing naive to aware raises. Getting this wrong would not fail
    loudly — it would throw inside the refresh path and log everyone out, which
    is the very symptom being fixed.
    """
    if used_at.tzinfo is None:
        used_at = used_at.replace(tzinfo=timezone.utc)
    return (now - used_at).total_seconds() <= REFRESH_REUSE_GRACE_SECONDS


async def verify_and_consume_refresh_token(
    db,
    raw_token: str,
) -> tuple[str | None, str | None, str]:
    """Validate + mark-used in one atomic step.

    Returns ``(username, family_id, status)`` where ``status`` is:

    - ``"ok"`` — token valid + rotated. ``username`` + ``family_id`` populated;
      caller issues a new access + a new refresh inside the same family.
    - ``"race"`` — consumed moments ago, inside ``REFRESH_REUSE_GRACE_SECONDS``.
      Almost certainly one client racing itself. **No side effect**: the family
      survives and the caller returns a plain 401 so the loser can pick up the
      token the winner just stored.
    - ``"reuse"`` — consumed longer ago than the grace window. Treated as a
      replay: whole family revoked; ``username`` + ``family_id`` populated so
      the caller can log + return a descriptive 401.
    - ``"invalid"`` — token not found or expired. ``username`` / ``family_id``
      are None. Returned as 401 by the caller without a side effect.

    The ``ok`` case flips ``used_at`` via an UPDATE … WHERE used_at IS NULL so
    two concurrent /auth/refresh hits on the same token can't both get ``ok``.

    **Why the grace window exists.** This function used to call every loser a
    replay, and said so in as many words: "that second request IS a replay,
    even if it's the same legit client racing itself". Technically true, and it
    logged real users out constantly, because the frontend races itself *on a
    schedule*: the proactive refresh timer is computed from the token's own
    ``exp``, so every open tab holding the same token reaches the same absolute
    deadline within milliseconds. Two tabs meant a revoked session roughly once
    an hour, and the user experienced it as "the token randomly expires".

    The window is the honest way to split the two cases. Inside it we cannot
    distinguish a self-race from a thief replaying seconds after the victim —
    so we choose the error to make. Outside it, nothing changes: a leaked token
    surfacing later still collapses the family, which is the case worth
    protecting against, and the one an attacker actually gets.

    Note the loser is still refused. The grace does not hand out a session; it
    only declines to punish the whole family for a collision the client caused.
    """
    from sqlalchemy import select, update

    from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType

    token_hash = _hash_refresh_token(raw_token)
    row = (
        await db.execute(
            select(AuthEphemeralToken)
            .where(AuthEphemeralToken.token == token_hash)
            .where(AuthEphemeralToken.token_type == TokenType.REFRESH)
        )
    ).scalar_one_or_none()

    if row is None:
        return None, None, "invalid"

    # Expiry check first — expired rows are just stale, not hostile.
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        return None, None, "invalid"

    # Reuse detection: if used_at already set, this is either a client racing
    # itself (moments ago) or a replay (any later). See the docstring.
    if row.used_at is not None:
        if _within_reuse_grace(row.used_at, now):
            return row.username, row.family_id, "race"
        if row.family_id:
            await revoke_refresh_family(db, row.family_id)
        return row.username, row.family_id, "reuse"

    # Race-proof consume: only one concurrent request flips used_at from
    # NULL → now. The loser will re-select the row and hit the reuse path.
    result = await db.execute(
        update(AuthEphemeralToken)
        .where(AuthEphemeralToken.id == row.id)
        .where(AuthEphemeralToken.used_at.is_(None))
        .values(used_at=now)
    )
    if result.rowcount == 0:
        # Lost the race by microseconds — the other request consumed it between
        # our SELECT and our UPDATE. By definition inside the grace window, so
        # never a replay: this branch is only reachable when the winner is still
        # in flight.
        return row.username, row.family_id, "race"

    return row.username, row.family_id, "ok"


async def revoke_refresh_family(db, family_id: str) -> None:
    """Delete every refresh-token row for a family_id.

    Called on: (1) detected reuse — all siblings of the replayed token die;
    (2) explicit logout — the current family is cleaned up; (3) chaining
    from ``revoke_all_refresh_tokens_for_user`` below.
    """
    from sqlalchemy import delete

    from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType

    await db.execute(
        delete(AuthEphemeralToken)
        .where(AuthEphemeralToken.token_type == TokenType.REFRESH)
        .where(AuthEphemeralToken.family_id == family_id)
    )


def refresh_cookie_secure_flag(request) -> bool:
    """Resolve the ``Secure`` flag for the refresh-token cookie.

    Auto-detect by default so the same binary runs seamlessly on plain-HTTP
    LAN installs and HTTPS prod deployments. The operator can force either
    polarity via ``AUTH_REFRESH_COOKIE_SECURE`` (hard override).

    Auto rules:

    - ``request.url.scheme == 'https'`` → Secure=True.
    - When behind a reverse proxy listed in ``TRUSTED_PROXY_IPS`` (existing
      §18.5 env var), honour ``X-Forwarded-Proto`` so Caddy / nginx /
      Traefik terminating TLS upstream of BamDude still produces Secure
      cookies.
    - Anything else → Secure=False. The cookie still gets set on plain
      HTTP, but browsers won't upgrade it to HTTPS-only; acceptable for
      LAN deployments where HTTPS isn't on the table.
    """
    from backend.app.core.config import settings

    if settings.auth_refresh_cookie_secure is not None:
        return settings.auth_refresh_cookie_secure

    scheme = request.url.scheme
    client_host = request.client.host if request.client else None
    if client_host:
        trusted = frozenset(ip.strip() for ip in os.environ.get("TRUSTED_PROXY_IPS", "").split(",") if ip.strip())
        if client_host in trusted:
            xfp = request.headers.get("X-Forwarded-Proto", "").lower()
            if xfp:
                scheme = xfp.split(",")[0].strip()
    return scheme == "https"


async def revoke_all_refresh_tokens_for_user(db, username: str) -> None:
    """Hard-revoke every active refresh token for a user.

    Called on password change + admin-initiated session kill. All the user's
    devices are forced through /auth/refresh once, which 401s, which drops
    them to /login. Access tokens issued before this call still die naturally
    via the ``iat`` freshness check against ``password_changed_at``.
    """
    from sqlalchemy import delete

    from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType

    await db.execute(
        delete(AuthEphemeralToken)
        .where(AuthEphemeralToken.token_type == TokenType.REFRESH)
        .where(AuthEphemeralToken.username == username)
    )


def _is_token_fresh(iat: int | float | None, user: User) -> bool:
    """Return False if the token was issued before the user's last password change.

    Used to invalidate all sessions after a password reset/change (§18.4 M-R7-B / I2).
    Tokens without an ``iat`` claim are rejected unconditionally — every token
    issued by this server carries ``iat`` since §18.4 landed, and any pre-§18.4
    token whose max TTL (24 h) has since expired would already be rejected by
    the ``exp`` check. Legacy tokens without ``iat`` but still valid by ``exp``
    would be the only loss here, and that window closes automatically as they
    time out.
    """
    if iat is None:
        return False
    if not hasattr(user, "password_changed_at") or user.password_changed_at is None:
        # No password change recorded — treat as "no freshness floor", pre-m012 rows.
        return True
    token_issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
    pca = user.password_changed_at
    if pca.tzinfo is None:
        pca = pca.replace(tzinfo=timezone.utc)
    # JWT iat is whole seconds; truncate pca so tokens issued in the same second pass.
    pca = pca.replace(microsecond=0)
    return token_issued_at >= pca


async def revoke_jti(jti: str, expires_at: datetime, username: str | None = None) -> None:
    """Store a revoked JWT ``jti`` so it is rejected on future requests.

    Silently ignores duplicate inserts (e.g. double-logout replaying the same token).
    """
    async with async_session() as db:
        revoked = AuthEphemeralToken(
            token=jti,
            token_type=TokenType.REVOKED_JTI,
            username=username,
            expires_at=expires_at,
        )
        db.add(revoked)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # jti already revoked — desired state, ignore


async def is_jti_revoked(jti: str, db: AsyncSession | None = None) -> bool:
    """Return True if the given ``jti`` has been revoked.

    Pass ``db`` to reuse the caller's session instead of opening a new one
    (issue #2572): callers that already hold a session (e.g. ``/auth/me``)
    otherwise checked out a second pooled connection per request, doubling pool
    pressure — a login burst then exhausted the pool. With ``db`` omitted a
    short session is opened as before, for callers that check the jti before
    they have a session open.
    """

    async def _query(session: AsyncSession) -> bool:
        result = await session.execute(
            select(AuthEphemeralToken).where(
                AuthEphemeralToken.token == jti,
                AuthEphemeralToken.token_type == TokenType.REVOKED_JTI,
            )
        )
        return result.scalar_one_or_none() is not None

    if db is not None:
        return await _query(db)
    async with async_session() as own_db:
        return await _query(own_db)


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Get a user by username (case-insensitive) with groups loaded for permission checks."""
    result = await db.execute(
        select(User).where(func.lower(User.username) == func.lower(username)).options(selectinload(User.groups))
    )
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Get a user by email (case-insensitive) with groups loaded for permission checks."""
    result = await db.execute(
        select(User).where(func.lower(User.email) == func.lower(email)).options(selectinload(User.groups))
    )
    return result.scalar_one_or_none()


async def authenticate_user(db: AsyncSession, username: str, password: str) -> User | None:
    """Authenticate a user by username and password.

    Username lookup is case-insensitive. Password is case-sensitive.
    """
    user = await get_user_by_username(db, username)
    if not user:
        return None
    if getattr(user, "auth_source", "local") in ("ldap", "oidc"):
        return None  # LDAP/OIDC users must authenticate via their provider, not local password
    if not user.password_hash or not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


async def authenticate_user_by_email(db: AsyncSession, email: str, password: str) -> User | None:
    """Authenticate a user by email and password.

    Email lookup is case-insensitive. Password is case-sensitive.
    """
    user = await get_user_by_email(db, email)
    if not user:
        return None
    if getattr(user, "auth_source", "local") in ("ldap", "oidc"):
        return None  # LDAP/OIDC users must authenticate via their provider
    if not user.password_hash or not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


async def has_any_admin(db: AsyncSession) -> bool:
    """Check whether at least one active admin user exists.

    An "admin" is any active user who either:
      - has ``role == 'admin'`` (legacy flag), or
      - is a member of the "Administrators" group.

    Used by the bootstrap / setup flow: if ``has_any_admin()`` is ``False``,
    the system is considered "unconfigured" and the setup middleware will
    block every non-whitelisted endpoint until ``/auth/setup`` creates the
    first admin.
    """
    try:
        # Legacy admin role
        legacy_q = select(func.count()).select_from(User).where(User.is_active.is_(True), User.role == "admin")
        legacy_count = (await db.execute(legacy_q)).scalar_one() or 0
        if legacy_count > 0:
            return True

        # Membership in the "Administrators" group
        group_q = (
            select(func.count())
            .select_from(User)
            .join(user_groups, user_groups.c.user_id == User.id)
            .join(Group, Group.id == user_groups.c.group_id)
            .where(User.is_active.is_(True), Group.name == "Administrators")
        )
        group_count = (await db.execute(group_q)).scalar_one() or 0
        return group_count > 0
    except Exception as e:
        # If the query fails (e.g. tables not yet created on fresh install),
        # treat it as "no admin" so the setup flow is reachable.
        logger.debug("has_any_admin() query failed: %s", e)
        return False


async def _validate_api_key(db: AsyncSession, api_key_value: str) -> APIKey | None:
    """Validate an API key and return the APIKey object if valid, None otherwise.

    This is an internal helper used by auth functions to check API keys.
    """
    try:
        result = await db.execute(select(APIKey).where(APIKey.enabled.is_(True)))
        api_keys = result.scalars().all()

        for api_key in api_keys:
            if verify_password(api_key_value, api_key.key_hash):
                # Check expiration
                if api_key.expires_at:
                    expires = api_key.expires_at
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if expires < datetime.now(timezone.utc):
                        return None  # Expired
                # Update last_used timestamp
                api_key.last_used = datetime.now(timezone.utc)
                await db.commit()
                return api_key
    except Exception as e:
        logger.warning("API key validation error: %s", e)
    return None


async def get_current_user_optional(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
) -> User | None:
    """Get the current authenticated user from JWT token, or None if not authenticated.

    §18.4: also checks ``jti`` (revocation) and ``iat`` (freshness vs
    ``user.password_changed_at``). Tokens that fail either check are treated
    exactly like malformed tokens — return None.
    """
    if credentials is None:
        return None

    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
    except JWTError:
        return None

    jti = payload.get("jti")
    async with async_session() as db:
        # Reuse this one session for the revocation check too, so each request
        # makes a single pooled checkout instead of two (#2572).
        if jti and await is_jti_revoked(jti, db):
            return None
        user = await get_user_by_username(db, username)
        if user is None or not user.is_active:
            return None
        if not _is_token_fresh(payload.get("iat"), user):
            return None
        return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
) -> User:
    """Get the current authenticated user from JWT token.

    §18.4: rejects revoked ``jti`` values and tokens issued before the user's
    last password change (``iat < password_changed_at``).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise credentials_exception
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    jti = payload.get("jti")
    async with async_session() as db:
        # Reuse this one session for the revocation check too, so each request
        # makes a single pooled checkout instead of two (#2572).
        if jti and await is_jti_revoked(jti, db):
            raise credentials_exception
        user = await get_user_by_username(db, username)
        if user is None:
            raise credentials_exception
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is disabled",
            )
        if not _is_token_fresh(payload.get("iat"), user):
            raise credentials_exception
        return user


async def get_current_active_user(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """Get the current active user (alias for clarity)."""
    return current_user


def generate_api_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns:
        tuple: (full_key, key_hash, key_prefix)
            - full_key: The complete API key (only shown once on creation)
            - key_hash: Hashed version for storage and verification
            - key_prefix: First 8 characters for display purposes
    """
    # Generate a secure random API key (32 bytes = 64 hex characters)
    full_key = f"bb_{secrets.token_urlsafe(32)}"
    key_hash = get_password_hash(full_key)
    key_prefix = full_key[:8] + "..." if len(full_key) > 8 else full_key
    return full_key, key_hash, key_prefix


async def get_api_key(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    db: AsyncSession = Depends(get_db),
) -> APIKey:
    """Get and validate API key from request headers.

    Checks both 'Authorization: Bearer <key>' and 'X-API-Key: <key>' headers.
    """
    api_key_value = None
    if x_api_key:
        api_key_value = x_api_key
    elif authorization and authorization.startswith("Bearer "):
        api_key_value = authorization.replace("Bearer ", "")

    if not api_key_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Provide 'X-API-Key' header or 'Authorization: Bearer <key>'",
        )

    # Get all API keys and check them
    result = await db.execute(select(APIKey).where(APIKey.enabled.is_(True)))
    api_keys = result.scalars().all()

    for api_key in api_keys:
        # Check if key matches (verify against hash)
        if verify_password(api_key_value, api_key.key_hash):
            # Check expiration
            if api_key.expires_at:
                expires = api_key.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires < datetime.now(timezone.utc):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="API key has expired",
                    )
            # Update last_used timestamp
            api_key.last_used = datetime.now(timezone.utc)
            await db.commit()
            return api_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )


# ---------------------------------------------------------------------------
# API-key permission allowlist (upstream Bambuddy GHSA-r2qv-8222-hqg3, v0.2.4.5)
# ---------------------------------------------------------------------------
# Until now, the API-key branches of ``require_permission`` /
# ``require_any_permission`` / ``require_ownership_permission`` returned
# ``None`` (or ``(None, True)``) for ANY valid key, ignoring its scope flags.
# A key with every checkbox unticked could still start/stop prints, reorder
# the queue, reprint archives, delete any user's library files, and read every
# endpoint — the scope flags were only ever enforced by ``check_permission()``
# inside the legacy webhook router.
#
# Fix: ``_check_apikey_permissions`` now requires every requested Permission to
# be present in ``_APIKEY_SCOPE_BY_PERMISSION`` (allowlist) AND its scope flag
# to be True on the key. Unmapped permissions = administrative = 403. A new
# Permission added to ``core/permissions.py`` without an allowlist/denylist
# entry is therefore denied for API keys by default (fail-closed-by-construction),
# and the drift-detection test forces a conscious classification for each.
#
# Mapping (see also the api-keys docs):
#   can_read_status     → every ``*_READ`` + camera + stats + system + websocket
#   can_queue           → queue write ops + archive reprint
#   can_control_printer → physical printer + smart-plug control
#   can_manage_library  → library upload/rename/delete-OWN + notes + MakerWorld import
#   can_manage_inventory→ inventory + forecast writes (+ SpoolBuddy-kiosk writes)
#   can_manage_maintenance→ per-printer maintenance log/reset + type-catalog CRUD (#1832 follow-up)
#   can_manage_archives → archive create/update/delete (NOT purge) (#1888)
#   can_manage_projects → project create/update/delete + membership (add-archives) (#1893)
#   can_access_cloud    → cloud auth (defence-in-depth alongside the router gate)
#   admin-only          → unmapped (default-deny): all create/update/delete of
#                         admin resources, settings writes, user/group/api-key
#                         admin, git backup, firmware OTA, discovery scan,
#                         library ALL-ownership + purges
_APIKEY_SCOPE_BY_PERMISSION: dict[Permission, str] = {
    # can_read_status — read-only access to status, history, and configuration
    Permission.PRINTERS_READ: "can_read_status",
    # Legacy flat read flags retained for back-compat with existing API keys;
    # new endpoints gate on the OWN/ALL split (maziggy/bambuddy-security #2).
    # OWN and ALL map to the same read scope — keys have no per-row ownership
    # identity, so a passing key keeps full (can_modify_all=True) read access.
    Permission.ARCHIVES_READ: "can_read_status",
    Permission.ARCHIVES_READ_OWN: "can_read_status",
    Permission.ARCHIVES_READ_ALL: "can_read_status",
    Permission.QUEUE_READ: "can_read_status",
    Permission.QUEUE_READ_OWN: "can_read_status",
    Permission.QUEUE_READ_ALL: "can_read_status",
    Permission.LIBRARY_READ: "can_read_status",
    Permission.LIBRARY_READ_OWN: "can_read_status",
    Permission.LIBRARY_READ_ALL: "can_read_status",
    Permission.PROJECTS_READ: "can_read_status",
    Permission.INVENTORY_READ: "can_read_status",
    Permission.INVENTORY_VIEW_ASSIGNMENTS: "can_read_status",
    Permission.INVENTORY_FORECAST_READ: "can_read_status",
    Permission.SMART_PLUGS_READ: "can_read_status",
    # A sensor reading is status, exactly as a plug reading is — so it rides
    # the scope a key already has rather than growing a column of its own.
    Permission.SMART_SENSORS_READ: "can_read_status",
    Permission.CAMERA_VIEW: "can_read_status",
    Permission.MAINTENANCE_READ: "can_read_status",
    Permission.PIPELINES_READ: "can_read_status",
    Permission.KPROFILES_READ: "can_read_status",
    Permission.NOTIFICATIONS_READ: "can_read_status",
    Permission.NOTIFICATION_TEMPLATES_READ: "can_read_status",
    Permission.EXTERNAL_LINKS_READ: "can_read_status",
    Permission.FIRMWARE_READ: "can_read_status",
    Permission.AMS_HISTORY_READ: "can_read_status",
    Permission.PRINTER_SENSOR_HISTORY_READ: "can_read_status",
    Permission.STATS_READ: "can_read_status",
    Permission.STATS_FILTER_BY_USER: "can_read_status",
    # ⚠️ USERS_READ_SLIM grants no data an API key could not already reach: for
    # API-keyed requests the permission deps return None as ``current_user``, so
    # the stats/archives user-filter guard short-circuits and ``?created_by_id=N``
    # is already honoured for every N. Without a way to discover the ids, that
    # filter is only addressable by brute force. The slim listing makes it
    # usable; the full USERS_READ listing (emails, roles, group membership,
    # permission sets) stays unmapped = admin-only.
    Permission.USERS_READ_SLIM: "can_read_status",
    Permission.SYSTEM_READ: "can_read_status",
    # SETTINGS_READ stays read-only so kiosk / integration keys can fetch the
    # UI-language setting; SETTINGS_UPDATE remains admin-only (denylist).
    Permission.SETTINGS_READ: "can_read_status",
    Permission.MAKERWORLD_VIEW: "can_read_status",
    Permission.WEBSOCKET_CONNECT: "can_read_status",
    # can_queue — queue write ops + reprint (which enqueues an existing archive)
    Permission.QUEUE_CREATE: "can_queue",
    Permission.QUEUE_UPDATE_OWN: "can_queue",
    Permission.QUEUE_UPDATE_ALL: "can_queue",
    Permission.QUEUE_DELETE_OWN: "can_queue",
    Permission.QUEUE_DELETE_ALL: "can_queue",
    Permission.QUEUE_REORDER: "can_queue",
    Permission.ARCHIVES_REPRINT_OWN: "can_queue",
    Permission.ARCHIVES_REPRINT_ALL: "can_queue",
    # can_control_printer — physical-world side effects on hardware
    Permission.PRINTERS_CONTROL: "can_control_printer",
    Permission.PRINTERS_FILES: "can_control_printer",
    Permission.PRINTERS_AMS_RFID: "can_control_printer",
    Permission.PRINTERS_CLEAR_PLATE: "can_control_printer",
    Permission.SMART_PLUGS_CONTROL: "can_control_printer",
    # can_manage_library — file-manager scope (upload/rename/delete library
    # entries + notes + MakerWorld import). OWN and ALL ownership variants map to
    # the same scope so the ``require_ownership_permission`` checker (which gates
    # API keys on ``all_perm``) passes a Manage-Library key through — API keys
    # have no per-row ownership identity, so splitting OWN/ALL across
    # allowlist/denylist made the whole library curation surface unreachable via
    # API key (upstream Bambuddy #1832). Matches the ``can_queue`` precedent.
    # LIBRARY_PURGE stays admin-only (bypasses the soft-delete window).
    Permission.LIBRARY_UPLOAD: "can_manage_library",
    Permission.LIBRARY_UPDATE_OWN: "can_manage_library",
    Permission.LIBRARY_UPDATE_ALL: "can_manage_library",
    Permission.LIBRARY_DELETE_OWN: "can_manage_library",
    Permission.LIBRARY_DELETE_ALL: "can_manage_library",
    Permission.LIBRARY_NOTES_WRITE: "can_manage_library",
    Permission.MAKERWORLD_IMPORT: "can_manage_library",
    # can_manage_inventory — inventory write scope (spool/catalog/forecast +
    # SpoolBuddy-kiosk NFC/scale writes that ride INVENTORY_UPDATE).
    Permission.INVENTORY_CREATE: "can_manage_inventory",
    Permission.INVENTORY_UPDATE: "can_manage_inventory",
    Permission.INVENTORY_DELETE: "can_manage_inventory",
    Permission.INVENTORY_FORECAST_WRITE: "can_manage_inventory",
    # can_manage_maintenance — carved out of the admin denylist so HA-style
    # automations can log "cleaned nozzle" / reset a maintenance counter via
    # ``POST /maintenance/items/{item_id}/perform`` without granting broader
    # printer control or settings write (upstream Bambuddy #1832 follow-up).
    # Also covers per-printer maintenance CRUD (assign/remove items, edit
    # intervals) and the maintenance-type-catalog CRUD (a config surface —
    # grouping it with item writes matches the operator mental model of "keys
    # that log maintenance can also manage what gets tracked"). MAINTENANCE_READ
    # stays under can_read_status.
    Permission.MAINTENANCE_CREATE: "can_manage_maintenance",
    Permission.MAINTENANCE_UPDATE: "can_manage_maintenance",
    Permission.MAINTENANCE_DELETE: "can_manage_maintenance",
    # can_manage_archives — print-history curation. Carved out of the admin
    # denylist so automations can prune old prints via API key (#1888): the
    # archive delete/update routes gate on
    # ``require_ownership_permission(ARCHIVES_*_ALL, ARCHIVES_*_OWN)``, which
    # resolves the ALL permission for API keys (no per-row ownership identity,
    # same as can_manage_library), so OWN and ALL map to the same scope.
    # ARCHIVES_PURGE stays admin-only (denylist) as a genuinely destructive op
    # that drops the print's stats contribution, mirroring LIBRARY_PURGE.
    # ARCHIVES_REPRINT_* stays under can_queue (it enqueues a print).
    Permission.ARCHIVES_CREATE: "can_manage_archives",
    Permission.ARCHIVES_UPDATE_OWN: "can_manage_archives",
    Permission.ARCHIVES_UPDATE_ALL: "can_manage_archives",
    Permission.ARCHIVES_DELETE_OWN: "can_manage_archives",
    Permission.ARCHIVES_DELETE_ALL: "can_manage_archives",
    # can_manage_projects — project curation. Carved out of the admin denylist
    # so automations can create projects and batch-add archives via API key
    # (#1893). The project mutation routes gate on plain
    # ``RequirePermission(Permission.PROJECTS_*)`` (no OWN/ALL ownership split —
    # projects have no per-row ownership permission), so the three CRUD
    # permissions map directly to the one scope. Membership edits
    # (add-archives-to-project) gate on PROJECTS_UPDATE, so they're covered.
    # PROJECTS_READ stays under can_read_status.
    Permission.PROJECTS_CREATE: "can_manage_projects",
    Permission.PROJECTS_UPDATE: "can_manage_projects",
    Permission.PROJECTS_DELETE: "can_manage_projects",
    # can_access_cloud — narrow opt-in; also enforced at the router-level
    # ``_cloud_api_key_gate``, gated here too for defence-in-depth.
    Permission.CLOUD_AUTH: "can_access_cloud",
    # ORCA_CLOUD_AUTH folds into the same ``can_access_cloud`` scope: same trust
    # dimension (third-party cloud access for profile sync), so an operator who
    # already accepted "this key can talk to clouds for the owner" doesn't need
    # a second toggle for Orca. Splitting later requires a new column + migration
    # — easy to add if the trust dimensions diverge.
    Permission.ORCA_CLOUD_AUTH: "can_access_cloud",
}

# Retained for documentation + drift-detection. Entries here are also absent
# from ``_APIKEY_SCOPE_BY_PERMISSION`` so they fail closed via the allowlist;
# this frozenset is a redundant explicit "these are admin" marker, not the
# load-bearing check.
_APIKEY_DENIED_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        # Settings administration (cred storage; rewriting these reaches SMTP/LDAP/MQTT).
        Permission.SETTINGS_UPDATE,
        Permission.SETTINGS_BACKUP,
        Permission.SETTINGS_RESTORE,
        # User / group / API-key administration.
        Permission.USERS_READ,
        Permission.USERS_CREATE,
        Permission.USERS_UPDATE,
        Permission.USERS_DELETE,
        Permission.GROUPS_READ,
        Permission.GROUPS_CREATE,
        Permission.GROUPS_UPDATE,
        Permission.GROUPS_DELETE,
        Permission.API_KEYS_READ,
        Permission.API_KEYS_CREATE,
        Permission.API_KEYS_UPDATE,
        Permission.API_KEYS_DELETE,
        # Git backup admin + firmware OTA.
        Permission.GIT_BACKUP,
        Permission.GIT_RESTORE,
        Permission.FIRMWARE_UPDATE,
        # Resource administration (CRUD on the catalog/registry itself).
        Permission.PRINTERS_CREATE,
        Permission.PRINTERS_UPDATE,
        Permission.PRINTERS_DELETE,
        # ARCHIVES_CREATE / _UPDATE_OWN / _UPDATE_ALL / _DELETE_OWN / _DELETE_ALL
        # moved to the allowlist under can_manage_archives (#1888) — splitting
        # them across allow/deny made the whole archive-management surface
        # unreachable for API keys via require_ownership_permission (same
        # regression class as the library/maintenance carve-outs). ARCHIVES_PURGE
        # stays denied as a genuinely destructive op that drops the print's
        # stats contribution.
        Permission.ARCHIVES_PURGE,
        # LIBRARY_UPDATE_ALL / LIBRARY_DELETE_ALL moved to the allowlist under
        # can_manage_library (#1832) — splitting them across allow/deny made the
        # library curation surface unreachable for API keys via
        # require_ownership_permission. Purge stays denied (genuinely destructive).
        Permission.LIBRARY_PURGE,
        # PROJECTS_CREATE / _UPDATE / _DELETE moved to the allowlist under
        # can_manage_projects (#1893) — they were denied for every API key,
        # making the project-management surface (create, add-archives, delete)
        # unreachable, same regression class as the archives/library carve-outs.
        # MAINTENANCE_CREATE / _UPDATE / _DELETE moved to the allowlist under
        # can_manage_maintenance (#1832 follow-up) for the same reason.
        # Slicer Pipelines writes/runs are admin-only for API keys (no scope column),
        # mirroring upstream's deny; PIPELINES_READ rides can_read_status above.
        Permission.PIPELINES_WRITE,
        Permission.KPROFILES_CREATE,
        Permission.KPROFILES_UPDATE,
        Permission.KPROFILES_DELETE,
        Permission.NOTIFICATIONS_CREATE,
        Permission.NOTIFICATIONS_UPDATE,
        Permission.NOTIFICATIONS_DELETE,
        Permission.NOTIFICATIONS_USER_EMAIL,
        Permission.NOTIFICATION_TEMPLATES_UPDATE,
        Permission.EXTERNAL_LINKS_CREATE,
        Permission.EXTERNAL_LINKS_UPDATE,
        Permission.EXTERNAL_LINKS_DELETE,
        Permission.SMART_PLUGS_CREATE,
        Permission.SMART_PLUGS_UPDATE,
        Permission.SMART_PLUGS_DELETE,
        Permission.SMART_SENSORS_CREATE,
        Permission.SMART_SENSORS_UPDATE,
        Permission.SMART_SENSORS_DELETE,
        # Network scanning — operator only (no API-key scope for this).
        Permission.DISCOVERY_SCAN,
    }
)


def _resolve_apikey_scope(perm_string: str) -> str | None:
    """Return the scope-flag attribute name gating ``perm_string`` for API keys.

    None when the permission is unmapped (= admin-only / not API-key-usable).
    """
    try:
        perm = Permission(perm_string)
    except ValueError:
        return None
    return _APIKEY_SCOPE_BY_PERMISSION.get(perm)


def apikey_effective_permissions(api_key: APIKey, owner: User | None = None) -> list[str]:
    """Return the permissions ``api_key`` can actually exercise, sorted.

    This is the exact set ``_check_apikey_permissions`` will let through: every
    mapped permission whose scope flag is True on the key, further narrowed to
    what ``owner`` may do. Unmapped permissions are administrative and never
    resolve for a key, so they are absent.

    ⚠️ ``owner=None`` means a **legacy ownerless key**, where the scope flags
    are the whole of the key's authority — not "skip the owner check". A caller
    holding an owned key must pass the owner, or ``/auth/me`` will over-report
    and drift from the gate, which is the whole defect this pairs with.
    """
    return sorted(
        perm.value
        for perm, scope_attr in _APIKEY_SCOPE_BY_PERMISSION.items()
        if getattr(api_key, scope_attr, False) and (owner is None or owner.has_permission(perm.value))
    )


async def resolve_apikey_owner(db: AsyncSession, api_key: APIKey) -> User | None:
    """Load the owner of ``api_key`` for an authorization decision.

    Distinct from the "who is this, if anyone" question — here two cases that
    both look like "no user" must not be conflated:

    - ``user_id IS NULL`` — a key predating per-user ownership. There is no
      owner to narrow against, so the scope flags stand alone. Returns None.
    - ``user_id`` set but the row is missing or deactivated — the key's
      authority came from a user who no longer has any. ⚠️ Raises 403 rather
      than returning None, because returning None here would **fail open**:
      deactivating a user would leave their keys working at full scope
      authority.

    Groups are eager-loaded because ``has_permission`` walks them, and a lazy
    load inside the permission check would raise MissingGreenlet.
    """
    if api_key.user_id is None:
        return None
    result = await db.execute(select(User).where(User.id == api_key.user_id).options(selectinload(User.groups)))
    owner = result.scalar_one_or_none()
    if owner is None or not owner.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key owner is deactivated or no longer exists",
        )
    return owner


async def authorize_api_key(
    db: AsyncSession,
    api_key: APIKey,
    perm_strings: list[str],
    *,
    require_any: bool = False,
) -> None:
    """Resolve the key's owner and run the full permission gate. Raises 403.

    The single entry point for every route-level gate: resolving the owner and
    then forgetting to use it is exactly the drift this exists to prevent.
    """
    owner = await resolve_apikey_owner(db, api_key)
    _check_apikey_permissions(api_key, perm_strings, owner=owner, require_any=require_any)


def _check_apikey_permissions(
    api_key: APIKey,
    perm_strings: list[str],
    *,
    owner: User | None = None,
    require_any: bool = False,
) -> None:
    """Raise 403 unless ``api_key`` is allowed to use ``perm_strings``.

    Allowlist semantics: every requested permission MUST be present in
    ``_APIKEY_SCOPE_BY_PERMISSION`` AND its scope flag must be True on
    ``api_key``. Unmapped permissions = administrative = 403.

    ⚠️ **A key must not out-rank the user it belongs to**, so when ``owner`` is
    given the permission must additionally be one the owner holds. Scope flags
    are chosen at creation time by whoever holds ``api_keys:create``; that is
    admin-only in the default groups, but a custom group can grant it, and
    without this check such a user could mint themselves a key with
    ``can_control_printer`` and act through it beyond their own permissions.
    ``owner=None`` is only correct for legacy ownerless keys — see
    ``resolve_apikey_owner``.

    By default ALL requested permissions must pass (mirrors
    ``require_permission``). When ``require_any=True``, only one needs to pass
    (mirrors ``require_any_permission``).
    """
    if not perm_strings:
        # Fail closed: an empty perm list would otherwise silently allow.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot be used for unspecified permissions",
        )

    last_failure: HTTPException | None = None
    for perm_str in perm_strings:
        scope_attr = _resolve_apikey_scope(perm_str)
        if scope_attr is None:
            failure: HTTPException | None = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API keys cannot be used for administrative operations",
            )
        elif not getattr(api_key, scope_attr, False):
            failure = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key does not have '{scope_attr}' permission",
            )
        elif owner is not None and not owner.has_permission(perm_str):
            failure = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key owner does not have '{perm_str}' permission",
            )
        else:
            failure = None

        if failure is None and require_any:
            return  # at least one passed
        if failure is not None and not require_any:
            raise failure
        last_failure = failure

    if require_any and last_failure is not None:
        raise last_failure


def check_permission(api_key: APIKey, permission: str) -> None:
    """Check if API key has the required permission.

    Args:
        api_key: The API key object
        permission: One of 'queue', 'control_printer', 'read_status'

    Raises:
        HTTPException: If permission is not granted
    """
    permission_map = {
        "queue": "can_queue",
        "control_printer": "can_control_printer",
        "read_status": "can_read_status",
    }

    if permission not in permission_map:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unknown permission: {permission}",
        )

    attr_name = permission_map[permission]
    if not getattr(api_key, attr_name, False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key does not have '{permission}' permission",
        )


# The coarse webhook permission names predate the Permission enum. Each maps to
# the enum member that best represents it, so the owner can be held to the same
# standard here as on the modern routes.
_WEBHOOK_PERMISSION_EQUIVALENT: dict[str, Permission] = {
    "queue": Permission.QUEUE_CREATE,
    "control_printer": Permission.PRINTERS_CONTROL,
    "read_status": Permission.PRINTERS_READ,
}


async def check_webhook_permission(db: AsyncSession, api_key: APIKey, permission: str) -> None:
    """``check_permission`` plus the owner checks the modern routes apply.

    ⚠️ ``/webhook/*`` reaches its scope flags through ``check_permission``
    rather than ``_check_apikey_permissions``, so it does NOT pick up the owner
    narrowing automatically. Without this it would be the way around the gate:
    the same key refused printer control on ``/printers/{id}/stop`` could stop
    the print through ``/webhook/printer/{id}/stop``.

    ``resolve_apikey_owner`` also kills a key whose owner was deactivated,
    which this door would otherwise honour indefinitely.
    """
    check_permission(api_key, permission)
    owner = await resolve_apikey_owner(db, api_key)
    equivalent = _WEBHOOK_PERMISSION_EQUIVALENT.get(permission)
    if owner is not None and equivalent is not None and not owner.has_permission(equivalent.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key owner does not have '{equivalent.value}' permission",
        )


def check_printer_access(api_key: APIKey, printer_id: int) -> None:
    """Check if API key has access to the specified printer.

    Args:
        api_key: The API key object
        printer_id: The printer ID to check access for

    Raises:
        HTTPException: If access is denied
    """
    # If printer_ids is None, access to all printers (empty list = no access)
    if api_key.printer_ids is None:
        return

    # Check if printer_id is in allowed list
    if printer_id not in api_key.printer_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key does not have access to printer {printer_id}",
        )


# Convenience dependencies - these are functions that return Depends objects
def require_admin():
    """Dependency factory requiring the caller be an **admin user**.

    Admin = ``User.is_admin`` (legacy ``role == "admin"`` **OR** membership
    in the Administrators group) — NOT the bare ``role`` column, so a
    default-install operator who was made admin by being added to
    Administrators (rather than by flipping the legacy role) still passes.

    Resolution goes through ``get_current_user`` (JWT only), which means an
    API key — presented via ``X-API-Key`` or ``Bearer bb_...`` — never
    satisfies this dependency: keys carry no user identity, so they can't be
    admin. Layer this **on top of** ``RequirePermission(...)`` on privileged
    user/group-management writes (upstream Bambuddy security-hardening #1) so
    a non-admin operator who merely holds ``users:update`` / ``groups:update``
    can't self-escalate by editing the Administrators group or minting an
    admin account.
    """

    async def admin_checker(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if not current_user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires admin role",
            )
        return current_user

    return admin_checker


def RequireAdmin():
    """Dependency that requires the caller be an admin user (``is_admin``)."""
    return Depends(require_admin())


def require_permission(*permissions: str | Permission):
    """Dependency factory that requires user to have ALL specified permissions.

    Accepts both JWT tokens (via Authorization: Bearer header) and API keys
    (via X-API-Key header or Authorization: Bearer bb_xxx).

    API keys bypass the per-resource permission check (legacy behavior); their
    access is instead narrowed through the API-key-specific ``can_queue`` /
    ``can_control_printer`` / ``can_read_status`` flags elsewhere.

    Args:
        *permissions: Permission strings or Permission enum values to require

    Returns:
        A dependency function that validates permissions. Returns ``User`` for
        JWT-authenticated requests or ``None`` for API-key requests.
    """
    # Convert Permission enums to strings
    perm_strings = [p.value if isinstance(p, Permission) else p for p in permissions]

    async def permission_checker(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> User | None:
        async with async_session() as db:
            # Check for API key first (X-API-Key header)
            if x_api_key:
                api_key = await _validate_api_key(db, x_api_key)
                if api_key:
                    # GHSA-r2qv-8222-hqg3: gate on the key's scope flags instead
                    # of allowing any valid key unconditionally.
                    await authorize_api_key(db, api_key, perm_strings)
                    return None  # API key valid + scoped, allow access

            credentials_exception = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

            if credentials is None:
                raise credentials_exception

            token = credentials.credentials
            # Check if it's an API key (starts with bb_)
            if token.startswith("bb_"):
                api_key = await _validate_api_key(db, token)
                if api_key:
                    await authorize_api_key(db, api_key, perm_strings)
                    return None  # API key valid + scoped, allow access
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid API key",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Otherwise treat as JWT
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                username: str = payload.get("sub")
                if username is None:
                    raise credentials_exception
            except JWTError:
                raise credentials_exception

            user = await get_user_by_username(db, username)
            if user is None or not user.is_active:
                raise credentials_exception

            if not user.has_all_permissions(*perm_strings):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing required permissions: {', '.join(perm_strings)}",
                )
            return user

    return permission_checker


def RequirePermission(*permissions: str | Permission):
    """Convenience dependency that requires ALL specified permissions."""
    return Depends(require_permission(*permissions))


def require_energy_cost_update():
    """Dependency for ``POST /settings/electricity-price`` (upstream Bambuddy
    #1356 / commit ae29a7dc).

    Two accept paths:

    - JWT user with ``SETTINGS_UPDATE`` permission — the standard admin path.
    - API key with ``can_update_energy_cost = True`` — explicit opt-in
      narrowly-scoped to this single endpoint. Since the API-key permission
      allowlist (GHSA-r2qv-8222-hqg3) ``SETTINGS_UPDATE`` is admin-only for
      API keys, so ``PATCH /settings`` now 403s them (it can rewrite SMTP /
      LDAP / MQTT credentials). This endpoint is the sanctioned narrow path
      for Home-Assistant dynamic-tariff integrations to update
      ``energy_cost_per_kwh`` without that blast radius.

    The narrow path:

    * API keys without ``can_update_energy_cost`` get 403 even though a
      valid key was supplied — communicates the operator must opt in
      explicitly on the key's row.
    * Unauthenticated requests get the standard 401.
    """

    async def permission_checker(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> User | None:
        async with async_session() as db:
            credentials_exception = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

            # API-key path — X-API-Key header OR ``Bearer bb_xxx``.
            api_key_value: str | None = None
            if x_api_key:
                api_key_value = x_api_key
            elif credentials is not None and credentials.credentials.startswith("bb_"):
                api_key_value = credentials.credentials

            if api_key_value is not None:
                api_key = await _validate_api_key(db, api_key_value)
                if api_key is None:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid API key",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                # Fails closed if the owner has been deactivated. ⚠️ The scope
                # flag itself is NOT narrowed against the owner's permissions
                # the way the general gate is: this door exists precisely
                # because no user permission maps to it (SETTINGS_UPDATE stays
                # denied for keys even when the owner is an administrator).
                await resolve_apikey_owner(db, api_key)
                if not api_key.can_update_energy_cost:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="API key does not have 'update_energy_cost' permission",
                    )
                return None

            # JWT path — standard SETTINGS_UPDATE check.
            if credentials is None:
                raise credentials_exception

            try:
                payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
                username: str = payload.get("sub")
                if username is None:
                    raise credentials_exception
            except JWTError:
                raise credentials_exception

            user = await get_user_by_username(db, username)
            if user is None or not user.is_active:
                raise credentials_exception
            if not user.has_all_permissions(Permission.SETTINGS_UPDATE.value):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing required permissions: {Permission.SETTINGS_UPDATE.value}",
                )
            return user

    return permission_checker


def require_any_permission(*permissions: str | Permission):
    """Dependency factory: pass when the user has ANY of the listed permissions.

    Mirror of ``require_permission`` with ``has_any_permission`` instead of
    ``has_all_permissions``. Used by stock-forecasting endpoints so operators
    with the legacy ``inventory:update`` permission keep access without
    needing the new ``inventory:forecast_write`` re-granted, and viewers
    with ``inventory:read`` can still see the panel.
    """
    perm_strings = [p.value if isinstance(p, Permission) else p for p in permissions]

    async def permission_checker(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> User | None:
        async with async_session() as db:
            if x_api_key:
                api_key = await _validate_api_key(db, x_api_key)
                if api_key:
                    # GHSA-r2qv-8222-hqg3: require at least one requested scope.
                    await authorize_api_key(db, api_key, perm_strings, require_any=True)
                    return None

            credentials_exception = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

            if credentials is None:
                raise credentials_exception

            token = credentials.credentials
            if token.startswith("bb_"):
                api_key = await _validate_api_key(db, token)
                if api_key:
                    await authorize_api_key(db, api_key, perm_strings, require_any=True)
                    return None
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid API key",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                username: str = payload.get("sub")
                if username is None:
                    raise credentials_exception
            except JWTError:
                raise credentials_exception

            user = await get_user_by_username(db, username)
            if user is None or not user.is_active:
                raise credentials_exception

            if not user.has_any_permission(*perm_strings):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing any of the required permissions: {', '.join(perm_strings)}",
                )
            return user

    return permission_checker


def RequireAnyPermission(*permissions: str | Permission):
    """Convenience dependency that requires ANY of the specified permissions."""
    return Depends(require_any_permission(*permissions))


async def verify_camwall_token(token: str) -> bool:
    """Verify a Cam Wall kiosk token (upstream #2531). Reusable — not consumed.

    Only the matching long-lived scope passes. A bare ``camera_stream`` token is
    refused: those are already in the wild, minted to hand out video, and must
    not gain the ability to enumerate the fleet by name. The 60-minute ephemeral
    token belongs to a logged-in browser, which reaches the same data through
    the ordinary printers API.
    """
    async with async_session() as db:
        from backend.app.services.long_lived_tokens import verify_token as verify_long_lived

        record = await verify_long_lived(db, token, scope="camwall")
        return record is not None


def require_camwall_token():
    """Dependency that validates a Cam Wall token passed as ``?token=``.

    Auth is always on here, so unlike upstream there is no "if enabled" escape:
    the token is always required.
    """

    async def checker(token: str | None = None) -> None:
        if not token or not await verify_camwall_token(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Valid Cam Wall token required. Create one under Settings > API Keys with the 'Cam Wall' scope."
                ),
            )

    return checker


async def verify_overlay_token(token: str) -> bool:
    """Verify a streaming-overlay token (upstream #2613). Reusable — not consumed.

    Only the matching long-lived scope passes. The overlay status feed names the
    file being printed, so it must not be reachable by a bare ``camera_stream``
    token (handed out for video alone). The 60-minute ephemeral token belongs to
    a logged-in browser, which reaches the same data through the ordinary
    printers API and has no need of this endpoint.
    """
    async with async_session() as db:
        from backend.app.services.long_lived_tokens import verify_token as verify_long_lived

        record = await verify_long_lived(db, token, scope="overlay")
        return record is not None


def require_overlay_token():
    """Dependency that validates a streaming-overlay token passed as ``?token=``.

    Used by the read-only overlay status feed (upstream #2613), which OBS — or
    any embed with no login session — loads with the token in the URL because it
    has no JWT to carry. Auth is always on here, so unlike upstream there is no
    "if enabled" escape: the token is always required.
    """

    async def checker(token: str | None = None) -> None:
        if not token or not await verify_overlay_token(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Valid overlay token required. Create one under Settings > API Keys "
                    "with the 'Streaming Overlay' scope."
                ),
            )

    return checker


def require_camera_stream_token():
    """Dependency that validates a camera-stream token passed as ``?token=...``.

    Used for camera stream / snapshot endpoints loaded via ``<img>`` / ``<video>``
    tags — those can't send Authorization headers, so the frontend obtains a
    token from ``POST /printers/camera/stream-token`` and appends it to the URL.
    """

    async def checker(token: str | None = None) -> None:
        if not token or not await verify_camera_stream_token(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Valid camera stream token required. Obtain one from POST /api/v1/printers/camera/stream-token"
                ),
            )

    return checker


RequireCameraStreamToken = Depends(require_camera_stream_token())
RequireOverlayToken = Depends(require_overlay_token())
RequireCamWallToken = Depends(require_camwall_token())


def require_ownership_permission(
    all_permission: str | Permission,
    own_permission: str | Permission,
):
    """Dependency factory for ownership-based permission checks.

    - User with ``all_permission`` can modify any item
    - User with ``own_permission`` can only modify items where created_by_id == user.id
    - Ownerless items (created_by_id = null) require ``all_permission``
    - API keys (via X-API-Key header or Bearer bb_xxx) get full access (can_modify_all=True)

    Returns:
        A dependency function that returns (user, can_modify_all).
        - can_modify_all=True: user can modify any item
        - can_modify_all=False: user can only modify their own items
    """
    all_perm = all_permission.value if isinstance(all_permission, Permission) else all_permission
    own_perm = own_permission.value if isinstance(own_permission, Permission) else own_permission

    async def checker(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> tuple[User | None, bool]:
        """Returns (user, can_modify_all)."""
        async with async_session() as db:
            # Check for API key first (X-API-Key header)
            if x_api_key:
                api_key = await _validate_api_key(db, x_api_key)
                if api_key:
                    # GHSA-r2qv-8222-hqg3: previously any valid key received
                    # (None, True) — a "queue-only" key could delete any user's
                    # archives / library files / queue items. OWN and ALL map to
                    # the same scope flag, so gating on ``all_perm`` is correct;
                    # keys have no per-row ownership identity so a passing key
                    # keeps can_modify_all=True.
                    await authorize_api_key(db, api_key, [all_perm])
                    return None, True

            # Check for Bearer token (could be JWT or API key)
            if credentials is not None:
                token = credentials.credentials
                # Check if it's an API key (starts with bb_)
                if token.startswith("bb_"):
                    api_key = await _validate_api_key(db, token)
                    if api_key:
                        await authorize_api_key(db, api_key, [all_perm])
                        return None, True
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid API key",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

                # Otherwise treat as JWT
                try:
                    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                    username: str = payload.get("sub")
                    if username is None:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Could not validate credentials",
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                except JWTError:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Could not validate credentials",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

                user = await get_user_by_username(db, username)
                if user is None or not user.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Could not validate credentials",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

                if user.has_permission(all_perm):
                    return user, True
                if user.has_permission(own_perm):
                    return user, False

                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing permission: {own_perm} or {all_perm}",
                )

            # No credentials provided
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return checker
