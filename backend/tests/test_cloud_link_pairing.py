"""Cloud Link pairing — turning a code somebody typed into a credential.

Pairing is the one exchange in the link that is plain HTTPS rather than an
envelope frame: there is no credential yet, so there is nothing to sign a frame
with. It is also the only place a user-supplied string reaches the network,
which is why the format check comes first and the tests below insist that a
malformed code never leaves the machine.

The portal here is real — an aiohttp server on a loopback port, answering the
same path the production one does. Mocking ``ClientSession`` would test our
mock's idea of aiohttp; a socket tests the URL we actually built, the payload
we actually sent, and the status handling as aiohttp actually delivers it.
"""

from __future__ import annotations

import socket

import pytest
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import APP_VERSION
from backend.app.models.cloud_link import CloudLink, CloudLinkAudit
from backend.app.services.cloud_link.pairing import PairingError, pair
from backend.app.services.cloud_link.store import get_config, get_secret

PAIR_PATH = "/api/link/v1/pair"


async def make_portal(handler, path: str = PAIR_PATH):
    """An in-process portal on a free loopback port."""
    app = web.Application()
    app.router.add_post(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.fixture
async def portal():
    """Start portals, hand back their base URL, take them down afterwards."""
    runners = []

    async def _start(handler, path: str = PAIR_PATH) -> str:
        runner, url = await make_portal(handler, path)
        runners.append(runner)
        return url

    yield _start

    for runner in runners:
        await runner.cleanup()


async def _point_at(session: AsyncSession, portal_url: str) -> None:
    config = await get_config(session)
    config.portal_url = portal_url
    await session.commit()


def _issues_credentials(instance_id: str = "inst_abc", secret: str = "s3cr3t-instance-token-000111"):
    """A portal that accepts anything, and the record of what it was asked."""
    seen: list[dict] = []

    async def handler(request):
        seen.append(await request.json())
        return web.json_response({"instance_id": instance_id, "instance_secret": secret}, status=201)

    return handler, seen


# ------------------------------------------------------------- the happy path


async def test_a_code_the_portal_accepts_becomes_a_stored_credential(db_session: AsyncSession, portal):
    """201 is the only success, and what it carries goes to disk encrypted.

    The secret IS the credential — anything holding it can speak for this farm
    — so the assertion is not merely that it round-trips but that the plaintext
    is nowhere in the row.
    """
    from backend.app.core.encryption import is_encryption_active

    assert is_encryption_active(), "the plaintext-fallback path would make the assertion below vacuous"

    handler, seen = _issues_credentials()
    await _point_at(db_session, await portal(handler))

    await pair(db_session, "ABCD-EFGH")

    row = (await db_session.execute(select(CloudLink).where(CloudLink.id == 1))).scalar_one()
    assert row.instance_id == "inst_abc"
    assert row.instance_secret_encrypted
    assert "s3cr3t-instance-token-000111" not in row.instance_secret_encrypted
    assert await get_secret(db_session) == "s3cr3t-instance-token-000111"

    assert seen == [
        {
            "pairing_code": "ABCD-EFGH",
            "instance_name": socket.gethostname(),
            "bamdude_version": APP_VERSION,
        }
    ], "the portal is told who we are and what we run — version from the constant, never a literal"


async def test_pairing_is_recorded_in_the_audit_without_the_secret(db_session: AsyncSession, portal):
    """The audit is the operator's record of what crossed the link. It is read
    by humans in a table and kept for a month, so a secret written into a
    summary is a secret in a place nobody thinks to look for one."""
    handler, _ = _issues_credentials(secret="do-not-write-me-down")
    await _point_at(db_session, await portal(handler))

    await pair(db_session, "ABCD-EFGH")

    entry = (await db_session.execute(select(CloudLinkAudit))).scalar_one()
    assert entry.direction == "up"
    assert entry.kind == "pair"
    assert entry.ok is True
    assert "do-not-write-me-down" not in entry.summary
    assert "inst_abc" in entry.summary, "a summary that cannot be tied to a pairing records nothing"


async def test_a_code_is_taken_however_the_user_typed_it(db_session: AsyncSession, portal):
    """The code is shown in uppercase and read back off a screen or a phone.

    Case and stray spaces are the user's typing, not a wrong code, so they are
    normalised before the format check — and the portal is sent the canonical
    form it issued.
    """
    handler, seen = _issues_credentials()
    await _point_at(db_session, await portal(handler))

    await pair(db_session, "  abcd-efgh\n")

    assert seen[0]["pairing_code"] == "ABCD-EFGH"


# --------------------------------------------------------------- the URL join


async def test_a_stored_url_with_a_trailing_slash_still_reaches_the_endpoint(db_session: AsyncSession, portal):
    """``https://portal/`` and ``https://portal`` are one portal.

    Naive concatenation makes ``//api/link/v1/pair``, which is a different path
    to the server and comes back 404 — reported to the user as an invalid code,
    sending them off to re-read a code that was never the problem.
    """
    base = await portal(_issues_credentials()[0])
    await _point_at(db_session, base + "/")

    await pair(db_session, "ABCD-EFGH")

    assert (await get_config(db_session)).instance_id == "inst_abc"


async def test_a_portal_under_a_sub_path_keeps_its_sub_path(db_session: AsyncSession, portal):
    """A portal behind a reverse proxy lives at ``https://host/cloud`` and the
    endpoint is below it. ``urljoin`` with an absolute path would throw that
    prefix away and knock on the proxy's root instead."""
    handler, seen = _issues_credentials()
    base = await portal(handler, path="/cloud" + PAIR_PATH)
    await _point_at(db_session, base + "/cloud")

    await pair(db_session, "ABCD-EFGH")

    assert len(seen) == 1


# -------------------------------------------------------------- the sad paths


async def test_a_code_the_portal_does_not_know_is_an_invalid_code(db_session: AsyncSession, portal):
    """404 is the portal saying "no such pairing code" — expired, mistyped or
    already spent. It is the one failure the user can fix themselves, so it
    must not be blurred into the generic transport bucket."""

    async def not_found(request):
        return web.json_response({"error": "unknown code"}, status=404)

    await _point_at(db_session, await portal(not_found))

    with pytest.raises(PairingError) as ei:
        await pair(db_session, "ABCD-EFGH")
    assert ei.value.code == "invalid_code"

    assert (await get_config(db_session)).instance_id is None, "a refused pairing leaves the farm as it was"


async def test_a_portal_that_is_not_listening_is_a_network_failure(db_session: AsyncSession):
    """Nothing on the far end, so nothing about the code is known yet. Telling
    the user their code is invalid here would be a guess, and the wrong one."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    await _point_at(db_session, f"http://127.0.0.1:{port}")

    with pytest.raises(PairingError) as ei:
        await pair(db_session, "ABCD-EFGH")
    assert ei.value.code == "network"


async def test_a_portal_that_breaks_is_a_network_failure_not_a_bad_code(db_session: AsyncSession, portal):
    """A 500 says the portal is having a bad day; the code may well be fine.

    Every non-201 that is not a 404 lands in ``network`` — the bucket that
    means "try again", which is the correct advice for all of them.
    """

    async def broken(request):
        return web.Response(status=500, text="boom")

    await _point_at(db_session, await portal(broken))

    with pytest.raises(PairingError) as ei:
        await pair(db_session, "ABCD-EFGH")
    assert ei.value.code == "network"


async def test_a_201_without_credentials_saves_nothing(db_session: AsyncSession, portal):
    """A success that carries no secret is not a pairing. Storing the half that
    did arrive would leave the farm believing it is paired and unable to say
    why nothing ever connects."""

    async def empty_success(request):
        return web.json_response({"instance_id": "inst_abc"}, status=201)

    await _point_at(db_session, await portal(empty_success))

    with pytest.raises(PairingError) as ei:
        await pair(db_session, "ABCD-EFGH")
    assert ei.value.code == "network"
    assert (await get_config(db_session)).instance_id is None


# ------------------------------------------------------------ the format gate


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "ABCDEFGH",
        "ABCD-EFG",
        "ABCD-EFGH-IJKL",
        "ABCI-EFGH",
        "ABC0-EFGH",
        "ABC1-EFGH",
        "ABCO-EFGH",
        "AB!D-EFGH",
    ],
)
async def test_a_malformed_code_is_refused_without_touching_the_network(db_session: AsyncSession, portal, typed):
    """The check comes first, and the portal is never asked.

    The alphabet drops the four characters that look like each other on a
    screen (I/1, O/0), so a code carrying one was mistyped by definition — and
    a request per keystroke of a half-typed code is load the portal would have
    to rate-limit on our behalf.
    """
    hits: list[str] = []

    async def must_not_be_called(request):
        hits.append(request.path)
        return web.json_response({"instance_id": "x", "instance_secret": "y"}, status=201)

    await _point_at(db_session, await portal(must_not_be_called))

    with pytest.raises(PairingError) as ei:
        await pair(db_session, typed)
    assert ei.value.code == "bad_format"
    assert hits == [], f"{typed!r} reached the portal — the format gate is not first"
