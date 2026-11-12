"""2FA (TOTP + Email OTP) and OIDC authentication routes.

Security model
--------------
* Pre-auth tokens  : secrets.token_urlsafe(32) stored with immutable account identity/generation in the DB for five minutes.
  They are single-use and do NOT grant access to any protected resource.
* TOTP codes       : verified with pyotp (30-second window, ±1 step tolerance).
* Email OTP codes  : 6-digit numeric, hashed with pbkdf2_sha256, 10-minute TTL,
  max 5 failed attempts per code before invalidation.
* Backup codes     : 10 × 8-char alphanumeric codes, each stored as pbkdf2_sha256 hash,
  single-use.
* OIDC state       : secrets.token_urlsafe(32) bound to provider_id + nonce, 10-minute TTL.
* OIDC exchange    : secrets.token_urlsafe(32), 2-minute TTL, single-use.
* Rate limiting    : max 5 failed 2FA verification attempts per user within 15 minutes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import secrets
import string
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import jwt
import pyotp
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from jwt import PyJWKClient
from passlib.context import CryptContext
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, undefer

from backend.app.api.routes._oidc_helpers import assert_safe_public_https_url
from backend.app.api.routes.settings import get_setting, set_setting
from backend.app.core.auth import (
    AUTH_DATABASE_FAILURES,
    RequireAdmin,
    RequirePermission,
    get_auth_db,
    get_current_active_user,
    get_user_by_email,
    get_user_by_username,
    hash_password_async,
    verify_password_async,
)
from backend.app.core.auth_lifetime import (
    FrozenAuthority,
    VerifiedOutcome,
    binding,
    binding_conditions,
    capture_account,
    capture_ephemeral,
    guard_accounts,
    refused,
    stage_end_sessions,
    utc_naive,
    utcnow,
    validate_captured,
)
from backend.app.core.auth_runtime import auth_runtime
from backend.app.core.credential_config import canonical_url, lock_credential_config, merge_secret_fields
from backend.app.core.database import take_write_lock
from backend.app.core.oidc_env import env_bool
from backend.app.core.permissions import Permission
from backend.app.models.auth_ephemeral import AuthEphemeralToken, AuthRateLimitEvent, EventType, TokenType
from backend.app.models.group import Group
from backend.app.models.oidc_provider import OIDCProvider, UserOIDCLink
from backend.app.models.user import User
from backend.app.models.user_otp_code import UserOTPCode
from backend.app.models.user_totp import UserTOTP
from backend.app.schemas.auth import (
    AUTO_LINK_REQUIREMENTS_ERROR,
    AdminDisable2FARequest,
    BackupCodesResponse,
    EmailOTPDisableRequest,
    EmailOTPEnableConfirmRequest,
    EmailOTPSendRequest,
    GroupBrief,
    LoginResponse,
    OIDCAuthorizeResponse,
    OIDCExchangeRequest,
    OIDCLinkResponse,
    OIDCProviderCreate,
    OIDCProviderPublicResponse,
    OIDCProviderResponse,
    OIDCProviderUpdate,
    TOTPDisableRequest,
    TOTPEnableRequest,
    TOTPEnableResponse,
    TOTPSetupRequest,
    TOTPSetupResponse,
    TwoFAStatusResponse,
    TwoFAVerifyRequest,
    TwoFAVerifyResponse,
    UserResponse,
)
from backend.app.services.email_service import get_smtp_settings, send_email
from backend.app.services.oidc_icon import OIDCIconError, fetch_icon

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """Return *dt* with UTC timezone attached.

    SQLite/aiosqlite strips timezone info when reading DateTime(timezone=True)
    columns back – the stored value is always UTC, so we just re-attach the
    info when doing Python-level comparisons.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Passlib context (same scheme as auth.py)
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# ---------------------------------------------------------------------------
# TTL / rate-limit constants
# ---------------------------------------------------------------------------
MAX_2FA_ATTEMPTS = 5
MAX_LOGIN_ATTEMPTS = 10
LOCKOUT_WINDOW = timedelta(minutes=15)
MAX_EMAIL_OTP_SENDS = 3
EMAIL_OTP_SEND_WINDOW = timedelta(minutes=10)
PRE_AUTH_TOKEN_TTL = timedelta(minutes=5)
OIDC_STATE_TTL = timedelta(minutes=10)
OIDC_EXCHANGE_TTL = timedelta(minutes=2)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/auth", tags=["2fa", "oidc"])


# ---------------------------------------------------------------------------
# Helper: user response
# ---------------------------------------------------------------------------
def _user_to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        is_admin=user.is_admin,
        auth_source=getattr(user, "_auth_source", None) or getattr(user, "auth_source", "local") or "local",
        groups=[GroupBrief(id=g.id, name=g.name) for g in user.groups],
        permissions=sorted(user.get_permissions()),
        created_at=user.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Helper: QR code generation
# ---------------------------------------------------------------------------
def _generate_totp_qr_b64(provisioning_uri: str) -> str:
    """Generate a base64-encoded PNG QR code for the given TOTP provisioning URI."""
    import qrcode  # type: ignore

    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Helper: backup code generation
# ---------------------------------------------------------------------------
def _generate_backup_codes() -> tuple[list[str], list[str]]:
    """Return (plain_codes, hashed_codes) — 10 codes of 8 alphanumeric chars each."""
    alphabet = string.ascii_uppercase + string.digits
    plain = ["".join(secrets.choice(alphabet) for _ in range(8)) for _ in range(10)]
    hashed = [pwd_context.hash(c) for c in plain]
    return plain, hashed


async def _generate_backup_codes_async() -> tuple[list[str], list[str]]:
    alphabet = string.ascii_uppercase + string.digits
    plain = ["".join(secrets.choice(alphabet) for _ in range(8)) for _ in range(10)]
    # Each real hash uses the same bounded owner as password/key work. Caller
    # cancellation never frees a slot before that hash actually finishes.
    hashed = [await hash_password_async(code) for code in plain]
    return plain, hashed


# ---------------------------------------------------------------------------
# DB-backed pre-auth token helpers
# ---------------------------------------------------------------------------
async def create_pre_auth_token(db, username, challenge_id=None, *, remember_me=None, verified: VerifiedOutcome):
    capture = FrozenAuthority(
        "pending", verified.account.account_identity, verified.account.user_id, verified.account.generation
    )
    proof = await guard_accounts(db, [capture])
    principal = await validate_captured(db, capture, proof=proof)
    if verified.auth_source not in {"local", "ldap", "oidc"}:
        raise refused()
    token = secrets.token_urlsafe(32)
    db.add(
        AuthEphemeralToken(
            token=token,
            token_type=TokenType.PRE_AUTH,
            username=principal.user.username,
            challenge_id=challenge_id,
            remember_me=remember_me,
            auth_source=verified.auth_source,
            expires_at=utcnow() + PRE_AUTH_TOKEN_TTL,
            **binding(capture),
        )
    )
    await db.commit()
    return token


async def consume_pre_auth_token(db, token, challenge_id=None):
    row = await _peek_pre_auth_record(db, token, challenge_id)
    if row is None:
        return None
    capture = capture_ephemeral(row, pending=True)
    proof = await guard_accounts(db, [capture])
    principal = await validate_captured(db, capture, proof=proof)
    result = await db.execute(
        delete(AuthEphemeralToken)
        .where(
            AuthEphemeralToken.id == row.id,
            AuthEphemeralToken.token == token,
            AuthEphemeralToken.token_type == TokenType.PRE_AUTH,
            AuthEphemeralToken.expires_at > utcnow(),
            *binding_conditions(capture),
        )
        .returning(AuthEphemeralToken.id)
    )
    return principal.user.username if result.scalar_one_or_none() is not None else None


async def _peek_pre_auth_record(db, token, challenge_id=None):
    row = (
        await db.execute(
            select(AuthEphemeralToken).where(
                AuthEphemeralToken.token == token,
                AuthEphemeralToken.token_type == TokenType.PRE_AUTH,
                AuthEphemeralToken.expires_at > utcnow(),
            )
        )
    ).scalar_one_or_none()
    if row is None or (row.challenge_id is not None and row.challenge_id != challenge_id):
        return None
    try:
        principal = await validate_captured(db, capture_ephemeral(row, pending=True))
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return None
    if row.auth_source not in {"local", "ldap", "oidc"}:
        return None
    row._principal = principal
    return row


async def peek_pre_auth_token(db: AsyncSession, token: str, challenge_id: str | None = None) -> str | None:
    """Compatibility helper for callers that only need the validated username."""
    record = await _peek_pre_auth_record(db, token, challenge_id)
    return record.username if record is not None else None


# ---------------------------------------------------------------------------
# DB-backed rate-limiting helpers
# ---------------------------------------------------------------------------
async def check_rate_limit(
    db: AsyncSession,
    username: str,
    event_type: str = EventType.TWO_FA_ATTEMPT,
    max_attempts: int = MAX_2FA_ATTEMPTS,
) -> None:
    """Raise HTTP 429 if the user has exceeded the failed attempt limit.

    The username is normalised to lower-case so case-variant attempts
    (which all resolve to the same user) share the same rate-limit bucket.

    L-2: Known TOCTOU — the SELECT (count) and the subsequent INSERT
    (record_failed_attempt) are not atomic.  Two concurrent requests can both
    read a count below the threshold and both proceed.  This is an inherent
    trade-off of the event-log rate-limit pattern: fixing it would require
    a serialising lock (SELECT FOR UPDATE on a dedicated counter row), which
    adds contention and is not worth it for a soft rate-limit whose window is
    already measured in minutes.  In practice the race window is microseconds
    and the limit can be slightly exceeded only under precise concurrent timing.
    """
    username_key = username.lower()
    now = utcnow()
    cutoff = now - LOCKOUT_WINDOW
    result = await db.execute(
        select(AuthRateLimitEvent).where(
            AuthRateLimitEvent.username == username_key,
            AuthRateLimitEvent.event_type == event_type,
            AuthRateLimitEvent.occurred_at > cutoff,
        )
    )
    recent_count = len(result.scalars().all())
    if recent_count >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Please try again later.",
        )


