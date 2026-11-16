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

import asyncio
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


def _renewed_values(captured, access_token: str, refresh_token: str | None) -> dict:
    """A renewed pair in the words of the storage that holds the credential.

    A ``User`` credential is columns; the ownerless one is ``Settings`` rows
    under their own keys, whose value is never NULL — there "not known to be
    dead" is the empty string (see :data:`CLOUD_TOKEN_INVALID_KEY`), and a
    refresh token Bambu did not rotate keeps its row.
    """
    if captured.user_id is not None:
        return {"cloud_token": access_token, "cloud_refresh_token": refresh_token, "cloud_token_invalid_at": None}
    values = {CLOUD_TOKEN_KEY: access_token, CLOUD_TOKEN_INVALID_KEY: ""}
    if refresh_token:
        values[CLOUD_REFRESH_TOKEN_KEY] = refresh_token
    return values


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
            return await write(db, captured, _renewed_values(captured, access_token, refresh_token))
    except HTTPException as exc:
        if exc.status_code not in (401, 403, 409, 503):
            raise
        return None


# One renewal at a time per credential. Every consumer of the stored sign-in
# renews an expired access token, several of them at once (the Slice dialog),
# and Bambu honours a refresh token once: without this the callers that lost
# the race were refused and reported — or recorded — "expired" for a sign-in
# another caller had just renewed. The lock is keyed by the credential's
# identity; what a renewal settled is remembered per snapshot, so a caller
# that waited uses the stored pair instead of asking Bambu again.
_REFUSED = object()
_RENEWAL_MEMORY = 8
_renewal_locks: dict[tuple, tuple[object, asyncio.Lock]] = {}
_renewal_outcomes: dict[tuple, dict[tuple, object]] = {}


def _renewal_identity(captured) -> tuple:
    from backend.app.services.cloud_credentials import CredentialSnapshot

    if isinstance(captured, CredentialSnapshot):
        return (captured.account_identity, captured.provider)
    return ("unidentified",)


def _renewal_lock(identity: tuple) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    held = _renewal_locks.get(identity)
    if held is None or held[0] is not loop:
        held = _renewal_locks[identity] = (loop, asyncio.Lock())
    return held[1]


def _remember_renewal(captured, outcome) -> None:
    """What became of the credential ``captured`` names: its renewed snapshot, or ``_REFUSED``."""
    settled = _renewal_outcomes.setdefault(_renewal_identity(captured), {})
    settled[captured.namespace] = outcome
    while len(settled) > _RENEWAL_MEMORY:
        settled.pop(next(iter(settled)))


def _settled_renewal(captured):
    """The newest snapshot an earlier renewal of ``captured`` led to, ``_REFUSED``, or None."""
    settled = _renewal_outcomes.get(_renewal_identity(captured), {})
    found = None
    for _ in range(_RENEWAL_MEMORY):
        following = settled.get((found or captured).namespace)
        if following is None:
            break
        found = following
        if found is _REFUSED:
            break
    return found


