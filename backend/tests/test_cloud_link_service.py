"""Cloud Link service — one link, one task, and nothing left running.

The service is the only thing in the agent that decides whether the link
exists. Everything it owns is a pair: a listener registered and unregistered, a
task created and cancelled, an uplink built and dropped. These tests are about
the *second* half of each pair, because that is the half a happy path never
exercises and the half a leaked task punishes three tests later.

Four things they pin, none of which a "does it connect" test would notice:

* **A disabled, unpaired or revoked farm spawns nothing at all.** Cloud Link
  ships off; ``start()`` running the loop "just to see" would open a socket
  nobody asked for.
* **``stop()`` cancels, it does not merely ask.** ``CloudLinkClient.run``
  honours its stop event, but a stop that arrives mid-handshake waits out the
  20 s handshake timeout first — longer than Docker's or systemd's default
  grace period. The stub client here deliberately ignores its stop event, so
  the test fails if the service ever goes back to only setting it.
* **The listener count comes back to where it started.** A registered
  ``uplink.feed`` runs ahead of every browser write in the product; one left
  behind after ``stop()`` is a queue filling forever for a link that is gone.
* **A client that ends by itself cleans up after itself.** The loop returns on
  its own when the farm is not paired — the listener has to go with it, not
  wait for a ``stop()`` nobody will call.

The client is stubbed by name (``service.CloudLinkClient``) rather than by a
constructor seam: the service's job is to *build* the real one, and a seam
would let the wiring be wrong while the tests were right. One test runs the
real client against a closed port to keep that honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.websocket import ConnectionManager
from backend.app.models.cloud_link import CloudLinkAudit
from backend.app.services.cloud_link import service as service_module
from backend.app.services.cloud_link.service import CloudLinkService, cloud_link_service
from backend.app.services.cloud_link.store import get_config, save_credentials, set_publish_set

# --------------------------------------------------------------- the fixtures


@pytest.fixture
def session_factory(test_engine):
    """The shape ``core/database.async_session`` has."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def connections():
    """A real ``ConnectionManager``, so the listener count is the product's own.

    A fresh instance rather than the ``ws_manager`` singleton: a test that
    leaked a listener onto the singleton would break whichever later test
    happened to broadcast.
    """
    return ConnectionManager()


class StubClient:
    """``CloudLinkClient`` narrowed to what the service asks of it.

    ⚠️ ``run`` **ignores its stop event on purpose.** The real client honours
    it, but only after up to ~35 s of handshake and connect timeouts, and the
    service's contract is that ``stop()`` returns promptly regardless. A stub
    that returned on the event would let a service which only sets it pass.
    """

    instances: list[StubClient] = []

    def __init__(self, *, session_factory, uplink, **_ignored):
        self.session_factory = session_factory
        self.uplink = uplink
        self.connected = False
        self.runs = 0
        self.cancelled = False
        self.snapshots_requested = 0
        self.started = asyncio.Event()
        StubClient.instances.append(self)

    async def run(self, stop_event: asyncio.Event) -> None:
        self.runs += 1
        self.started.set()
        self.connected = True
        try:
            await asyncio.Event().wait()  # never set — only a cancel ends this
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.connected = False

    def request_snapshot(self) -> None:
        self.snapshots_requested += 1


class ReturningClient(StubClient):
    """A client whose loop ends by itself — the "not paired" outcome."""

    async def run(self, stop_event: asyncio.Event) -> None:
        self.runs += 1
        self.started.set()
        return


@pytest.fixture(autouse=True)
def _forget_stub_instances():
    StubClient.instances.clear()
    yield
    StubClient.instances.clear()


@pytest.fixture
def stub_client(monkeypatch):
    monkeypatch.setattr(service_module, "CloudLinkClient", StubClient)
    return StubClient


def make_service(session_factory, connections, **overrides) -> CloudLinkService:
    settings = {"prune_first_delay_s": 3600.0, "prune_interval_s": 3600.0}
    settings.update(overrides)
    return CloudLinkService(session_factory=session_factory, connections=connections, **settings)


async def configure(
    session_factory,
    *,
    enabled: bool = True,
    paired: bool = True,
    revoked: bool = False,
    portal_url: str = "http://127.0.0.1:9/",
    published: list[int] | None = None,
) -> None:
    """Put the pairing row into the state a test needs."""
    async with session_factory() as session:
        config = await get_config(session)
        config.enabled = enabled
        config.portal_url = portal_url
        config.revoked = revoked
        await session.commit()
        if paired:
            await save_credentials(session, "inst_1", "s3cr3t")
            # ``save_credentials`` clears ``revoked`` — a fresh pair is a fresh
            # start — so a revoked fixture has to say so afterwards.
            if revoked:
                config = await get_config(session)
                config.revoked = True
                await session.commit()
        if published is not None:
            await set_publish_set(session, published)


def link_tasks() -> list[asyncio.Task]:
    """Every live task the service named as its own."""
    return [
        t
        for t in asyncio.all_tasks()
        if not t.done() and (t.get_name() or "").startswith("cloud-link") and t is not asyncio.current_task()
    ]


