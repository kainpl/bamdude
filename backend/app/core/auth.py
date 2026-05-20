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
REFRESH_TOKEN_EXPIRE_HOURS_SESSION = 12
REFRESH_TOKEN_COOKIE_NAME = "bamdude_refresh"
# The refresh cookie is only ever sent on these paths — narrows the CSRF
# surface and keeps unrelated routes from seeing the cookie in their logs.
REFRESH_TOKEN_COOKIE_PATH = "/api/v1/auth"

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
        from backend.app.services.long_lived_tokens import verify_token as verify_long_lived

        record = await verify_long_lived(db, token, scope="camera_stream")
        return record is not None


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


def refresh_token_ttl(remember_me: bool) -> timedelta:
    """Absolute DB-side TTL for a refresh token.

    Without ``remember_me`` the cookie is a session cookie (closes with the
    browser), but a session-cookie alone doesn't stop a server-side replay
    if the user just locks the screen and leaves the tab open — hence the
    separate 12 h DB-cap that kicks in even if the browser stays open.
    """
    if remember_me:
        return timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER)
    return timedelta(hours=REFRESH_TOKEN_EXPIRE_HOURS_SESSION)


async def create_refresh_token(
    db,
    *,
    username: str,
    remember_me: bool,
    family_id: str | None = None,
) -> tuple[str, str, datetime]:
    """Mint a refresh token and persist its hash.

    Returns ``(raw_token, family_id, expires_at)``. Caller is responsible
    for committing the session and setting the cookie on the response.

    ``family_id`` links every rotation descended from one /login. Pass the
    existing id when rotating (so reuse detection can see the lineage);
    leave it None for fresh logins so a new family is created.
    """
    from backend.app.models.auth_ephemeral import AuthEphemeralToken

    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_refresh_token(raw_token)
    if family_id is None:
        family_id = secrets.token_hex(16)
    expires_at = datetime.now(timezone.utc) + refresh_token_ttl(remember_me)

    row = AuthEphemeralToken.new_refresh(
        token_hash=token_hash,
        username=username,
        family_id=family_id,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return raw_token, family_id, expires_at


async def verify_and_consume_refresh_token(
    db,
    raw_token: str,
) -> tuple[str | None, str | None, str]:
    """Validate + mark-used in one atomic step.

    Returns ``(username, family_id, status)`` where ``status`` is:

    - ``"ok"`` — token valid + rotated. ``username`` + ``family_id`` populated;
      caller issues a new access + a new refresh inside the same family.
    - ``"reuse"`` — token already consumed before. Whole family revoked as a
      side effect; ``username`` + ``family_id`` populated so the caller can
      log + return a descriptive 401.
    - ``"invalid"`` — token not found or expired. ``username`` / ``family_id``
      are None. Returned as 401 by the caller without a side effect.

    The ``ok`` case flips ``used_at`` via an UPDATE … WHERE used_at IS NULL
    so two concurrent /auth/refresh hits on the same token can't both get
    ``ok`` — exactly one wins; the loser reads back ``used_at`` non-null and
    gets ``reuse`` (which is correct — that second request IS a replay, even
    if it's the same legit client racing itself).
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

    # Reuse detection: if used_at already set, this is a replay. Collapse
    # the whole family so a stolen cookie can't survive past the first
    # legitimate refresh.
    if row.used_at is not None:
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
        # Lost the race — another request just consumed it. Same as reuse.
        if row.family_id:
            await revoke_refresh_family(db, row.family_id)
        return row.username, row.family_id, "reuse"

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


async def is_jti_revoked(jti: str) -> bool:
    """Return True if the given ``jti`` has been revoked."""
    async with async_session() as db:
        result = await db.execute(
            select(AuthEphemeralToken).where(
                AuthEphemeralToken.token == jti,
                AuthEphemeralToken.token_type == TokenType.REVOKED_JTI,
            )
        )
        return result.scalar_one_or_none() is not None


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
    if jti and await is_jti_revoked(jti):
        return None

    async with async_session() as db:
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
    if jti and await is_jti_revoked(jti):
        raise credentials_exception

    async with async_session() as db:
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


def require_role(required_role: str):
    """Dependency factory for role-based access control."""

    async def role_checker(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if current_user.role != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required_role} role",
            )
        return current_user

    return role_checker


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
def RequireAdmin():
    """Dependency that requires admin role."""
    return Depends(require_role("admin"))


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
                    return None  # API key valid, allow access

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
                    return None  # API key valid, allow access
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
                    return None, True  # API key valid, allow all

            # Check for Bearer token (could be JWT or API key)
            if credentials is not None:
                token = credentials.credentials
                # Check if it's an API key (starts with bb_)
                if token.startswith("bb_"):
                    api_key = await _validate_api_key(db, token)
                    if api_key:
                        return None, True  # API key valid, allow all
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
