"""Cloud Link camera snapshot — one frame, on the portal's request.

The portal cannot reach a printer's camera: the farm is behind whatever NAT,
firewall or CGNAT the user's home has, and the whole point of the agent is that
nothing has to be opened for it. So "show me the camera" is a *command* — the
portal names a printer and hands over a one-shot upload URL, and the agent
pushes the frame outward to it. The image never travels over the WebSocket:
frames on that link are JSON, small and ordered, and a JPEG among them would
stall every status behind it for the length of the upload.

**This module is the whole camera path and it runs on the reader task.**
:func:`capture_and_upload` is invoked as a post-action by the client loop, after
the ``cmd_result`` has already been sent, and the loop contains every exception
it raises. That containment is the contract, not a courtesy: a print farm has
cameras that are unplugged, printers that are offline and portals mid-deploy,
and none of those may cost the link. What the operator gets instead is a
``cmd:camera_snapshot`` audit row with ``ok=False``. This module contains its
own faults as well — the loop is the belt and the outer guard below is the
suspenders — because a row naming ``ClientConnectorError`` is worth more to an
operator than one saying the snapshot "could not be delivered".

**The two guards are the entire security of this command** (spec §5), and they
answer two different questions:

* *May this portal look at this printer?* — the publish set the user ticked,
  plus the product's own availability (``is_active AND NOT archived``), read
  fresh from the database. The in-memory set alone is only as current as the
  last snapshot, so a printer archived a minute ago would still be in it.
* *May this portal be given the picture?* — ``upload_url`` is a string a
  compromised portal chose, so without a pin the one command that reads
  hardware would also be the command that says where the reading goes. Scheme,
  host and port must equal the **configured** portal's, compared as parsed
  components (``https://portal.test@evil.test/`` is what defeats a prefix
  match), and TLS is required unless the portal is on this machine.

⚠️ **Arguments are checked twice, and the split is deliberate.**
:mod:`~backend.app.services.cloud_link.commands` asks only whether the two
arguments are non-empty strings — a shape check it can make on the reader task
before answering the portal. Whether the printer may be looked at and whether
the URL may be posted to need the database and the stored portal URL, so they
live here, where the work happens.

⚠️ **A frame is never resized, re-encoded or truncated.** The portal enforces
its own size cap on what it accepts; the agent sends the bytes the camera gave
it, and a rejection comes back as a status code this module audits. Shrinking a
frame to fit a limit we only assume would put a picture on the operator's screen
that their camera never took.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, urlsplit

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.printer import Printer
from backend.app.services.cloud_link.store import LOOPBACK_HOSTS, get_config
from backend.app.services.cloud_link.uplink import AVAILABLE_PRINTER

if TYPE_CHECKING:  # pragma: no cover — annotations only, so this module stays light
    from backend.app.services.cloud_link.commands import CameraAuditBudget
    from backend.app.services.cloud_link.uplink import Uplink

logger = logging.getLogger(__name__)

#: How long the whole upload may take, connection included. A total bound and
#: not a read bound: the portal's endpoint is a one-shot URL, so a stalled POST
#: is not something to keep waiting on — it is a snapshot that has already
#: missed the moment somebody asked about.
UPLOAD_TIMEOUT_S = 15.0

#: What the portal answers when it has stored the frame. Anything else is a
#: failure with a number an operator can act on: 404 is an upload URL that has
#: already been used, 5xx is a portal mid-deploy.
UPLOAD_OK_STATUS = 204

#: Schemes an upload may use. The pin below already requires the portal's own
#: scheme, and a portal URL may legitimately be ``wss://`` — which is not an
#: address anything can POST to. Naming the two that are keeps that case a clear
#: refusal instead of an obscure aiohttp error.
UPLOAD_SCHEMES = frozenset({"http", "https"})

#: Ports that are implied rather than written. ``https://portal.test`` and
#: ``https://portal.test:443`` are one endpoint, and a pin that called them two
#: would refuse a portal for merely spelling its own URL differently.
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

#: Seconds a camera capture may take. The product's own snapshot endpoint uses
#: the same bound — the portal is waiting on a request of its own behind this,
#: and a camera that has not answered in fifteen seconds is not about to.
CAMERA_TIMEOUT_S = 15


@dataclass(frozen=True, slots=True)
class _Camera:
    """How to reach one printer's camera, read out while the session was open.

    Columns rather than the ``Printer`` row so nothing here can touch a lazy
    attribute after the session has closed — this runs off the reader task,
    where a ``MissingGreenlet`` would surface as an unexplained failed snapshot.
    """

    ip_address: str
    access_code: str
    model: str | None
    external_enabled: bool
    external_url: str | None
    external_type: str | None
    external_snapshot_url: str | None


async def capture_and_upload(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    uplink: Uplink,
    printer_id: str,
    upload_url: str,
    audit: CameraAuditBudget,
) -> None:
    """Grab one frame from a printer's camera and POST it to ``upload_url``.

    Never raises. The client loop contains what escapes anyway, which is why
    this can afford to be direct about the order of its guards — but an
    exception that reached the loop would be audited as a delivery failure
    whatever actually went wrong, so the outer guard here exists to keep the
    *reason* in the row.

    Args:
        session_factory: Opens a database session. A **factory**, never a live
            session: this runs off the reader task, long after the loop's own
            sessions have closed, and it must not share one with a caller whose
            transaction it cannot see the state of.
        uplink: The link's uplink — the in-memory publish set, and the way to
            the printer manager that holds the live camera, without this module
            growing its own dependency on either.
        printer_id: The printer as the portal names it (``UplinkPrinter.id``, a
            string on the contract). Non-empty per :mod:`commands`; whether it
            is a printer id at all is decided here.
        upload_url: The portal's one-shot destination for the image. Non-empty
            per :mod:`commands`; whether it points at the portal is decided
            here.
        audit: The connection's audit budget. Every outcome below goes through
            it, so a portal spraying snapshot requests writes at most a handful
            of rows per socket. See
            :class:`~backend.app.services.cloud_link.commands.CameraAuditBudget`.
    """
    try:
        await _capture_and_upload(
            session_factory=session_factory,
            uplink=uplink,
            printer_id=printer_id,
            upload_url=upload_url,
            audit=audit,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("Cloud Link: the camera snapshot for printer %r failed", printer_id)
        await audit.write(f"the camera snapshot failed — {type(e).__name__}", ok=False)


async def _capture_and_upload(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    uplink: Uplink,
    printer_id: str,
    upload_url: str,
    audit: CameraAuditBudget,
) -> None:
    """The guards, in order, and then the work. Each refusal ends it.

    ⚠️ **The order is cheapest-and-most-restrictive first.** The publish set is
    in memory; availability and the portal URL are one database round trip; the
    camera is a network round trip to a printer; the upload is another to the
    portal. Every refusal therefore costs strictly less than the step it
    prevents — and, the part that is not about cost, a refused request opens
    **no socket at all**, so a portal cannot use a rejected snapshot to learn
    that a printer exists or to have this farm connect anywhere.
    """
    resolved = _as_printer_id(printer_id)
    if resolved is None:
        # The value is attacker-chosen and is deliberately not written down.
        await audit.write("refused a camera_snapshot for a printer id that is not a number", ok=False)
        return

    if resolved not in uplink.published:
        await audit.write(f"refused a camera_snapshot for printer {resolved} — not published to the portal", ok=False)
        return

    async with session_factory() as session:
        camera = await _camera_of(session, resolved)
        portal_url = (await get_config(session)).portal_url

    if camera is None:
        await audit.write(
            f"refused a camera_snapshot for printer {resolved} — archived, or parked in maintenance",
            ok=False,
        )
        return

    refusal = _upload_url_refusal(upload_url, portal_url)
    if refusal is not None:
        # The URL itself stays out of the row for the same reason the printer id
        # does above: it is text the portal chose, and ``summary`` is TEXT.
        await audit.write(f"refused a camera_snapshot for printer {resolved} — {refusal}", ok=False)
        return

    frame = await _capture(resolved, camera)
    if not frame:
        # Nothing is uploaded and nothing is faked: the portal's own request
        # times out to its 504, which is the honest answer to "no frame".
        await audit.write(f"no camera frame for printer {resolved} — nothing was uploaded", ok=False)
        return

    await _upload(frame=frame, upload_url=upload_url, printer_id=resolved, audit=audit)


# ---------------------------------------------------------------- the guards


def _as_printer_id(value: str) -> int | None:
    """The portal's string as a printer id, or ``None`` if it is not one.

    ``UplinkPrinter.id`` is a string on the contract and the primary key is an
    integer, so somebody has to convert — and a conversion that raised would be
    an exception on the reader task for what is simply a request to refuse.

    ⚠️ **Not a bare ``int()``.** Python's is far looser than the contract:
    ``int("1_0")`` is 10, ``int(" +7 ")`` is 7, and ``int("٧")`` is 7 because
    every Unicode decimal digit counts. None of those is a value this agent ever
    puts on the wire, so accepting them would only mean the same printer has
    several spellings — and a guard that can be reached by more strings than the
    protocol defines is a guard somebody will find a way past. ASCII digits, or
    nothing.
    """
    if not value.isascii() or not value.isdigit():
        return None
    return int(value)


async def _camera_of(session: AsyncSession, printer_id: int) -> _Camera | None:
    """How to reach this printer's camera — or ``None`` if it may not be seen.

    One query answers both questions, because they are one question: a printer
    that is archived or parked in maintenance is not "available", and its row
    simply does not come back. :data:`AVAILABLE_PRINTER` is the shared
    definition the snapshot builder filters the whole set with — asked again
    here rather than trusted from the last snapshot, because the publish set in
    memory is as old as the connection.
    """
    row = (
        await session.execute(
            select(
                Printer.ip_address,
                Printer.access_code,
                Printer.model,
                Printer.external_camera_enabled,
                Printer.external_camera_url,
                Printer.external_camera_type,
                Printer.external_camera_snapshot_url,
            )
            .where(Printer.id == printer_id)
            .where(*AVAILABLE_PRINTER)
        )
    ).first()
    if row is None:
        return None
    return _Camera(
        ip_address=row[0],
        access_code=row[1],
        model=row[2],
        external_enabled=bool(row[3]),
        external_url=row[4],
        external_type=row[5],
        external_snapshot_url=row[6],
    )


def _upload_url_refusal(upload_url: str, portal_url: str | None) -> str | None:
    """Why this URL may not be posted to, or ``None`` when it may.

    ⚠️ **Parsed, never string-matched.** ``portal_url`` is a prefix of
    ``https://portal.test.evil.example/``, and ``https://portal.test@evil.test/``
    parses to the host ``evil.test`` with ``portal.test`` as a username — both
    are accepted by the obvious ``startswith`` and both hand a camera to
    somebody else. Scheme, host and port are compared as a triple, with implied
    ports made explicit so that a portal spelling its own URL two ways is still
    one portal.

    The TLS rule is a second, independent question, and it mirrors
    ``store.validate_portal_url``: https, unless the portal is on this machine,
    where there is no network to protect and no certificate to be had. It stays
    even though the pin above already forces the portal's own scheme, because a
    farm whose stored portal URL is plain http (which ``validate_portal_url``
    refuses, but a hand-edited database would not) must not be talked into
    putting a camera frame on the wire in clear.

    Returns:
        A short reason for the audit row, or ``None`` if the URL is the portal's.
    """
    if not portal_url:
        return "this farm has no portal URL to check it against"

    try:
        target = urlsplit(upload_url)
        portal = urlsplit(portal_url)
        endpoint = (target.scheme.lower(), (target.hostname or "").lower(), _port(target))
        expected = (portal.scheme.lower(), (portal.hostname or "").lower(), _port(portal))
    except ValueError:
        return "the upload URL could not be parsed"

    scheme, host, _unused = endpoint
    if not host or endpoint != expected:
        return "the upload URL does not point at this farm's portal"
    if scheme not in UPLOAD_SCHEMES:
        return f"{scheme!r} is not an address a frame can be posted to"
    if scheme != "https" and host not in LOOPBACK_HOSTS:
        return "the upload URL is plain http and the portal is not on this machine"
    return None


def _port(parts: SplitResult) -> int | None:
    """The port a URL means, written or implied. ``ValueError`` if it is junk."""
    return parts.port or DEFAULT_PORTS.get(parts.scheme.lower())


# ---------------------------------------------------------------- the camera


async def _capture(printer_id: int, camera: _Camera) -> bytes | None:
    """One JPEG, through the product's own capture paths and no others.

    ⚠️ **Never open a socket to a camera from here.** Both camera kinds allow
    exactly one reader — Bambu firmware permits one connection, a USB camera one
    V4L2 handle — so a capture that races the live view does not degrade, it
    fails, and it drops the operator's stream on the way
    (``40-invariants/inv-single-camera-socket``). ``live_frame_for_capture`` is
    the product's single answer to "is somebody watching, and what have they
    got"; when it says defer, this returns the buffered frame or nothing at all,
    exactly as Obico's poller does.

    The imports are late for the reason Obico's are: this module is imported by
    the client loop at startup, and ``routes.camera`` drags the whole camera
    stack in behind it. They are also the seam the tests patch, which is how
    "the agent calls the product's function" is pinned rather than asserted.
    """
    from backend.app.api.routes.camera import live_frame_for_capture
    from backend.app.services.camera import capture_camera_frame_bytes
    from backend.app.services.external_camera import capture_frame as capture_external_frame

    defer, buffered = live_frame_for_capture(printer_id)
    if defer:
        if buffered is None:
            logger.info(
                "Cloud Link: a viewer is attached to printer %s and the buffer is empty — "
                "skipping rather than opening a competing camera handle",
                printer_id,
            )
        return buffered

    if camera.external_enabled and camera.external_url:
        return await capture_external_frame(
            camera.external_url,
            camera.external_type,
            timeout=CAMERA_TIMEOUT_S,
            snapshot_url=camera.external_snapshot_url,
        )

    return await capture_camera_frame_bytes(
        ip_address=camera.ip_address,
        access_code=camera.access_code,
        model=camera.model,
        timeout=CAMERA_TIMEOUT_S,
    )


# ---------------------------------------------------------------- the upload


async def _upload(*, frame: bytes, upload_url: str, printer_id: int, audit: CameraAuditBudget) -> None:
    """POST the frame and record what the portal said. Never raises.

    A session per upload rather than a shared one: this fires when somebody
    opens a camera in the portal — minutes apart, not milliseconds — so a
    long-lived pool would hold an idle connection open against a remote host for
    the life of the process, to save a handshake nobody is waiting on.
    """
    timeout = aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT_S)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(upload_url, data=frame, headers={"Content-Type": "image/jpeg"}) as response,
        ):
            status = response.status
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Cloud Link: the camera frame for printer %s did not reach the portal: %s", printer_id, e)
        await audit.write(
            f"the camera frame for printer {printer_id} did not reach the portal — {type(e).__name__}",
            ok=False,
        )
        return

    if status != UPLOAD_OK_STATUS:
        await audit.write(
            f"the portal refused the camera frame for printer {printer_id} with HTTP {status}",
            ok=False,
        )
        return

    await audit.write(f"uploaded a {len(frame)} byte camera frame for printer {printer_id}", ok=True)
