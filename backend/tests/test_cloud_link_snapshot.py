"""Cloud Link camera snapshot — the one command that reaches hardware.

Everything here is about the two things that can go wrong here and nowhere else
in the agent: it *reads a camera* on the portal's word, and it *pushes bytes
outward* to an address the portal chose. Both are the compromised portal's only
levers (spec §5), so both are pinned from the outside rather than trusted to a
docstring.

Four things these tests exist to pin:

* **The publish set is real, not decorative.** A portal naming a printer the
  user never ticked — or one archived or parked in maintenance since the last
  snapshot — gets nothing, and the camera is never touched.
* **The destination is pinned to the paired portal.** ``upload_url`` is the
  portal's own string, so a portal that has been taken over would otherwise be
  handing this farm an address at which to publish its cameras. Scheme, host
  and port must be the configured portal's, parsed rather than string-matched.
* **No refusal ever opens a socket.** Every refusal test runs against a real
  loopback HTTP server and then insists it recorded nothing — an assertion a
  mocked client could not make.
* **Nothing here raises.** This runs on the reader task; the outcome lives in an
  audit row and never in an exception. The client loop's containment is the belt
  and this module is the suspenders.

The portal is a real aiohttp server on a loopback port (the phase-0 pattern) so
the content type, the body and the status code are the ones that actually
crossed a socket. The camera is monkeypatched at the product's own functions —
the point of that seam is to pin **which** product function the agent calls,
because reimplementing the single-socket rule here is the failure mode
``inv-single-camera-socket`` exists to prevent.
"""

from __future__ import annotations

import socket

import pytest
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api.routes import camera as camera_routes
from backend.app.models.cloud_link import CloudLinkAudit, CloudLinkPrinter
from backend.app.models.printer import Printer
from backend.app.services import camera as camera_service, external_camera as external_camera_service
from backend.app.services.cloud_link import snapshot as snapshot_module
from backend.app.services.cloud_link.commands import CAMERA_SNAPSHOT_KIND, CameraAuditBudget
from backend.app.services.cloud_link.snapshot import capture_and_upload
from backend.app.services.cloud_link.store import get_config
from backend.app.services.cloud_link.uplink import Uplink

FRAME = b"\xff\xd8\xff\xe0-not-really-a-jpeg-but-exactly-these-bytes\xff\xd9"


# --------------------------------------------------------------- the fixtures


class UploadPortal:
    """The portal's upload endpoint, and a record of everything that reached it.

    ``requests`` is the whole point: every refusal test asserts it is empty,
    which is a claim about the wire that no mocked HTTP client could make.
    """

    def __init__(self, status: int):
        self.status = status
        self.requests: list[dict] = []


@pytest.fixture
async def upload_portal():
    """Start loopback upload endpoints, hand back their base URL, clean up."""
    runners = []

    async def _start(status: int = 204) -> tuple[UploadPortal, str]:
        instance = UploadPortal(status)

        async def handler(request: web.Request) -> web.Response:
            instance.requests.append(
                {
                    "method": request.method,
                    "path": request.path,
                    "content_type": request.headers.get("Content-Type"),
                    "body": await request.read(),
                }
            )
            return web.Response(status=instance.status)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return instance, f"http://127.0.0.1:{port}"

    yield _start

    for runner in runners:
        await runner.cleanup()


