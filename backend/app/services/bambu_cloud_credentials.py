"""Where the stored Bambu Cloud credential lives, and whether Bambu rejected it.

The service seam for the Bambu Cloud bearer (upstream #2845). The cloud routes,
the preset resolver and every model provider that runs on the same token
(MakerWorld) read and flag it here, so no service has to import a router
module to learn who is signed in.

Two identity shapes, both real: a signed-in ``User`` carries its own columns;
``None`` — an API key without an owner — reads and writes the global
``Settings`` rows.

Renewing a token (refresh + persist) and storing a fresh sign-in stay in
``api/routes/cloud.py``: that is the cloud session's logic, not storage.
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


async def mark_cloud_token_invalid(user_id: int | None) -> None:
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

    from sqlalchemy import update

    from backend.app.core.database import async_session

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            if user_id is not None:
                await db.execute(update(User).where(User.id == user_id).values(cloud_token_invalid_at=now))
            else:
                result = await db.execute(select(Settings).where(Settings.key == CLOUD_TOKEN_INVALID_KEY))
                row = result.scalar_one_or_none()
                if row:
                    row.value = now.isoformat()
                else:
                    db.add(Settings(key=CLOUD_TOKEN_INVALID_KEY, value=now.isoformat()))
            await db.commit()
        logger.warning("Bambu Cloud rejected the stored token (user_id=%s) - marking the sign-in as expired", user_id)
    except Exception:
        logger.exception("Could not record the Bambu Cloud token as invalid")


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