async def _adopt_renewed(renewed, cloud):
    """Use the pair another caller's renewal stored — nothing is asked of Bambu.

    Returns the ``RENEWED`` answer, or None when that pair is no longer the
    stored one (a logout, a new sign-in or a restore came after it).
    """
    from backend.app.core.database import async_session
    from backend.app.services.bambu_cloud import RENEWED
    from backend.app.services.cloud_credentials import current

    async with async_session() as db:
        if not await current(db, renewed):
            return None
        owner = await db.get(User, renewed.user_id) if renewed.user_id is not None else None
        token, _email, _region = await get_stored_token(db, owner)
        refresh = await get_stored_refresh_token(db, owner)
    if not token:
        return None
    if cloud is not None:
        cloud.set_token(token)
        cloud.set_refresh_token(refresh)
    return RENEWED, token, renewed


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

    One renewal runs at a time per credential. A caller that waited for
    another caller's renewal of the same stored pair gets ``RENEWED`` with the
    pair that renewal stored — Bambu is not asked again, and nothing is marked.

    ``mark_invalid`` lets a caller supply the marker it was built with.
    """
    async with _renewal_lock(_renewal_identity(captured)):
        return await _renew_or_invalidate(captured, cloud, mark_invalid or mark_cloud_token_invalid)


async def _renew_or_invalidate(captured, cloud, mark_invalid):
    from backend.app.services.bambu_cloud import RENEW_UNREACHABLE, RENEWED, BambuCloudService
    from backend.app.services.cloud_credentials import CredentialSnapshot, current

    identified = isinstance(captured, CredentialSnapshot)
    if identified:
        settled = _settled_renewal(captured)
        if settled is _REFUSED:
            # Bambu already refused this very refresh token; asking again
            # changes nothing. The mark is a compare-and-set, so it lands once.
            await mark_invalid(captured)
            return None, None, None
        if settled is not None:
            # Another caller renewed this sign-in while we waited. If its pair
            # was replaced since (logout, new sign-in), this old expiry says
            # nothing about the replacement: nothing is spent and nothing marked.
            return await _adopt_renewed(settled, cloud) or (None, None, None)
    owned = None
    try:
        if cloud is None and identified:
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
            except Exception as exc:
                # Not an answer from Bambu (a client closed at shutdown, a
                # bug): nothing is known about the credential, so nothing is
                # marked — the same as a renewal that could not reach Bambu.
                logger.warning("Bambu Cloud credential refresh did not complete (%s)", type(exc).__name__)
                return RENEW_UNREACHABLE, None, None
            if renewed:
                for attempt in range(2):
                    try:
                        stored = await _persist_renewed(captured, cloud.access_token, cloud.refresh_token)
                        break
                    except Exception as exc:
                        # Bambu issued the pair and we could not write it (a
                        # locked database, a dropped connection). The sign-in
                        # is not dead and is never marked so; only the type is
                        # logged — a driver error can quote the row it failed on.
                        logger.error(
                            "A renewed Bambu Cloud sign-in could not be stored (%s)%s",
                            type(exc).__name__,
                            "" if attempt else "; trying once more",
                        )
                else:
                    return RENEW_UNREACHABLE, None, None
                if stored is not None:
                    _remember_renewal(captured, stored)
                return RENEWED, cloud.access_token, stored
            if getattr(cloud, "last_refresh_failure", None) == RENEW_UNREACHABLE:
                return RENEW_UNREACHABLE, None, None
    finally:
        if owned is not None:
            await owned.close()
    await mark_invalid(captured)
    if identified:
        _remember_renewal(captured, _REFUSED)
    return None, None, None


async def renewal_credentials(db: AsyncSession, user: User | None, *, allow_global: bool = False):
    """What a caller that builds its own ``BambuCloudService`` needs to renew.

    Returns ``(snapshot, refresh_token)`` for the stored credential of ``user``
    (or of the ownerless ``Settings`` rows when ``allow_global`` — a server-owned
    job such as the Git backup, never a request). ``(None, None)`` when renewal
    cannot be prepared (no owner, credential admission closed, the account
    changed): the caller then works as before — an expiry is reported, not
    renewed. Preparing a renewal is an extra and never fails the caller.
    """
    from backend.app.services.cloud_credentials import snapshot

    if user is None and not allow_global:
        return None, None
    try:
        captured = await snapshot(db, user, "bambu", allow_global=allow_global)
        owner = await db.get(User, captured.user_id, populate_existing=True) if captured.user_id is not None else None
        return captured, await get_stored_refresh_token(db, owner)
    except Exception:
        logger.warning("Bambu Cloud renewal could not be prepared; an expired sign-in will be reported as expired")
        return None, None


async def renew_for_service(cloud, captured) -> str | None:
    """``on_auth_failure`` for a ``BambuCloudService`` built outside the cloud routes.

    Delegates to :func:`renew_or_invalidate` with the caller's own service (it
    then carries the renewed pair, and repeats its request once) and keeps the
    service's snapshot on the stored pair.
    """
    from backend.app.services.cloud_credentials import CredentialSnapshot

    held = getattr(cloud, "credential_snapshot", None)
    outcome, _token, stored = await renew_or_invalidate(
        held if isinstance(held, CredentialSnapshot) else captured, cloud
    )
    if stored is not None:
        cloud.credential_snapshot = stored
    return outcome


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
