"""``PUBLIC_API_PATTERNS`` opens exactly the routes it names, and nothing else.

``main.py::auth_middleware`` skips its blanket JWT gate for any request whose
path matches one of these patterns. Every route behind them still carries its
own gate — a stream token, an overlay token, a one-shot nonce, a download
token, or a plain ``RequirePermission`` — so a wide pattern never *exposed*
anything. What it did do was remove the middleware's defence in depth from
routes nobody had listed, silently:

* the entries were plain SUBSTRINGS tested with ``in path``, so ``"/timelapse"``
  (commented "the timelapse video") also opened the six write routes beside it,
  ``"/camera/stream"`` opened ``POST /printers/camera/stream-token``,
  ``"/auth/oidc/providers"`` opened the provider CRUD, and ``"/thumbnail"`` was
  satisfied by a CLIENT-NAMED tail — ``…/card-download/thumbnail.txt`` sailed
  past the middleware and was refused only by the route's own permission;
* one entry (``"/auth/2fa/send-code"``) named a route that does not exist,
  while the email-OTP sender the login page actually calls
  (``/auth/2fa/email/send``) was left outside the list, so the second factor
  could never send its code.

The patterns are now anchored regexes, one per intended route. This file is the
table of what "intended" means: ``PUBLIC_ROUTES`` below lists every route
template the whitelist is allowed to open, and the sweep fails in BOTH
directions — a route that matches and is not in the table, and a table entry
nothing matches.

⚠️ The middleware runs BEFORE routing. There is no matched route to consult
there, only the raw path — which is why the patterns must be anchored and why
``.+`` may appear only where the ROUTE ITSELF declares ``{x:path}``.
"""

import re

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from backend.app.main import PUBLIC_API_PATTERNS, app

# Every route template ``PUBLIC_API_PATTERNS`` is meant to open, and why.
# A path is listed when ANY of its methods needs the gate skipped: the
# middleware sees a path, not a method, so ``DELETE /archives/1/timelapse``
# rides in on the ``<video>`` GET beside it and is stopped by its own
# ownership permission instead.
PUBLIC_ROUTES: dict[str, str] = {
    # Images an <img src> loads, which cannot carry an Authorization header.
    "/api/v1/archives/{archive_id}/thumbnail": "archive card thumbnail",
    "/api/v1/library/files/{file_id}/thumbnail": "library card thumbnail",
    "/api/v1/archives/{archive_id}/plate-thumbnail/{plate_index}": "per-plate thumbnail",
    "/api/v1/library/files/{file_id}/plate-thumbnail/{plate_index}": "per-plate thumbnail",
    "/api/v1/archives/{archive_id}/plate-preview": "plate preview, loaded like its siblings",
    "/api/v1/archives/{archive_id}/photos/{filename}": "operator photos of a finished print",
    "/api/v1/archives/{archive_id}/project-image/{image_path:path}": "pictures inside the 3MF",
    "/api/v1/archives/{archive_id}/qrcode": "QR image for the print",
    "/api/v1/archives/{archive_id}/timelapse": "timelapse <video>",
    "/api/v1/library/files/{file_id}/card-file/{zip_path:path}": "model-card pictures (stream token)",
    "/api/v1/products/{product_id}/attachment-image/{filename}": "product gallery (stream token)",
    "/api/v1/products/{product_id}/cover-image": "product cover (stream token)",
    "/api/v1/projects/{project_id}/cover-image": "order cover (stream token)",
    "/api/v1/external-links/{link_id}/icon": "external-link favicon",
    "/api/v1/auth/oidc/providers/{provider_id}/icon": "OIDC button icon on the login page",
    "/api/v1/makerworld/imports/{library_file_id}/cover": "MakerWorld import cover",
    "/api/v1/makerworld/imports/{library_file_id}/cover-variant": "MakerWorld variant cover",
    "/api/v1/makerworld/thumbnail": "MakerWorld CDN proxy for <img>",
    # Camera surface — long-lived scoped tokens in the URL, no session.
    "/api/v1/printers/{printer_id}/camera-cover": "current job's cover (stream token)",
    "/api/v1/printers/{printer_id}/camera/stream": "MJPEG stream (stream token)",
    "/api/v1/printers/{printer_id}/camera/snapshot": "snapshot (stream token)",
    "/api/v1/printers/{printer_id}/camera/plate-detection/references/{index}/thumbnail": (
        "calibration reference picture (stream token)"
    ),
    "/api/v1/printers/{printer_id}/overlay-status": "OBS overlay feed (overlay token)",
    "/api/v1/camwall/printers": "kiosk wall feed (camwall token)",
    # Token-in-the-path downloads for slicer protocol handlers.
    "/api/v1/archives/{archive_id}/dl/{token}/{filename}": "bambustudioopen:// download",
    "/api/v1/library/files/{file_id}/dl/{token}/{filename}": "orcaslicer:// download",
    # The Obico ML service fetches a frame by one-shot nonce.
    "/api/v1/obico/cached-frame/{nonce}": "Obico frame by nonce",
    # Login-flow routes the browser calls before it has a JWT.
    "/api/v1/auth/2fa/verify": "second factor trades a pre-auth token for a JWT",
    "/api/v1/auth/2fa/email/send": "email OTP sender, pre-auth token only",
    "/api/v1/auth/oidc/providers": "the login page lists enabled providers",
    "/api/v1/auth/oidc/authorize/{provider_id}": "starts the PKCE flow",
    "/api/v1/auth/oidc/callback": "lands from the identity provider",
    "/api/v1/auth/oidc/exchange": "swaps the bridge token for a JWT",
}