@pytest.fixture
def session_factory(test_engine):
    """The shape ``core/database.async_session`` has — the capture opens its own
    short session, exactly as the store expects."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


class FakeManager:
    """``printer_manager`` narrowed to what the uplink asks of it."""

    def get_status(self, printer_id: int):
        return None

    def get_model(self, printer_id: int):
        return None

    def get_printer(self, printer_id: int):
        return None


async def set_up(
    session_factory,
    portal_url: str,
    *,
    printer_id: int = 7,
    published: bool = True,
    **printer_kwargs,
) -> Uplink:
    """One printer, one portal URL, and an uplink that has seen the publish set."""
    async with session_factory() as session:
        config = await get_config(session)
        config.portal_url = portal_url
        session.add(
            Printer(
                id=printer_id,
                name=f"Printer {printer_id}",
                serial_number=f"SN{printer_id:06d}",
                ip_address="192.168.1.10",
                access_code="12345678",
                model="P1S",
                **printer_kwargs,
            )
        )
        if published:
            session.add(CloudLinkPrinter(printer_id=printer_id))
        await session.commit()

    uplink = Uplink(manager=FakeManager())
    # What ``build_snapshot`` would have left behind: the publish set as it
    # stood at connect time.
    uplink.set_publish_set({printer_id} if published else set())
    return uplink


def budget(session_factory, limit: int = 5) -> CameraAuditBudget:
    return CameraAuditBudget(session_factory=session_factory, limit=limit)


async def camera_rows(session_factory) -> list[CloudLinkAudit]:
    async with session_factory() as session:
        rows = (await session.execute(select(CloudLinkAudit).order_by(CloudLinkAudit.id))).scalars().all()
    return [row for row in rows if row.kind == CAMERA_SNAPSHOT_KIND]


def a_camera_holding(monkeypatch, frame: bytes | None, *, viewer_attached: bool = False) -> list:
    """Point the product's camera functions at ``frame``. Returns the call log.

    Patched on the modules the implementation imports from, not on
    :mod:`snapshot` — so a version of the agent that grew its own RTSP client
    would sail past every one of these patches and fail the assertions below.
    """
    calls: list = []

    def live_frame_for_capture(printer_id: int):
        calls.append(("live_frame_for_capture", printer_id))
        return (True, frame) if viewer_attached else (False, None)

    async def capture_camera_frame_bytes(**kwargs):
        calls.append(("capture_camera_frame_bytes", kwargs))
        return frame

    async def capture_external(url, camera_type, timeout=15, snapshot_url=None):
        calls.append(("capture_external_frame", url, camera_type, snapshot_url))
        return frame

    monkeypatch.setattr(camera_routes, "live_frame_for_capture", live_frame_for_capture)
    monkeypatch.setattr(camera_service, "capture_camera_frame_bytes", capture_camera_frame_bytes)
    monkeypatch.setattr(external_camera_service, "capture_frame", capture_external)
    return calls


# ------------------------------------------------------------- the happy path


async def test_a_published_printer_frame_reaches_the_portal_byte_for_byte(session_factory, upload_portal, monkeypatch):
    """The whole point of the feature, and the only test that opens a socket on purpose."""
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    a_camera_holding(monkeypatch, FRAME)
    audit = budget(session_factory)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload/one-shot-token",
        audit=audit,
    )

    assert len(portal.requests) == 1
    request = portal.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/upload/one-shot-token"
    assert request["content_type"] == "image/jpeg"
    assert request["body"] == FRAME, "the bytes the camera gave us, unaltered"

    rows = await camera_rows(session_factory)
    assert [row.ok for row in rows] == [True]
    assert str(len(FRAME)) in rows[0].summary, "an operator can see something actually went"
    assert rows[0].direction == "down", "the portal asked for it"


# ---------------------------------------------------- the printer-side guards


async def test_a_printer_the_user_never_published_is_refused_before_anything_happens(
    session_factory, upload_portal, monkeypatch
):
    """The publish set is the user's whole control over what the portal can see.

    A portal that could name any printer id would make the settings page a
    decoration — and the refusal has to land before the camera *and* before the
    socket, because either one is already a disclosure that the printer exists.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url, published=False)
    calls = a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert portal.requests == [], "a refusal must never open a socket"
    assert calls == [], "and must never reach a camera"
    rows = await camera_rows(session_factory)
    assert [row.ok for row in rows] == [False]
    assert "publish" in rows[0].summary


@pytest.mark.parametrize(
    ("field", "value"),
    [("archived", True), ("is_active", False)],
    ids=["archived", "maintenance"],
)
async def test_a_printer_no_longer_available_is_refused_though_the_allowlist_still_names_it(
    session_factory, upload_portal, monkeypatch, field, value
):
    """``CloudLinkPrinter`` survives archiving; availability is asked fresh.

    The in-memory publish set is only as current as the last snapshot, so a
    printer archived a second ago would still be in it. ``is_active AND NOT
    archived`` — the same definition ``build_snapshot`` filters on — is what
    actually decides, and it is read from the database at capture time.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url, **{field: value})
    # Deliberately still in the in-memory set: this test exists to prove the
    # database is consulted, not the cached answer.
    uplink.set_publish_set({7})
    calls = a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert portal.requests == []
    assert calls == []
    assert [row.ok for row in await camera_rows(session_factory)] == [False]


@pytest.mark.parametrize("printer_id", ["seven", "7; DROP", "1e3", " 7 ", "+7", "1_0", "٧"])
async def test_a_printer_id_that_is_not_a_number_is_refused_like_one_that_is_not_published(
    session_factory, upload_portal, monkeypatch, printer_id
):
    """``UplinkPrinter.id`` is a string on the contract and an integer here.

    The conversion is a guard, not a formality: everything downstream indexes
    the publish set and the printers table with it, and a value that is not a
    printer id must end the same way a printer id nobody published does.

    The last four are why a bare ``int()`` is not that guard — Python reads
    ``" 7 "``, ``"+7"`` and the Arabic-Indic ``"٧"`` as seven and ``"1_0"`` as
    ten. Every one of them is a spelling this agent never emits, and a guard
    reachable by more strings than the protocol defines is one somebody will
    find a way past.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    calls = a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id=printer_id,
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert portal.requests == []
    assert calls == []
    rows = await camera_rows(session_factory)
    assert [row.ok for row in rows] == [False]
    assert printer_id not in rows[0].summary, "an attacker-chosen string is not written down"


