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

Each entry also names the GATE that is left once the middleware steps aside — a
stream / overlay / camwall token dependency, a credential the handler reads out
of the path or body, or ``anonymous`` where there is deliberately none. That
column is checked too, so "this route has no gate" is always a decision on the
record rather than something to reconstruct later.

⚠️ The middleware runs BEFORE routing. There is no matched route to consult
there, only the raw path — which is why the patterns must be anchored and why
``.+`` may appear only where the ROUTE ITSELF declares ``{x:path}``.
"""

import re

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from backend.app.main import PUBLIC_API_PATTERNS, app

# How a whitelisted route proves the caller may have what it serves. Skipping
# the middleware's blanket JWT gate is all a pattern does; this column says what
# is left standing behind it, per route:
#
# * ``stream-token`` / ``overlay-token`` / ``camwall-token`` — a scoped token in
#   the query string, checked by a route DEPENDENCY (the camera surface);
# * ``slicer-token`` / ``pre-auth`` / ``nonce`` — an unguessable credential the
#   HANDLER reads out of the path or the body: a download token for the slicer
#   protocol handlers, the login flow's pre-auth / bridge tokens, Obico's
#   one-shot frame nonce;
# * ``anonymous`` — no gate at all, and that is the decision: the bytes are
#   public to anyone who can already reach the install. Saying so here is the
#   point of the tag — a gate-less entry is otherwise indistinguishable from an
#   oversight.
#
# ``setup`` is in the vocabulary and used by no pattern: the setup-gate routes
# are whitelisted by exact path in ``PUBLIC_API_ROUTES``, not by a pattern.
_DEPENDENCY_GATES = frozenset({"stream-token", "overlay-token", "camwall-token"})
_HANDLER_GATES = frozenset({"slicer-token", "pre-auth", "nonce"})
_GATES = _DEPENDENCY_GATES | _HANDLER_GATES | frozenset({"anonymous", "setup"})

# Every route template ``PUBLIC_API_PATTERNS`` is meant to open — ``(gate, why)``.
# A path is listed when ANY of its methods needs the gate skipped: the
# middleware sees a path, not a method, so ``DELETE /archives/1/timelapse``
# rides in on the ``<video>`` GET beside it and is stopped by its own
# ownership permission instead. The gate describes the route the entry was
# WRITTEN for — the GET where a path carries several methods.
PUBLIC_ROUTES: dict[str, tuple[str, str]] = {
    # Images an <img src> loads, which cannot carry an Authorization header.
    # ⚠️ The archive and library pictures are ANONYMOUS by design and have been
    # since long before this table: anyone who can reach the install can read a
    # thumbnail, a plate preview, an operator photo or a timelapse by guessing an
    # id. Whether that should stay is a policy question, raised in the pass-6
    # report — it is written down here so the next reader does not mistake it for
    # a gate somebody forgot.
    "/api/v1/archives/{archive_id}/thumbnail": ("anonymous", "archive card thumbnail"),
    "/api/v1/library/files/{file_id}/thumbnail": ("anonymous", "library card thumbnail"),
    "/api/v1/archives/{archive_id}/plate-thumbnail/{plate_index}": ("anonymous", "per-plate thumbnail"),
    "/api/v1/library/files/{file_id}/plate-thumbnail/{plate_index}": ("anonymous", "per-plate thumbnail"),
    "/api/v1/archives/{archive_id}/plate-preview": ("anonymous", "plate preview, loaded like its siblings"),
    "/api/v1/archives/{archive_id}/photos/{filename}": ("anonymous", "operator photos of a finished print"),
    "/api/v1/archives/{archive_id}/project-image/{image_path:path}": ("anonymous", "pictures inside the 3MF"),
    "/api/v1/archives/{archive_id}/qrcode": ("anonymous", "QR image for the print"),
    "/api/v1/archives/{archive_id}/timelapse": ("anonymous", "timelapse <video>"),
    "/api/v1/library/files/{file_id}/card-file/{zip_path:path}": ("stream-token", "model-card pictures"),
    "/api/v1/products/{product_id}/attachment-image/{filename}": ("stream-token", "product gallery"),
    "/api/v1/products/{product_id}/cover-image": ("stream-token", "product cover"),
    "/api/v1/projects/{project_id}/cover-image": ("stream-token", "order cover"),
    "/api/v1/external-links/{link_id}/icon": ("anonymous", "external-link favicon"),
    "/api/v1/auth/oidc/providers/{provider_id}/icon": ("anonymous", "OIDC button icon on the login page"),
    "/api/v1/makerworld/imports/{library_file_id}/cover": ("anonymous", "MakerWorld import cover"),
    "/api/v1/makerworld/imports/{library_file_id}/cover-variant": ("anonymous", "MakerWorld variant cover"),
    "/api/v1/makerworld/thumbnail": ("anonymous", "MakerWorld CDN proxy for <img>, SSRF-allowlisted upstream"),
    # Camera surface — long-lived scoped tokens in the URL, no session.
    "/api/v1/printers/{printer_id}/camera-cover": ("stream-token", "current job's cover"),
    "/api/v1/printers/{printer_id}/camera/stream": ("stream-token", "MJPEG stream"),
    "/api/v1/printers/{printer_id}/camera/snapshot": ("stream-token", "snapshot"),
    "/api/v1/printers/{printer_id}/camera/plate-detection/references/{index}/thumbnail": (
        "stream-token",
        "calibration reference picture",
    ),
    "/api/v1/printers/{printer_id}/overlay-status": ("overlay-token", "OBS overlay feed"),
    "/api/v1/camwall/printers": ("camwall-token", "kiosk wall feed"),
    # Token-in-the-path downloads for slicer protocol handlers.
    "/api/v1/archives/{archive_id}/dl/{token}/{filename}": ("slicer-token", "bambustudioopen:// download"),
    "/api/v1/library/files/{file_id}/dl/{token}/{filename}": ("slicer-token", "orcaslicer:// download"),
    # The Obico ML service fetches a frame by one-shot nonce.
    "/api/v1/obico/cached-frame/{nonce}": ("nonce", "Obico frame by 32-byte single-use nonce"),
    # Login-flow routes the browser calls before it has a JWT.
    "/api/v1/auth/2fa/verify": ("pre-auth", "second factor trades a pre-auth token for a JWT"),
    "/api/v1/auth/2fa/email/send": ("pre-auth", "email OTP sender, pre-auth token only"),
    "/api/v1/auth/oidc/providers": ("anonymous", "the login page lists enabled providers"),
    "/api/v1/auth/oidc/authorize/{provider_id}": ("anonymous", "starts the PKCE flow, before any credential"),
    "/api/v1/auth/oidc/callback": ("pre-auth", "lands from the identity provider, bound by state + nonce"),
    "/api/v1/auth/oidc/exchange": ("pre-auth", "swaps the bridge token for a JWT"),
}

_PARAM = re.compile(r"\{([^}]+)\}")


def _fills(route: APIRoute, *, hostile: bool) -> list[str]:
    """Every shape of this template the middleware could see, as concrete paths.

    Each parameter is substituted TWICE — once as digits, once as a word — and
    the caller unions what the two match. Reading the handler's annotation to
    decide which shape a parameter takes was a second model of the router living
    in this file: ``\\d+`` in a pattern is a claim about what the MIDDLEWARE
    sees, and the middleware sees whatever the client sent, whatever the handler
    is annotated with. Both shapes, unioned, is the honest question.

    ``hostile`` fills every client-controlled segment with a word the old
    substring whitelist reacted to. The matched set must not change: that is
    the ``card-download/thumbnail.txt`` bug, asked of every route at once.
    """
    word = "thumbnail" if hostile else "name.png"
    tail = "x/thumbnail/cover-image" if hostile else "nested/name.png"

    def sub(as_digits: bool):
        def _one(m: re.Match[str]) -> str:
            if m.group(1).endswith(":path"):
                return tail
            return "7" if as_digits else word

        return _one

    return [_PARAM.sub(sub(True), route.path), _PARAM.sub(sub(False), route.path)]


def _matching(route: APIRoute, *, hostile: bool) -> list[str]:
    """Patterns that open this route in ANY of the shapes a client could send."""
    hits: list[str] = []
    for path in _fills(route, hostile=hostile):
        for pattern in PUBLIC_API_PATTERNS:
            if pattern.match(path) and pattern.pattern not in hits:
                hits.append(pattern.pattern)
    return hits


def _intended(path: str) -> APIRoute:
    """The route an entry was written for: the GET, or the only method there is.

    A whitelisted path often carries writes too (``DELETE`` on a timelapse, the
    cover upload beside the cover GET) — they ride in because the middleware
    sees a path and not a method, and are stopped by their own permission. The
    gate tag describes the read the entry exists for.
    """
    here = [r for r in _api_routes() if r.path == path]
    assert here, f"the table lists a route that is not registered: {path}"
    return next((r for r in here if "GET" in r.methods), here[0])


def _gate_dependencies(route: APIRoute) -> list[str]:
    """Sub-dependencies that could BE a gate — ``get_db`` is plumbing, not one.

    ⚠️ "has at least one dependency" is not the question: every handler that
    touches the database has ``get_db``, so the naive count says every route is
    gated. What distinguishes them is a dependency that is not the session.
    """
    names = []
    for dependency in route.dependant.dependencies:
        name = getattr(dependency.call, "__name__", None) or type(dependency.call).__name__
        if name != "get_db":
            names.append(name)
    return names


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
        hits = _matching(route, hostile=hostile)
        if hits:
            opened.add(route.path)
            if route.path not in PUBLIC_ROUTES:
                unexpected[route.path] = hits

    missing = sorted(set(PUBLIC_ROUTES) - opened)
    assert not unexpected, f"patterns open routes the table does not list: {unexpected}"
    assert not missing, f"table lists routes no pattern opens: {missing}"


def test_every_listed_route_carries_the_gate_its_entry_names():
    """The table's second column is a claim about the code, so it is checked.

    A whitelisted route with no gate of its own is only acceptable when it is
    MEANT to be anonymous — and then the entry says ``anonymous``. This fails in
    both directions: a token dependency appearing on a route tagged anonymous is
    as much a drift as one disappearing from a route tagged ``stream-token``.
    The handler-gated tags (a download token, a pre-auth token, a nonce) are the
    routes whose credential is not a dependency at all; they must carry none, or
    the tag is describing the wrong thing.
    """
    for path, (gate, why) in PUBLIC_ROUTES.items():
        assert gate in _GATES, f"{path}: unknown gate {gate!r}"
        deps = _gate_dependencies(_intended(path))
        if gate in _DEPENDENCY_GATES:
            assert deps, f"{path} ({why}): tagged {gate}, but its dependant carries no gate"
        else:
            assert not deps, f"{path} ({why}): tagged {gate}, but {deps} now gates it — retag the entry"


def test_every_pattern_earns_its_place():
    """A pattern matching no route at all is rot — that is how
    ``"/auth/2fa/send-code"`` survived beside the route actually called."""
    live = {path for r in _api_routes() for hostile in (False, True) for path in _fills(r, hostile=hostile)}
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
