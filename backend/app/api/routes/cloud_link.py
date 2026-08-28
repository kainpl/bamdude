"""Cloud Link settings API — the six calls the settings page makes.

Thin by design. The store owns persistence, ``pairing`` owns the one outward
HTTPS call, the service owns the task. What is decided *here* is four things,
and each of them is here because no lower layer can make it.

**Authorization.** ``cloud_link:manage`` decides whether this farm answers to
something outside the LAN, so every route sits behind it and none of them is
reachable with an API key (``core/auth.py`` denies the permission outright — an
automation token must not be able to mint an instance secret).

**Validating the publish set.** ``set_publish_set`` stores what it is given and
the uplink filters what it publishes; neither can tell a user *why* a printer
was refused, and neither is reached at all by a request naming a printer that
does not exist. This layer holds the request and a session, so it is the only
one that can answer with the offending ids. It refuses the whole save rather
than the offending half: a partial save on an allowlist is the worst outcome
available, because the user reads the page as having stored what they saw.

**Turning three pairing failures into three repairs.** ``bad_format`` → fix the
typing (400), ``invalid_code`` → fetch a new code (404), ``network`` → try
again later (502). ⚠️ ``network`` is NOT only a transport failure: a portal
that answers 500, or a proxy that answers 502, arrives here as ``network``
too, so the message says "refused or unreachable" and never sends the user to
check their router.

**Applying a change, not merely saving it.** A running link holds its own copy
of the publish set and its own socket, so pairing restarts it, the toggle
starts or stops it, and a new publish set asks it for a fresh snapshot. A route
that wrote the row and stopped would leave the portal being told about the farm
as it was before the user touched it.

Two shapes worth naming:

* **Every mutating route answers with the full status.** The page is already
  displaying these fields and a save changes several at once — an
  acknowledgement would leave it stale for a round trip and wrong if the
  re-fetch failed.
* ⚠️ **``POST /pair`` blocks for up to 15 s.** It is a synchronous HTTPS call
  to the portal (``PAIR_TIMEOUT_SECONDS``), and there is no job to poll:
  pairing happens once, with a person watching a spinner they started. Nothing
  else in this module talks to the network.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.cloud_link import CloudLinkAudit
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.cloud_link import (
    CloudLinkAuditEntry,
    CloudLinkAuditPage,
    CloudLinkEnabledRequest,
    CloudLinkPairRequest,
    CloudLinkPublishSetRequest,
    CloudLinkStatus,
)
from backend.app.services.cloud_link.pairing import PairingError, pair
from backend.app.services.cloud_link.service import cloud_link_service
from backend.app.services.cloud_link.store import (
    clear_credentials,
    get_config,
    get_publish_set,
    set_publish_set,
    validate_portal_url,
    write_audit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cloud-link", tags=["cloud-link"])

#: The largest page of audit rows we will build. The table is bounded only by
#: time and its summaries are free text the far end supplied, so "give me all
#: of it" is a response nobody can render.
MAX_AUDIT_PAGE_SIZE = 100

#: What the user is told when pairing failed for anything that is not a bad
#: code. ⚠️ Deliberately covers both halves — see the module docstring.
NETWORK_DETAIL = "The portal refused the pairing or is unreachable. Check the portal URL and try again in a moment."

_PAIRING_FAILURES = {
    "bad_format": (400, "That pairing code is not in the format the portal issues (for example ABCD-EFGH)."),
    "invalid_code": (
        404,
        "The portal does not know that pairing code. It may have expired or already been used — "
        "generate a new one and try again.",
    ),
    "network": (502, NETWORK_DETAIL),
}


async def _status(db: AsyncSession) -> CloudLinkStatus:
    """The one response shape, assembled from the service and the store.

    ``service.status`` answers the six fields that are the link's *state*;
    the three added here are configuration the service has no business
    reading through a request session. ``published_printer_ids`` is the raw
    allowlist — the uplink's availability filter belongs to what is
    published, not to what the user chose.
    """
    state = await cloud_link_service.status(db)
    config = await get_config(db)
    published = await get_publish_set(db)
    return CloudLinkStatus(
        **state,
        portal_url=config.portal_url,
        instance_id=config.instance_id,
        published_printer_ids=sorted(published),
    )


@router.get("/status", response_model=CloudLinkStatus)
async def get_status(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_LINK_MANAGE),
):
    """Everything the settings page shows. Reads only — no network call."""
    return await _status(db)


@router.post("/pair", response_model=CloudLinkStatus)
async def pair_instance(
    body: CloudLinkPairRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_LINK_MANAGE),
):
    """Redeem a pairing code, switch the link on, and bring it up.

    The portal URL is validated and saved **first** when one was supplied:
    ``pair`` reads it from the row, so there is no ordering in which the user's
    new portal is used without also being stored. A pairing that then fails
    leaves the new URL standing — it is where the user asked to point, and
    reverting it would make a second attempt silently retry the old portal.

    ``enabled`` is set here rather than in ``save_credentials`` because
    everywhere else in this subsystem holding a credential and choosing to use
    it are two decisions. Typing a code into this form is the one moment a user
    makes both at once.

    ⚠️ Blocks for up to ``PAIR_TIMEOUT_SECONDS`` — see the module docstring.
    """
    config = await get_config(db)

    if body.portal_url is not None:
        try:
            config.portal_url = validate_portal_url(body.portal_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.commit()

    try:
        await pair(db, body.pairing_code)
    except PairingError as exc:
        status_code, detail = _PAIRING_FAILURES.get(exc.code, (502, NETWORK_DETAIL))
        raise HTTPException(status_code=status_code, detail=detail) from exc

    config.enabled = True
    await db.commit()

    # A fresh credential is only useful once the link is rebuilt around it —
    # and a restart rather than a start because a farm that was already
    # connected on the previous credential has a socket to let go of.
    await cloud_link_service.restart()
    return await _status(db)


@router.post("/unpair", response_model=CloudLinkStatus)
async def unpair_instance(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_LINK_MANAGE),
):
    """Forget the pairing and take the link down. Idempotent.

    The order is the point: the link is stopped **before** the credential is
    deleted, so nothing is mid-reconnect with a secret that is about to go.

    ``revoked`` and ``last_error`` are deliberately left alone — they are the
    store's record of *why* a link ended, and an unpair that follows a
    revocation should not erase the explanation the user is reading. A later
    pairing clears both.

    The publish set is left alone too. It is the operator's answer to "which
    machines may leave the LAN", and a farm that unpairs to re-pair against the
    same portal should not have to rebuild it from memory. Nothing is published
    while unpaired, so keeping it exposes nothing.

    Idempotent because there are two ways to arrive here with nothing to do: a
    farm that was never paired, and a second click on a slow page. An error for
    either would report a problem with a link that is exactly as gone as the
    user wanted.
    """
    config = await get_config(db)
    instance_id = config.instance_id

    await cloud_link_service.stop()
    await clear_credentials(db)
    config.enabled = False
    await db.commit()

    # A row for something that never crossed the wire, on purpose: an unpair is
    # the answer to "why did this farm stop being visible", and that question is
    # asked of the audit table by somebody who did not do it. ``direction="up"``
    # because this side acted, the same way the pairing row reads.
    #
    # ⚠️ The summary names the instance, never the secret: the audit is read by
    # humans in a table and kept for a month.
    summary = f"unpaired from {config.portal_url}"
    if instance_id:
        summary += f" (instance {instance_id})"
    try:
        await write_audit(db, "up", "unpair", summary)
    except Exception as e:
        # Bookkeeping must not un-unpair an unpair. The credential is already
        # gone; a failed audit insert reported as an error would send the user
        # to repeat an operation that has already happened.
        #
        # ⚠️ The rollback is what makes that true. A commit that raises leaves
        # the session poisoned with the failed insert still pending, so the very
        # next statement — the ``_status`` read three lines below — would raise
        # ``PendingRollbackError`` and turn a successful unpair into a 500. The
        # unpair itself committed above, so there is nothing of ours left in the
        # session to lose here; only the audit row goes.
        await db.rollback()
        logger.warning("Cloud Link: unpaired, but the audit row could not be written — %s", e)

    logger.info("Cloud Link: unpaired%s", f" (was instance {instance_id})" if instance_id else "")
    return await _status(db)


@router.put("/publish-set", response_model=CloudLinkStatus)
async def update_publish_set(
    body: CloudLinkPublishSetRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_LINK_MANAGE),
):
    """Replace the allowlist of printers the portal may be told about.

    Validation lives here and nowhere else (see the module docstring). Both
    halves of availability are checked — ``is_active AND NOT archived``, the
    same definition the uplink publishes by — so a box the user ticks is a box
    that does something.

    The whole save is refused when any id is bad, and the ids are named. A
    partial save would leave the page showing a set the database does not hold,
    on the one control whose job is keeping a machine off the internet.
    """
    wanted = sorted(set(body.printer_ids))
    if wanted:
        available = set(
            (
                await db.execute(
                    select(Printer.id)
                    .where(Printer.id.in_(wanted))
                    .where(Printer.is_active.is_(True))
                    .where(Printer.archived.is_(False))
                )
            )
            .scalars()
            .all()
        )
        rejected = [pid for pid in wanted if pid not in available]
        if rejected:
            raise HTTPException(
                status_code=422,
                detail=(
                    "These printers cannot be published because they do not exist, are archived, "
                    f"or are in maintenance mode: {', '.join(str(pid) for pid in rejected)}."
                ),
            )

    await set_publish_set(db, wanted)
    # A running link holds its own copy of the set. ``request_snapshot`` drops
    # anything just unticked immediately and re-tells the portal the whole farm;
    # it is silent when no link is running.
    await cloud_link_service.request_snapshot()
    logger.info("Cloud Link: publish set saved (%d printer(s))", len(wanted))
    return await _status(db)


@router.put("/enabled", response_model=CloudLinkStatus)
async def set_enabled(
    body: CloudLinkEnabledRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_LINK_MANAGE),
):
    """The switch — and the socket goes with it.

    Committed before the service is asked to act, because ``start()`` reads
    this row to decide whether to do anything at all. Turning it off stops the
    link now rather than at the next restart: this is the user's only way to
    disconnect a farm in a hurry.

    Enabling an unpaired farm is allowed and does nothing — ``start()`` logs
    that it has no credential and returns, and the status this answers with
    shows ``enabled`` beside ``paired: false``, which is what the page needs to
    say so.
    """
    config = await get_config(db)
    config.enabled = bool(body.enabled)
    await db.commit()

    if config.enabled:
        await cloud_link_service.start()
    else:
        await cloud_link_service.stop()
    return await _status(db)


@router.get("/audit", response_model=CloudLinkAuditPage)
async def list_audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_AUDIT_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_LINK_MANAGE),
):
    """The record of what crossed the link, newest first.

    Ordered by ``ts`` and then by ``id``: the stamp is a server default and
    SQLite's has one-second resolution, so a burst of rows would otherwise come
    back in whatever order the scan produced them — and the page boundary would
    move between two requests for the same page.

    A page past the end is empty, not a 404. The table is swept daily and grows
    from the link itself, so a page that existed a second ago legitimately does
    not now; an error would be a dead end in the UI for something that is not
    wrong.
    """
    total = int((await db.execute(select(func.count()).select_from(CloudLinkAudit))).scalar_one())
    rows = (
        (
            await db.execute(
                select(CloudLinkAudit)
                .order_by(CloudLinkAudit.ts.desc(), CloudLinkAudit.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return CloudLinkAuditPage(
        items=[CloudLinkAuditEntry.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
