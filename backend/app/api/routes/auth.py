import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from jwt.exceptions import PyJWTError as JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.settings import get_external_login_url
from backend.app.core.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    REFRESH_TOKEN_COOKIE_NAME,
    REFRESH_TOKEN_COOKIE_PATH,
    REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER,
    SECRET_KEY,
    Permission,
    RequirePermission,
    _is_token_fresh,
    _validate_api_key,
    authenticate_user,
    authenticate_user_by_email,
    create_access_token,
    create_refresh_token,
    get_current_active_user,
    get_password_hash,
    get_user_by_email,
    get_user_by_username,
    has_any_admin,
    is_jti_revoked,
    refresh_cookie_secure_flag,
    revoke_all_refresh_tokens_for_user,
    revoke_jti,
    revoke_refresh_family,
    security,
    verify_and_consume_refresh_token,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import ALL_PERMISSIONS, DEFAULT_GROUPS
from backend.app.models.group import Group
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.auth import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    GroupBrief,
    LoginRequest,
    LoginResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SetupRequest,
    SetupResponse,
    SMTPSettings,
    TestSMTPRequest,
    TestSMTPResponse,
    UserResponse,
)
from backend.app.services.email_service import (
    create_password_reset_email_from_template,
    generate_secure_password,
    get_smtp_settings,
    save_smtp_settings,
    send_email,
)


def _user_to_response(user: User) -> UserResponse:
    """Convert a User model to UserResponse schema."""
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        is_admin=user.is_admin,
        auth_source=getattr(user, "auth_source", "local"),
        groups=[GroupBrief(id=g.id, name=g.name) for g in user.groups],
        permissions=sorted(user.get_permissions()),
        created_at=user.created_at.isoformat(),
    )


async def _issue_refresh_cookie(
    db: AsyncSession,
    response: Response,
    request: Request,
    *,
    username: str,
    remember_me: bool,
    family_id: str | None = None,
) -> None:
    """Mint + persist a refresh token and set the cookie on ``response``.

    ``family_id=None`` creates a fresh family (new /login). Pass the existing
    family when rotating inside /auth/refresh so reuse detection sees the
    whole lineage.

    Cookie shape (§18.14):

    - ``HttpOnly`` — refresh token invisible to JS; XSS can't exfiltrate.
    - ``SameSite=Lax`` — sent on same-site navigations + same-site POSTs,
      blocks cross-origin form submissions which is enough for a
      non-preflighted POST /auth/refresh.
    - ``Secure`` — auto-detected by ``refresh_cookie_secure_flag`` (HTTPS
      vs plain HTTP, with X-Forwarded-Proto awareness) unless
      ``AUTH_REFRESH_COOKIE_SECURE`` env forces the polarity.
    - ``Path=/api/v1/auth`` — cookie is only transmitted to auth endpoints,
      trimming the attack surface against CSRF on unrelated routes.
    - ``Max-Age`` — set only when ``remember_me=True`` (30 d). Without it
      the cookie is a session cookie that dies with the browser process.
    """
    raw_refresh, resolved_family, expires_at = await create_refresh_token(
        db,
        username=username,
        remember_me=remember_me,
        family_id=family_id,
    )
    cookie_kwargs: dict = {
        "key": REFRESH_TOKEN_COOKIE_NAME,
        "value": raw_refresh,
        "httponly": True,
        "secure": refresh_cookie_secure_flag(request),
        "samesite": "lax",
        "path": REFRESH_TOKEN_COOKIE_PATH,
    }
    if remember_me:
        # 30 d in seconds. Browser persists across restarts; matches DB TTL.
        cookie_kwargs["max_age"] = REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER * 24 * 3600
    response.set_cookie(**cookie_kwargs)
    _ = resolved_family, expires_at  # logged by the helpers; not needed here


def _clear_refresh_cookie(response: Response, request: Request) -> None:
    """Match ``_issue_refresh_cookie`` attributes so the browser recognises
    this as the same cookie and evicts it (cookie identity = name + path +
    domain). Missing ``Secure`` / ``SameSite`` on the clear would leave the
    original sitting in the jar on some browsers.
    """
    response.delete_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        path=REFRESH_TOKEN_COOKIE_PATH,
        httponly=True,
        secure=refresh_cookie_secure_flag(request),
        samesite="lax",
    )


