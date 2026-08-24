"""Cloud Link REST surface — the six calls the settings page makes.

The routes are thin on purpose: the store owns persistence, ``pairing`` owns
the one outward call, the service owns the task. What is genuinely decided
*here* — and therefore what these tests are about — is four things.

* **Who may ask at all.** ``cloud_link:manage`` decides whether this farm is
  reachable from outside the LAN. Every route sits behind it, and the table
  below is checked against the router itself, so a seventh route cannot be
  added without either a permission or a failing test.
* **The publish set is validated here and nowhere else.** ``set_publish_set``
  stores what it is given; the uplink filters what it publishes. Neither of
  them can tell the user "printer 7 is archived", and neither is reached by a
  request that names a printer that does not exist. This layer is the only one
  holding both the request and a session.
* **A pairing failure has three different repairs.** Fix the typing, fetch a
  new code, try again later — 400, 404, 502. Collapsing them would send the
  user down the wrong one.
* **A change to the link is applied, not merely saved.** Pairing restarts the
  link, the toggle starts or stops it, a new publish set asks for a fresh
  snapshot. A route that writes the row and stops leaves the running link
  describing the farm the user just changed.

The portal here is real — an aiohttp server on a loopback port, as in
``test_cloud_link_pairing``. The *service* is not: its lifecycle is Task 8's
contract and is tested there against real tasks and a real
``ConnectionManager``. Here it is spied on, because what this layer owes is the
call, and starting a genuine client would have every route test opening a
socket and racing the leak check.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api.routes import cloud_link as cloud_link_routes
from backend.app.models.cloud_link import DEFAULT_PORTAL_URL, CloudLink, CloudLinkAudit, CloudLinkPrinter
from backend.app.services.cloud_link.service import cloud_link_service
from backend.app.services.cloud_link.store import get_config, get_secret, save_credentials, set_publish_set

BASE = "/api/v1/cloud-link"
PAIR_PATH = "/api/link/v1/pair"

#: Every route on the router, and a body that gets past request validation.
#: Used twice: once to prove each one refuses a caller without the permission,
#: and once — against the router itself — to prove the list is complete.
ROUTES = [
    ("get", "/status", None),
    ("post", "/pair", {"pairing_code": "ABCD-EFGH"}),
    ("post", "/unpair", None),
    ("put", "/publish-set", {"printer_ids": []}),
    ("put", "/enabled", {"enabled": False}),
    ("get", "/audit", None),
]


# ------------------------------------------------------------- the fixtures


@pytest.fixture
async def viewer_client(async_client, test_engine):
    """A logged-in user who is not an administrator.

    Built on ``async_client`` rather than beside it: that fixture is what
    installs the ``get_db`` override and patches the module-level
    ``async_session`` the permission dependency opens, so a second client is
    only meaningful while it is alive.
    """
    from backend.app.core.auth import create_access_token, get_password_hash
    from backend.app.main import app
    from backend.app.models.group import Group
    from backend.app.models.user import User

    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        # ``scalar_one`` on purpose: a groupless user would be refused too, and
        # the test would pass while proving nothing about the Viewers group.
        viewers = (await db.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
        viewer = User(
            username="test_viewer",
            password_hash=get_password_hash("Test_ViewerPass1!"),
            role="user",
            is_active=True,
        )
        viewer.groups.append(viewers)
        db.add(viewer)
        await db.commit()

    token = create_access_token(data={"sub": "test_viewer"})
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest.fixture
async def portal():
    """Start portals on loopback, hand back their base URL, take them down."""
    runners = []

    async def _start(handler, path: str = PAIR_PATH) -> str:
        app = web.Application()
        app.router.add_post(path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        runners.append(runner)
        return f"http://127.0.0.1:{port}"

    yield _start

    for runner in runners:
        await runner.cleanup()


class Spy:
    """Records that it was awaited, and how often."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1