# -------------------------------------------------------- the destination pin


@pytest.mark.parametrize(
    "elsewhere",
    [
        "https://evil.test/collect",
        "http://evil.test/collect",
        "https://127.0.0.1:1/collect",
        "http://127.0.0.1/collect",
        "not a url at all",
        "https://",
        "file:///etc/passwd",
        "http://127.0.0.1:99999/collect",
    ],
)
async def test_an_upload_url_that_is_not_this_farms_portal_is_refused(
    session_factory, upload_portal, monkeypatch, elsewhere
):
    """The pin against a portal that has been taken over publishing our cameras.

    ``upload_url`` is a string the portal chose, so without this the one command
    that reads hardware would also be a command that says where the reading
    goes. Scheme, host and port must all be the configured portal's — and the
    comparison is on parsed components, because ``startswith`` on a URL is how
    ``http://portal.test@evil.test/`` gets accepted.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    calls = a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=elsewhere,
        audit=budget(session_factory),
    )

    assert portal.requests == [], "the portal's own endpoint saw nothing either"
    assert calls == [], "and the camera was never read for a frame with nowhere to go"
    assert [row.ok for row in await camera_rows(session_factory)] == [False]


async def test_a_userinfo_prefix_does_not_smuggle_a_foreign_host_past_the_pin(
    session_factory, upload_portal, monkeypatch
):
    """The exact trick a string comparison would wave through."""
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    a_camera_holding(monkeypatch, FRAME)
    host_and_port = url.removeprefix("http://")

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"http://{host_and_port}@evil.test/collect",
        audit=budget(session_factory),
    )

    assert portal.requests == []
    assert [row.ok for row in await camera_rows(session_factory)] == [False]


async def test_a_portal_reachable_only_over_plain_http_cannot_be_uploaded_to_from_afar(
    session_factory, upload_portal, monkeypatch
):
    """The TLS rule is separate from the pin, and outlives it.

    A portal URL that is neither loopback nor https should never have been
    stored (``validate_portal_url`` refuses it), so this is the belt to that
    braces: even an upload URL matching the configured portal exactly is refused
    when it would put a camera frame on the wire in clear.
    """
    portal, _url = await upload_portal()
    uplink = await set_up(session_factory, "http://portal.test")
    a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url="http://portal.test/upload",
        audit=budget(session_factory),
    )

    assert portal.requests == []
    assert [row.ok for row in await camera_rows(session_factory)] == [False]


async def test_a_farm_with_no_portal_url_has_nothing_to_pin_against_and_refuses(
    session_factory, upload_portal, monkeypatch
):
    """No configured portal means no comparison is possible — and "no comparison
    is possible" must read as *no*, never as *anything goes*."""
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    async with session_factory() as session:
        config = await get_config(session)
        config.portal_url = ""
        await session.commit()
    a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert portal.requests == []
    assert [row.ok for row in await camera_rows(session_factory)] == [False]


# ------------------------------------------------------ the frame acquisition


async def test_the_frame_comes_from_the_broadcaster_when_a_viewer_is_watching(
    session_factory, upload_portal, monkeypatch
):
    """The single-socket rule is the product's, and this path must not re-decide it.

    Both camera kinds allow exactly one reader, so a capture that races the live
    view does not degrade — it fails, and it kicks the operator's stream off on
    the way. ``live_frame_for_capture`` is the product's one answer to that
    question, and this test pins that the agent asks it rather than opening a
    socket of its own.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    calls = a_camera_holding(monkeypatch, b"buffered-frame", viewer_attached=True)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert calls == [("live_frame_for_capture", 7)], "asked once, and no competing capture"
    assert portal.requests[0]["body"] == b"buffered-frame"


async def test_with_no_viewer_attached_the_products_one_shot_capture_is_used(
    session_factory, upload_portal, monkeypatch
):
    """The headless case — a printer nobody is watching, which is most of them.

    Pinned down to the arguments, because this is the seam where an agent that
    grew its own capture would still pass every other test in this file.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    calls = a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert [name for name, *_ in calls] == ["live_frame_for_capture", "capture_camera_frame_bytes"]
    assert calls[1][1] == {
        "ip_address": "192.168.1.10",
        "access_code": "12345678",
        "model": "P1S",
        "timeout": snapshot_module.CAMERA_TIMEOUT_S,
    }