def _api_key_to_user_response(api_key) -> UserResponse:
    """Create a synthetic admin UserResponse for a valid API key."""
    return UserResponse(
        id=0,
        username=f"api-key:{api_key.key_prefix}",
        email=None,
        role="admin",
        is_active=True,
        is_admin=True,
        groups=[],
        permissions=sorted(ALL_PERMISSIONS),
        created_at=api_key.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# §18.5 M-R9-A: Real client IP resolution for rate limiting behind reverse proxies.
# Set TRUSTED_PROXY_IPS (comma-separated) to enable X-Forwarded-For trust.
# Without this env var client.host is used directly (safe default for direct-deploy).
# ---------------------------------------------------------------------------
_TRUSTED_PROXY_IPS: frozenset[str] = frozenset(
    ip.strip() for ip in os.environ.get("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
)


def _get_client_ip(request: Request) -> str:
    """Return the real client IP for rate-limiting purposes.

    When ``TRUSTED_PROXY_IPS`` is configured and the direct TCP peer is a
    trusted proxy, ``X-Forwarded-For`` is evaluated right-to-left: the
    rightmost IP that is NOT itself a trusted proxy is the true client
    address (M-R10-A fix for multi-hop nginx chains). Standard nginx with
    ``proxy_add_x_forwarded_for`` appends the client IP, so the rightmost
    entry that isn't a known proxy is always the real caller.

    Falls back to ``request.client.host`` when ``TRUSTED_PROXY_IPS`` is
    unset (direct deployment without a reverse proxy).
    """
    # I5: per-request unique token instead of "unknown" when the transport has
    # no client address — prevents collision with any literal username and
    # prevents all such requests from sharing a single rate-limit bucket.
    direct_ip = request.client.host if request.client else f"__no_ip_{secrets.token_hex(8)}__"
    if _TRUSTED_PROXY_IPS and direct_ip in _TRUSTED_PROXY_IPS:
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        ips = [ip.strip() for ip in forwarded_for.split(",") if ip.strip()]
        for ip in reversed(ips):
            if ip not in _TRUSTED_PROXY_IPS:
                return ip
        if ips:
            return ips[0]
    return direct_ip


router = APIRouter(prefix="/auth", tags=["authentication"])


async def is_advanced_auth_enabled(db: AsyncSession) -> bool:
    """Check if advanced authentication is enabled."""
    result = await db.execute(select(Settings).where(Settings.key == "advanced_auth_enabled"))
    setting = result.scalar_one_or_none()
    if setting is None:
        return False
    return setting.value.lower() == "true"


async def set_advanced_auth_enabled(db: AsyncSession, enabled: bool) -> None:
    """Set advanced authentication enabled status."""
    from sqlalchemy import func
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(Settings).values(key="advanced_auth_enabled", value="true" if enabled else "false")
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"], set_={"value": "true" if enabled else "false", "updated_at": func.now()}
    )
    await db.execute(stmt)


async def is_setup_completed(db: AsyncSession) -> bool:
    """Check if setup has been completed."""
    result = await db.execute(select(Settings).where(Settings.key == "setup_completed"))
    setting = result.scalar_one_or_none()
    return setting and setting.value.lower() == "true"


async def set_setup_completed(db: AsyncSession, completed: bool) -> None:
    """Set setup completed status."""
    from sqlalchemy import func
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(Settings).values(key="setup_completed", value="true" if completed else "false")
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"], set_={"value": "true" if completed else "false", "updated_at": func.now()}
    )
    await db.execute(stmt)
    # Note: Don't commit here - let get_db handle it or commit explicitly in the route


@router.post("/setup", response_model=SetupResponse)
async def setup_auth(request: SetupRequest, db: AsyncSession = Depends(get_db)):
    """First-time setup: create the initial admin user and auto-login.

    Public endpoint - intentionally unauthenticated, because the system has no
    admin yet. Once any admin exists, subsequent requests are rejected with 403.
    """
    import logging

    logger = logging.getLogger(__name__)

    try:
        # Block re-runs once an admin exists. This is the sole gate - we don't
        # read any "setup_completed" flag, because the admin-count is the real
        # source of truth (a stale flag with zero admins would otherwise lock
        # the system out).
        if await has_any_admin(db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Setup has already been completed. An admin user already exists.",
            )

        if not request.admin_username.strip() or not request.admin_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Admin username and password are required",
            )

        # Username collision guard - safe to run even before setup completes.
        existing_user = await get_user_by_username(db, request.admin_username)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this username already exists",
            )

        # Orphan cleanup — if the previous admin was deleted via raw SQL (or
        # an earlier reset_admin that pre-dates the cascade), dangling rows
        # in ``user_groups`` can survive and collide with a freshly-inserted
        # admin that reuses the old primary key (SQLite reuses IDs on
        # INTEGER PRIMARY KEY without AUTOINCREMENT). Clear them before the
        # new user → admin-group link is flushed.
        from sqlalchemy import text as _text

        await db.execute(_text("DELETE FROM user_groups WHERE user_id NOT IN (SELECT id FROM users)"))

        # Ensure the "Administrators" system group exists. It is normally seeded
        # by migration m001, but a rescue path (CLI reset_admin + fresh DB) may
        # reach setup before seeds run, so we create it on demand.
        admin_group_result = await db.execute(select(Group).where(Group.name == "Administrators"))
        admin_group = admin_group_result.scalar_one_or_none()
        if admin_group is None:
            admin_group_def = DEFAULT_GROUPS["Administrators"]
            admin_group = Group(
                name="Administrators",
                description=admin_group_def["description"],
                permissions=list(admin_group_def["permissions"]),
                is_system=admin_group_def["is_system"],
            )
            db.add(admin_group)
            await db.flush()
            logger.info("Seeded missing 'Administrators' system group during setup")

        admin_user = User(
            username=request.admin_username.strip(),
            email=request.admin_email.strip() if request.admin_email else None,
            password_hash=get_password_hash(request.admin_password),
            role="admin",
            is_active=True,
        )
        admin_user.groups.append(admin_group)
        db.add(admin_user)

        # Mark setup as completed for UI fast-path. The authoritative check is
        # still has_any_admin(), so this flag is advisory only.
        await set_setup_completed(db, True)
        await db.commit()
        await db.refresh(admin_user)

        # Invalidate the setup-gate cache so subsequent requests are unblocked
        # without a restart.
        try:
            from backend.app.main import invalidate_setup_gate_cache  # local import to avoid cycle

            invalidate_setup_gate_cache()
        except Exception:
            # main.py cache is nice-to-have; tolerate its absence (e.g. during
            # test imports).
            pass

        # Reload with groups populated for the response.
        result = await db.execute(select(User).where(User.id == admin_user.id).options(selectinload(User.groups)))
        admin_user = result.scalar_one()

        access_token = create_access_token(
            data={"sub": admin_user.username},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )

        logger.info("Initial admin user created during setup: %s", admin_user.username)

        return SetupResponse(
            admin_created=True,
            access_token=access_token,
            token_type="bearer",
            user=_user_to_response(admin_user),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Setup error: %s", e, exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Setup failed",
        )