def closed_port() -> int:
    """A loopback port nothing is listening on, so a connect refuses at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.asynccontextmanager
async def started(service: CloudLinkService):
    """Start for the body, and insist the service is gone afterwards."""
    await service.start()
    try:
        yield service
    finally:
        await asyncio.wait_for(service.stop(), 5.0)


# ------------------------------------------------------- nothing to start


class TestItStartsNothingItWasNotAskedFor:
    """Cloud Link ships off. Three states mean "do not connect", and each of
    them has its own repair — so each is checked on its own rather than
    through one combined "not ready" flag."""

    async def test_disabled_spawns_nothing(self, session_factory, connections, stub_client):
        await configure(session_factory, enabled=False)
        service = make_service(session_factory, connections)

        await service.start()

        assert service.running is False
        assert link_tasks() == []
        assert connections._internal_listeners == []
        assert StubClient.instances == []

    async def test_unpaired_spawns_nothing(self, session_factory, connections, stub_client):
        await configure(session_factory, enabled=True, paired=False)
        service = make_service(session_factory, connections)

        await service.start()

        assert service.running is False
        assert link_tasks() == []
        assert connections._internal_listeners == []

    async def test_revoked_spawns_nothing(self, session_factory, connections, stub_client):
        await configure(session_factory, enabled=True, paired=True, revoked=True)
        service = make_service(session_factory, connections)

        await service.start()

        assert service.running is False
        assert link_tasks() == []
        assert connections._internal_listeners == []


# ------------------------------------------------------------ start and stop


class TestItStartsExactlyOneLink:
    async def test_enabled_and_paired_spawns_one_client_task(self, session_factory, connections, stub_client):
        await configure(session_factory, published=[3, 7])
        service = make_service(session_factory, connections)

        async with started(service):
            await asyncio.wait_for(StubClient.instances[0].started.wait(), 2.0)

            assert service.running is True
            assert len(StubClient.instances) == 1
            assert StubClient.instances[0].runs == 1
            # One client loop. The audit-prune tick is a child of it, named so
            # it is visible here and cancelled with its parent.
            assert sum(1 for t in link_tasks() if t.get_name() == "cloud-link") == 1

    async def test_it_registers_the_uplink_and_seeds_the_publish_set(self, session_factory, connections, stub_client):
        await configure(session_factory, published=[3, 7])
        service = make_service(session_factory, connections)

        async with started(service):
            uplink = StubClient.instances[0].uplink
            assert connections._internal_listeners == [uplink.feed]
            assert uplink.published == {3, 7}

    async def test_a_second_start_is_a_no_op(self, session_factory, connections, stub_client):
        await configure(session_factory)
        service = make_service(session_factory, connections)

        async with started(service):
            await asyncio.wait_for(StubClient.instances[0].started.wait(), 2.0)
            await service.start()

            assert len(StubClient.instances) == 1
            assert StubClient.instances[0].runs == 1
            assert len(connections._internal_listeners) == 1
            assert sum(1 for t in link_tasks() if t.get_name() == "cloud-link") == 1


class TestItStopsWhatItStarted:
    async def test_stop_cancels_the_task_rather_than_asking_it_to_end(self, session_factory, connections, stub_client):
        await configure(session_factory)
        service = make_service(session_factory, connections)
        await service.start()
        client = StubClient.instances[0]
        await asyncio.wait_for(client.started.wait(), 2.0)

        # The stub never returns on its stop event: only a cancel ends it, and
        # the timeout is what fails if the service waits for the event instead.
        await asyncio.wait_for(service.stop(), 2.0)

        assert client.cancelled is True
        assert service.running is False
        assert link_tasks() == []

    async def test_stop_unregisters_the_listener(self, session_factory, connections, stub_client):
        await configure(session_factory)
        service = make_service(session_factory, connections)
        await service.start()
        assert len(connections._internal_listeners) == 1

        await asyncio.wait_for(service.stop(), 2.0)

        assert connections._internal_listeners == []

    async def test_a_stop_before_the_task_ever_ran_still_unregisters(self, session_factory, connections, stub_client):
        """The cleanup cannot live in the task's ``finally`` alone.

        ``stop()`` immediately after ``start()`` cancels a task that has not
        taken its first step, so the coroutine raises at its first line and the
        ``finally`` inside it never runs. Without ``stop()`` doing its own
        removal, this is a listener left on the hub forever.
        """
        await configure(session_factory)
        service = make_service(session_factory, connections)

        await service.start()
        assert len(connections._internal_listeners) == 1
        await asyncio.wait_for(service.stop(), 2.0)

        assert connections._internal_listeners == []
        assert link_tasks() == []

    async def test_stop_without_a_start_is_not_an_error(self, session_factory, connections, stub_client):
        service = make_service(session_factory, connections)

        await asyncio.wait_for(service.stop(), 2.0)
        await asyncio.wait_for(service.stop(), 2.0)

        assert service.running is False

    async def test_a_client_that_ends_by_itself_takes_its_listener_with_it(
        self, session_factory, connections, monkeypatch
    ):
        monkeypatch.setattr(service_module, "CloudLinkClient", ReturningClient)
        await configure(session_factory)
        service = make_service(session_factory, connections)

        await service.start()
        for _ in range(200):
            if not connections._internal_listeners:
                break
            await asyncio.sleep(0.01)

        assert connections._internal_listeners == []
        assert service.running is False
        await service.stop()
        assert link_tasks() == []

    async def test_restart_replaces_the_link(self, session_factory, connections, stub_client):
        await configure(session_factory)
        service = make_service(session_factory, connections)
        await service.start()
        first = StubClient.instances[0]
        await asyncio.wait_for(first.started.wait(), 2.0)

        try:
            await asyncio.wait_for(service.restart(), 5.0)

            assert first.cancelled is True
            assert len(StubClient.instances) == 2
            assert len(connections._internal_listeners) == 1
            assert sum(1 for t in link_tasks() if t.get_name() == "cloud-link") == 1
        finally:
            await asyncio.wait_for(service.stop(), 5.0)


# ----------------------------------------------------------------- the status


class TestTheStatus:
    async def test_it_reports_the_row_and_the_live_flag(self, session_factory, connections, stub_client):
        await configure(session_factory)
        service = make_service(session_factory, connections)

        async with started(service):
            await asyncio.wait_for(StubClient.instances[0].started.wait(), 2.0)
            async with session_factory() as session:
                status = await service.status(session)

            assert status == {
                "enabled": True,
                "paired": True,
                "connected": True,
                "revoked": False,
                "last_error": None,
                "last_connected_at": None,
            }

    async def test_a_stopped_link_is_never_connected(self, session_factory, connections, stub_client):
        await configure(session_factory, enabled=False)
        service = make_service(session_factory, connections)
        await service.start()

        async with session_factory() as session:
            status = await service.status(session)

        assert status["enabled"] is False
        assert status["paired"] is True
        assert status["connected"] is False


# ---------------------------------------------------------- the snapshot ask


class TestRequestSnapshot:
    async def test_it_narrows_the_publish_set_and_asks_for_a_snapshot(self, session_factory, connections, stub_client):
        await configure(session_factory, published=[3, 7])
        service = make_service(session_factory, connections)

        async with started(service):
            client = StubClient.instances[0]
            await asyncio.wait_for(client.started.wait(), 2.0)
            async with session_factory() as session:
                await set_publish_set(session, [7, 9])

            await service.request_snapshot()

            # 3 is gone at once — an untick must take effect immediately. 9 is
            # NOT added here: only ``build_snapshot_chunks`` may add, because
            # only it checks that the printer is available.
            assert client.uplink.published == {7}
            assert client.snapshots_requested == 1

    async def test_it_is_silent_when_there_is_no_link(self, session_factory, connections, stub_client):
        await configure(session_factory, enabled=False)
        service = make_service(session_factory, connections)
        await service.start()

        await service.request_snapshot()  # must not raise

        assert StubClient.instances == []


# ------------------------------------------------------------- the audit tick


class TestTheAuditPrune:
    async def test_it_prunes_and_stops_with_the_service(self, session_factory, connections, stub_client):
        await configure(session_factory)
        async with session_factory() as session:
            session.add(
                CloudLinkAudit(
                    ts=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=45),
                    direction="up",
                    kind="connect",
                    summary="long ago",
                )
            )
            session.add(CloudLinkAudit(direction="up", kind="connect", summary="just now"))
            await session.commit()

        service = make_service(session_factory, connections, prune_first_delay_s=0.01, prune_interval_s=0.05)

        async with started(service):
            for _ in range(300):
                async with session_factory() as session:
                    remaining = (await session.execute(select(func.count(CloudLinkAudit.id)))).scalar_one()
                if remaining == 1:
                    break
                await asyncio.sleep(0.01)

            assert remaining == 1

        # The tick is a child of the client task, so stopping the service ends
        # it too — nothing may still be pruning after the link is gone.
        assert link_tasks() == []


# -------------------------------------------------------------- the real one


class TestTheRealClient:
    """One test with no stub, so the wiring cannot be right only in the mock.

    The portal is a closed loopback port: the connect refuses immediately, the
    client records the failure and backs off, and the service still has to be
    able to take it down promptly.
    """

    async def test_it_builds_runs_and_cancels_the_real_client(self, session_factory, connections):
        await configure(session_factory, portal_url=f"http://127.0.0.1:{closed_port()}/")
        service = make_service(session_factory, connections)

        await service.start()
        assert service.running is True
        assert len(connections._internal_listeners) == 1

        await asyncio.wait_for(service.stop(), 5.0)

        assert service.running is False
        assert connections._internal_listeners == []
        assert link_tasks() == []


class TestTheSingleton:
    def test_the_module_exposes_one(self):
        assert isinstance(cloud_link_service, CloudLinkService)
