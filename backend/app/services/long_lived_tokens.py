"""Service layer for long-lived camera-stream tokens (#1108).

Token format: ``bblt_<8-char-prefix>_<32-char-secret>``.

- The full token is shown to the user **exactly once** at create time.
- ``lookup_prefix`` (the 8-char middle part) is indexed and used to cheaply
  fetch the candidate row — at most one in practice — without scanning the
  whole table on every request.
- ``secret_hash`` is a pbkdf2_sha256 hash of the full token (matching the
  rest of the codebase's password hashing). Even a DB dump can't be replayed
  against the camera endpoint.
- ``last_used_at`` is updated on successful verify, but rate-limited to once
  per minute per token so an MJPEG keep-alive doesn't write to the DB on
  every chunk.
- ``revoked_at`` set → verify returns False. Retired upgrade records can be
  deleted by their owner or an admin; deleting metadata never restores access.

Maximum lifetime is 365 days (issue #1108 explicitly rejected "infinite"
tokens — a leaked permanent token would be irrevocable footgun-by-design).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import _scan_api_key, hash_password_async, reserve_audit_attempt
from backend.app.core.auth_lifetime import capture_long_lived, guard_accounts, utcnow, validate_captured
from backend.app.core.auth_runtime import auth_runtime
from backend.app.core.database import async_session
from backend.app.models.long_lived_token import LongLivedToken

# Issue #1108 hard cap. Bump here if policy changes — UI default is shorter
# (90 days) and the create route enforces this ceiling.
MAX_TOKEN_LIFETIME_DAYS = 365

# Only V1 scope. Adding "snapshot" or "control" later means adding a value
# to this tuple and an `if scope == ...` branch in the route, no schema work.
# Every scope is a separate grant, never implied by another. A token minted for
# one purpose must not silently widen when a later scope is added.
#
#   camera_stream — the MJPEG stream / snapshot endpoints and nothing else
#                   (#1108). What a Home Assistant or Frigate card needs.
#   camwall       — those same streams *plus* the read-only tile metadata the
#                   Cam Wall draws: printer names and print state (upstream
#                   #2531). Strictly wider than camera_stream, so it gets its
#                   own scope rather than quietly extending tokens already
#                   handed out. Notably it does NOT expose the print filename.
#   overlay       — the streaming overlay (upstream #2613): the camera stream
#                   plus the single-printer status the /overlay page draws,
#                   which *includes the print filename*. A distinct grant
#                   precisely because it reveals the part name; folding it into
#                   camera_stream would silently give every token already handed
#                   out for video the ability to read what is being printed.
# monitor is metadata-only: deliberately absent from STREAM_SCOPES below.
ALLOWED_SCOPES: frozenset[str] = frozenset({"camera_stream", "camwall", "overlay", "monitor"})

# Scopes the camera stream / snapshot endpoints honour. A Cam Wall or overlay
# token has to be able to pull the video its own view is showing.
STREAM_SCOPES: tuple[str, ...] = ("camera_stream", "camwall", "overlay")

# Don't write to last_used_at more than once per minute per token. MJPEG
# streams call verify() at most once per fetch (the browser holds the
# connection open), but snapshots may rapid-fire — this caps DB churn.
_LAST_USED_DEBOUNCE = timedelta(minutes=1)

# Token format constants — kept in one place so format changes are localized.
_TOKEN_PREFIX = "bblt_"
_LOOKUP_LEN = 8
_SECRET_LEN = 32  # urlsafe characters → ~190 bits of entropy


@dataclass(frozen=True)
class CreatedToken:
    """Returned to the route on create. ``plaintext`` is shown to the user
    exactly once and never persisted; only ``record`` survives in the DB.
    """

    record: LongLivedToken
    plaintext: str


def _generate_token_parts() -> tuple[str, str, str]:
    """Return ``(plaintext, lookup_prefix, hash_input)``.

    ``hash_input`` is the same string we hand to pbkdf2 so verify() can
    produce a matching hash from the user-submitted token.

    The prefix is hex on purpose — ``token_urlsafe`` can emit ``_`` which
    would collide with the ``bblt_<prefix>_<secret>`` format separator and
    break the parser. Hex is fine for a non-secret indexed lookup column;
    the security comes from the 32-char ``token_urlsafe`` secret part.
    """
    lookup_prefix = secrets.token_hex(_LOOKUP_LEN // 2)  # 4 bytes → 8 hex chars
    secret_part = secrets.token_urlsafe(48).replace("_", "").replace("-", "")[:_SECRET_LEN]
    plaintext = f"{_TOKEN_PREFIX}{lookup_prefix}_{secret_part}"
    return plaintext, lookup_prefix, plaintext


def _parse_token(token: str) -> tuple[str, str] | None:
    """Pull ``(lookup_prefix, full_token)`` from a submitted string.

    Returns None if the format doesn't match — short-circuits the DB lookup
    on garbage / wrong-format inputs.
    """
    if not token.startswith(_TOKEN_PREFIX):
        return None
    rest = token[len(_TOKEN_PREFIX) :]
    sep = rest.find("_")
    if sep != _LOOKUP_LEN:
        return None
    lookup_prefix = rest[:_LOOKUP_LEN]
    return lookup_prefix, token


def _is_expired(record: LongLivedToken, now: datetime) -> bool:
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= now


async def create_token(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    expires_in_days: int,
    scope: str = "camera_stream",
    authority,
) -> CreatedToken:
    """Mint a new long-lived token. Caller is responsible for permission checks.

    Raises ValueError if ``expires_in_days`` exceeds the policy cap or
    ``scope`` is not in ``ALLOWED_SCOPES``. The route translates these into
    a 400 with the offending field.
    """
    if scope not in ALLOWED_SCOPES:
        raise ValueError(f"unsupported scope: {scope!r}")
    if expires_in_days <= 0:
        raise ValueError("expires_in_days must be positive (#1108: no infinite tokens)")
    if expires_in_days > MAX_TOKEN_LIFETIME_DAYS:
        raise ValueError(f"expires_in_days exceeds policy maximum of {MAX_TOKEN_LIFETIME_DAYS}")
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 100:
        raise ValueError("name must be 100 chars or fewer")

    plaintext, lookup_prefix, hash_input = _generate_token_parts()
    secret_hash = await hash_password_async(hash_input)
    proof = await guard_accounts(db, [authority])
    principal = await validate_captured(db, authority, proof=proof)
    if principal.user.id != user_id or authority.kind != "session":
        raise HTTPException(403, "A signed-in owner is required")
    required = "api_keys:create" if scope == "monitor" else "camera:view"
    if not principal.user.has_permission(required):
        raise HTTPException(403, "Required permission is no longer granted")
    record = LongLivedToken(
        user_id=principal.user.id,
        owner_identity=principal.user.account_identity,
        contract_version=1,
        name=name,
        lookup_prefix=lookup_prefix,
        secret_hash=secret_hash,
        scope=scope,
        expires_at=utcnow() + timedelta(days=expires_in_days),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return CreatedToken(record=record, plaintext=plaintext)


async def verify_token(db, token, *, scope="camera_stream"):
    # Raw-secret verification owns short sessions; never commits a handler's DB.
    task = auth_runtime.spawn(_resolve_token(token, scope))
    return await asyncio.shield(task)


def _coerce_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def list_user_tokens(db: AsyncSession, user_id: int) -> list[LongLivedToken]:
    """Current tokens plus visibly retired upgrade records, newest first.

    Ordinary revoked tokens stay hidden; expired tokens remain manageable.
    """
    result = await db.execute(
        select(LongLivedToken)
        .where(
            LongLivedToken.user_id == user_id,
            or_(
                LongLivedToken.revoked_at.is_(None),
                LongLivedToken.retired_at.is_not(None),
                LongLivedToken.retired_reason.is_not(None),
            ),
        )
        .order_by(LongLivedToken.created_at.desc())
    )
    return list(result.scalars().all())


async def list_all_tokens(db: AsyncSession) -> list[LongLivedToken]:
    """Admin view of current tokens and visibly retired upgrade records."""
    result = await db.execute(
        select(LongLivedToken)
        .where(
            or_(
                LongLivedToken.revoked_at.is_(None),
                LongLivedToken.retired_at.is_not(None),
                LongLivedToken.retired_reason.is_not(None),
            )
        )
        .order_by(LongLivedToken.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_token(db, token_id: int):
    record = await db.get(LongLivedToken, token_id)
    if record is None:
        return False
    if record.owner_identity:
        capture = capture_long_lived(record)
        await guard_accounts(db, [capture], require_active=False)
        record = (
            await db.execute(
                select(LongLivedToken)
                .where(LongLivedToken.id == token_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = utcnow()
    await db.commit()
    return True


async def remove_retired_token(db, token_id: int, expected_identity: str) -> bool:
    """Remove only the selected retired record; caller checks actor and scope.

    Metadata deletion is a distinct intent from revoking a current token. Both
    its immutable identity and retirement must still match at the actual write.
    """
    deleted = await db.execute(
        delete(LongLivedToken).where(
            LongLivedToken.id == token_id,
            LongLivedToken.token_identity == expected_identity,
            or_(LongLivedToken.retired_at.is_not(None), LongLivedToken.retired_reason.is_not(None)),
        )
    )
    await db.commit()
    return deleted.rowcount == 1


_audit_after: OrderedDict[str, float] = OrderedDict()


async def _resolve_token(token, scope):
    from backend.app.core.auth import AUTH_DATABASE_FAILURES, APIKeyValidationFailure

    parsed = _parse_token(token)
    if parsed is None:
        return None
    prefix, full = parsed
    scopes = (scope,) if isinstance(scope, str) else tuple(scope)
    try:
        async with async_session() as db:
            auth_runtime.track_transaction(db)
            rows = (
                await db.execute(
                    select(
                        LongLivedToken.id,
                        LongLivedToken.secret_hash,
                        LongLivedToken.token_identity,
                        LongLivedToken.owner_identity,
                        LongLivedToken.user_id,
                    ).where(
                        LongLivedToken.lookup_prefix == prefix,
                        LongLivedToken.scope.in_(scopes),
                        LongLivedToken.contract_version == 1,
                        LongLivedToken.revoked_at.is_(None),
                        LongLivedToken.retired_at.is_(None),
                        LongLivedToken.retired_reason.is_(None),
                    )
                )
            ).all()
        winner = await _scan_api_key(full, [(r.id, r.secret_hash) for r in rows])
        if winner is None:
            return None
        original = next(r for r in rows if r.id == winner[0] and r.secret_hash == winner[1])
        from backend.app.core.auth_lifetime import FrozenAuthority

        captured = FrozenAuthority(
            "long_lived", original.owner_identity or "", original.user_id, token_identity=original.token_identity
        )
        if await _read_token_authority(captured, winner[1], scopes) is None:
            return None
        await _audit_use(captured)
        return await _read_token_authority(captured, winner[1], scopes)
    except APIKeyValidationFailure as exc:
        raise HTTPException(503, "Credential verification is busy") from exc
    except AUTH_DATABASE_FAILURES as exc:
        raise HTTPException(503, "Authentication temporarily unavailable", headers={"Retry-After": "5"}) from exc


async def _read_token_authority(captured, winning_hash, scopes):
    async with async_session() as db:
        auth_runtime.track_transaction(db)
        try:
            principal = await validate_captured(db, captured)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return None
        record = principal.long_lived_token
        if record.secret_hash != winning_hash or record.scope not in scopes:
            return None
        needed = ("printers:read", "queue:read") if record.scope == "monitor" else ("camera:view",)
        if not all(principal.user.has_permission(p) for p in needed):
            return None
        record._auth_capture = principal.capture
    return record


async def _audit_use(captured):
    from backend.app.core.auth import AUTH_DATABASE_FAILURES

    loop = asyncio.get_running_loop()
    identity = captured.token_identity
    if not reserve_audit_attempt(_audit_after, identity, loop.time()):
        return
    try:
        async with async_session() as db:
            auth_runtime.track_transaction(db)
            await db.execute(
                update(LongLivedToken)
                .where(
                    LongLivedToken.token_identity == identity,
                    LongLivedToken.owner_identity == captured.account_identity,
                    LongLivedToken.contract_version == 1,
                    LongLivedToken.revoked_at.is_(None),
                    LongLivedToken.retired_at.is_(None),
                    LongLivedToken.retired_reason.is_(None),
                )
                .values(last_used_at=utcnow())
            )
            await db.commit()
    except AUTH_DATABASE_FAILURES:
        logging.getLogger(__name__).warning("Long-lived token usage audit unavailable")
