"""Where the stored Bambu Cloud credential lives, and whether Bambu rejected it.

The service seam for the Bambu Cloud bearer (upstream #2845). The cloud routes,
the preset resolver and every model provider that runs on the same token
(MakerWorld) read and flag it here, so no service has to import a router
module to learn who is signed in.

Two identity shapes, both real: a signed-in ``User`` carries its own columns;
``None`` — an API key without an owner — reads and writes the global
``Settings`` rows.

Renewing an expired token (refresh + persist, else mark dead) lives here too
(:func:`renew_or_invalidate`): MakerWorld runs on the same bearer and must be
able to renew it without importing a router. Storing a fresh sign-in stays in
``api/routes/cloud.py``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings
from backend.app.models.user import User

logger = logging.getLogger(__name__)

# Keys for storing cloud credentials in settings
CLOUD_TOKEN_KEY = "bambu_cloud_token"
CLOUD_REFRESH_TOKEN_KEY = "bambu_cloud_refresh_token"
CLOUD_EMAIL_KEY = "bambu_cloud_email"
CLOUD_REGION_KEY = "bambu_cloud_region"
# Ownerless (API-key fallback) counterpart of ``User.cloud_token_invalid_at``.
# Stores an ISO timestamp; absent/empty means "not known to be dead".
CLOUD_TOKEN_INVALID_KEY = "bambu_cloud_token_invalid_at"


async def is_cloud_token_invalid(db: AsyncSession, user: User | None = None) -> bool:
    """Whether the stored Bambu token is known to have been rejected.

    Set by :func:`mark_cloud_token_invalid` the first time Bambu answers its
    genuine expiry 401, cleared on a fresh login/logout. This is the only durable
    record we have: Bambu's access token is opaque (no readable expiry) and we do
    not persist the refresh token, so without this flag a dead credential looks
    exactly like a live one (upstream #2562).
    """
    if user is not None:
        return user.cloud_token_invalid_at is not None
    result = await db.execute(select(Settings).where(Settings.key == CLOUD_TOKEN_INVALID_KEY))
    row = result.scalar_one_or_none()
    return bool(row and row.value)


async def mark_cloud_token_invalid(captured) -> None:
    """Record that Bambu rejected the stored token.

    Opens its own session on purpose. This runs from
    ``BambuCloudService._on_auth_failure``, i.e. in the middle of a route that is
    about to fail — writing through that route's session would tie the flag to a
    transaction the route may still roll back, and the fact that the credential
    is dead is true regardless of how the request ends.

    Best-effort: a bookkeeping failure must never replace the 401 the caller
    actually needs to see.
    """
    from datetime import datetime, timezone

    from backend.app.core.database import async_session
    from backend.app.services.cloud_credentials import CredentialSnapshot, write

    if not isinstance(captured, CredentialSnapshot):
        # A numeric account address cannot safely identify an earlier network request.
        return

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            values = (
                {"cloud_token_invalid_at": now}
                if captured.user_id is not None
                else {CLOUD_TOKEN_INVALID_KEY: now.isoformat()}
            )
            await write(db, captured, values)
        logger.warning("Bambu Cloud rejected the stored credential")
    except Exception as exc:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException) and exc.status_code in (401, 403, 409, 503):
            return  # Logout/rotation/restore won; this old failure says nothing about the replacement.
        logger.exception("Could not record the Bambu Cloud token as invalid")


async def _persist_renewed(captured, access_token: str, refresh_token: str | None):
    """Store a renewed pair under the snapshot that began the request.

    Returns the snapshot of the stored pair, or None when a logout, a new
    sign-in or a restore won the race — an older refresh never resurrects a
    credential somebody replaced.
    """
    from fastapi import HTTPException

    from backend.app.core.database import async_session
    from backend.app.services.cloud_credentials import write

    try:
        async with async_session() as db:
            return await write(
                db,
                captured,
                {
                    "cloud_token": access_token,
                    "cloud_refresh_token": refresh_token,
                    "cloud_token_invalid_at": None,
                },
            )
    except HTTPException as exc:
        if exc.status_code not in (401, 403, 409, 503):
            raise
        return None


async def renew_or_invalidate(captured, cloud=None, *, mark_invalid=None):
    """Bambu answered its expiry 401: renew the access token, else record it dead.

    The one answer to "the stored token expired", shared by the cloud routes
    and by every provider on the same bearer (MakerWorld). ``cloud`` is the
    caller's own :class:`BambuCloudService` when it has one (it then carries
    the renewed pair); without it a service is built from the stored refresh
    token and region, and closed again.

    Returns ``(outcome, access_token, snapshot)``:

    * ``RENEWED`` — a new pair is stored; ``access_token`` is the new bearer
      and ``snapshot`` the stored pair's snapshot (None when the store lost a
      race with a logout or a new sign-in);
    * ``RENEW_UNREACHABLE`` — the renewal could not reach Bambu. Nothing is
      written: a network failure never invalidates a stored token;
    * ``None`` — not renewable (no refresh token, or Bambu refused it): the
      token is marked invalid through :func:`mark_cloud_token_invalid`.

    ``mark_invalid`` lets a caller supply the marker it was built with.
    """
    from backend.app.services.bambu_cloud import RENEW_UNREACHABLE, RENEWED, BambuCloudService
    from backend.app.services.cloud_credentials import CredentialSnapshot, current

    mark_invalid = mark_invalid or mark_cloud_token_invalid
    owned = None
    try:
        if cloud is None and isinstance(captured, CredentialSnapshot):
            from backend.app.core.database import async_session

            async with async_session() as db:
                # The stored refresh token is spent only for the credential the
                # failed request was made with: after a logout or a new sign-in
                # it belongs to somebody else's session, and this old expiry
                # says nothing about it.
                if not await current(db, captured):
                    return None, None, None
                owner = await db.get(User, captured.user_id) if captured.user_id is not None else None
                refresh = await get_stored_refresh_token(db, owner)
                _token, _email, region = await get_stored_token(db, owner)
            if refresh:
                cloud = owned = BambuCloudService(region=region)
                owned.set_refresh_token(refresh)
        if cloud is not None:
            try:
                renewed = await cloud.refresh_access_token()
            except Exception:
                logger.warning("Bambu Cloud credential refresh did not complete")
                renewed = False
            if renewed:
                stored = await _persist_renewed(captured, cloud.access_token, cloud.refresh_token)
                return RENEWED, cloud.access_token, stored
            if getattr(cloud, "last_refresh_failure", None) == RENEW_UNREACHABLE:
                return RENEW_UNREACHABLE, None, None
    finally:
        if owned is not None:
            await owned.close()
    await mark_invalid(captured)
    return None, None, None


def _normalise_region(region: str | None) -> str:
    """Treat NULL/empty/unknown as 'global' for legacy rows that predate the region column."""
    return region if region in ("global", "china") else "global"


async def get_stored_token(db: AsyncSession, user: User | None = None) -> tuple[str | None, str | None, str]:
    """Get stored cloud token, email, and region.

    When a user is provided, returns that user's per-user credentials.
    When user is None (an API key without an owner), falls back to the global
    Settings table.
    Region defaults to ``"global"`` when unset (including for rows that predate
    the ``cloud_region`` column).
    """
    if user is not None:
        return user.cloud_token, user.cloud_email, _normalise_region(user.cloud_region)

    # Fallback: global storage (no owning user)
    result = await db.execute(
        select(Settings).where(Settings.key.in_([CLOUD_TOKEN_KEY, CLOUD_EMAIL_KEY, CLOUD_REGION_KEY]))
    )
    settings = {s.key: s.value for s in result.scalars().all()}
    return (
        settings.get(CLOUD_TOKEN_KEY),
        settings.get(CLOUD_EMAIL_KEY),
        _normalise_region(settings.get(CLOUD_REGION_KEY)),
    )


async def get_stored_refresh_token(db: AsyncSession, user: User | None = None) -> str | None:
    """The stored refresh token, or None. Separate from :func:`get_stored_token`
    so its seven existing 3-tuple call sites stay untouched — only the
    authenticated-service builder needs this."""
    if user is not None:
        return user.cloud_refresh_token
    result = await db.execute(select(Settings).where(Settings.key == CLOUD_REFRESH_TOKEN_KEY))
    row = result.scalar_one_or_none()
    return row.value or None if row else None