async def record_failed_attempt(db: AsyncSession, username: str, event_type: str = EventType.TWO_FA_ATTEMPT) -> None:
    """Record a failed attempt for rate-limiting purposes."""
    db.add(AuthRateLimitEvent(username=username.lower(), event_type=event_type))
    await db.commit()


async def clear_failed_attempts(db: AsyncSession, username: str, event_type: str = EventType.TWO_FA_ATTEMPT) -> None:
    """Delete all recorded failed attempts for a user on successful verification."""
    await db.execute(
        delete(AuthRateLimitEvent).where(
            AuthRateLimitEvent.username == username.lower(),
            AuthRateLimitEvent.event_type == event_type,
        )
    )
    await db.commit()


async def check_email_otp_send_rate(db: AsyncSession, username: str) -> None:
    """Raise HTTP 429 if the user has requested too many OTP emails recently.

    I1: This function only *checks* the limit.  The caller is responsible for
    recording the slot via ``record_email_otp_send`` **after** the email has
    been sent successfully.  This prevents failed sends from consuming a slot
    (wasting the user's quota) and makes it impossible to farm rate-limit events
    without actually triggering a send.
    """
    username_key = username.lower()
    now = utcnow()
    cutoff = now - EMAIL_OTP_SEND_WINDOW
    result = await db.execute(
        select(AuthRateLimitEvent).where(
            AuthRateLimitEvent.username == username_key,
            AuthRateLimitEvent.event_type == EventType.EMAIL_SEND,
            AuthRateLimitEvent.occurred_at > cutoff,
        )
    )
    recent_count = len(result.scalars().all())
    if recent_count >= MAX_EMAIL_OTP_SENDS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many OTP email requests. Please wait {EMAIL_OTP_SEND_WINDOW.seconds // 60} minutes.",
        )


async def record_email_otp_send(db: AsyncSession, username: str) -> None:
    """Record a successful OTP email send for rate-limiting purposes (I1).

    Must be called *after* the email has been sent successfully so that failed
    sends do not consume a slot from the user's quota.
    """
    db.add(AuthRateLimitEvent(username=username.lower(), event_type=EventType.EMAIL_SEND))
    await db.commit()


# ---------------------------------------------------------------------------
# TOTP replay-protection helper
# ---------------------------------------------------------------------------
def _assert_totp_not_replayed(totp_obj: pyotp.TOTP, totp_record: UserTOTP, code: str) -> None:
    """Raise HTTP 400 if this TOTP code was already accepted in its time window.

    M3 fix: store the counter of the *accepted* code rather than the current
    wall-clock counter.  With valid_window=1, pyotp accepts codes from the
    previous 30-second step.  Using timecode(now) would store the wrong counter
    when the previous-window code is accepted, allowing immediate replay.
    """
    # Determine which time-step the accepted code belongs to.
    now = datetime.now(timezone.utc)
    accepted_counter: int | None = None
    for offset in (0, -1, 1):
        candidate_time = now.timestamp() + offset * totp_obj.interval
        candidate_counter = totp_obj.timecode(datetime.fromtimestamp(candidate_time, tz=timezone.utc))
        if totp_obj.generate_otp(candidate_counter) == code:
            accepted_counter = candidate_counter
            break
    if accepted_counter is None:
        raise HTTPException(400, "Invalid verification code")

    totp_record.accept_counter(accepted_counter)


# ---------------------------------------------------------------------------
# OIDC email-claim resolution helpers (B.16, A.31)
# ---------------------------------------------------------------------------

_EMAIL_SHAPE_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _is_valid_email_shaped(value: str | None) -> bool:
    """Shape-check a claim value before treating it as an email address.

    Used by both Fall A (standard ``email`` claim with require_email_verified)
    and Fall C (custom claim like Azure ``preferred_username``/``upn``).
    Without this, providers that mark numeric user IDs or tenant strings as
    ``email_verified=True`` would slip through the Fall A path.
    """
    if not value or len(value) > 255:
        return False
    return _EMAIL_SHAPE_RE.fullmatch(value) is not None


def _enforce_auto_link_safety(provider: OIDCProvider) -> None:
    """Block the unsafe combo (``auto_link`` + ``email`` claim + ``require_ev=False``).

    Mirrors the Pydantic ``model_validator`` on OIDCProviderCreate/Update — re-runs
    after the route's setattr loop so partial updates spanning two requests can't
    sneak through (each request validates fine alone; only the combined final
    state is unsafe). Raises HTTP 422 with the same error string the frontend
    form already maps for the schema-level rejection.
    """
    if provider.auto_link_existing_accounts and provider.email_claim == "email" and not provider.require_email_verified:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=AUTO_LINK_REQUIREMENTS_ERROR,
        )


def _resolve_provider_email(provider: OIDCProvider, claims: dict, provider_sub: str) -> str | None:
    """Extract the email identity from OIDC ID-token claims for the given provider.

    Three resolution paths:
      Fall C — ``email_claim != "email"`` (custom claim, e.g. Azure
               ``preferred_username``/``upn``): shape-check only, no
               email_verified gate. Recommended Azure configuration.
      Fall A — ``email_claim == "email"`` + ``require_email_verified=True``
               (default): strict, ``email_verified`` must be True.
      Fall B — ``email_claim == "email"`` + ``require_email_verified=False``:
               permissive, explicit ``email_verified=False`` drops the email,
               absent/None keeps it. Required for legacy IdPs that never send
               ``email_verified``; only safe when the operator deliberately
               trusts the provider not to surface unverified emails.

    Returns a lowercased + stripped email string, or None when the claim is
    absent / fails the shape check / fails the verified gate.
    """
    provider_id = provider.id
    raw_claim_value = claims.get(provider.email_claim)
    if raw_claim_value is not None and not isinstance(raw_claim_value, str):
        # Non-string claim (list, int, etc.) — would crash on .lower().
        logger.warning(
            "OIDC provider %d: email_claim %r has unexpected type %s for sub=%r, ignoring",
            provider_id,
            provider.email_claim,
            type(raw_claim_value).__name__,
            provider_sub,
        )
        raw_claim_value = None
    raw_email: str | None = raw_claim_value.lower().strip() if raw_claim_value else None

    if provider.email_claim != "email":
        # Fall C: custom claim — no email_verified gate, just shape-check.
        if raw_email and _is_valid_email_shaped(raw_email):
            return raw_email
        if raw_email:
            logger.warning(
                "OIDC provider %d: email_claim %r value failed shape check for sub=%r, ignoring",
                provider_id,
                provider.email_claim,
                provider_sub,
            )
        return None

    email_verified = claims.get("email_verified")
    if provider.require_email_verified:
        # Fall A: strict — fail closed unless email_verified is explicitly True.
        if raw_email and not _is_valid_email_shaped(raw_email):
            logger.warning(
                "OIDC provider %d: email claim failed shape check for sub=%r, ignoring",
                provider_id,
                provider_sub,
            )
            return None
        if email_verified is True:
            return raw_email
        if raw_email:
            logger.info(
                "OIDC provider %d: ignoring email for sub=%r because email_verified=%r",
                provider_id,
                provider_sub,
                email_verified,
            )
        return None

    # Fall B: permissive — explicit False drops, absent/None keeps.
    if raw_email and not _is_valid_email_shaped(raw_email):
        logger.warning(
            "OIDC provider %d: email claim failed shape check for sub=%r, ignoring",
            provider_id,
            provider_sub,
        )
        return None
    if email_verified is False:
        return None
    if email_verified is not True:
        # Log only when permissive path actually fires (ev absent/None),
        # not on every successful login.
        logger.info(
            "OIDC provider %r (%d): accepting email for sub=%r without email_verified claim (permissive mode)",
            provider.name,
            provider.id,
            provider_sub,
        )
    return raw_email


def _resolve_standard_email_for_user_record(provider: OIDCProvider, claims: dict, provider_sub: str) -> str | None:
    """Resolve the standard ``email`` claim for populating a new ``User.email``.

    Issue #1569: when an operator sets ``email_claim`` to a non-email identity
    claim (e.g. ``preferred_username`` on Authentik), the primary
    ``_resolve_provider_email`` returns ``None`` because the identity value isn't
    email-shaped. This helper lets the auto-create-users path still capture the
    user's real email from the standard ``email`` claim the IdP sends alongside.

    This is NOT a substitute for ``_resolve_provider_email`` and does NOT feed
    ``auto_link_existing_accounts`` — that gate stays on the primary resolver, so
    the GHSA Fall-B/C security guards remain intact. It applies the same
    shape + ``email_verified`` logic as the primary resolver does for the
    standard ``email`` claim.
    """
    raw_claim_value = claims.get("email")
    if raw_claim_value is not None and not isinstance(raw_claim_value, str):
        logger.warning(
            "OIDC provider %d: standard 'email' claim has unexpected type %s for sub=%r, ignoring",
            provider.id,
            type(raw_claim_value).__name__,
            provider_sub,
        )
        return None
    raw_email = raw_claim_value.lower().strip() if raw_claim_value else None
    if not raw_email:
        return None
    if not _is_valid_email_shaped(raw_email):
        logger.warning(
            "OIDC provider %d: standard 'email' claim failed shape check for sub=%r, ignoring",
            provider.id,
            provider_sub,
        )
        return None
    email_verified = claims.get("email_verified")
    if provider.require_email_verified:
        if email_verified is True:
            return raw_email
        logger.info(
            "OIDC provider %d: ignoring fallback email for sub=%r because email_verified=%r",
            provider.id,
            provider_sub,
            email_verified,
        )
        return None
    if email_verified is False:
        return None
    return raw_email


# ---------------------------------------------------------------------------
# Settings helpers (email 2FA flag)
# ---------------------------------------------------------------------------
async def _get_email_2fa_enabled(db: AsyncSession, user_id: int) -> bool:
    val = await get_setting(db, f"user_{user_id}_email_2fa_enabled")
    return val == "true"


async def _set_email_2fa_enabled(db: AsyncSession, user_id: int, enabled: bool) -> None:
    await set_setting(db, f"user_{user_id}_email_2fa_enabled", "true" if enabled else "false")


