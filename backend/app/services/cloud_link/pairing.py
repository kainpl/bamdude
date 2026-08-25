"""Cloud Link pairing — the one exchange that turns a code into a credential.

Everything else the link does travels as a signed envelope frame over the
socket. Pairing cannot: there is no credential yet, so there is nothing to sign
with. It is therefore a single plain HTTPS POST, the only unauthenticated call
this agent ever makes outward, and the only place a string the user typed
reaches the network.

That shapes the whole module:

* **The format gate is first, and it is not politeness.** A code is checked
  against the alphabet the portal issues from before any socket is opened, so
  a half-typed code in a form field costs nothing and the portal never has to
  rate-limit our keystrokes on our behalf.
* **Failure is three words, not a stack trace.** ``bad_format``,
  ``invalid_code`` and ``network`` are the only outcomes a caller sees, because
  they are the only three that lead to different user actions: fix the typing,
  fetch a new code, or try again later. The detail goes to the log, where it
  helps whoever is debugging without asking the user to interpret it.
* **Nothing is stored until everything arrived.** ``save_credentials`` runs
  once, with both halves in hand — a farm that believes it is paired while
  holding half a credential cannot explain why nothing connects.
"""

from __future__ import annotations

import json
import logging
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import APP_VERSION
from backend.app.services.cloud_link.store import get_config, save_credentials, write_audit

logger = logging.getLogger(__name__)

#: Where the portal accepts a pairing code, appended below whatever path the
#: configured portal URL already carries.
PAIR_PATH = "/api/link/v1/pair"

#: One user waiting at a settings page. Long enough for a portal that is slow,
#: short enough that "nothing is happening" becomes an error message rather
#: than a spinner nobody knows how to end.
PAIR_TIMEOUT_SECONDS = 15

#: The alphabet the portal issues codes from: A–Z and 2–9 with ``I``, ``O``,
#: ``0`` and ``1`` removed, because a code is read off one screen and typed
#: into another and those four are each other under most fonts. A code
#: containing one was therefore mistyped, and we can say so without asking.
PAIRING_CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")