async def test_an_external_camera_is_captured_through_the_external_path(session_factory, upload_portal, monkeypatch):
    """A USB or RTSP camera is not the printer's, and reaching it is not the same
    call. Same branch the product's own snapshot endpoint takes."""
    portal, url = await upload_portal()
    uplink = await set_up(
        session_factory,
        url,
        external_camera_enabled=True,
        external_camera_url="rtsp://192.168.1.99/live",
        external_camera_type="rtsp",
        external_camera_snapshot_url="http://192.168.1.99/snap.jpg",
    )
    calls = a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert calls[1] == (
        "capture_external_frame",
        "rtsp://192.168.1.99/live",
        "rtsp",
        "http://192.168.1.99/snap.jpg",
    )
    assert portal.requests[0]["body"] == FRAME


async def test_no_frame_is_an_audited_non_event_and_never_an_empty_upload(session_factory, upload_portal, monkeypatch):
    """A camera that gives nothing back is an ordinary Tuesday on a print farm.

    The honest answer is silence: the portal's request times out to its own 504
    and the operator gets a row. Uploading a zero-byte body instead would have
    the portal show a broken image where it should show "no frame".
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)
    a_camera_holding(monkeypatch, None)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert portal.requests == [], "nothing to send is not something to send"
    rows = await camera_rows(session_factory)
    assert [row.ok for row in rows] == [False]
    assert "7" in rows[0].summary


# ------------------------------------------------------------- the upload end


async def test_a_portal_that_answers_anything_but_204_is_an_audited_failure(
    session_factory, upload_portal, monkeypatch
):
    """A one-shot upload URL expires; a portal mid-deploy 502s. Neither may raise.

    The status goes in the summary because it is the whole difference between
    "the link is misconfigured" and "that URL had already been used".
    """
    portal, url = await upload_portal(status=404)
    uplink = await set_up(session_factory, url)
    a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert len(portal.requests) == 1, "the attempt was real"
    rows = await camera_rows(session_factory)
    assert [row.ok for row in rows] == [False]
    assert "404" in rows[0].summary


async def test_a_portal_that_is_not_there_at_all_is_audited_and_never_raised(session_factory, monkeypatch):
    """The upload runs on the reader task — an escaping ``ClientError`` would drop
    the socket and take the whole link down over one dead endpoint."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    uplink = await set_up(session_factory, url)
    a_camera_holding(monkeypatch, FRAME)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert [row.ok for row in await camera_rows(session_factory)] == [False]


async def test_a_capture_that_explodes_is_contained_here_and_not_only_by_the_caller(
    session_factory, upload_portal, monkeypatch
):
    """Belt and suspenders, and this is the suspenders.

    The client loop contains anything this raises, which is why it *may* raise —
    and precisely why it must not: the loop's row would say "the snapshot could
    not be delivered" for a fault that has a much better description available
    right here.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url)

    async def exploding(**kwargs):
        raise RuntimeError("the camera stack fell over")

    monkeypatch.setattr(camera_routes, "live_frame_for_capture", lambda pid: (False, None))
    monkeypatch.setattr(camera_service, "capture_camera_frame_bytes", exploding)

    await capture_and_upload(
        session_factory=session_factory,
        uplink=uplink,
        printer_id="7",
        upload_url=f"{url}/upload",
        audit=budget(session_factory),
    )

    assert portal.requests == []
    rows = await camera_rows(session_factory)
    assert [row.ok for row in rows] == [False]
    assert "RuntimeError" in rows[0].summary


# -------------------------------------------------------------- the audit cap


async def test_the_budget_stops_writing_and_keeps_counting(session_factory, upload_portal, monkeypatch):
    """The cap bounds the table, never the behaviour.

    Past the limit the capture still runs, still refuses, still returns — the
    only thing that stops is the row. A cap that also changed the answer would
    hand a hostile portal a way to tell how many times it had been refused.
    """
    portal, url = await upload_portal()
    uplink = await set_up(session_factory, url, published=False)
    a_camera_holding(monkeypatch, FRAME)
    audit = budget(session_factory, limit=2)

    for _ in range(5):
        await capture_and_upload(
            session_factory=session_factory,
            uplink=uplink,
            printer_id="7",
            upload_url=f"{url}/upload",
            audit=audit,
        )

    assert len(await camera_rows(session_factory)) == 2
    assert audit.written == 2
    assert audit.suppressed == 3
    assert portal.requests == [], "every one of the five was still refused"


async def test_a_reset_budget_starts_the_next_connection_clean(session_factory):
    """The cap is per connection: a reconnect forgives, because an operator's
    mistyped command should not be silenced for the life of the process."""
    audit = budget(session_factory, limit=1)
    await audit.write("first", ok=False)
    await audit.write("second", ok=False)
    assert audit.suppressed == 1

    audit.reset()
    await audit.write("after the reconnect", ok=False)

    assert [row.summary for row in await camera_rows(session_factory)] == ["first", "after the reconnect"]
    assert audit.suppressed == 0