def _derive_oidc_username_seed(claims: dict, provider_email: str | None, provider_sub: str) -> str:
    """Pick a human-readable username seed from OIDC ID-token claims (#1173).

    Resolution order:
      1. ``provider_email.split("@")[0]`` when an email was resolved (the
         common case for a standard "email" claim or a custom claim that
         happens to carry an email — best effort to keep usernames stable
         across IdPs).
      2. ``preferred_username`` claim — Bambu's recommended Azure config
         and the OIDC standard claim for "what the user knows themselves
         as".
      3. ``name`` claim — fallback for IdPs that omit preferred_username.
      4. ``provider_sub[:30]`` — last-resort opaque seed (the ``oidc_<sha256>``
         shape was never user-friendly, but a sub-derived id is at least
         stable across logins).

    Each candidate is sanitised before it falls through, so a value that
    strips to empty (``"!!!"`` → ``""``) skips to the next candidate
    rather than locking in a useless name. ``isinstance(str)`` guards on
    the claim lookups: misconfigured IdPs sometimes ship lists / numbers
    where strings are expected, and ``.strip()`` would crash without them.
    """
    if provider_email:
        return provider_email.split("@")[0]

    pref = claims.get("preferred_username")
    if isinstance(pref, str):
        seed = re.sub(r"[^a-zA-Z0-9._-]", "", pref.strip())[:30]
        if seed:
            return seed

    name = claims.get("name")
    if isinstance(name, str):
        seed = re.sub(r"[^a-zA-Z0-9._-]", "", name.strip())[:30]
        if seed:
            return seed

    return provider_sub[:30]


# ===========================================================================
# 2FA Endpoints
# ===========================================================================


@router.get("/2fa/status", response_model=TwoFAStatusResponse)
async def get_2fa_status(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
) -> TwoFAStatusResponse:
    """Return the current 2FA configuration for the authenticated user."""
    proof = await guard_accounts(db, [current_user._auth_capture])
    current_user = (await validate_captured(db, current_user._auth_capture, proof=proof)).user
    result = await db.execute(select(UserTOTP).where(UserTOTP.user_id == current_user.id))
    totp_record = result.scalar_one_or_none()

    totp_enabled = totp_record is not None and totp_record.is_enabled
    backup_codes_remaining = len(totp_record.backup_code_hashes) if totp_record else 0
    email_otp_enabled = await _get_email_2fa_enabled(db, current_user.id)

    return TwoFAStatusResponse(
        totp_enabled=totp_enabled,
        email_otp_enabled=email_otp_enabled,
        backup_codes_remaining=backup_codes_remaining,
    )