class PairingError(Exception):
    """A pairing that did not happen, and the one word that says why.

    ``code`` is one of ``bad_format``, ``invalid_code``, ``network`` — the
    route layer maps it to a status and a translated message, so the set is
    part of the contract and grows only alongside that mapping.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _pair_url(portal_url: str) -> str:
    """Join :data:`PAIR_PATH` below the portal URL, trailing slash and all.

    Parsed rather than concatenated because both naive forms are wrong in a way
    that surfaces as the wrong error message. ``portal + PAIR_PATH`` on a URL
    stored with a trailing slash asks for ``//api/link/v1/pair``, which is a
    different path to the server and comes back 404 — reported to the user as
    an invalid code. ``urljoin`` with a leading-slash path discards any prefix,
    so a portal proxied at ``https://host/cloud`` would be knocked on at the
    proxy's root. Query and fragment are dropped: they belong to a URL somebody
    pasted, not to this request.
    """
    parts = urlsplit(portal_url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + PAIR_PATH, "", ""))


async def pair(session: AsyncSession, pairing_code: str) -> None:
    """Redeem ``pairing_code`` at the configured portal and store the result.

    Returns nothing on success — the outcome is in the database, and the caller
    reads it back through the store like everybody else. Raises
    :class:`PairingError` otherwise.
    """
    # Case and surrounding whitespace are how a person types a code read off a
    # screen, not a different code. Normalise, then check — and send the portal
    # the canonical form it issued.
    code = (pairing_code or "").strip().upper()
    if not PAIRING_CODE_RE.match(code):
        raise PairingError("bad_format")

    config = await get_config(session)
    # Read off the row once, here. Everything below logs it, and the recovery
    # path at the bottom can leave ``config`` expired — an attribute read after
    # that point is a lazy load from a non-greenlet context, i.e. the very
    # failure this function goes to some trouble not to produce.
    portal_url = config.portal_url
    url = _pair_url(portal_url)
    payload = {
        "pairing_code": code,
        # The name is what the operator will pick this farm out by in a portal
        # listing several, and the version is what tells the portal which
        # envelope features this agent has.
        "instance_name": socket.gethostname(),
        "bamdude_version": APP_VERSION,
    }

    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=PAIR_TIMEOUT_SECONDS)) as http,
            http.post(url, json=payload) as response,
        ):
            status = response.status
            body = await response.text()
    except (aiohttp.ClientError, OSError) as exc:
        # aiohttp wraps a refused connection and a failed lookup as
        # ClientError; a total-timeout expiry arrives as TimeoutError, which is
        # an OSError on every interpreter we support.
        logger.warning("Cloud Link: pairing could not reach %s — %s", url, exc)
        raise PairingError("network") from exc

    if status == 404:
        # The portal knows the endpoint and does not know the code: expired,
        # mistyped past the format gate, or already spent. The only failure the
        # user can fix themselves, so it stays distinct.
        logger.info("Cloud Link: portal rejected the pairing code")
        raise PairingError("invalid_code")

    if status != 201:
        # Anything else is the portal, not the code — a 500, a proxy's 502, an
        # HTML error page. "Try again" is the right advice for all of them, and
        # that is what ``network`` means to the caller. The status goes to the
        # log so it can be diagnosed without guessing.
        #
        # ⚠️ **The body is logged only for an error status.** An off-contract
        # 2xx is the one case where the far end believed it was answering
        # successfully, and a success body from a pairing endpoint is exactly
        # where a credential lives — a portal that answered 200 instead of 201
        # would have its issued secret copied into the application log, where
        # it outlives the encrypted column and travels in every bug report.
        # A 4xx/5xx body is an error page and carries nothing to protect.
        if status >= 400:
            logger.warning("Cloud Link: pairing refused by %s — HTTP %s: %s", url, status, body[:200])
        else:
            logger.warning("Cloud Link: pairing refused by %s — HTTP %s (body withheld)", url, status)
        raise PairingError("network")

    try:
        issued = json.loads(body)
        instance_id = issued["instance_id"]
        secret = issued["instance_secret"]
    except (ValueError, TypeError, KeyError):
        logger.warning("Cloud Link: portal answered 201 without a usable credential")
        raise PairingError("network")

    if not instance_id or not secret:
        logger.warning("Cloud Link: portal answered 201 with an empty credential")
        raise PairingError("network")

    await save_credentials(session, instance_id=instance_id, secret=secret)
    # The summary is read by a human in a table and kept for a month — the id
    # identifies the pairing, the secret is never written anywhere but the
    # encrypted column.
    #
    # ⚠️ **Bookkeeping must not un-pair a pairing.** The credential is already
    # committed by the line above; if the audit insert fails (a locked
    # database, a session the caller left in a bad state) letting it escape
    # would report a failed pairing to a user who is, in fact, paired — and
    # send them to redeem a code that has already been spent. A missing audit
    # row is a gap in the record; a false error is a farm that cannot connect.
    #
    # ⚠️ Catching is not enough, because this session belongs to the CALLER.
    # ``write_audit`` is add + commit, and a commit that fails leaves the
    # transaction deactivated with the bad insert pending — so the route's own
    # ``config.enabled = True`` / ``commit`` right after this returns raises
    # ``PendingRollbackError``, and the false error the catch exists to prevent
    # arrives anyway, one frame further out.
    #
    # ⚠️ And the rollback alone is not enough either: it expires every instance
    # in the session, ``config`` among them, and the caller is still holding
    # that object. Touching an expired attribute emits a lazy load from a
    # non-greenlet context — a ``MissingGreenlet``, i.e. the same 500 wearing a
    # different name. The refresh is what hands the caller back a usable row.
    #
    # The credential committed on the line above and is in its own transaction,
    # so none of this can cost it.
    #
    # ⚠️ And the recovery itself is wrapped, because it is I/O on the same
    # database that just failed. A busy SQLite or a dropped connection makes
    # ``rollback``/``refresh`` raise in turn, and an exception escaping from
    # HERE is the exact 500-on-a-successful-pairing this whole block exists to
    # prevent — arriving from the handler written to prevent it. There is
    # nothing further to try at that point, so it is logged and swallowed: the
    # credential is committed either way, and the caller's own failure (if the
    # session really is unusable) will be an honest one about the caller's own
    # write rather than about pairing.
    try:
        await write_audit(session, "up", "pair", f"paired with {portal_url} as instance {instance_id}")
    except Exception as exc:
        logger.warning("Cloud Link: paired, but the audit row could not be written — %s", exc)
        try:
            await session.rollback()
            await session.refresh(config)
        except Exception as recovery_exc:
            logger.warning("Cloud Link: could not restore the caller's session after that — %s", recovery_exc)
    logger.info("Cloud Link: paired with %s as instance %s", portal_url, instance_id)