@pytest.fixture
def service_spies(monkeypatch):
    """Replace the singleton's four lifecycle calls with recorders.

    ``monkeypatch.setattr`` on the instance, so the route's own import of the
    singleton sees them and the originals come back at teardown.
    """
    spies = {name: Spy() for name in ("start", "stop", "restart", "request_snapshot")}
    for name, spy in spies.items():
        monkeypatch.setattr(cloud_link_service, name, spy)
    return spies


def _issues_credentials(instance_id: str = "inst_abc", secret: str = "s3cr3t-instance-token-000111"):
    """A portal that accepts anything, and the record of what it was asked."""
    seen: list[dict] = []

    async def handler(request):
        seen.append(await request.json())
        return web.json_response({"instance_id": instance_id, "instance_secret": secret}, status=201)

    return handler, seen


def _closed_port_url() -> str:
    """A loopback URL with nothing listening on it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------- who may ask


@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m}{p}" for m, p, _ in ROUTES])
async def test_every_route_refuses_a_caller_without_the_permission(viewer_client, method, path, body):
    """One missing dependency is a farm a viewer can put on the internet.

    Parametrised over the whole table rather than spot-checked: the routes that
    look harmless are exactly the ones a decorator gets left off — ``status``
    names the portal this farm answers to, and ``audit`` is the record of
    everything that crossed.
    """
    response = await getattr(viewer_client, method)(f"{BASE}{path}", **({"json": body} if body else {}))
    assert response.status_code == 403, response.text


def test_the_table_above_covers_every_route_on_the_router():
    """A drift guard: a seventh route needs a seventh permission test.

    Compared against the router's own routes so that adding one and forgetting
    the test fails here rather than in production, where the symptom is a
    viewer holding a control they should never have seen.
    """
    on_router = {
        (method.lower(), route.path)
        for route in cloud_link_routes.router.routes
        for method in route.methods
        if method != "HEAD"
    }
    assert on_router == {(method, f"/cloud-link{path}") for method, path, _ in ROUTES}


# ------------------------------------------------------------------ status


async def test_status_describes_a_farm_that_has_configured_nothing(async_client):
    """The shape is the contract the settings page renders, and every field is
    present before anything is set up — a page that has to distinguish "false"
    from "the key was missing" renders neither."""
    response = await async_client.get(f"{BASE}/status")
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == {
        "enabled",
        "paired",
        "connected",
        "portal_url",
        "instance_id",
        "last_connected_at",
        "last_error",
        "revoked",
        "published_printer_ids",
    }
    assert body["enabled"] is False, "Cloud Link ships off"
    assert body["paired"] is False
    assert body["connected"] is False
    assert body["revoked"] is False
    assert body["portal_url"] == DEFAULT_PORTAL_URL
    assert body["instance_id"] is None
    assert body["published_printer_ids"] == []


async def test_status_reports_the_allowlist_as_saved_not_as_published(
    async_client, db_session: AsyncSession, printer_factory
):
    """The raw set, including a printer that is currently unavailable.

    The uplink publishes ``is_active AND NOT archived``; this endpoint feeds
    the checkbox list. Filtering here would silently untick a box the user
    ticked the moment they archived a printer, and re-saving the page would
    then drop it for good.
    """
    live = await printer_factory(name="Live")
    archived = await printer_factory(name="Archived", archived=True)
    await set_publish_set(db_session, [live.id, archived.id])

    body = (await async_client.get(f"{BASE}/status")).json()
    assert body["published_printer_ids"] == sorted([live.id, archived.id])


# ----------------------------------------------------------------- pairing


async def test_pairing_stores_the_credential_enables_the_link_and_restarts_it(
    async_client, db_session: AsyncSession, portal, service_spies
):
    """The happy path, end to end: a code in, a running link out.

    ``enabled`` is set by this route and not by the store — holding a
    credential and choosing to use it are two decisions everywhere else in the
    subsystem, and this is the one place where the user made both at once by
    typing a code into the pairing form.
    """
    handler, seen = _issues_credentials(instance_id="inst_route", secret="route-secret-0001")
    url = await portal(handler)

    response = await async_client.post(f"{BASE}/pair", json={"pairing_code": "abcd-efgh", "portal_url": url})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["paired"] is True
    assert body["enabled"] is True
    assert body["instance_id"] == "inst_route"
    assert body["portal_url"] == url

    assert [s["pairing_code"] for s in seen] == ["ABCD-EFGH"], "the portal is sent the canonical form"
    assert await get_secret(db_session) == "route-secret-0001"
    assert service_spies["restart"].calls == 1, "a new credential is only useful once the link is rebuilt"


async def test_a_code_in_the_wrong_shape_is_a_400_and_never_reaches_the_portal(async_client, portal, service_spies):
    """The format gate is the reason a half-typed code costs nothing."""
    handler, seen = _issues_credentials()
    url = await portal(handler)

    response = await async_client.post(f"{BASE}/pair", json={"pairing_code": "nope", "portal_url": url})
    assert response.status_code == 400, response.text
    assert seen == [], "a malformed code must not leave the machine"
    assert service_spies["restart"].calls == 0


async def test_a_code_the_portal_does_not_know_is_a_404(async_client, portal, service_spies):
    """Distinct from every other failure because it is the only one the user
    can fix themselves — fetch a new code."""

    async def refuses(request):
        return web.json_response({"detail": "no such code"}, status=404)

    url = await portal(refuses)
    response = await async_client.post(f"{BASE}/pair", json={"pairing_code": "ABCD-EFGH", "portal_url": url})
    assert response.status_code == 404, response.text
    assert service_spies["restart"].calls == 0


async def test_a_portal_that_answers_nothing_is_a_502_that_does_not_blame_the_wire(async_client, service_spies):
    """``network`` covers a refused connection AND a portal that answered 500,
    so the message must not tell the user their network is down when the far
    end simply said no. Getting this wrong sends somebody to their router."""
    response = await async_client.post(
        f"{BASE}/pair", json={"pairing_code": "ABCD-EFGH", "portal_url": _closed_port_url()}
    )
    assert response.status_code == 502, response.text
    detail = response.json()["detail"].lower()
    assert "refused" in detail and "unreachable" in detail
    assert service_spies["restart"].calls == 0


async def test_a_portal_url_that_is_not_tls_is_refused_before_anything_is_saved(
    async_client, db_session: AsyncSession, service_spies
):
    """Plain http across a network hands the instance secret to the path.

    The check runs before the URL is stored, so a rejected one leaves the
    previous portal standing — a farm must not end up pointed at something it
    was told it could not use.
    """
    response = await async_client.post(
        f"{BASE}/pair", json={"pairing_code": "ABCD-EFGH", "portal_url": "http://cloud.example.com"}
    )
    assert response.status_code == 400, response.text

    config = await get_config(db_session)
    await db_session.refresh(config)
    assert config.portal_url == DEFAULT_PORTAL_URL


async def test_pairing_without_a_portal_url_keeps_the_one_already_saved(
    async_client, db_session: AsyncSession, portal, service_spies
):
    """The field is optional: re-pairing with the same portal is the common
    case, and an absent value must not be read as "reset to the default"."""
    handler, _ = _issues_credentials()
    url = await portal(handler)
    config = await get_config(db_session)
    config.portal_url = url
    await db_session.commit()

    response = await async_client.post(f"{BASE}/pair", json={"pairing_code": "ABCD-EFGH"})
    assert response.status_code == 200, response.text
    assert response.json()["portal_url"] == url


# ------------------------------------------------------------- publish set


async def test_saving_the_publish_set_stores_it_and_asks_for_a_fresh_snapshot(
    async_client, db_session: AsyncSession, printer_factory, service_spies
):
    """Saved and applied. The running link holds its own copy of the set, so a
    route that only wrote the rows would leave the portal being told about a
    printer the user just unticked until the next reconnect."""
    one = await printer_factory(name="One")
    two = await printer_factory(name="Two")

    response = await async_client.put(f"{BASE}/publish-set", json={"printer_ids": [two.id, one.id]})
    assert response.status_code == 200, response.text
    assert response.json()["published_printer_ids"] == sorted([one.id, two.id])

    stored = (await db_session.execute(select(CloudLinkPrinter.printer_id))).scalars().all()
    assert sorted(stored) == sorted([one.id, two.id])
    assert service_spies["request_snapshot"].calls == 1


async def test_the_publish_set_refuses_an_archived_printer_and_names_it(
    async_client, db_session: AsyncSession, printer_factory, service_spies
):
    """422 with the id in it. "Something was wrong" is unactionable on a page
    listing thirty machines, and the allowlist is exactly the control where a
    silent partial save is worst."""
    live = await printer_factory(name="Live")
    archived = await printer_factory(name="Archived", archived=True)

    response = await async_client.put(f"{BASE}/publish-set", json={"printer_ids": [live.id, archived.id]})
    assert response.status_code == 422, response.text
    assert str(archived.id) in response.json()["detail"]

    stored = (await db_session.execute(select(CloudLinkPrinter.printer_id))).scalars().all()
    assert stored == [], "a rejected save changes nothing at all — not even the half that was valid"
    assert service_spies["request_snapshot"].calls == 0


async def test_the_publish_set_refuses_a_printer_in_maintenance_too(async_client, printer_factory, service_spies):
    """Availability is ``is_active AND NOT archived`` — both halves. A printer
    parked in Maintenance Mode is not published by the uplink either, so
    accepting it here would tick a box that does nothing."""
    parked = await printer_factory(name="Parked", is_active=False)

    response = await async_client.put(f"{BASE}/publish-set", json={"printer_ids": [parked.id]})
    assert response.status_code == 422, response.text
    assert str(parked.id) in response.json()["detail"]


async def test_the_publish_set_refuses_a_printer_that_does_not_exist(async_client, service_spies):
    """A stale settings page saving an id that has since been deleted. The FK
    is decorative on SQLite, so without this the row would be stored and the
    snapshot would quietly skip it forever."""
    response = await async_client.put(f"{BASE}/publish-set", json={"printer_ids": [4242]})
    assert response.status_code == 422, response.text
    assert "4242" in response.json()["detail"]


async def test_an_empty_publish_set_is_a_valid_answer(
    async_client, db_session: AsyncSession, printer_factory, service_spies
):
    """Unticking everything is how a paired farm stops publishing anything
    without unpairing. It must not be mistaken for "no change asked for"."""
    one = await printer_factory(name="One")
    await set_publish_set(db_session, [one.id])

    response = await async_client.put(f"{BASE}/publish-set", json={"printer_ids": []})
    assert response.status_code == 200, response.text
    assert response.json()["published_printer_ids"] == []
    assert (await db_session.execute(select(CloudLinkPrinter.printer_id))).scalars().all() == []


# ---------------------------------------------------------------- unpairing


async def test_unpair_takes_the_link_down_forgets_the_credential_and_records_it(
    async_client, db_session: AsyncSession, service_spies
):
    """Order matters: the link is stopped before the credential goes, so
    nothing is mid-reconnect with a secret that is about to be deleted."""
    await save_credentials(db_session, instance_id="inst_gone", secret="secret-to-forget")
    config = await get_config(db_session)
    config.enabled = True
    await db_session.commit()

    response = await async_client.post(f"{BASE}/unpair")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["paired"] is False
    assert body["enabled"] is False
    assert body["instance_id"] is None

    # ⚠️ The test session loaded this row before the request did, and it is
    # built with ``expire_on_commit=False`` — so ``get_secret``'s
    # ``session.get`` would answer from the identity map with the credential as
    # it was, not as the route left it. Expiring first is what makes this a
    # read of the database rather than of our own memory.
    db_session.expire_all()
    assert await get_secret(db_session) is None
    assert service_spies["stop"].calls == 1

    kinds = (await db_session.execute(select(CloudLinkAudit.kind))).scalars().all()
    assert "unpair" in kinds
    summaries = (await db_session.execute(select(CloudLinkAudit.summary))).scalars().all()
    assert not any("secret-to-forget" in s for s in summaries), "a secret never reaches the audit"


async def test_unpair_is_idempotent(async_client, db_session: AsyncSession, service_spies):
    """A farm that was never paired, or a second click on a slow page. Both are
    a no-op that answers 200 — an error here would tell the user something is
    wrong with a link that is exactly as gone as they wanted."""
    first = await async_client.post(f"{BASE}/unpair")
    second = await async_client.post(f"{BASE}/unpair")

    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["paired"] is False
    assert await get_secret(db_session) is None


# ------------------------------------------------------------- the toggle


async def test_turning_the_link_on_saves_the_choice_and_starts_it(
    async_client, db_session: AsyncSession, service_spies
):
    response = await async_client.put(f"{BASE}/enabled", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True

    row = (await db_session.execute(select(CloudLink))).scalar_one()
    await db_session.refresh(row)
    assert row.enabled is True
    assert service_spies["start"].calls == 1
    assert service_spies["stop"].calls == 0


async def test_turning_the_link_off_saves_the_choice_and_stops_it(
    async_client, db_session: AsyncSession, service_spies
):
    """The switch is the user's only way to disconnect a farm in a hurry, so
    the socket has to go with the flag — not at the next restart."""
    config = await get_config(db_session)
    config.enabled = True
    await db_session.commit()

    response = await async_client.put(f"{BASE}/enabled", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False

    await db_session.refresh(config)
    assert config.enabled is False
    assert service_spies["stop"].calls == 1
    assert service_spies["start"].calls == 0


# ---------------------------------------------------------------- the audit


async def _seed_audit(session: AsyncSession, count: int) -> None:
    """Rows with distinct stamps, oldest first.

    Stamped explicitly rather than through ``write_audit``: SQLite's
    ``CURRENT_TIMESTAMP`` has one-second resolution, so five rows written in a
    burst would share a stamp and "newest first" would be untestable.
    """
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=count)
    for i in range(count):
        session.add(
            CloudLinkAudit(
                ts=base + timedelta(hours=i),
                direction="up" if i % 2 else "down",
                kind="status",
                summary=f"entry {i}",
                ok=i != 2,
            )
        )
    await session.commit()


async def test_the_audit_comes_back_newest_first_in_pages(async_client, db_session: AsyncSession):
    await _seed_audit(db_session, 5)

    first = (await async_client.get(f"{BASE}/audit", params={"page": 1, "page_size": 2})).json()
    assert first["total"] == 5
    assert first["page"] == 1
    assert first["page_size"] == 2
    assert [i["summary"] for i in first["items"]] == ["entry 4", "entry 3"]
    assert set(first["items"][0]) == {"ts", "direction", "kind", "summary", "ok"}

    last = (await async_client.get(f"{BASE}/audit", params={"page": 3, "page_size": 2})).json()
    assert [i["summary"] for i in last["items"]] == ["entry 0"]


async def test_a_page_past_the_end_is_empty_rather_than_an_error(async_client, db_session: AsyncSession):
    """The list shrinks under the reader — the prune sweep runs daily and an
    unpair adds rows. A 404 for a page that existed a second ago is a dead end
    in the UI for something that is not wrong."""
    await _seed_audit(db_session, 2)

    body = (await async_client.get(f"{BASE}/audit", params={"page": 9, "page_size": 2})).json()
    assert body["items"] == []
    assert body["total"] == 2


async def test_the_page_size_is_capped(async_client, db_session: AsyncSession):
    """The audit is unbounded in rows and holds free text the far end supplied.
    A caller asking for all of it is a response nobody can render."""
    assert (await async_client.get(f"{BASE}/audit", params={"page_size": 1000})).status_code == 422
    assert (await async_client.get(f"{BASE}/audit", params={"page": 0})).status_code == 422


async def test_the_audit_carries_the_failure_flag(async_client, db_session: AsyncSession):
    """``ok`` is the only field that distinguishes a message that crossed from
    one that did not, and a failure is the row an operator opens the table
    for."""
    await _seed_audit(db_session, 5)

    items = (await async_client.get(f"{BASE}/audit", params={"page_size": 100})).json()["items"]
    failed = [i for i in items if not i["ok"]]
    assert [i["summary"] for i in failed] == ["entry 2"]