@router.post("/2fa/totp/setup", response_model=TOTPSetupResponse)
async def setup_totp(
    body: TOTPSetupRequest | None = Body(default=None),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
):
    await check_rate_limit(db, current_user.username)
    existing = (await db.execute(select(UserTOTP).where(UserTOTP.user_id == current_user.id))).scalar_one_or_none()
    expected = existing._secret_enc if existing else None
    was_enabled = bool(existing and existing.is_enabled)
    old_secret = None
    if was_enabled:
        try:
            old_secret = existing.secret
        except RuntimeError:
            raise HTTPException(500, "TOTP secret unavailable") from None
        if not body or not pyotp.TOTP(old_secret).verify(body.code or "", valid_window=1):
            await record_failed_attempt(db, current_user.username)
            raise HTTPException(400, "Current TOTP code required to replace an active authenticator")
    await db.rollback()
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.username, issuer_name="BamDude")
    qr = _generate_totp_qr_b64(uri)
    proof = await guard_accounts(db, [current_user._auth_capture])
    principal = await validate_captured(db, current_user._auth_capture, proof=proof)
    existing = (
        await db.execute(
            select(UserTOTP)
            .where(UserTOTP.user_id == principal.user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (existing._secret_enc if existing else None) != expected or bool(
        existing and existing.is_enabled
    ) != was_enabled:
        raise HTTPException(409, "Authenticator configuration changed")
    if was_enabled:
        _assert_totp_not_replayed(pyotp.TOTP(old_secret), existing, body.code)
    if existing is None:
        existing = UserTOTP(user_id=principal.user.id, secret=secret, is_enabled=False)
        db.add(existing)
    else:
        existing.secret, existing.is_enabled, existing.backup_code_hashes = secret, False, []
    existing.pending_account_identity = principal.capture.account_identity
    existing.pending_generation = principal.capture.generation
    existing.pending_sid = principal.capture.sid
    await db.commit()
    return TOTPSetupResponse(secret=secret, qr_code_b64=qr, issuer="BamDude")


@router.post("/2fa/totp/enable", response_model=TOTPEnableResponse)
async def enable_totp(
    body: TOTPEnableRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
):
    await check_rate_limit(db, current_user.username)
    row = (await db.execute(select(UserTOTP).where(UserTOTP.user_id == current_user.id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(400, "TOTP setup not initiated. Call /auth/2fa/totp/setup first.")
    try:
        expected = row._secret_enc
        valid = pyotp.TOTP(row.secret).verify(body.code, valid_window=1)
    except RuntimeError:
        raise HTTPException(500, "TOTP secret unavailable") from None
    if not valid:
        await record_failed_attempt(db, current_user.username)
        raise HTTPException(400, "Invalid TOTP code")
    await db.rollback()
    plain, hashed = await _generate_backup_codes_async()
    proof = await guard_accounts(db, [current_user._auth_capture])
    principal = await validate_captured(db, current_user._auth_capture, proof=proof)
    row = (
        await db.execute(
            select(UserTOTP)
            .where(UserTOTP.user_id == principal.user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.is_enabled
        or row._secret_enc != expected
        or row.pending_account_identity != principal.capture.account_identity
        or row.pending_generation != principal.capture.generation
        or row.pending_sid != principal.capture.sid
    ):
        raise HTTPException(400, "TOTP setup not initiated. Call /auth/2fa/totp/setup first.")
    row.is_enabled, row.backup_code_hashes = True, hashed
    row.pending_account_identity = row.pending_generation = row.pending_sid = None
    await db.commit()
    return TOTPEnableResponse(
        message="TOTP enabled successfully. Store your backup codes in a safe place.", backup_codes=plain
    )


@router.post("/2fa/totp/disable")
async def disable_totp(
    body: TOTPDisableRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
):
    row = await _verified_factor_for_mutation(db, current_user, body.code)
    await db.delete(row)
    await db.commit()
    return {"message": "TOTP disabled"}


@router.post("/2fa/totp/regenerate-backup-codes", response_model=BackupCodesResponse)
async def regenerate_backup_codes(
    body: TOTPDisableRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
):
    # Hash generation precedes the factor guard; verification rereads the factor.
    plain, hashed = await _generate_backup_codes_async()
    row = await _verified_factor_for_mutation(db, current_user, body.code)
    row.backup_code_hashes = hashed
    await db.commit()
    return BackupCodesResponse(
        backup_codes=plain, message="Backup codes regenerated. Store them safely — they will not be shown again."
    )


@router.post("/2fa/email/enable")
async def enable_email_otp(
    current_user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_auth_db)
):
    await check_email_otp_send_rate(db, current_user.username)
    email = current_user.email
    if not email:
        raise HTTPException(400, "You must have an email address configured to enable email OTP 2FA")
    smtp = await get_smtp_settings(db)
    if not smtp:
        raise HTTPException(500, "Email service is not configured")
    await db.rollback()
    code = str(secrets.randbelow(1_000_000)).zfill(6)
    hashed = await hash_password_async(code)
    proof = await guard_accounts(db, [current_user._auth_capture])
    principal = await validate_captured(db, current_user._auth_capture, proof=proof)
    if principal.user.email != email:
        raise refused()
    await db.execute(
        delete(AuthEphemeralToken).where(
            AuthEphemeralToken.account_identity == principal.capture.account_identity,
            AuthEphemeralToken.token_type == TokenType.EMAIL_OTP_SETUP,
        )
    )
    token = secrets.token_urlsafe(32)
    db.add(
        AuthEphemeralToken(
            token=token,
            token_type=TokenType.EMAIL_OTP_SETUP,
            username=principal.user.username,
            nonce=hashed,
            expires_at=utcnow() + timedelta(minutes=10),
            **binding(principal.capture),
        )
    )
    await db.commit()
    try:
        await asyncio.shield(
            auth_runtime.spawn(
                asyncio.to_thread(
                    send_email,
                    smtp_settings=smtp,
                    to_email=email,
                    subject="Verify your BamDude email address for 2FA",
                    body_text=f"Your email 2FA setup code is: {code}",
                    body_html=f"<p>Email 2FA setup code:</p><h2>{code}</h2>",
                )
            )
        )
    except Exception:
        raise HTTPException(500, "Failed to send verification email") from None
    await record_email_otp_send(db, current_user.username)
    return {"message": "Verification code sent to your email address", "setup_token": token}


@router.post("/2fa/email/enable/confirm")
async def confirm_enable_email_otp(
    body: EmailOTPEnableConfirmRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
):
    await check_rate_limit(db, current_user.username)
    row = (
        await db.execute(
            select(AuthEphemeralToken).where(
                AuthEphemeralToken.token == body.setup_token,
                AuthEphemeralToken.token_type == TokenType.EMAIL_OTP_SETUP,
                AuthEphemeralToken.expires_at > utcnow(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(400, "Invalid or expired setup token")
    captured, hashed, row_id = capture_ephemeral(row), row.nonce, row.id
    if captured != FrozenAuthority(
        "session",
        current_user._auth_capture.account_identity,
        0,
        current_user._auth_capture.generation,
        current_user._auth_capture.sid,
    ):
        raise HTTPException(400, "Invalid or expired setup token")
    await db.rollback()
    if not await verify_password_async(body.code, hashed):
        await record_failed_attempt(db, current_user.username)
        raise HTTPException(400, "Invalid verification code")
    proof = await guard_accounts(db, [captured])
    principal = await validate_captured(db, captured, proof=proof)
    spent = await db.execute(
        delete(AuthEphemeralToken)
        .where(
            AuthEphemeralToken.id == row_id,
            AuthEphemeralToken.token == body.setup_token,
            AuthEphemeralToken.token_type == TokenType.EMAIL_OTP_SETUP,
            *binding_conditions(captured),
            AuthEphemeralToken.nonce == hashed,
            AuthEphemeralToken.expires_at > utcnow(),
        )
        .returning(AuthEphemeralToken.id)
    )
    if spent.scalar_one_or_none() is None:
        raise HTTPException(400, "Invalid or expired setup token")
    await _set_email_2fa_enabled(db, principal.user.id, True)
    await db.commit()
    return {"message": "Email OTP 2FA enabled"}


@router.post("/2fa/email/disable")
async def disable_email_otp(
    body: EmailOTPDisableRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
):
    expected = current_user.password_hash
    if expected and (not body.password or not await verify_password_async(body.password, expected)):
        raise HTTPException(401, "Invalid password")
    proof = await guard_accounts(db, [current_user._auth_capture])
    principal = await validate_captured(db, current_user._auth_capture, proof=proof)
    if principal.user.password_hash != expected or (not expected and principal.auth_source not in {"ldap", "oidc"}):
        raise HTTPException(401, "Invalid password")
    await _set_email_2fa_enabled(db, principal.user.id, False)
    await db.execute(delete(UserOTPCode).where(UserOTPCode.account_identity == principal.capture.account_identity))
    await db.commit()
    return {"message": "Email OTP 2FA disabled"}


@router.post("/2fa/email/send")
async def send_email_otp(request: Request, body: EmailOTPSendRequest, db: AsyncSession = Depends(get_auth_db)):
    challenge = request.cookies.get("2fa_challenge")
    row = await _peek_pre_auth_record(db, body.pre_auth_token, challenge)
    if row is None:
        raise HTTPException(401, "Invalid or expired pre-auth token")
    capture, username, source, preference = (
        capture_ephemeral(row, pending=True),
        row.username,
        row.auth_source,
        row.remember_me,
    )
    user = row._principal.user
    email = user.email
    if not email:
        raise HTTPException(400, "User has no email address configured")
    await check_email_otp_send_rate(db, username)
    smtp = await get_smtp_settings(db)
    if not smtp:
        raise HTTPException(500, "Email service is not configured")
    await db.rollback()
    code = str(secrets.randbelow(1_000_000)).zfill(6)
    hashed = await hash_password_async(code)
    try:
        await asyncio.shield(
            auth_runtime.spawn(
                asyncio.to_thread(
                    send_email,
                    smtp_settings=smtp,
                    to_email=email,
                    subject="Your BamDude verification code",
                    body_text=f"Your BamDude login code is: {code}\nThis code expires in 10 minutes.",
                    body_html=f"<p>Your BamDude login verification code:</p><h2>{code}</h2><p>Expires in 10 minutes.</p>",
                )
            )
        )
    except Exception:
        raise HTTPException(500, "Failed to send OTP email") from None
    proof = await guard_accounts(db, [capture])
    principal = await validate_captured(db, capture, proof=proof)
    if principal.user.email != email or not await consume_pre_auth_token(db, body.pre_auth_token, challenge):
        raise refused()
    await db.execute(
        UserOTPCode.__table__.update()
        .where(UserOTPCode.user_id == principal.user.id, UserOTPCode.used.is_(False))
        .values(used=True)
    )
    db.add(
        UserOTPCode(
            user_id=principal.user.id,
            account_identity=capture.account_identity,
            generation=capture.generation,
            sid=capture.sid,
            challenge_id=challenge,
            code_hash=hashed,
            attempts=0,
            used=False,
            expires_at=utcnow() + timedelta(minutes=UserOTPCode.OTP_TTL_MINUTES),
        )
    )
    db.add(AuthRateLimitEvent(username=username, event_type=EventType.EMAIL_SEND, occurred_at=utcnow()))
    fresh = await create_pre_auth_token(
        db,
        username,
        challenge_id=challenge,
        remember_me=preference,
        verified=VerifiedOutcome(capture_account(principal.user), source),
    )
    return {"message": "Code sent to your email address", "pre_auth_token": fresh}


@router.post("/2fa/verify", response_model=TwoFAVerifyResponse)
async def verify_2fa(
    request: Request, response: Response, body: TwoFAVerifyRequest, db: AsyncSession = Depends(get_auth_db)
) -> TwoFAVerifyResponse:
    challenge = request.cookies.get("2fa_challenge")
    pending = await _peek_pre_auth_record(db, body.pre_auth_token, challenge)
    if pending is None:
        raise HTTPException(401, "Invalid or expired pre-auth token")
    capture = capture_ephemeral(pending, pending=True)
    username, source, preference = pending.username, pending.auth_source, pending.remember_me
    user_id = pending._principal.user.id
    await check_rate_limit(db, username)
    factor = (await db.execute(select(UserTOTP).where(UserTOTP.user_id == user_id))).scalar_one_or_none()
    code_row = None
    matched = None
    factor_hashes = factor.backup_code_hashes if factor and factor.is_enabled else []
    secret = factor.secret if factor and factor.is_enabled else None
    if body.method == "email":
        code_row = (
            await db.execute(
                select(UserOTPCode)
                .where(
                    UserOTPCode.user_id == user_id,
                    UserOTPCode.account_identity == capture.account_identity,
                    UserOTPCode.generation == capture.generation,
                    UserOTPCode.challenge_id == challenge,
                    UserOTPCode.used.is_(False),
                    UserOTPCode.expires_at > utcnow(),
                )
                .order_by(UserOTPCode.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        code_id, hashed = (code_row.id, code_row.code_hash) if code_row else (None, None)
    else:
        code_id = hashed = None
    await db.rollback()  # slow PBKDF work never owns a checkout/lifetime lock
    if body.method == "totp":
        valid = bool(secret and pyotp.TOTP(secret).verify(body.code, valid_window=1))
    elif body.method == "email":
        valid = bool(hashed and await verify_password_async(body.code, hashed))
    else:
        valid = False
        for index, value in enumerate(factor_hashes):
            if await verify_password_async(body.code, value) and matched is None:
                matched = index
                valid = True
    proof = await guard_accounts(db, [capture])
    principal = await validate_captured(db, capture, proof=proof)
    current = await _peek_pre_auth_record(db, body.pre_auth_token, challenge)
    if current is None:
        raise HTTPException(401, "Invalid or expired pre-auth token")
    if body.method == "email":
        code_row = await db.get(UserOTPCode, code_id, populate_existing=True) if code_id else None
        if (
            code_row is None
            or code_row.used
            or code_row.code_hash != hashed
            or code_row.attempts >= UserOTPCode.MAX_ATTEMPTS
            or code_row.account_identity != capture.account_identity
            or code_row.generation != capture.generation
            or code_row.challenge_id != challenge
            or utc_naive(code_row.expires_at) <= utcnow()
        ):
            valid = False
        if code_row is not None and not valid:
            code_row.attempts += 1
            if code_row.attempts >= UserOTPCode.MAX_ATTEMPTS:
                code_row.used = True
            await db.commit()
    else:
        factor = (
            await db.execute(
                select(UserTOTP)
                .where(UserTOTP.user_id == principal.user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if factor is None or not factor.is_enabled:
            valid = False
        elif body.method == "totp":
            valid = valid and factor.secret == secret
        else:
            valid = valid and factor.backup_code_hashes == factor_hashes
    if not valid:
        await record_failed_attempt(db, username)
        raise HTTPException(401, "Invalid verification code")
    if not await consume_pre_auth_token(db, body.pre_auth_token, challenge):
        raise HTTPException(401, "Invalid or expired pre-auth token")
    if body.method == "email":
        code_row.used = True
    elif body.method == "totp":
        _assert_totp_not_replayed(pyotp.TOTP(secret), factor, body.code)
    else:
        factor.backup_code_hashes = [value for index, value in enumerate(factor_hashes) if index != matched]
    # The helper used by failed attempts commits. Successful cleanup belongs
    # to this same factor/pre-auth/session publication transaction instead.
    await db.execute(
        delete(AuthRateLimitEvent).where(
            AuthRateLimitEvent.username == username,
            AuthRateLimitEvent.event_type == EventType.TWO_FA_ATTEMPT,
        )
    )
    from backend.app.api.routes.auth import _issue_refresh_cookie

    issued = await _issue_refresh_cookie(
        db,
        response,
        request,
        verified=VerifiedOutcome(capture_account(principal.user), source),
        remember_me=preference if preference is not None else body.remember_me,
    )
    await db.commit()
    response.delete_cookie("2fa_challenge", path="/api/v1/auth/2fa")
    return TwoFAVerifyResponse(
        access_token=issued.access_token, token_type="bearer", user=_user_to_response(issued.user)
    )


@router.delete("/2fa/admin/{user_id}", dependencies=[RequireAdmin()])
async def admin_disable_2fa(
    user_id: int,
    body: AdminDisable2FARequest = Body(default_factory=AdminDisable2FARequest),
    current_user: User | None = RequirePermission(Permission.USERS_UPDATE),
    db: AsyncSession = Depends(get_auth_db),
):
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    target_capture = capture_account(target)
    actor = current_user._auth_capture
    expected_hash = current_user.password_hash
    await db.rollback()
    if expected_hash and (
        not body.admin_password or not await verify_password_async(body.admin_password, expected_hash)
    ):
        raise HTTPException(401, "Admin password required")
    proof = await guard_accounts(db, [actor, target_capture], require_active=False)
    admin = await validate_captured(db, actor, required_permissions=(Permission.USERS_UPDATE.value,), proof=proof)
    if not admin.user.is_admin:
        raise HTTPException(403, "Administrator required")
    if admin.user.password_hash != expected_hash:
        raise HTTPException(401, "Admin password required")
    if not expected_hash and admin.auth_source not in {"ldap", "oidc"}:
        raise HTTPException(401, "Admin password required")
    await db.execute(delete(UserTOTP).where(UserTOTP.user_id == user_id))
    await _set_email_2fa_enabled(db, user_id, False)
    await db.execute(delete(UserOTPCode).where(UserOTPCode.user_id == user_id))
    await stage_end_sessions(db, target_capture, "admin_mfa_reset")
    await db.commit()
    return {"message": "2FA disabled for user"}


# ===========================================================================
# OIDC Endpoints
# ===========================================================================


def _redact_url_for_log(url: str) -> str:
    """Return ``scheme://host/path`` with query string and fragment stripped.

    Admin-supplied icon URLs are usually CDN paths, but nothing stops an
    admin from pasting a presigned URL whose query string carries an
    ``X-Amz-Signature`` / OAuth token / etc. Operators need a forensic
    trail without those secrets ending up in log files.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "<unparseable>"
    netloc = parsed.netloc or "<no-host>"
    return f"{parsed.scheme}://{netloc}{parsed.path}"


async def _fetch_icon_or_400(icon_url: str) -> tuple[bytes, str, str]:
    """Validate URL + fetch icon, mapping any failure to ``HTTPException(400)``.

    Centralises the SSRF guard + fetcher invocation so create / update /
    refresh all behave identically — admin always gets a 400 with a
    precise reason, never a 500 / opaque server error. Both failure
    paths log at WARNING so operators have a forensic trail later —
    without these log lines the admin's UI toast was the only record of
    the failure.
    """
    try:
        assert_safe_public_https_url(icon_url)
    except ValueError as exc:
        logger.warning("OIDC icon URL rejected by SSRF guard: url=%s reason=%s", _redact_url_for_log(icon_url), exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        return await fetch_icon(icon_url)
    except OIDCIconError as exc:
        logger.warning("OIDC icon fetch failed: url=%s reason=%s", _redact_url_for_log(icon_url), exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _etag_matches(if_none_match: str | None, etag_raw: str | None) -> bool:
    """RFC 7232 §3.2 ``If-None-Match`` comparison.

    Supports:

    - ``*`` wildcard — matches any current representation when the
      resource exists (and it does here; we wouldn't have an etag
      otherwise).
    - Multiple comma-separated tokens.
    - Weak-validator prefix ``W/`` (RFC 7232 §2.3) — accepted on GET
      since cached representations of a static byte-blob are byte-
      identical.

    Returns False on missing header or missing stored etag.
    """
    if not if_none_match or not etag_raw:
        return False
    quoted = f'"{etag_raw}"'
    tokens = [t.strip() for t in if_none_match.split(",")]
    if "*" in tokens:
        return True
    return any(tok.removeprefix("W/") == quoted for tok in tokens)


@router.get("/oidc/providers", response_model=list[OIDCProviderPublicResponse])
async def list_oidc_providers(
    db: AsyncSession = Depends(get_auth_db),
) -> list[OIDCProviderPublicResponse]:
    """List all enabled OIDC providers (public)."""
    result = await db.execute(select(OIDCProvider).where(OIDCProvider.is_enabled.is_(True)))
    providers = result.scalars().all()
    return [OIDCProviderPublicResponse.model_validate(p) for p in providers]


@router.get("/oidc/providers/all", response_model=list[OIDCProviderResponse], dependencies=[RequireAdmin()])
async def list_all_oidc_providers(
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
    db: AsyncSession = Depends(get_auth_db),
) -> list[OIDCProviderResponse]:
    """List ALL OIDC providers including disabled ones (admin only)."""
    result2 = await db.execute(select(OIDCProvider))
    providers = result2.scalars().all()
    return [OIDCProviderResponse.model_validate(p) for p in providers]


@router.post(
    "/oidc/providers",
    response_model=OIDCProviderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[RequireAdmin()],
)
async def create_oidc_provider(
    body: OIDCProviderCreate,
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_auth_db),
) -> OIDCProviderResponse:
    """Create a new OIDC provider (admin only).

    If ``icon_url`` is supplied, the icon is fetched server-side and
    cached in the BLOB columns. A fetch failure aborts the create with
    400 — no half-configured provider is left in the DB. Upstream
    Bambuddy #1333.
    """
    await lock_credential_config(db)
    values = body.model_dump(exclude={"secret_actions"})
    values = merge_secret_fields({}, values, {"client_secret"}, body.secret_actions, creating=True)
    if values["is_enabled"] and not values.get("client_secret"):
        raise HTTPException(400, "A client secret is required for an enabled OIDC provider")
    # Validate default_group_id references an existing group. Pydantic only
    # checks the shape (int | None); the cross-row sanity belongs in the
    # route layer because it depends on DB state. Raises 422 on a stale or
    # invalid id rather than silently storing a dangling reference (#1173).
    if body.default_group_id is not None:
        grp_chk = await db.execute(select(Group).where(Group.id == body.default_group_id))
        if not grp_chk.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="default_group_id references a non-existent group",
            )

    # Fetch the icon BEFORE creating the row so a failure leaves the DB
    # clean (no half-configured provider). _fetch_icon_or_400 raises
    # HTTPException(400) on SSRF guard rejection or upstream issue.
    icon_data: bytes | None = None
    icon_content_type: str | None = None
    icon_etag: str | None = None
    if body.icon_url:
        await db.rollback()
        icon_data, icon_content_type, icon_etag = await _fetch_icon_or_400(body.icon_url)
        await lock_credential_config(db)
        if body.default_group_id is not None:
            if not (await db.execute(select(Group).where(Group.id == body.default_group_id))).scalar_one_or_none():
                raise HTTPException(422, "default_group_id references a non-existent group")

    provider = OIDCProvider(
        name=body.name,
        issuer_url=body.issuer_url.rstrip("/"),
        client_id=body.client_id,
        client_secret=values["client_secret"],
        scopes=body.scopes,
        is_enabled=body.is_enabled,
        auto_create_users=body.auto_create_users,
        auto_link_existing_accounts=body.auto_link_existing_accounts,
        email_claim=body.email_claim,
        require_email_verified=body.require_email_verified,
        icon_url=body.icon_url,
        icon_data=icon_data,
        icon_content_type=icon_content_type,
        icon_etag=icon_etag,
        default_group_id=body.default_group_id,
        is_autologin=body.is_autologin,
    )
    # Defense-in-depth: re-checks the safety guard against the constructed
    # ORM object so any future code path that bypasses Pydantic validation
    # (direct ORM use, scripts) still hits the same gate.
    _enforce_auto_link_safety(provider)
    db.add(provider)
    # #1589: at most one provider may be the autologin target. When a new one is
    # created with the flag set, clear it on all others first so the session
    # still satisfies the invariant after add.
    if body.is_autologin:
        await db.execute(update(OIDCProvider).where(OIDCProvider.is_autologin.is_(True)).values(is_autologin=False))
    await db.commit()
    await db.refresh(provider)
    return OIDCProviderResponse.model_validate(provider)


def _refuse_if_env_managed(provider) -> None:
    """Startup rewrites this provider from ``BAMDUDE_OIDC_*`` on every boot, so an
    edit accepted here would be silently reverted at the next restart. Being told
    no is better than being told yes and then having it undone.

    ``BAMDUDE_LOCAL_LOGIN`` (#1589) remains the recovery path if the provider
    becomes unusable, so refusing outright cannot lock anyone out.
    """
    if provider.is_env_managed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This OIDC provider is managed by environment variables and cannot be modified.",
        )


def _oidc_snapshot(provider: OIDCProvider) -> dict:
    return {
        field: getattr(provider, field) for field in OIDCProviderCreate.model_fields if field != "secret_actions"
    } | {"is_env_managed": provider.is_env_managed, "icon_etag": provider.icon_etag}


def _oidc_scope(values: dict) -> tuple:
    return canonical_url(values.get("issuer_url")), values.get("client_id") or ""


@router.put("/oidc/providers/{provider_id}", response_model=OIDCProviderResponse, dependencies=[RequireAdmin()])
async def update_oidc_provider(
    provider_id: int,
    body: OIDCProviderUpdate,
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_auth_db),
) -> OIDCProviderResponse:
    """Update an existing OIDC provider (admin only).

    Icon refetch fires when:

    1. The submitted ``icon_url`` differs from the stored one (URL
       changed), OR
    2. The submitted ``icon_url`` equals the stored one AND
       ``icon_content_type`` is NULL — upgrade-path edge case where old
       providers carry ``icon_url`` but no cached bytes until the admin
       first saves them.

    On fetch failure the request aborts with 400 *before* commit, so the
    existing cached bytes (if any) remain untouched. Upstream Bambuddy
    #1333.
    """
    await lock_credential_config(db)
    result2 = await db.execute(select(OIDCProvider).where(OIDCProvider.id == provider_id))
    provider = result2.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found")
    _refuse_if_env_managed(provider)

    # Same DB-state cross-check as create — when the operator supplies a
    # default_group_id, refuse the update if the referenced group doesn't
    # exist (#1173). The setattr loop below would otherwise persist a
    # dangling reference.
    if body.default_group_id is not None:
        grp_chk = await db.execute(select(Group).where(Group.id == body.default_group_id))
        if not grp_chk.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="default_group_id references a non-existent group",
            )

    before = _oidc_snapshot(provider)
    submitted = body.model_dump(exclude_unset=True, exclude={"secret_actions"})
    # Legacy non-secret nulls keep their previous semantics; a supplied secret
    # null is an intentional clear and must pass the disabled-mode check.
    changes = {
        key: value for key, value in submitted.items() if value is not None or key in {"client_secret", "icon_url"}
    }
    resolved = merge_secret_fields(before, changes, {"client_secret"}, body.secret_actions, scope=_oidc_scope)
    if resolved.get("is_enabled") and not resolved.get("client_secret"):
        raise HTTPException(400, "A client secret is required for an enabled OIDC provider")
    _enforce_auto_link_safety(SimpleNamespace(**resolved))
    dumped = {key: value for key, value in resolved.items() if key not in {"is_env_managed", "icon_etag"}}

    # Decide whether an icon refetch is needed BEFORE mutating the ORM
    # object, so the comparison sees provider.icon_url / icon_content_type
    # as they are in the database.
    new_icon_url = changes.get("icon_url")
    needs_icon_refetch = new_icon_url is not None and (
        new_icon_url != provider.icon_url or provider.icon_content_type is None
    )

    # Fetch FIRST. If the upstream is unreachable or SSRF-blocked,
    # _fetch_icon_or_400 raises HTTPException(400) here — provider
    # attributes are still untouched, so the in-memory ORM object stays
    # consistent on the way out (and the DB row is safe regardless via
    # get_db()'s rollback).
    fetched_icon: tuple[bytes, str, str] | None = None
    if needs_icon_refetch:
        await db.rollback()
        fetched_icon = await _fetch_icon_or_400(new_icon_url)
        await lock_credential_config(db)
        provider = (
            await db.execute(
                select(OIDCProvider).where(OIDCProvider.id == provider_id).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if provider is None or _oidc_snapshot(provider) != before:
            raise HTTPException(409, "OIDC provider changed during icon verification; reload and retry")
        if body.default_group_id is not None:
            if not (await db.execute(select(Group).where(Group.id == body.default_group_id))).scalar_one_or_none():
                raise HTTPException(422, "default_group_id references a non-existent group")

    # Explicit ``icon_url: null`` in the PUT body means "clear the icon".
    # ``exclude_none=True`` above drops None values, which would otherwise
    # silently ignore this request. Check ``model_fields_set`` on the
    # unfiltered body to distinguish "client cleared it" from "client
    # didn't include this field at all".
    if "icon_url" in body.model_fields_set and body.icon_url is None:
        provider.icon_url = None
        provider.icon_data = None
        provider.icon_content_type = None
        provider.icon_etag = None

    for field, value in dumped.items():
        if field == "issuer_url" and value:
            value = value.rstrip("/")
        setattr(provider, field, value)

    if fetched_icon is not None:
        provider.icon_data, provider.icon_content_type, provider.icon_etag = fetched_icon

    # Combined-State-Guard: after applying partial updates, re-check the
    # final in-memory state. Catches the case where two requests each pass
    # schema validation alone but together produce the unsafe combo.
    _enforce_auto_link_safety(provider)

    # #1589: at most one provider may be the autologin target. Clear the flag on
    # every other provider when this one becomes the autologin. Excludes the
    # current row so SQLAlchemy doesn't fight our in-memory set above.
    if body.is_autologin is True:
        await db.execute(
            update(OIDCProvider)
            .where(OIDCProvider.id != provider.id, OIDCProvider.is_autologin.is_(True))
            .values(is_autologin=False)
        )

    await db.commit()
    await db.refresh(provider)
    return OIDCProviderResponse.model_validate(provider)


# ---------------------------------------------------------------------------
# OIDC provider icon proxy (#1333 / commit 8a7598f6)
# ---------------------------------------------------------------------------


@router.get("/oidc/providers/{provider_id}/icon")
async def get_oidc_provider_icon(
    provider_id: int,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    db: AsyncSession = Depends(get_auth_db),
) -> Response:
    """Serve the cached icon for an enabled OIDC provider (public, no auth).

    Unauthenticated because ``<img>`` tags cannot send Authorization
    headers and the login page renders these icons before the user is
    signed in — the same justification used for our other public-asset
    endpoints. The SSRF guard runs at admin-config time (create / update /
    refresh), not here.

    Disabled providers respond 404 to avoid leaking their existence to
    anonymous callers (mirrors ``GET /oidc/providers`` which filters on
    ``is_enabled``).
    """
    result = await db.execute(
        select(OIDCProvider)
        .options(undefer(OIDCProvider.icon_data))
        .where(OIDCProvider.id == provider_id, OIDCProvider.is_enabled.is_(True))
    )
    provider = result.scalar_one_or_none()
    if provider is None or provider.icon_content_type is None or provider.icon_data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Icon not found")

    etag_value = f'"{provider.icon_etag}"'
    cache_headers = {"ETag": etag_value, "Cache-Control": "public, max-age=3600"}

    if _etag_matches(if_none_match, provider.icon_etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=cache_headers)

    return Response(
        content=provider.icon_data,
        media_type=provider.icon_content_type,
        headers=cache_headers,
    )


@router.delete(
    "/oidc/providers/{provider_id}/icon", status_code=status.HTTP_204_NO_CONTENT, dependencies=[RequireAdmin()]
)
async def delete_oidc_provider_icon(
    provider_id: int,
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_auth_db),
) -> Response:
    """Remove the icon entirely for a provider (admin only).

    Clears all four icon columns — ``icon_url`` plus the three cached-bytes
    columns. "Remove icon" means the whole record is gone, not just the
    cache; without this the admin form would still show the URL while
    the login page rendered a blank fallback (confusing half-state). To
    re-add an icon the admin re-types the URL in the edit form.
    """
    result = await db.execute(select(OIDCProvider).where(OIDCProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found")
    _refuse_if_env_managed(provider)

    # Setting deferred columns is safe — no read happens, just a write.
    provider.icon_url = None
    provider.icon_data = None
    provider.icon_content_type = None
    provider.icon_etag = None
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/oidc/providers/{provider_id}", dependencies=[RequireAdmin()])
async def delete_oidc_provider(
    provider_id: int,
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_auth_db),
) -> dict:
    """Delete an OIDC provider and all its user links (admin only)."""
    await lock_credential_config(db)
    result2 = await db.execute(select(OIDCProvider).where(OIDCProvider.id == provider_id))
    provider = result2.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found")
    _refuse_if_env_managed(provider)

    await db.delete(provider)
    await db.commit()
    return {"message": "Provider deleted"}


@router.get("/oidc/authorize/{provider_id}", response_model=OIDCAuthorizeResponse)
async def oidc_authorize(
    provider_id: int,
    remember_me: bool = False,
    db: AsyncSession = Depends(get_auth_db),
) -> OIDCAuthorizeResponse:
    """Return the OIDC authorization URL for the given provider."""
    result = await db.execute(
        select(OIDCProvider).where(OIDCProvider.id == provider_id).where(OIDCProvider.is_enabled.is_(True))
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found or not enabled")

    db.expunge(provider)
    await db.rollback()
    # Fetch discovery document
    discovery_url = f"{provider.issuer_url.rstrip('/')}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            discovery = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch OIDC discovery for provider %d: %s", provider_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch OIDC discovery document")

    authorization_endpoint = discovery.get("authorization_endpoint")
    if not authorization_endpoint:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="OIDC discovery document missing authorization_endpoint"
        )
    # B2: SSRF guard — reject non-HTTP(S) schemes in the authorization endpoint
    if not authorization_endpoint.startswith(("https://", "http://")):
        logger.warning("OIDC discovery authorization_endpoint has invalid scheme: %s", authorization_endpoint)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OIDC discovery document contains invalid authorization_endpoint",
        )

    external_url = await _get_base_external_url(db)
    redirect_uri = f"{external_url}/api/v1/auth/oidc/callback"

    now = utcnow()
    # Prune expired OIDC states from the DB
    await db.execute(
        delete(AuthEphemeralToken).where(
            AuthEphemeralToken.token_type == TokenType.OIDC_STATE,
            AuthEphemeralToken.expires_at < now,
        )
    )
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    # PKCE (S256) – required by PocketID and recommended for all OIDC flows
    code_verifier = secrets.token_urlsafe(48)  # 64-char URL-safe string
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()

    db.add(
        AuthEphemeralToken(
            token=state,
            token_type=TokenType.OIDC_STATE,
            contract_version=1,
            provider_id=provider_id,
            nonce=nonce,
            code_verifier=code_verifier,
            remember_me=remember_me,
            expires_at=now + OIDC_STATE_TTL,
        )
    )
    await db.commit()

    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": provider.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    auth_url = f"{authorization_endpoint}?{params}"
    return OIDCAuthorizeResponse(auth_url=auth_url)


@router.get("/oidc/callback")
async def oidc_callback(
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=2048),
    error: str | None = Query(default=None, max_length=256),
    db: AsyncSession = Depends(get_auth_db),
) -> RedirectResponse:
    """Handle the OIDC authorization code callback from the identity provider."""
    external_url = await _get_base_external_url(db)
    frontend_error_url = f"{external_url}/?oidc_error="

    try:
        if error:
            logger.warning("OIDC callback received error: %s", error)
            return RedirectResponse(url=f"{frontend_error_url}oidc_provider_error", status_code=302)

        if not code or not state:
            return RedirectResponse(url=f"{frontend_error_url}missing_parameters", status_code=302)

        # Atomically validate and consume OIDC state from DB (I6: single-use enforcement).
        # DELETE...RETURNING ensures concurrent callbacks with the same state token
        # cannot both succeed — only the first DELETE finds the row.
        now = utcnow()
        state_del = await db.execute(
            delete(AuthEphemeralToken)
            .where(
                AuthEphemeralToken.token == state,
                AuthEphemeralToken.token_type == TokenType.OIDC_STATE,
                AuthEphemeralToken.contract_version == 1,
                AuthEphemeralToken.expires_at > now,  # reject expired tokens atomically
            )
            .returning(
                AuthEphemeralToken.provider_id,
                AuthEphemeralToken.nonce,
                AuthEphemeralToken.code_verifier,
                AuthEphemeralToken.remember_me,
            )
        )
        state_row = state_del.one_or_none()
        if state_row is None:
            await db.rollback()
            return RedirectResponse(url=f"{frontend_error_url}invalid_state", status_code=302)

        provider_id, nonce, code_verifier, stored_remember_me = state_row
        remember_me = stored_remember_me is True
        await db.commit()

        # Load provider
        result = await db.execute(select(OIDCProvider).where(OIDCProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        if not provider:
            return RedirectResponse(url=f"{frontend_error_url}provider_not_found", status_code=302)

        redirect_uri = f"{external_url}/api/v1/auth/oidc/callback"

        # ── Step 1: Fetch discovery document ────────────────────────────────
        db.expunge(provider)
        await db.rollback()
        discovery_url = f"{provider.issuer_url.rstrip('/')}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                disc_resp = await client.get(discovery_url)
                disc_resp.raise_for_status()
                discovery = disc_resp.json()
        except Exception as exc:
            logger.error("OIDC discovery fetch failed for provider %d: %s", provider_id, exc)
            return RedirectResponse(url=f"{frontend_error_url}discovery_failed", status_code=302)

        token_endpoint = discovery.get("token_endpoint")
        jwks_uri = discovery.get("jwks_uri")
        if not token_endpoint or not jwks_uri:
            return RedirectResponse(url=f"{frontend_error_url}invalid_discovery_document", status_code=302)
        # L-R7-C: Reject non-HTTP(S) URLs in the discovery document to prevent
        # SSRF via crafted responses (e.g. file://, gopher://, internal schemes).
        if not token_endpoint.startswith(("https://", "http://")) or not jwks_uri.startswith(("https://", "http://")):
            logger.warning(
                "OIDC discovery document contains non-HTTP URL(s): token=%s jwks=%s", token_endpoint, jwks_uri
            )
            return RedirectResponse(url=f"{frontend_error_url}invalid_discovery_document", status_code=302)

        # ── Step 2: Exchange authorization code for tokens ───────────────────
        token_form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": provider.client_id,
        }
        if provider.client_secret:
            token_form["client_secret"] = provider.client_secret
        if code_verifier:
            token_form["code_verifier"] = code_verifier

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                token_resp = await client.post(
                    token_endpoint,
                    data=token_form,
                    headers={"Accept": "application/json"},
                )
        except Exception as exc:
            logger.error("OIDC token exchange request failed for provider %d: %s", provider_id, exc)
            return RedirectResponse(url=f"{frontend_error_url}token_exchange_network_error", status_code=302)

        if not token_resp.is_success:
            try:
                err_body = token_resp.json()
                oidc_err = err_body.get("error", "")
                oidc_desc = err_body.get("error_description", "")
            except Exception:
                oidc_err = ""
                oidc_desc = token_resp.text[:200]
            logger.error(
                "OIDC token exchange HTTP %d for provider %d. redirect_uri=%r error=%r desc=%r",
                token_resp.status_code,
                provider_id,
                redirect_uri,
                oidc_err,
                oidc_desc,
            )
            # Encode the OIDC error code into the redirect so the user sees it in the toast.
            # URL-encode the value to prevent query-parameter injection from provider responses.
            raw_err = oidc_err[:40] if oidc_err else str(token_resp.status_code)
            safe_err = urllib.parse.quote(raw_err, safe="")
            return RedirectResponse(
                url=f"{frontend_error_url}token_exchange_{safe_err}",
                status_code=302,
            )

        try:
            token_data = token_resp.json()
        except Exception as exc:
            logger.error("OIDC token exchange non-JSON response for provider %d: %s", provider_id, exc)
            return RedirectResponse(url=f"{frontend_error_url}token_exchange_bad_response", status_code=302)

        id_token = token_data.get("id_token")
        if not id_token:
            # Only log the keys present — values may contain secrets (access_token, etc.)
            logger.error(
                "OIDC token response missing id_token for provider %d; keys present: %s",
                provider_id,
                list(token_data.keys()),
            )
            return RedirectResponse(url=f"{frontend_error_url}no_id_token", status_code=302)

        # ── Step 3: Fetch JWKS and validate ID token ─────────────────────────
        # Use the issuer from the discovery document as the canonical value (OIDC Core
        # §3.1.3.7 requires iss == discovery issuer exactly).  We strip trailing slashes
        # from both sides because some providers (e.g. Authentik, older PocketID versions)
        # are inconsistent between the discovery issuer and the JWT iss claim.
        discovery_issuer: str = discovery.get("issuer", provider.issuer_url).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as jwks_http:
                jwks_resp = await jwks_http.get(jwks_uri)
                jwks_resp.raise_for_status()
                jwks_data = jwks_resp.json()

            jwks_client = PyJWKClient(jwks_uri)
            jwks_client.fetch_data = lambda: jwks_data  # type: ignore[method-assign]
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)

            # M-3: Decode without built-in issuer check, then compare normalised
            # (both sides rstrip("/")) to handle providers like Authentik that include
            # a trailing slash in iss but not in the discovery issuer, or vice-versa.
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256", "ES256", "RS384", "ES384", "RS512"],
                audience=provider.client_id,
                options={"verify_iss": False},
            )
            token_iss = claims.get("iss", "").rstrip("/")
            if token_iss != discovery_issuer:
                raise jwt.exceptions.InvalidIssuerError("Invalid issuer")
        except Exception as exc:
            logger.error("OIDC JWT validation failed for provider %d: %s", provider_id, exc, exc_info=True)
            return RedirectResponse(url=f"{frontend_error_url}token_validation_failed", status_code=302)

        # Verify nonce — fail closed: we always send a nonce, so the provider must echo it.
        # Skipping the check when nonce is absent would allow CSRF on non-nonce providers.
        token_nonce = claims.get("nonce")
        if token_nonce is None or token_nonce != nonce:
            logger.warning("OIDC nonce mismatch for provider %d (present=%r)", provider_id, token_nonce is not None)
            return RedirectResponse(url=f"{frontend_error_url}nonce_mismatch", status_code=302)

        provider_sub: str = claims.get("sub", "")
        if not provider_sub:
            return RedirectResponse(url=f"{frontend_error_url}missing_sub_claim", status_code=302)

        # Resolve email via the provider's configured claim + verified policy.
        # See _resolve_provider_email docstring for Fall A/B/C semantics.
        provider_email: str | None = _resolve_provider_email(provider, claims, provider_sub)

        # ── Step 4: Resolve / create user ────────────────────────────────────
        try:
            # Resolve link + immutable account in one statement. A second lookup
            # by recycled user_id would let deletion/recreation transplant an IdP.
            linked = (
                await db.execute(
                    select(UserOIDCLink, User)
                    .join(User, User.id == UserOIDCLink.user_id)
                    .where(UserOIDCLink.provider_id == provider_id, UserOIDCLink.provider_user_id == provider_sub)
                    .options(selectinload(User.groups))
                )
            ).one_or_none()
            link, user = linked if linked is not None else (None, None)
            if link:
                resolved = capture_account(user)
                await guard_accounts(db, [resolved])
                current_link = (
                    await db.execute(
                        select(UserOIDCLink.id).where(
                            UserOIDCLink.id == link.id,
                            UserOIDCLink.user_id == resolved.user_id,
                            UserOIDCLink.provider_id == provider_id,
                            UserOIDCLink.provider_user_id == provider_sub,
                        )
                    )
                ).scalar_one_or_none()
                if current_link is None:
                    raise refused()
            else:
                # 2. No OIDC link yet — check for an existing user with the same email.
                # Use case-insensitive matching (func.lower) so that "User@Example.com"
                # and "user@example.com" are treated as the same identity, preventing
                # an attacker-controlled IdP from bypassing the auto-link guard by
                # registering the target email with different casing.
                email_user: User | None = None
                if provider_email:
                    email_user = await get_user_by_email(db, provider_email)

                if email_user and provider.auto_link_existing_accounts:
                    await guard_accounts(db, [capture_account(email_user)])
                    # M-4: Only auto-link when the provider has auto_link_existing_accounts
                    # enabled.  Operators can disable this to require explicit account linking,
                    # preventing an attacker-controlled IdP from hijacking local accounts.
                    #
                    # M-NEW-6: Refuse auto-link if the target user already has any OIDC
                    # link (to any provider).  Without this guard an attacker who controls
                    # a second OIDC provider with auto_link enabled could add themselves as
                    # a second IdP for a user that already authenticates via a legitimate
                    # provider, effectively taking over the account.
                    existing_links_result = await db.execute(
                        select(UserOIDCLink).where(UserOIDCLink.user_id == email_user.id)
                    )
                    has_existing_oidc_link = existing_links_result.scalar_one_or_none() is not None
                    if has_existing_oidc_link:
                        logger.warning(
                            "Auto-link rejected for user '%s': already linked to another OIDC provider",
                            email_user.username,
                        )
                        return RedirectResponse(url=f"{frontend_error_url}no_linked_account", status_code=302)
                    db.add(
                        UserOIDCLink(
                            user_id=email_user.id,
                            provider_id=provider_id,
                            provider_user_id=provider_sub,
                            provider_email=provider_email,
                        )
                    )
                    await db.flush()
                    user = email_user
                    logger.info(
                        "Auto-linked existing user '%s' to OIDC provider %d via email match",
                        email_user.username,
                        provider_id,
                    )
                elif provider.auto_create_users:
                    # Issue #1569: when email_claim is a non-email identity claim
                    # (e.g. preferred_username on Authentik), the primary resolver
                    # returns None for the email field because the identity value
                    # isn't email-shaped. Fall back to the standard 'email' claim
                    # for User.email so the operator can split
                    # username-from-preferred_username and email-from-email. The
                    # auto-link gate above stays on provider_email, so the GHSA
                    # Fall-B/C guards remain intact.
                    user_email_for_storage = provider_email
                    if user_email_for_storage is None and provider.email_claim != "email":
                        user_email_for_storage = _resolve_standard_email_for_user_record(provider, claims, provider_sub)

                    # 3. No existing user — create one. Username synthesis is
                    # extracted into ``_derive_oidc_username_seed`` so the
                    # resolution order (email local-part →
                    # preferred_username → name → provider_sub fallback) is
                    # unit-testable in isolation (#1173).
                    raw = _derive_oidc_username_seed(claims, provider_email, provider_sub)
                    candidate = re.sub(r"[^a-zA-Z0-9._-]", "", raw)[:30] or "oidcuser"

                    username = candidate
                    counter = 1
                    while True:
                        existing = await get_user_by_username(db, username)
                        if not existing:
                            break
                        username = f"{candidate}{counter}"
                        counter += 1

                    # I9 + #1173: Assign new OIDC users to a group before
                    # flush. Accessing ``new_user.groups`` after the flush
                    # triggers a lazy-load that fails in async context.
                    # Resolution order:
                    #   1. ``provider.default_group_id`` (operator-configured)
                    #   2. ``"Viewers"`` (system fallback for read-only access)
                    #   3. no group (last resort if Viewers was deleted)
                    # SQLite has FK enforcement off by default, so a deleted
                    # default group shows up as a dangling id; the lookup
                    # below returns None and falls through to Viewers.
                    default_group: Group | None = None
                    if provider.default_group_id is not None:
                        dg_result = await db.execute(select(Group).where(Group.id == provider.default_group_id))
                        default_group = dg_result.scalar_one_or_none()
                    if default_group is None:
                        viewers_result = await db.execute(select(Group).where(Group.name == "Viewers"))
                        default_group = viewers_result.scalar_one_or_none()

                    new_user = User(
                        username=username,
                        email=user_email_for_storage,
                        # M-1: auth_source="oidc" prevents local password-reset flow
                        # for users who should only authenticate via OIDC.
                        auth_source="oidc",
                        password_hash=None,  # OIDC users never use password auth
                        role="user",
                        is_active=True,
                        groups=[default_group] if default_group else [],
                    )
                    db.add(new_user)
                    await db.flush()

                    db.add(
                        UserOIDCLink(
                            user_id=new_user.id,
                            provider_id=provider_id,
                            provider_user_id=provider_sub,
                            provider_email=user_email_for_storage,
                        )
                    )
                    await db.flush()
                    user = new_user
                    logger.info("Auto-created user '%s' via OIDC provider %d", username, provider_id)
                else:
                    return RedirectResponse(url=f"{frontend_error_url}no_linked_account", status_code=302)

            if not user or not user.is_active:
                return RedirectResponse(url=f"{frontend_error_url}account_inactive", status_code=302)

            # Issue an OIDC exchange token (short-lived, single-use) stored in DB.
            # I7: Opportunistically prune expired exchange tokens to keep the table small.
            now2 = utcnow()
            await db.execute(
                delete(AuthEphemeralToken).where(
                    AuthEphemeralToken.token_type == TokenType.OIDC_EXCHANGE,
                    AuthEphemeralToken.expires_at < now2,
                )
            )
            await guard_accounts(db, [capture_account(user)])
            exchange_token = secrets.token_urlsafe(32)
            db.add(
                AuthEphemeralToken(
                    token=exchange_token,
                    token_type=TokenType.OIDC_EXCHANGE,
                    auth_source="oidc",
                    **binding(capture_account(user)),
                    username=user.username,
                    remember_me=remember_me,
                    expires_at=now2 + OIDC_EXCHANGE_TTL,
                )
            )
            await db.commit()

            # H-4: Use a URL fragment (#) instead of a query parameter so the exchange
            # token is never sent to the server in the Referer header or server logs.
            return RedirectResponse(url=f"{external_url}/login#oidc_token={exchange_token}", status_code=302)

        except HTTPException:
            raise
        except AUTH_DATABASE_FAILURES as exc:
            raise HTTPException(503, "Authentication temporarily unavailable") from exc
        except Exception as exc:
            logger.error("OIDC user resolution failed for provider %d: %s", provider_id, exc, exc_info=True)
            try:
                await db.rollback()
            except Exception as rb_exc:
                logger.error("DB rollback failed after OIDC user-resolution error: %s", rb_exc, exc_info=True)
            return RedirectResponse(url=f"{frontend_error_url}user_resolution_failed", status_code=302)

    except HTTPException:
        raise
    except AUTH_DATABASE_FAILURES as exc:
        raise HTTPException(503, "Authentication temporarily unavailable") from exc
    except Exception as exc:
        # L-1: Log the exception class name internally but never expose it in the
        # redirect URL — leaking exception names aids attacker reconnaissance.
        logger.error("Unexpected error in OIDC callback (%s): %s", type(exc).__name__, exc, exc_info=True)
        try:
            return RedirectResponse(url=f"{frontend_error_url}internal_error", status_code=302)
        except Exception:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="OIDC callback failed")


@router.post("/oidc/exchange", response_model=LoginResponse)
async def oidc_exchange(
    body: OIDCExchangeRequest, raw_request: Request, response: Response, db: AsyncSession = Depends(get_auth_db)
) -> LoginResponse:
    row = (
        await db.execute(
            select(AuthEphemeralToken).where(
                AuthEphemeralToken.token == body.oidc_token,
                AuthEphemeralToken.token_type == TokenType.OIDC_EXCHANGE,
                AuthEphemeralToken.expires_at > utcnow(),
            )
        )
    ).scalar_one_or_none()
    if row is None or row.auth_source != "oidc":
        raise HTTPException(401, "Invalid or expired OIDC exchange token")
    captured = capture_ephemeral(row, pending=True)
    preference = row.remember_me is True
    proof = await guard_accounts(db, [captured])
    principal = await validate_captured(db, captured, proof=proof)
    spent = await db.execute(
        delete(AuthEphemeralToken)
        .where(
            AuthEphemeralToken.id == row.id,
            AuthEphemeralToken.token == body.oidc_token,
            AuthEphemeralToken.token_type == TokenType.OIDC_EXCHANGE,
            AuthEphemeralToken.auth_source == "oidc",
            AuthEphemeralToken.expires_at > utcnow(),
            *binding_conditions(captured),
        )
        .returning(AuthEphemeralToken.id)
    )
    if spent.scalar_one_or_none() is None:
        raise refused()
    user = principal.user
    factor = (await db.execute(select(UserTOTP).where(UserTOTP.user_id == user.id))).scalar_one_or_none()
    totp_enabled = bool(factor and factor.is_enabled)
    email_enabled = await _get_email_2fa_enabled(db, user.id)
    verified = VerifiedOutcome(capture_account(user), "oidc")
    if totp_enabled or email_enabled:
        challenge = secrets.token_urlsafe(32)
        pending = await create_pre_auth_token(
            db, user.username, challenge_id=challenge, remember_me=preference, verified=verified
        )
        response.set_cookie(
            key="2fa_challenge",
            value=challenge,
            httponly=True,
            secure=raw_request.url.scheme == "https",
            samesite="lax",
            max_age=300,
            path="/api/v1/auth/2fa",
        )
        methods = (
            (["totp"] if totp_enabled else [])
            + (["email"] if email_enabled else [])
            + (["backup"] if totp_enabled else [])
        )
        return LoginResponse(
            requires_2fa=True, pre_auth_token=pending, two_fa_methods=methods, user=_user_to_response(user)
        )
    from backend.app.api.routes.auth import _issue_refresh_cookie

    issued = await _issue_refresh_cookie(db, response, raw_request, verified=verified, remember_me=preference)
    await db.commit()
    return LoginResponse(access_token=issued.access_token, token_type="bearer", user=_user_to_response(issued.user))


@router.get("/oidc/links", response_model=list[OIDCLinkResponse])
async def list_oidc_links(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
) -> list[OIDCLinkResponse]:
    """List all OIDC provider links for the current user."""
    proof = await guard_accounts(db, [current_user._auth_capture])
    current_user = (await validate_captured(db, current_user._auth_capture, proof=proof)).user
    result = await db.execute(
        select(UserOIDCLink).where(UserOIDCLink.user_id == current_user.id).options(selectinload(UserOIDCLink.provider))
    )
    links = result.scalars().all()
    # Defensive null-check on link.provider: on PostgreSQL the FK cascade
    # ensures the provider exists, but SQLite ships with FK enforcement
    # off, so a deleted provider could in theory leave the link briefly
    # orphan until the next init_db() cleanup runs. Returning "<deleted>"
    # instead of crashing keeps the endpoint usable in that edge case.
    # Upstream Bambuddy #1285 follow-up / commit 4d8dbc83.
    return [
        OIDCLinkResponse(
            id=link.id,
            provider_id=link.provider_id,
            provider_name=link.provider.name if link.provider else "<deleted>",
            provider_email=link.provider_email,
            created_at=link.created_at.isoformat(),
        )
        for link in links
    ]


@router.delete("/oidc/links/{provider_id}")
async def remove_oidc_link(
    provider_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_auth_db),
) -> dict:
    """Remove the OIDC link between the current user and a provider."""
    # Serialize simultaneous removals on the account, not on individual links:
    # two different final links must not both pass an unlocked count.
    proof = await guard_accounts(db, [current_user._auth_capture])
    await validate_captured(db, current_user._auth_capture, proof=proof)
    await take_write_lock(db, User.__table__, current_user.id)
    user = (await db.execute(select(User).where(User.id == current_user.id).with_for_update())).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    result = await db.execute(
        select(UserOIDCLink)
        .where(UserOIDCLink.user_id == current_user.id)
        .where(UserOIDCLink.provider_id == provider_id)
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC link not found")

    remaining = await db.scalar(
        select(UserOIDCLink.id)
        .join(OIDCProvider, OIDCProvider.id == UserOIDCLink.provider_id)
        .where(
            UserOIDCLink.user_id == user.id,
            UserOIDCLink.provider_id != provider_id,
            OIDCProvider.is_enabled.is_(True),
        )
        .limit(1)
    )
    local_login = (
        env_bool("BAMDUDE_LOCAL_LOGIN", False, strict=False)
        or (await get_setting(db, "local_login_enabled") or "true").lower() == "true"
    )
    ldap_login = (await get_setting(db, "ldap_enabled") or "false").lower() == "true" and bool(
        await get_setting(db, "ldap_server_url")
    )
    alternative = (
        remaining is not None
        or (local_login and user.auth_source not in {"oidc", "ldap"} and bool(user.password_hash))
        or (user.auth_source == "ldap" and ldap_login)
    )
    if not alternative:
        raise HTTPException(status_code=400, detail="Cannot remove your last available login method")

    await db.delete(link)
    await db.commit()
    return {"message": "OIDC link removed"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _get_base_external_url(db: AsyncSession) -> str:
    """Return the base external URL (no trailing slash, no /login suffix)."""
    external_url = await get_setting(db, "external_url")
    if external_url:
        return external_url.rstrip("/")
    return os.environ.get("APP_URL", "http://localhost:5173").rstrip("/")


async def _verified_factor_for_mutation(db, user, code):
    await check_rate_limit(db, user.username)
    row = (await db.execute(select(UserTOTP).where(UserTOTP.user_id == user.id))).scalar_one_or_none()
    if row is None or not row.is_enabled:
        raise HTTPException(400, "TOTP is not enabled")
    expected, hashes = row._secret_enc, row.backup_code_hashes
    failed_decryption = False
    try:
        secret = row.secret
        valid_totp = pyotp.TOTP(secret).verify(code, valid_window=1)
    except RuntimeError:
        secret, valid_totp, failed_decryption = None, False, True
    await db.rollback()
    valid_backup = False
    if not valid_totp:
        for hashed in hashes:
            valid_backup = await verify_password_async(code, hashed) or valid_backup
    if not valid_totp and not valid_backup:
        if not failed_decryption:
            await record_failed_attempt(db, user.username)
        raise HTTPException(400, "Invalid code")
    proof = await guard_accounts(db, [user._auth_capture])
    principal = await validate_captured(db, user._auth_capture, proof=proof)
    row = (
        await db.execute(
            select(UserTOTP)
            .where(UserTOTP.user_id == principal.user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or not row.is_enabled or row._secret_enc != expected or row.backup_code_hashes != hashes:
        raise HTTPException(409, "Authenticator configuration changed")
    if valid_totp:
        _assert_totp_not_replayed(pyotp.TOTP(secret), row, code)
    return row