_PARAM = re.compile(r"\{([^}]+)\}")


def _is_int_param(route: APIRoute, name: str) -> bool:
    annotation = getattr(route.endpoint, "__annotations__", {}).get(name)
    return annotation is int or annotation == "int"


def _fill(route: APIRoute, *, hostile: bool) -> str:
    """Turn a route template into a concrete path the middleware could see.

    ``hostile`` fills every client-controlled segment with a word the old
    substring whitelist reacted to. The matched set must not change: that is
    the ``card-download/thumbnail.txt`` bug, asked of every route at once.
    """

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name.endswith(":path"):
            return "x/thumbnail/cover-image" if hostile else "nested/name.png"
        if _is_int_param(route, name.split(":")[0]):
            return "7"
        return "thumbnail" if hostile else "name.png"

    return _PARAM.sub(sub, route.path)


def _matching(path: str) -> list[str]:
    return [p.pattern for p in PUBLIC_API_PATTERNS if p.match(path)]


def _api_routes() -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/")]


def test_every_pattern_is_anchored_at_both_ends():
    """``re.match`` anchors the start on its own; ``^`` says so to the reader,
    and ``$`` is what stops a client-named tail from widening the match."""
    for pattern in PUBLIC_API_PATTERNS:
        assert pattern.pattern.startswith("^"), pattern.pattern
        assert pattern.pattern.endswith("$"), pattern.pattern


@pytest.mark.parametrize("hostile", [False, True], ids=["ordinary", "hostile-segments"])
def test_the_whitelist_opens_exactly_the_routes_in_the_table(hostile):
    opened: set[str] = set()
    unexpected: dict[str, list[str]] = {}
    for route in _api_routes():
        hits = _matching(_fill(route, hostile=hostile))
        if hits:
            opened.add(route.path)
            if route.path not in PUBLIC_ROUTES:
                unexpected[route.path] = hits

    missing = sorted(set(PUBLIC_ROUTES) - opened)
    assert not unexpected, f"patterns open routes the table does not list: {unexpected}"
    assert not missing, f"table lists routes no pattern opens: {missing}"


def test_every_pattern_earns_its_place():
    """A pattern matching no route at all is rot — that is how
    ``"/auth/2fa/send-code"`` survived beside the route actually called."""
    live = {_fill(r, hostile=False) for r in _api_routes()} | {_fill(r, hostile=True) for r in _api_routes()}
    dead = [p.pattern for p in PUBLIC_API_PATTERNS if not any(p.match(path) for path in live)]
    assert not dead, f"patterns matching no registered route: {dead}"