@router.get("/status")
async def get_auth_status(db: AsyncSession = Depends(get_db)):
    """Get authentication status (public endpoint).

    ``requires_setup`` is ``True`` iff no active admin user exists. Auth itself
    is always on - the legacy ``auth_enabled`` field is kept as ``True`` for
    backward compatibility with older frontends.
    """
    requires_setup = not await has_any_admin(db)
    return {"auth_enabled": True, "requires_setup": requires_setup}


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    raw_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Login and get access token.

    Supports username or email-based login. Username lookup is case-insensitive.

    §18.5 rate limiting: two sliding-window buckets — per-username
    (MAX_LOGIN_ATTEMPTS_PER_USERNAME in 15 min) and per-client-IP
    (MAX_LOGIN_ATTEMPTS_PER_IP in 15 min). On successful login both buckets
    are cleared. Behind a reverse proxy configure ``TRUSTED_PROXY_IPS`` so
    the real client IP is used; otherwise the TCP peer is.
    """
    from backend.app.core.rate_limit import (
        MAX_LOGIN_ATTEMPTS_PER_IP,
        MAX_LOGIN_ATTEMPTS_PER_USERNAME,
        check_rate_limit,
        clear_failed_attempts,
        record_failed_attempt,
    )
    from backend.app.models.auth_ephemeral import EventType

    client_ip = _get_client_ip(raw_request)
    await check_rate_limit(
        db, request.username, event_type=EventType.LOGIN_ATTEMPT, max_attempts=MAX_LOGIN_ATTEMPTS_PER_USERNAME
    )
    await check_rate_limit(db, client_ip, event_type=EventType.LOGIN_IP, max_attempts=MAX_LOGIN_ATTEMPTS_PER_IP)

    # Check if LDAP is enabled
    ldap_user = None
    ldap_settings = await _get_ldap_settings(db)
    if ldap_settings:
        try:
            from backend.app.services.ldap_service import (
                authenticate_ldap_user,
                parse_ldap_config,
            )

            ldap_config = parse_ldap_config(ldap_settings)
            if ldap_config:
                ldap_user = authenticate_ldap_user(ldap_config, request.username, request.password)
                if ldap_user:
                    # LDAP auth succeeded - find or create local user
                    user = await get_user_by_username(db, ldap_user.username)
                    if user and user.auth_source != "ldap":
                        # Username exists as local user - don't override
                        user = None
                        ldap_user = None
                    elif not user:
                        if not ldap_config.auto_provision:
                            # User doesn't exist and auto-provision is off
                            ldap_user = None
                        else:
                            # Auto-provision LDAP user
                            user = await _provision_ldap_user(db, ldap_user, ldap_config)

                    if user and ldap_user:
                        # Update email and group mappings on each login
                        await _sync_ldap_user(db, user, ldap_user, ldap_config)
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning("LDAP authentication error, falling back to local: %s", e)
            ldap_user = None

    # Try username-based authentication (skip if already authenticated via LDAP)
    if not ldap_user:
        user = await authenticate_user(db, request.username, request.password)

    # If username auth failed and advanced auth is enabled, try email-based authentication
    if not user and not ldap_user:
        advanced_auth = await is_advanced_auth_enabled(db)
        if advanced_auth:
            user = await authenticate_user_by_email(db, request.username, request.password)

    if not user:
        # §18.5: record the failure into both buckets so repeated attempts trip
        # the rate limit even when the username doesn't exist.
        await record_failed_attempt(db, request.username, event_type=EventType.LOGIN_ATTEMPT)
        await record_failed_attempt(db, client_ip, event_type=EventType.LOGIN_IP)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reload user with groups for proper permission calculation
    result = await db.execute(select(User).where(User.id == user.id).options(selectinload(User.groups)))
    user = result.scalar_one()

    # §18.5: successful login clears both rate-limit buckets for this pair.
    await clear_failed_attempts(db, user.username, event_type=EventType.LOGIN_ATTEMPT)
    await clear_failed_attempts(db, client_ip, event_type=EventType.LOGIN_IP)

    # --- 2FA check ---------------------------------------------------------
    # If the user has any active 2FA method, return a pre_auth_token + cookie
    # instead of a full JWT. The caller completes the flow via
    # ``POST /api/v1/auth/2fa/verify``.
    from backend.app.models.settings import Settings as _Settings
    from backend.app.models.user_totp import UserTOTP

    totp_row = (await db.execute(select(UserTOTP).where(UserTOTP.user_id == user.id))).scalar_one_or_none()
    totp_enabled = totp_row is not None and totp_row.is_enabled

    email_2fa_row = (
        await db.execute(select(_Settings).where(_Settings.key == f"user_{user.id}_email_2fa_enabled"))
    ).scalar_one_or_none()
    email_otp_enabled = email_2fa_row is not None and email_2fa_row.value.lower() == "true" and user.email is not None

    if totp_enabled or email_otp_enabled:
        from backend.app.api.routes.mfa import create_pre_auth_token

        challenge_id = secrets.token_urlsafe(32)
        pre_auth_token = await create_pre_auth_token(db, user.username, challenge_id=challenge_id)
        response.set_cookie(
            key="2fa_challenge",
            value=challenge_id,
            httponly=True,
            secure=raw_request.url.scheme == "https",
            samesite="lax",
            max_age=300,
            path="/api/v1/auth/2fa",
        )
        methods: list[str] = []
        if totp_enabled:
            methods.append("totp")
        if email_otp_enabled:
            methods.append("email")
        if totp_enabled:
            methods.append("backup")
        return LoginResponse(
            requires_2fa=True,
            pre_auth_token=pre_auth_token,
            two_fa_methods=methods,
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.username}, expires_delta=access_token_expires)

    # Sliding-session refresh cookie — §18.14. Issued alongside every access
    # token from a password login so the frontend can transparently refresh
    # the short-lived JWT without bouncing the operator through /login
    # every hour. Explicit commit — the test harness's get_db override
    # doesn't auto-commit, so flushing alone would roll back on session close.
    await _issue_refresh_cookie(
        db,
        response,
        raw_request,
        username=user.username,
        remember_me=request.remember_me,
    )
    await db.commit()

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=_user_to_response(user),
    )


@router.post("/refresh", response_model=LoginResponse)
async def refresh_access_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Sliding-session refresh (§18.14).

    Reads the ``bamdude_refresh`` HttpOnly cookie, validates it, and if OK
    rotates: the old refresh row is marked ``used_at=now`` and a new refresh
    is issued inside the same family. The new refresh is set as the cookie
    on the response; a freshly-minted access token is returned in the JSON
    body.

    Reuse detection — if the incoming token is already-used, it's a replay
    (stolen cookie or a race condition in the client that survived through
    the coalescing guard). The whole family is revoked so every sibling
    refresh dies at once; caller gets a 401 and the frontend drops to
    /login. OWASP refresh-token rotation.

    Invalid / expired / missing cookies → 401 without family side effects.

    Remember-me is preserved across rotations: if the original login had a
    30-day cookie, each rotation keeps the ``remember_me=True`` behaviour;
    otherwise both the DB TTL (12 h) and the cookie (session) stay short.
    Detection heuristic: a cookie that still carries a ``Max-Age`` attribute
    tells the browser it's a persistent cookie, and the original request
    gets a ``max_age`` resolved from the current session's DB expiry — if
    the DB-side expiry of the CURRENT refresh token is more than the
    session TTL, treat as remember-me. Simpler than round-tripping a flag.
    """
    from backend.app.core.auth import REFRESH_TOKEN_EXPIRE_HOURS_SESSION

    raw_refresh = request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    if not raw_refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token",
        )

    # We need the current row's expires_at BEFORE it gets mutated to decide
    # whether this session is "remember-me" or not. The verify-and-consume
    # path doesn't return the expiry, so peek first via the same hash.
    from backend.app.core.auth import _hash_refresh_token
    from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType

    peeked = (
        await db.execute(
            select(AuthEphemeralToken)
            .where(AuthEphemeralToken.token == _hash_refresh_token(raw_refresh))
            .where(AuthEphemeralToken.token_type == TokenType.REFRESH)
        )
    ).scalar_one_or_none()
    was_remember_me = False
    if peeked is not None:
        # Inherit remember-me from the original login: if the refresh row
        # lives longer than the session-cookie TTL, it was created via
        # remember_me=True. Exact-match comparison would break the moment
        # we tweak TTL constants, hence the >-with-slack guard (any refresh
        # with > session TTL + 1 h headroom is definitely a remember-me row).
        session_secs = REFRESH_TOKEN_EXPIRE_HOURS_SESSION * 3600
        lifespan_secs = (peeked.expires_at - peeked.created_at).total_seconds()
        was_remember_me = lifespan_secs > session_secs + 3600

    username, family_id, result_status = await verify_and_consume_refresh_token(db, raw_refresh)

    if result_status == "reuse":
        # Belt-and-braces: even though verify_and_consume_refresh_token
        # already revoked the family, clear the cookie so the client stops
        # resending a dead token on every subsequent call.
        _clear_refresh_cookie(response, request)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token replay detected; session revoked",
        )
    if result_status == "invalid" or not username:
        _clear_refresh_cookie(response, request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # Load the user to mint a fresh access token — and so the response body
    # can carry a current snapshot of the user's permissions (UI may refresh
    # stale permission caches on sliding-refresh, not just on login).
    user_row = (
        await db.execute(select(User).where(User.username == username).options(selectinload(User.groups)))
    ).scalar_one_or_none()
    if user_row is None or not user_row.is_active:
        _clear_refresh_cookie(response, request)
        if family_id:
            await revoke_refresh_family(db, family_id)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account unavailable",
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    new_access = create_access_token(data={"sub": user_row.username}, expires_delta=access_token_expires)

    # Rotate within the same family so reuse detection sees the lineage.
    await _issue_refresh_cookie(
        db,
        response,
        request,
        username=user_row.username,
        remember_me=was_remember_me,
        family_id=family_id,
    )

    await db.commit()

    return LoginResponse(
        access_token=new_access,
        token_type="bearer",
        user=_user_to_response(user_row),
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get current user information.

    Accepts JWT tokens (via Authorization: Bearer header) and API keys
    (via X-API-Key header or Authorization: Bearer bb_xxx).
    API keys return a synthetic admin user with all permissions.
    """
    import jwt
    from jwt.exceptions import PyJWTError as JWTError

    # Check for API key via X-API-Key header
    if x_api_key:
        api_key = await _validate_api_key(db, x_api_key)
        if api_key:
            return _api_key_to_user_response(api_key)

    # Check for Bearer token (could be JWT or API key)
    if credentials is not None:
        token = credentials.credentials
        # Check if it's an API key (starts with bb_)
        if token.startswith("bb_"):
            api_key = await _validate_api_key(db, token)
            if api_key:
                return _api_key_to_user_response(api_key)
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

        jti = payload.get("jti")
        if jti and await is_jti_revoked(jti):
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
        if not _is_token_fresh(payload.get("iat"), user):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Reload with groups for proper permission calculation
        result = await db.execute(select(User).where(User.id == user.id).options(selectinload(User.groups)))
        user = result.scalar_one()
        return _user_to_response(user)

    # No credentials provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
):
    """Logout — revoke the caller's JWT ``jti`` + refresh-token family (§18.4 + §18.14).

    Accepts the bearer token via the standard ``Authorization`` header. Validates
    the signature without enforcing ``exp`` (expired tokens still get their jti
    blacklisted) so a user who logs out after their token expired can't have
    anyone replay that token within our revocation window.

    Since §18.14 the logout also clears the sliding-session refresh cookie and
    revokes every sibling token in the same family so a stolen cookie can't
    outlive the click-to-logout moment. Other devices logged in under
    different ``family_id``s stay alive — logout is single-session by design.
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer ") :]
        try:
            payload = jwt.decode(
                token,
                SECRET_KEY,
                algorithms=[ALGORITHM],
                options={"verify_exp": False},
            )
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti:
                # Revoke until the token's original expiry so the blacklist doesn't
                # grow forever. After exp the JWT is dead regardless of the row.
                expires_at = (
                    datetime.fromtimestamp(exp, tz=timezone.utc)
                    if exp
                    else datetime.now(timezone.utc) + timedelta(hours=24)
                )
                await revoke_jti(jti, expires_at, username=payload.get("sub"))
        except JWTError:
            # Malformed / signature-invalid token — nothing to revoke.
            pass

    # Revoke the current refresh family + clear the cookie. Only touches the
    # family whose token the browser is holding; leaves other devices alone.
    raw_refresh = request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    if raw_refresh:
        from backend.app.core.auth import _hash_refresh_token
        from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType

        row = (
            await db.execute(
                select(AuthEphemeralToken)
                .where(AuthEphemeralToken.token == _hash_refresh_token(raw_refresh))
                .where(AuthEphemeralToken.token_type == TokenType.REFRESH)
            )
        ).scalar_one_or_none()
        if row is not None and row.family_id:
            await revoke_refresh_family(db, row.family_id)
        await db.commit()
    _clear_refresh_cookie(response, request)

    return {"message": "Logged out successfully"}


# Advanced Authentication Endpoints


@router.post("/smtp/test", response_model=TestSMTPResponse)
async def test_smtp_connection(
    test_request: TestSMTPRequest,
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Test SMTP connection using saved settings (admin only when auth enabled)."""
    import logging

    logger = logging.getLogger(__name__)

    try:
        smtp_settings = await get_smtp_settings(db)
        if not smtp_settings:
            return TestSMTPResponse(success=False, message="SMTP settings not configured. Save SMTP settings first.")

        # Send test email
        send_email(
            smtp_settings=smtp_settings,
            to_email=test_request.test_recipient,
            subject="BamBuddy SMTP Test",
            body_text="This is a test email from BamBuddy. If you received this, your SMTP settings are working correctly!",
            body_html="<p>This is a test email from <strong>BamBuddy</strong>.</p><p>If you received this, your SMTP settings are working correctly!</p>",
        )

        logger.info(f"Test email sent successfully to {test_request.test_recipient}")
        return TestSMTPResponse(success=True, message="Test email sent successfully")
    except Exception as e:
        logger.error("Failed to send test email: %s", e)
        # Note: `message` is surfaced to the admin in the Test SMTP dialog on purpose —
        # that endpoint is admin-only and the exception text is the diagnostic user needs.
        return TestSMTPResponse(success=False, message=f"Failed to send test email: {str(e)}")


@router.get("/smtp", response_model=SMTPSettings | None)
async def get_smtp_config(
    current_user: User | None = RequirePermission(Permission.SETTINGS_READ),
    db: AsyncSession = Depends(get_db),
):
    """Get SMTP settings (admin only when auth enabled). Password is not returned."""
    smtp_settings = await get_smtp_settings(db)
    if smtp_settings:
        # Don't return password in response
        smtp_settings.smtp_password = None
    return smtp_settings


@router.post("/smtp", response_model=dict)
async def save_smtp_config(
    smtp_settings: SMTPSettings,
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Save SMTP settings (admin only when auth enabled)."""
    import logging

    logger = logging.getLogger(__name__)

    try:
        await save_smtp_settings(db, smtp_settings)
        await db.commit()
        logger.info(f"SMTP settings updated by admin user: {current_user.username if current_user else 'anonymous'}")
        return {"message": "SMTP settings saved successfully"}
    except Exception as e:
        await db.rollback()
        logger.error("Failed to save SMTP settings: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save SMTP settings",
        )


@router.post("/advanced-auth/enable", response_model=dict)
async def enable_advanced_auth(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Enable advanced authentication (admin only).

    Requires SMTP settings to be configured and tested first.
    """
    import logging

    logger = logging.getLogger(__name__)

    # Reload user with groups for proper is_admin check
    result = await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    user = result.scalar_one()

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can enable advanced authentication",
        )

    # Verify SMTP settings are configured
    smtp_settings = await get_smtp_settings(db)
    if not smtp_settings:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP settings must be configured before enabling advanced authentication",
        )

    try:
        await set_advanced_auth_enabled(db, True)
        await db.commit()
        logger.info(f"Advanced authentication enabled by admin user: {user.username}")
        return {"message": "Advanced authentication enabled successfully", "advanced_auth_enabled": True}
    except Exception as e:
        await db.rollback()
        logger.error("Failed to enable advanced authentication: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enable advanced authentication",
        )


@router.post("/advanced-auth/disable", response_model=dict)
async def disable_advanced_auth(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Disable advanced authentication (admin only)."""
    import logging

    logger = logging.getLogger(__name__)

    # Reload user with groups for proper is_admin check
    result = await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    user = result.scalar_one()

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can disable advanced authentication",
        )

    try:
        await set_advanced_auth_enabled(db, False)
        await db.commit()
        logger.info(f"Advanced authentication disabled by admin user: {user.username}")
        return {"message": "Advanced authentication disabled successfully", "advanced_auth_enabled": False}
    except Exception as e:
        await db.rollback()
        logger.error("Failed to disable advanced authentication: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disable advanced authentication",
        )


@router.get("/advanced-auth/status")
async def get_advanced_auth_status(db: AsyncSession = Depends(get_db)):
    """Get advanced authentication status."""
    advanced_auth_enabled = await is_advanced_auth_enabled(db)
    smtp_configured = await get_smtp_settings(db) is not None
    return {
        "advanced_auth_enabled": advanced_auth_enabled,
        "smtp_configured": smtp_configured,
    }


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(
    request: ForgotPasswordRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Request password reset via email (advanced auth only).

    §18.5 rate limiting: 3/15 min per email address + 10/15 min per client IP.
    Buckets recorded on every call (not just failures) because the endpoint is
    public and intentionally returns success even for nonexistent emails
    (anti-enumeration) — a per-email counter without this would let an
    attacker mass-trigger sends as long as each email is unique.
    """
    import logging

    from backend.app.core.rate_limit import (
        MAX_PASSWORD_RESET_PER_IP,
        MAX_PASSWORD_RESET_PER_USERNAME,
        check_rate_limit,
        record_failed_attempt,
    )
    from backend.app.models.auth_ephemeral import EventType

    logger = logging.getLogger(__name__)

    client_ip = _get_client_ip(raw_request)
    await check_rate_limit(
        db, request.email, event_type=EventType.PASSWORD_RESET_SEND, max_attempts=MAX_PASSWORD_RESET_PER_USERNAME
    )
    await check_rate_limit(
        db, client_ip, event_type=EventType.PASSWORD_RESET_IP, max_attempts=MAX_PASSWORD_RESET_PER_IP
    )
    # Record the event eagerly — see docstring for the anti-enumeration rationale.
    await record_failed_attempt(db, request.email, event_type=EventType.PASSWORD_RESET_SEND)
    await record_failed_attempt(db, client_ip, event_type=EventType.PASSWORD_RESET_IP)

    # Check if advanced auth is enabled
    advanced_auth = await is_advanced_auth_enabled(db)
    if not advanced_auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Advanced authentication is not enabled",
        )

    # Get SMTP settings
    smtp_settings = await get_smtp_settings(db)
    if not smtp_settings:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service is not configured",
        )

    # Find user by email
    user = await get_user_by_email(db, request.email)

    # Always return success message to prevent email enumeration
    # but only send email if user exists and is not an LDAP user
    if user and user.is_active and user.auth_source != "ldap":
        try:
            # Generate new password
            new_password = generate_secure_password()
            user.password_hash = get_password_hash(new_password)
            user.password_changed_at = datetime.now(timezone.utc)  # §18.4: invalidate existing JWTs
            # §18.14: all sliding-session refresh tokens for this user die too,
            # so every other device the user was logged in on bounces to /login
            # after the next refresh attempt. Without this the old refresh cookie
            # would keep minting fresh access tokens against a rotated password.
            await revoke_all_refresh_tokens_for_user(db, user.username)
            await db.commit()

            login_url = await get_external_login_url(db)

            # Send password reset email
            subject, text_body, html_body = await create_password_reset_email_from_template(
                db, user.username, new_password, login_url
            )
            send_email(smtp_settings, user.email, subject, text_body, html_body)

            logger.info(f"Password reset email sent to {user.email}")
        except Exception as e:
            logger.error("Failed to send password reset email: %s", e)
            # Don't reveal error to user for security

    return ForgotPasswordResponse(
        message="If the email address is associated with an account, a password reset email has been sent."
    )


@router.post("/reset-password", response_model=ResetPasswordResponse)
async def reset_user_password(
    request: ResetPasswordRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset a user's password and send them an email (admin only, advanced auth only)."""
    import logging

    logger = logging.getLogger(__name__)

    # Reload user with groups for proper is_admin check
    result = await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    admin_user = result.scalar_one()

    if not admin_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can reset user passwords",
        )

    # Check if advanced auth is enabled
    advanced_auth = await is_advanced_auth_enabled(db)
    if not advanced_auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Advanced authentication is not enabled",
        )

    # Get SMTP settings
    smtp_settings = await get_smtp_settings(db)
    if not smtp_settings:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service is not configured",
        )

    # Find user to reset
    result = await db.execute(select(User).where(User.id == request.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.auth_source == "ldap":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reset password for LDAP users - passwords are managed by the LDAP server",
        )

    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have an email address configured",
        )

    try:
        # Generate new password
        new_password = generate_secure_password()
        user.password_hash = get_password_hash(new_password)
        user.password_changed_at = datetime.now(timezone.utc)  # §18.4: invalidate existing JWTs
        # §18.14: kill all the user's sliding-session refresh tokens too (same
        # reasoning as the forgot-password branch above).
        await revoke_all_refresh_tokens_for_user(db, user.username)
        await db.commit()

        login_url = await get_external_login_url(db)

        # Send password reset email
        subject, text_body, html_body = await create_password_reset_email_from_template(
            db, user.username, new_password, login_url
        )
        send_email(smtp_settings, user.email, subject, text_body, html_body)

        logger.info(f"Password reset by admin {admin_user.username} for user {user.username}")
        return ResetPasswordResponse(message=f"Password reset email sent to {user.email}")
    except Exception as e:
        await db.rollback()
        logger.error("Failed to reset password for user %s: %s", user.username, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password",
        )


# LDAP Authentication Helpers


async def _get_ldap_settings(db: AsyncSession) -> dict[str, str] | None:
    """Get LDAP settings from the database. Returns None if LDAP is not enabled."""
    ldap_keys = [
        "ldap_enabled",
        "ldap_server_url",
        "ldap_bind_dn",
        "ldap_bind_password",
        "ldap_search_base",
        "ldap_user_filter",
        "ldap_security",
        "ldap_group_mapping",
        "ldap_auto_provision",
        "ldap_ca_cert_path",
        "ldap_default_group",
    ]
    result = await db.execute(select(Settings).where(Settings.key.in_(ldap_keys)))
    settings = {s.key: s.value for s in result.scalars().all()}
    if settings.get("ldap_enabled", "false").lower() != "true":
        return None
    return settings


async def _provision_ldap_user(db: AsyncSession, ldap_user, ldap_config) -> User:
    """Create a new local user from LDAP authentication."""
    import logging

    from backend.app.services.ldap_service import resolve_group_mapping

    logger = logging.getLogger(__name__)

    new_user = User(
        username=ldap_user.username,
        email=ldap_user.email,
        password_hash=None,
        role="user",
        auth_source="ldap",
        is_active=True,
    )

    # Map LDAP groups to BamDude groups, falling back to the configured default group
    # when the user is authenticated but has no matching group mapping.
    mapped_group_names = resolve_group_mapping(ldap_user.groups, ldap_config.group_mapping)
    if not mapped_group_names and ldap_config.default_group:
        mapped_group_names = [ldap_config.default_group]
        logger.warning(
            "LDAP user %s has no mapped groups - assigning configured default group '%s'",
            ldap_user.username,
            ldap_config.default_group,
        )
    if mapped_group_names:
        groups_result = await db.execute(select(Group).where(Group.name.in_(mapped_group_names)))
        new_user.groups = list(groups_result.scalars().all())

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    logger.info("Auto-provisioned LDAP user: %s (groups: %s)", new_user.username, mapped_group_names)
    return new_user


async def _sync_ldap_user(db: AsyncSession, user: User, ldap_user, ldap_config) -> None:
    """Sync LDAP user attributes (email, groups) on each login."""
    import logging

    from backend.app.services.ldap_service import resolve_group_mapping

    logger = logging.getLogger(__name__)

    changed = False

    # Update email if changed
    if ldap_user.email and ldap_user.email != user.email:
        user.email = ldap_user.email
        changed = True

    # Sync group mappings - always update to match LDAP state (including revocation).
    # Fall back to the configured default group when the user has no mapped groups,
    # so authenticated LDAP users are never left permission-less.
    mapped_group_names = resolve_group_mapping(ldap_user.groups, ldap_config.group_mapping)
    if not mapped_group_names and ldap_config.default_group:
        mapped_group_names = [ldap_config.default_group]
        logger.warning(
            "LDAP user %s has no mapped groups - assigning configured default group '%s'",
            user.username,
            ldap_config.default_group,
        )
    if mapped_group_names:
        groups_result = await db.execute(select(Group).where(Group.name.in_(mapped_group_names)))
        new_groups = list(groups_result.scalars().all())
    else:
        new_groups = []
    current_group_ids = {g.id for g in user.groups}
    new_group_ids = {g.id for g in new_groups}
    if current_group_ids != new_group_ids:
        user.groups = new_groups
        changed = True

    if changed:
        await db.commit()
        logger.info("Synced LDAP user attributes: %s", user.username)


@router.post("/ldap/test")
async def test_ldap(
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Test LDAP connection using saved settings (admin only when auth enabled)."""
    import logging

    from backend.app.services.ldap_service import parse_ldap_config, test_ldap_connection

    logger = logging.getLogger(__name__)

    ldap_settings = await _get_ldap_settings(db)
    if not ldap_settings:
        # LDAP might not be enabled yet but settings might still exist - read all keys
        ldap_keys = [
            "ldap_enabled",
            "ldap_server_url",
            "ldap_bind_dn",
            "ldap_bind_password",
            "ldap_search_base",
            "ldap_user_filter",
            "ldap_security",
            "ldap_group_mapping",
            "ldap_auto_provision",
            "ldap_default_group",
        ]
        result = await db.execute(select(Settings).where(Settings.key.in_(ldap_keys)))
        ldap_settings = {s.key: s.value for s in result.scalars().all()}
        # Force enabled for test
        ldap_settings["ldap_enabled"] = "true"

    config = parse_ldap_config(ldap_settings)
    if not config:
        return {"success": False, "message": "LDAP server URL is not configured"}

    success, message = test_ldap_connection(config)
    if success:
        logger.info("LDAP connection test successful")
    else:
        logger.warning("LDAP connection test failed: %s", message)
    return {"success": success, "message": message}


@router.get("/ldap/status")
async def get_ldap_status(db: AsyncSession = Depends(get_db)):
    """Get LDAP authentication status."""
    # Only fetch the minimum keys needed - never load secrets
    ldap_keys = ["ldap_enabled", "ldap_server_url"]
    result = await db.execute(select(Settings).where(Settings.key.in_(ldap_keys)))
    settings = {s.key: s.value for s in result.scalars().all()}
    return {
        "ldap_enabled": settings.get("ldap_enabled", "false").lower() == "true",
        "ldap_configured": bool(settings.get("ldap_server_url")),
    }


# =============================================================================
# Long-lived camera-stream tokens (#1108)
# =============================================================================
# Camera-only V1. Issue scope: a token a user can paste into Home Assistant /
# Frigate / a kiosk and have it keep working for days/weeks rather than
# refreshing the 60-minute ephemeral token. Permission gate: CAMERA_VIEW
# (same blast radius as the existing 60-min token-mint endpoint).


def _long_lived_token_to_response(record, *, plaintext: str | None = None) -> dict:
    """Serialise a LongLivedToken row for the SPA. Plaintext is included
    only at create time (and then never again), per the issue's "shown once"
    contract.
    """
    return {
        "id": record.id,
        "user_id": record.user_id,
        "name": record.name,
        "scope": record.scope,
        "lookup_prefix": record.lookup_prefix,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        # Plaintext is the ONLY field the user ever sees in full — copied once
        # to a clipboard / kiosk config and then forgotten.
        "token": plaintext,
    }


@router.post("/tokens", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_long_lived_camera_token(
    payload: dict,
    current_user: User = RequirePermission(Permission.CAMERA_VIEW),
    db: AsyncSession = Depends(get_db),
):
    """Mint a long-lived camera-stream token (#1108).

    Body: ``{"name": str, "expires_in_days": int, "scope": "camera_stream"}``.

    The plaintext token is returned **exactly once** in the response. The DB
    only ever stores a pbkdf2 hash, so a leaked DB dump cannot replay the
    token. Hard cap of 365 days; the issue's ``expire_in: 0`` (never) is
    explicitly rejected.
    """
    import logging as _logging

    from backend.app.services.long_lived_tokens import (
        ALLOWED_SCOPES,
        MAX_TOKEN_LIFETIME_DAYS,
        create_token,
    )

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    expires_in_days = payload.get("expires_in_days")
    if not isinstance(expires_in_days, int) or expires_in_days <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"expires_in_days must be a positive integer (max {MAX_TOKEN_LIFETIME_DAYS}; #1108: no infinite tokens)"
            ),
        )
    scope = payload.get("scope", "camera_stream")
    if scope not in ALLOWED_SCOPES:
        raise HTTPException(status_code=400, detail=f"unsupported scope: {scope!r}")

    try:
        created = await create_token(
            db,
            user_id=current_user.id,
            name=name,
            expires_in_days=expires_in_days,
            scope=scope,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _logging.getLogger(__name__).info(
        "Long-lived camera token created: user=%s name=%r scope=%s expires=%s",
        current_user.username,
        name,
        scope,
        created.record.expires_at.isoformat(),
    )
    return _long_lived_token_to_response(created.record, plaintext=created.plaintext)


@router.get("/tokens", response_model=list[dict])
async def list_long_lived_tokens(
    user_id: int | None = None,
    current_user: User = RequirePermission(Permission.CAMERA_VIEW),
    db: AsyncSession = Depends(get_db),
):
    """List long-lived tokens.

    Default: caller's own tokens.
    Admins can pass ``?user_id=N`` to see another user's tokens, or omit it
    to see everything (handy for leak triage).
    """
    from backend.app.services.long_lived_tokens import list_user_tokens

    # Reload with groups so is_admin reflects group membership reliably.
    user_with_groups = (
        await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    ).scalar_one()

    if user_id is None or user_id == current_user.id:
        records = await list_user_tokens(db, current_user.id)
    elif user_with_groups.is_admin:
        records = await list_user_tokens(db, user_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can list other users' tokens",
        )
    return [_long_lived_token_to_response(r) for r in records]


@router.get("/tokens/all", response_model=list[dict])
async def list_all_long_lived_tokens(
    current_user: User = RequirePermission(Permission.CAMERA_VIEW),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: every active long-lived token in the system, newest first.
    Used by the leak-triage view in admin settings.
    """
    from backend.app.services.long_lived_tokens import list_all_tokens

    user_with_groups = (
        await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    ).scalar_one()
    if not user_with_groups.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin only",
        )
    records = await list_all_tokens(db)
    return [_long_lived_token_to_response(r) for r in records]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_long_lived_token(
    token_id: int,
    current_user: User = RequirePermission(Permission.CAMERA_VIEW),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a long-lived token. Owners can revoke their own; admins any."""
    import logging as _logging

    from backend.app.models.long_lived_token import LongLivedToken
    from backend.app.services.long_lived_tokens import revoke_token

    record = (await db.execute(select(LongLivedToken).where(LongLivedToken.id == token_id))).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Token not found")

    if record.user_id != current_user.id:
        # Reload for is_admin so admins can revoke any user's token (leak response).
        user_with_groups = (
            await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
        ).scalar_one()
        if not user_with_groups.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only revoke your own tokens",
            )

    revoked = await revoke_token(db, token_id)
    if not revoked:
        # Already revoked is treated as 404 for idempotency from the UI side.
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    _logging.getLogger(__name__).info(
        "Long-lived camera token revoked: id=%d by user=%s",
        token_id,
        current_user.username,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