@pytest.mark.asyncio
async def test_a_whitelisted_word_in_a_client_named_tail_no_longer_opens_the_gate(async_client):
    """``card-download`` serves a member of a 3MF by a path the CLIENT names.

    A member called ``thumbnail.txt`` used to satisfy the ``"/thumbnail"``
    substring, so the request reached the route and was refused there instead.

    ⚠️ The status code alone cannot tell the two layers apart: the route's own
    ``require_ownership_permission`` answers 401 with the SAME body and the same
    ``WWW-Authenticate`` header the middleware uses. What CAN be observed is a
    path carrying the same word that reaches no route at all — the substring
    whitelist handed it to the router, which answered 404 to a caller holding
    nothing.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        tail = await anonymous.get("/api/v1/library/files/1/card-download/thumbnail.txt")
        no_such_route = await anonymous.get("/api/v1/archives/1/thumbnail/not-a-route")

    assert tail.status_code == 401
    assert tail.json() == {"detail": "Authentication required"}
    assert tail.headers.get("WWW-Authenticate") == "Bearer"
    assert no_such_route.status_code == 401, "the router answered this one before any gate did"


@pytest.mark.asyncio
async def test_the_discovery_scan_is_not_a_cover(async_client):
    """ "dis-COVER" — the near miss that names the whole class.

    ``"/cover"`` did NOT reach this path, by the one character of luck that is
    the entry's leading slash: ``/smart-plugs/discover/scan`` has no ``/cover``
    in it. Drop that slash, or add a ``"cover"`` entry for some future route,
    and a substring whitelist opens the plug scanner. Pinned here so the
    anchored form is what a later reader copies.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        refused = await anonymous.post("/api/v1/smart-plugs/discover/scan")
        wrong_method = await anonymous.get("/api/v1/smart-plugs/discover/scan")

    assert refused.status_code == 401
    assert refused.json() == {"detail": "Authentication required"}
    assert wrong_method.status_code == 401, "405 would mean the router saw an unauthenticated request"


@pytest.mark.asyncio
async def test_the_email_otp_sender_reaches_its_own_pre_auth_gate(async_client):
    """The login page calls this with a pre-auth token and no session.

    The substring list named ``"/auth/2fa/send-code"`` — a route that has never
    existed — so the middleware answered 401 to the sender the client actually
    calls and email as a second factor could never deliver its code. Here the
    two layers ARE distinguishable: the route rejects a bad pre-auth token in
    its own words.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        refused = await anonymous.post("/api/v1/auth/2fa/email/send", json={"pre_auth_token": "not-a-real-token"})

    assert refused.status_code == 401
    assert refused.json() == {"detail": "Invalid or expired pre-auth token"}


@pytest.mark.asyncio
async def test_the_printer_cover_lives_on_its_own_segment_and_takes_a_stream_token(
    async_client, printer_factory, monkeypatch
):
    """The route used to be ``/printers/{id}/cover``, whitelisted as the bare
    substring ``"/cover"``. It now has a segment no other route shares, so the
    pattern can be anchored to it."""
    from backend.app.api.routes import printers as printer_routes
    from backend.app.core.auth import create_camera_stream_token
    from backend.app.services.bambu_mqtt import PrinterState

    printer = await printer_factory(name="Cover", serial_number="COVER0001")
    state = PrinterState(connected=True, state="RUNNING", subtask_name="job")
    monkeypatch.setattr(printer_routes.printer_manager, "get_status", lambda pid: state)
    monkeypatch.setitem(printer_routes._cover_cache, printer.id, {("job", "default"): b"\x89PNG-cover"})

    url = f"/api/v1/printers/{printer.id}/camera-cover"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        assert (await anonymous.get(url)).status_code == 401, "no token is still no entry"
        served = await anonymous.get(url, params={"token": await create_camera_stream_token()})

    assert served.status_code == 200, served.text
    assert served.content == b"\x89PNG-cover"
