"""A preview runtime's first start that fails does not leave previews off until BamDude restarts (spec
preview-runtime-first-start-recovery r2): the parent log names the phase a start entered and where it failed, a
failure whose cleanup is PROVEN is tried again in the background — three attempts in all, pauses 2 s and 4 s, one
admission deadline from the first attempt — and nothing unproven is ever retried. One owner holds the whole start;
a stop waits for its attempt and its cleanup, and never for a deadline."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from backend.app.services import preview_runtime as r
from backend.app.services.preview_process import PreviewProcess, SpawnUnproven
from backend.app.services.preview_protocol import PreviewError

LOGGER = "backend.app.services.preview_runtime"
SENTINEL = "SENTINEL-c0ffee-token"
SENTINEL_PATH = "C:/Users/sentinel-user/secret-dir"


# --- helpers ------------------------------------------------------------------------------------------------------


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def _phases(caplog) -> list[str]:
    return [m.split("phase=")[1].split()[0] for m in _messages(caplog) if m.startswith("Preview startup phase=")]


def _failures(caplog) -> list[str]:
    return [m for m in _messages(caplog) if m.startswith("Preview startup_failed:")]


def _everything(caplog) -> str:
    """Every line of every logger, with any exception text a handler would print."""
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts)


async def _until(predicate, seconds: float = 30.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never reached")


def _fast(monkeypatch, *, attempts: int = 3, pauses=(0.05, 0.05), ready: float = 0.5, budget: float | None = None):
    monkeypatch.setattr(r, "STARTUP_ATTEMPTS", attempts)
    monkeypatch.setattr(r, "STARTUP_PAUSES", pauses)
    monkeypatch.setattr(r, "READY_SECONDS", ready)
    if budget is not None:
        monkeypatch.setattr(r, "STARTUP_SECONDS", budget)


async def _stopped(rt) -> None:
    if not rt.closed or (rt.starter is not None and not rt.starter.done()):
        await asyncio.wait_for(rt.stop(), 60)


def _fail_ready_on_attempt(rt, attempt: int = 1, *, missed_wait: float = 0.5):
    """The service misses its readiness answer during ``attempt`` only.

    The failing attempt waits ``missed_wait`` for the answer that never comes, not the
    whole readiness wait ``_fast`` gave the real service: the miss, its phase and its
    proven cleanup are the same after half a second as after eight. Every other
    attempt keeps the ordinary wait. Call after ``_fast``.
    """
    real = rt._rpc

    async def rpc(command, operation=None, **kwargs):
        if operation is None and command.operation == "ready" and rt.start_attempt == attempt:
            raise TimeoutError("no ready answer")
        return await real(command, operation, **kwargs)

    rt._rpc = rpc
    ordinary, real_launch = r.READY_SECONDS, rt._launch

    async def launch(launch_attempt=None):
        # READY_SECONDS is read when ``_launch`` runs; ``_fast``'s monkeypatch restores it afterwards.
        r.READY_SECONDS = min(missed_wait, ordinary) if launch_attempt == attempt else ordinary
        return await real_launch(launch_attempt)

    rt._launch = launch


class _FakeProcess:
    pid = 424242

    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0


class _FakeService:
    """Stands in for PreviewProcess without any process: its stop can be held open or made to fail."""

    def __init__(self, *, stop_error: Exception | None = None, hold: threading.Event | None = None):
        self.process = _FakeProcess()
        self.tail = bytearray()
        self.stop_error, self.hold = stop_error, hold
        self.stop_entered = threading.Event()
        self.stopped = False

    def stop(self):
        self.stop_entered.set()
        if self.hold is not None:
            assert self.hold.wait(30), "test barrier never released"
        if self.stop_error is not None:
            raise self.stop_error
        self.process.alive = False
        self.stopped = True


class _SilentPeer:
    """A real loopback TCP peer that accepts and never speaks NATS: a connect that hangs in its handshake."""

    def __init__(self):
        self.writers, self.server = [], None

    async def __aenter__(self):
        self.server = await asyncio.start_server(lambda reader, writer: self.writers.append(writer), "127.0.0.1", 0)
        self.url = "nats://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc):
        for writer in self.writers:
            writer.close()
        self.server.close()
        await self.server.wait_closed()


def _closed_port_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Nothing listens, so every connect is refused at once; but nats-py retries a refused
    # server internally until the attempt's STARTUP_SECONDS run out, so give such a test a short budget.
    return f"nats://127.0.0.1:{port}"


def _record_clients(monkeypatch) -> list:
    import nats.aio.client as client_module

    clients, real = [], client_module.Client

    class Recorded(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(client_module, "Client", Recorded)
    return clients


@pytest.fixture
async def shared_broker(tmp_path):
    from backend.app.services.local_worker_broker import LocalWorkerBroker

    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    try:
        yield broker
    finally:
        await broker.stop()


# --- R1: phases in the parent log, nothing secret -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_phase_a_start_enters_is_logged_and_the_ready_line_counts_attempts(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    rt = r.PreviewRuntime(tmp_path / "preview")
    try:
        await rt.start()
        assert rt.ready
        assert _phases(caplog) == [
            "staging",
            "broker",
            "connect",
            "stream_list",
            "staging",
            "store_create",
            "service_spawn",
            "service_ready",
        ]
        ready = [m for m in _messages(caplog) if m.startswith("Preview startup ready:")]
        assert len(ready) == 1 and "attempts=1" in ready[0] and rt.generation in ready[0]
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "stream_list", "stream_delete", "store_create", "service_spawn"])
async def test_a_failure_names_its_phase_and_never_its_text(phase, tmp_path, monkeypatch, caplog, shared_broker):
    from nats.aio.client import Client
    from nats.js.api import ObjectStoreConfig
    from nats.js.client import JetStreamContext
    from nats.js.manager import JetStreamManager

    caplog.set_level(logging.DEBUG)
    _fast(monkeypatch, attempts=1)
    if phase == "stream_delete":  # a preview bucket of an earlier generation is there to delete
        js = shared_broker.nc.jetstream()
        await js.create_object_store(
            bucket="bamdude_preview_old", config=ObjectStoreConfig(bucket="bamdude_preview_old", max_bytes=1024**2)
        )
    boom = OSError(f"{SENTINEL} at {SENTINEL_PATH}")

    async def fail(*args, **kwargs):
        raise boom

    def fail_spawn(*args, **kwargs):
        raise boom

    target = {
        "connect": (Client, "connect"),
        "stream_list": (JetStreamManager, "streams_info"),
        "stream_delete": (JetStreamManager, "delete_stream"),
        "store_create": (JetStreamContext, "create_object_store"),
    }
    if phase == "service_spawn":
        monkeypatch.setattr(r, "PreviewProcess", fail_spawn)
    else:
        monkeypatch.setattr(*target[phase], fail)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    try:
        await rt.start()
        failures = _failures(caplog)
        assert len(failures) == 1, failures
        assert f"phase={phase}" in failures[0] and "error=OSError" in failures[0] and rt.generation in failures[0]
        assert rt.health().reason == "startup_failed"
        text = _everything(caplog)
        assert SENTINEL not in text and SENTINEL_PATH not in text and shared_broker.token not in text
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_the_startup_budget_names_the_phase_it_caught(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    _fast(monkeypatch, attempts=1, budget=0.3)
    async with _SilentPeer() as peer:
        rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url=peer.url))
        try:
            await rt.start()
            failures = _failures(caplog)
            assert len(failures) == 1 and "phase=connect" in failures[0] and "error=TimeoutError" in failures[0]
        finally:
            await _stopped(rt)


@pytest.mark.asyncio
async def test_a_missed_readiness_names_its_phase_and_its_cleanup(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    _fast(monkeypatch, attempts=1, ready=0.5)
    rt = r.PreviewRuntime(tmp_path / "preview")
    _fail_ready_on_attempt(rt, 1)
    try:
        await rt.start()
        failures = _failures(caplog)
        assert len(failures) == 1 and "phase=service_ready" in failures[0], failures
        assert int(failures[0].split("phase_ms=")[1].split()[0]) >= 400  # the ready wait, not the cleanup after it
        assert _phases(caplog)[-1] == "cleanup"  # the failed attempt's own cleanup is a phase of its own
        assert rt.service is None and not rt.uncertain  # its service was stopped with proof
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "cancel"])
async def test_nothing_secret_reaches_the_log_even_at_debug(ending, tmp_path, monkeypatch, caplog):
    from nats.aio.client import Client

    caplog.set_level(logging.DEBUG)
    _fast(monkeypatch, attempts=1, ready=8.0)
    if ending == "success":
        rt = r.PreviewRuntime(tmp_path / "preview")
        try:
            await rt.start()
            assert rt.ready
            assert rt.token not in _everything(caplog)
        finally:
            await _stopped(rt)
        return

    async def fail(*args, **kwargs):
        raise OSError(f"{SENTINEL} at {SENTINEL_PATH}")

    async with _SilentPeer() as peer:
        if ending == "failure":
            monkeypatch.setattr(Client, "connect", fail)
        rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token=SENTINEL, url=peer.url))
        try:
            if ending == "failure":
                await rt.start()
            else:
                starting = asyncio.create_task(rt.start())
                await _until(lambda: rt.phase == "connect")
                await asyncio.sleep(0.1)
                await asyncio.wait_for(rt.stop(), 30)
                await asyncio.wait_for(starting, 30)
            text = _everything(caplog)
            assert SENTINEL not in text and SENTINEL_PATH not in text
        finally:
            await _stopped(rt)


# --- R2: only a proven cleanup is retried --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_service_that_misses_its_first_ready_is_recovered_by_the_next_attempt(tmp_path, monkeypatch, caplog):
    """The real broker and service: the first attempt's service never answers ready; its cleanup proves it gone,
    the second attempt is ready, health says ready and an STL picture is made."""
    import trimesh

    caplog.set_level(logging.INFO, logger=LOGGER)
    _fast(monkeypatch, ready=8.0)  # a real service gets its ordinary wait; attempt 1 just spends it
    rt = r.PreviewRuntime(tmp_path / "preview")
    _fail_ready_on_attempt(rt, 1)
    try:
        await rt.start()
        assert not rt.ready and rt.health().state == "recovering"  # start() returned after the first attempt
        await _until(lambda: rt.ready, 60)
        assert rt.health().state == "ready" and rt.start_attempt == 2
        async with rt.attempt({"mesh": (trimesh.creation.box().export(file_type="stl"), "stl")}) as result:
            assert result.outcome == "ok"
        failures = _failures(caplog)
        assert len(failures) == 1 and "attempt=1/3" in failures[0] and "phase=service_ready" in failures[0]
        ready = [m for m in _messages(caplog) if m.startswith("Preview startup ready:")]
        assert len(ready) == 1 and "attempts=2" in ready[0]
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_spawn_unproven_before_the_service_is_assigned_is_final(tmp_path, monkeypatch, shared_broker):
    """Codex r1 probe, adapted to a real broker: an empty service field is no proof that nothing was spawned."""
    _fast(monkeypatch)
    spawns = []

    def lost_owner(*args, **kwargs):
        spawns.append(1)
        raise SpawnUnproven(12345)  # no real orphan is created by this test

    monkeypatch.setattr(r, "PreviewProcess", lost_owner)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    monkeypatch.setattr(r, "runtime", rt)
    monkeypatch.setattr(r, "_stopping", None)
    await rt.start()
    await asyncio.wait_for(asyncio.shield(rt.starter), 30)
    proven = await r.stop_preview_runtime()
    assert rt.uncertain and not proven, f"SpawnUnproven was treated as ended: uncertain={rt.uncertain}"
    assert spawns == [1] and rt.health().reason == "ownership_uncertain"  # never tried again
    assert rt.staging.exists()  # nothing the unproven tree may hold is removed


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "stop"])
async def test_spawn_unproven_that_ends_after_a_timeout_or_a_stop_is_still_final(
    ending, tmp_path, monkeypatch, shared_broker
):
    """The outer timeout or stop lands while the spawn is owned; the SpawnUnproven raised afterwards is recorded
    inside the spawn's own task, so neither hides it."""
    _fast(monkeypatch, budget=0.5 if ending == "timeout" else 30.0)
    entered, release, spawns = threading.Event(), threading.Event(), []

    def slow_lost_owner(*args, **kwargs):
        spawns.append(1)
        entered.set()
        assert release.wait(30)
        raise SpawnUnproven(12345)

    monkeypatch.setattr(r, "PreviewProcess", slow_lost_owner)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    starting = asyncio.create_task(rt.start())
    stopping = None
    try:
        await _until(entered.is_set)
        if ending == "stop":
            stopping = asyncio.create_task(rt.stop())
        await asyncio.sleep(0.8)
        assert not rt.starter.done()  # the spawn is still owned
        release.set()
        await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 30)
        if stopping is not None:
            await asyncio.wait_for(stopping, 30)
        await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 30)
        assert rt.uncertain and rt.health().reason == "ownership_uncertain" and spawns == [1]
    finally:
        release.set()
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_clean_failure_before_any_process_is_retried(tmp_path, monkeypatch):
    """Positive control: a spawn refused before any process exists is a proven end, so it is tried again."""
    _fast(monkeypatch, ready=8.0)
    calls = []

    def refuse_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the interpreter could not be started")
        return PreviewProcess(*args, **kwargs)

    monkeypatch.setattr(r, "PreviewProcess", refuse_once)
    rt = r.PreviewRuntime(tmp_path / "preview")
    try:
        await rt.start()
        await _until(lambda: rt.ready, 60)
        assert calls == [1, 1] and not rt.uncertain
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_service_whose_stop_cannot_be_proven_is_never_retried(tmp_path, monkeypatch, shared_broker):
    _fast(monkeypatch, ready=0.3)
    services = []

    def fake(*args, **kwargs):
        services.append(_FakeService(stop_error=PermissionError("the end is not proven")))
        return services[-1]

    monkeypatch.setattr(r, "PreviewProcess", fake)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    try:
        await rt.start()  # the fake never answers ready: the cleanup stops it, and that stop fails
        await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 30)
        assert rt.uncertain and len(services) == 1 and rt.health().reason == "ownership_uncertain"
    finally:
        for service in services:
            service.stop_error = None
        await _stopped(rt)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["recovery_required", "other"])
async def test_a_broker_that_fails_to_start_is_never_retried(error, tmp_path, monkeypatch):
    from embedded_nats import NatsServer, RecoveryRequired

    _fast(monkeypatch)
    starts = []

    def refuse(self):
        starts.append(1)
        if error == "recovery_required":
            raise RecoveryRequired("abandoned generation", reason="marker")
        raise OSError("the broker could not start")

    monkeypatch.setattr(NatsServer, "start", refuse)
    rt = r.PreviewRuntime(tmp_path / "preview")
    try:
        await rt.start()
        await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 30)
        assert starts == [1]
        assert rt.health().reason == ("recovery_required" if error == "recovery_required" else "startup_failed")
    finally:
        await _stopped(rt)


# --- R4: three attempts, one admission deadline, cleanup outside it -----------------------------------------------


@pytest.mark.asyncio
async def test_a_persistent_failure_ends_after_three_attempts_and_stays_visible(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    _fast(monkeypatch, pauses=(0.05, 0.1))
    rt = r.PreviewRuntime(tmp_path / "preview")
    launches = []

    async def launch(attempt=None):
        launches.append(attempt)
        raise PreviewError("unavailable")

    rt._launch = launch
    try:
        await rt.start()
        await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 30)
        await asyncio.sleep(0.4)  # longer than every pause: nothing more is tried
        assert launches == [1, 2, 3]
        assert rt.health().state == "unavailable" and rt.health().reason == "startup_failed"
        assert any("exhausted_attempts" in m for m in _failures(caplog))
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_deadline_spent_by_cleanup_admits_no_further_spawn(tmp_path, monkeypatch, caplog, shared_broker):
    """Cleanup counts against the admission deadline but is never cut by it: the cleanup ends, no spawn follows."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    _fast(monkeypatch, budget=0.3, ready=5.0)  # deadline: 3 x 0.3 + 0.1 = 1.0 s from the first attempt
    hold, services = threading.Event(), []

    def fake(*args, **kwargs):
        services.append(_FakeService(hold=hold))
        return services[-1]

    monkeypatch.setattr(r, "PreviewProcess", fake)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    threading.Timer(1.5, hold.set).start()  # the cleanup outlasts the whole admission deadline
    try:
        await rt.start()
        await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 30)
        assert len(services) == 1 and services[0].stopped  # the cleanup ended with its proof...
        assert rt.health().reason == "startup_failed"  # ...and no further spawn was admitted
        failures = _failures(caplog)  # one line, and it never announces a retry the deadline already forbids
        assert len(failures) == 1 and "next=exhausted_deadline" in failures[0], failures
    finally:
        hold.set()
        await _stopped(rt)


# --- R5: one owner, and a stop that waits for it ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stop_during_the_first_spawn_waits_for_it_and_leaves_no_monitor(tmp_path, monkeypatch, shared_broker):
    _fast(monkeypatch)
    entered, release, services = threading.Event(), threading.Event(), []

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        services.append(_FakeService())
        return services[-1]

    monkeypatch.setattr(r, "PreviewProcess", slow)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    starting = asyncio.create_task(rt.start())
    try:
        await _until(entered.is_set)
        stopping = asyncio.create_task(rt.stop())
        await asyncio.sleep(0.3)
        assert not stopping.done()  # the stop does not get ahead of the spawn it owns
        release.set()
        await asyncio.wait_for(stopping, 30)
        await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 30)
        assert services and services[0].stopped and rt.monitor is None and rt.closed and not rt.ready
    finally:
        release.set()
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_stop_during_a_pause_starts_no_further_attempt(tmp_path, monkeypatch):
    _fast(monkeypatch, pauses=(5.0, 5.0))
    rt = r.PreviewRuntime(tmp_path / "preview")
    launches = []

    async def launch(attempt=None):
        launches.append(attempt)
        raise PreviewError("unavailable")

    rt._launch = launch
    try:
        await rt.start()
        assert rt.health().state == "recovering"
        started = time.monotonic()
        await asyncio.wait_for(rt.stop(), 30)
        assert time.monotonic() - started < 2 and rt.starter.done()
        await asyncio.sleep(0.2)
        assert launches == [1]
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_two_stops_during_a_hung_cleanup_both_wait_for_it(tmp_path, monkeypatch, shared_broker):
    _fast(monkeypatch, ready=0.3)
    hold, services = threading.Event(), []

    def fake(*args, **kwargs):
        services.append(_FakeService(hold=hold))
        return services[-1]

    monkeypatch.setattr(r, "PreviewProcess", fake)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    starting = asyncio.create_task(rt.start())  # its first attempt's verdict waits for that attempt's cleanup
    try:
        await _until(lambda: services and services[0].stop_entered.is_set())
        first, second = asyncio.create_task(rt.stop()), asyncio.create_task(rt.stop())
        await asyncio.sleep(0.3)
        assert not first.done() and not second.done()  # a second cancel does not let go of the cleanup
        hold.set()
        await asyncio.wait_for(asyncio.gather(first, second), 30)
        await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 30)
        assert len(services) == 1 and services[0].stopped and not rt.uncertain
    finally:
        hold.set()
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_hung_cleanup_answers_a_restore_in_time_and_a_later_stop_proves_it(
    tmp_path, monkeypatch, shared_broker
):
    _fast(monkeypatch, ready=0.3)
    hold, services = threading.Event(), []

    def fake(*args, **kwargs):
        services.append(_FakeService(hold=hold))
        return services[-1]

    monkeypatch.setattr(r, "PreviewProcess", fake)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    monkeypatch.setattr(r, "runtime", rt)
    monkeypatch.setattr(r, "_stopping", None)
    starting = asyncio.create_task(rt.start())  # its first attempt's verdict waits for that attempt's cleanup
    try:
        await _until(lambda: services and services[0].stop_entered.is_set())
        started = time.monotonic()
        assert await r.stop_preview_runtime_within(0.2) is False
        assert await r.stop_preview_runtime_within(0.2) is False
        assert time.monotonic() - started < 3  # in time, though the cleanup still runs
        assert len([t for t in asyncio.all_tasks() if t.get_name() == "preview-runtime-stop"]) == 1
        assert r.runtime is rt and rt.staging.exists()
        hold.set()
        assert await asyncio.wait_for(r.stop_preview_runtime(), 30) is True
        assert r.runtime is None
        await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 30)
    finally:
        hold.set()
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_restore_during_recovery_stops_it_for_good(tmp_path, monkeypatch):
    from backend.app.services import library_preview, library_preview_jobs as jobs_module

    _fast(monkeypatch, pauses=(5.0, 5.0))
    rt = r.PreviewRuntime(tmp_path / "preview")
    launches = []

    async def launch(attempt=None):
        launches.append(attempt)
        raise PreviewError("unavailable")

    rt._launch = launch
    monkeypatch.setattr(r, "runtime", rt)
    monkeypatch.setattr(r, "_stopping", None)
    monkeypatch.setattr(library_preview, "publication", library_preview.PublicationGate())
    monkeypatch.setattr(jobs_module, "jobs", None)
    try:
        await rt.start()
        assert await asyncio.wait_for(jobs_module.quiesce_library_previews_for_restore(drain_seconds=5), 30) is True
        await asyncio.sleep(0.3)
        assert launches == [1] and r.runtime is None
    finally:
        await _stopped(rt)


# --- R3: the transport belongs to its attempt ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connect_that_never_answers_closes_its_client_on_every_attempt(tmp_path, monkeypatch):
    """Codex r1 probe, adapted to an explicit Client(): a real loopback peer that never sends INFO."""
    _fast(monkeypatch, budget=0.15)
    clients = _record_clients(monkeypatch)
    async with _SilentPeer() as peer:
        rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url=peer.url))
        try:
            await rt.start()
            await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 30)
            assert len(clients) == 3 and peer.writers, "the probe must reach an actual TCP connection"
            assert all(c.is_closed for c in clients), "a timed-out connect left its NATS client open"
        finally:
            for client in clients:
                await client.close()
            await _stopped(rt)


@pytest.mark.asyncio
async def test_a_stop_during_connect_closes_its_client(tmp_path, monkeypatch):
    _fast(monkeypatch, budget=30.0)
    clients = _record_clients(monkeypatch)
    async with _SilentPeer() as peer:
        rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url=peer.url))
        starting = asyncio.create_task(rt.start())
        try:
            await _until(lambda: rt.phase == "connect" and peer.writers)
            await asyncio.wait_for(rt.stop(), 30)
            await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 30)
            assert clients and all(c.is_closed for c in clients)
        finally:
            for client in clients:
                await client.close()
            await _stopped(rt)


@pytest.mark.asyncio
async def test_a_client_whose_close_fails_forbids_a_retry(tmp_path, monkeypatch):
    from nats.aio.client import Client

    _fast(monkeypatch, budget=0.5)  # a refused connect ends at the startup budget (see _closed_port_url)
    clients = _record_clients(monkeypatch)

    async def broken_close(self):
        raise RuntimeError("close failed")

    monkeypatch.setattr(Client, "close", broken_close)
    rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url=_closed_port_url()))
    try:
        await rt.start()
        await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 30)
        assert len(clients) == 1 and rt.health().reason == "startup_failed"  # never a second attempt
    finally:
        monkeypatch.undo()
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_late_close_callback_of_an_old_client_leaves_the_new_one_ready(tmp_path, monkeypatch, shared_broker):
    from nats.aio.client import Client

    _fast(monkeypatch, ready=8.0)
    callbacks, real_connect = [], Client.connect

    async def connect(self, *args, **kwargs):
        callbacks.append(kwargs.get("closed_cb"))
        return await real_connect(self, *args, **kwargs)

    monkeypatch.setattr(Client, "connect", connect)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    _fail_ready_on_attempt(rt, 1)
    try:
        await rt.start()
        await _until(lambda: rt.ready, 60)
        assert len(callbacks) == 2
        await callbacks[0]()  # attempt 1's client, closed long ago, reports late
        assert rt.ready and rt.health().state == "ready"
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_retry_sweeps_only_preview_buckets(tmp_path, monkeypatch, shared_broker):
    from nats.js.api import ObjectStoreConfig

    _fast(monkeypatch, ready=8.0)
    js = shared_broker.nc.jetstream()
    await js.create_object_store(bucket="foreign", config=ObjectStoreConfig(bucket="foreign", max_bytes=1024**2))
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    _fail_ready_on_attempt(rt, 1)  # after store_create: the second attempt sweeps the namespace again
    try:
        await rt.start()
        await _until(lambda: rt.ready, 60)
        names = sorted(stream.config.name for stream in await js.streams_info())
        assert names == sorted(["OBJ_foreign", f"OBJ_{rt.bucket}"])
    finally:
        await _stopped(rt)


# --- Codex implementation review r1: the start caller, the owner's first tick, a late wake, the terminal line ------


def _fake_transport(monkeypatch, *, entered=None, release=None) -> list:
    """Codex's fake Client: connect may be held on a barrier; nothing leaves the process."""
    import nats.aio.client as client_module

    clients = []

    class Client:
        is_connected = True

        def __init__(self):
            self.closed = False
            clients.append(self)

        async def connect(self, *args, **kwargs):
            if entered is not None:
                entered.set()
                await release.wait()

        def jetstream(self, **kwargs):
            return self

        async def streams_info(self):
            return []

        async def create_object_store(self, **kwargs):
            return object()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(client_module, "Client", Client)
    return clients


def _stopped_lines(caplog) -> list[str]:
    return [m for m in _messages(caplog) if m.startswith("Preview startup stopped:")]


@pytest.mark.asyncio
async def test_a_cancelled_start_caller_stops_the_owner_before_it_launches(tmp_path, monkeypatch, caplog):
    """FSR-I-R1: a lifespan whose startup is aborted cannot rely on its shutdown half; the cancelled caller ends
    the start through the runtime's one stop, owned, and only then hears its cancellation."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    entered, release = asyncio.Event(), asyncio.Event()
    clients = _fake_transport(monkeypatch, entered=entered, release=release)
    rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url="unused"))
    launches = []

    async def launch(attempt=None):
        launches.append(attempt)
        rt.ready, rt.reason = True, None

    rt._launch = launch
    starting = asyncio.create_task(rt.start())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 5)
        release.set()
        await asyncio.wait_for(asyncio.gather(rt.starter, return_exceptions=True), 5)
        assert launches == [] and rt.closed and not rt.ready and all(c.closed for c in clients)
        assert rt.monitor is None or rt.monitor.done()
        assert len(_stopped_lines(caplog)) == 1
    finally:
        release.set()
        await rt.stop()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["proven", "unproven"])
async def test_a_start_cancelled_during_its_spawn_ends_with_proof_or_keeps_the_uncertainty(
    ending, tmp_path, monkeypatch, shared_broker
):
    _fast(monkeypatch)
    entered, release, services = threading.Event(), threading.Event(), []

    def held_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        if ending == "unproven":
            raise SpawnUnproven(12345)
        services.append(_FakeService())
        return services[-1]

    monkeypatch.setattr(r, "PreviewProcess", held_spawn)
    rt = r.PreviewRuntime(tmp_path / "preview", shared_broker)
    starting = asyncio.create_task(rt.start())
    try:
        await _until(entered.is_set)
        starting.cancel()
        await asyncio.sleep(0.3)
        assert not starting.done()  # the cancelled caller waits for the owned spawn and the stop after it
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 30)
        assert rt.closed and rt.monitor is None and not rt.ready and rt.nc is None
        if ending == "proven":
            assert services[0].stopped and not rt.uncertain
        else:
            assert rt.uncertain and rt.health().reason == "ownership_uncertain"  # never lost on the way out
    finally:
        release.set()
        await _stopped(rt)
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_stop_before_the_owner_first_runs_ends_the_start(tmp_path, monkeypatch, caplog):
    """FSR-I-R2: a stop that lands before the owner ever ran must not leave start() waiting for a verdict."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    clients = _fake_transport(monkeypatch)
    prior = asyncio.all_tasks()
    rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url="unused"))
    starting = asyncio.create_task(rt.start())
    try:
        await asyncio.sleep(0)
        assert rt.starter is not None and rt.start_attempt == 0
        rt.starter.cancel()  # the owner is cancelled before it ever ran: none of its own code will say so
        await asyncio.wait_for(rt.stop(), 5)
        done, _ = await asyncio.wait({starting}, timeout=1)
        assert starting in done and not starting.cancelled()
        assert rt.closed and not clients and rt.service is None and rt.monitor is None
        assert len(_stopped_lines(caplog)) == 1 and "attempt=0/" in _stopped_lines(caplog)[0]
        assert not [t for t in asyncio.all_tasks() - prior if not t.done()]  # nothing left pending
    finally:
        pending = [t for t in asyncio.all_tasks() - prior if not t.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_late_wake_from_a_pause_admits_no_attempt_after_the_deadline(tmp_path, monkeypatch, caplog):
    """FSR-I-R3: the deadline is asked again when the pause ends — a suspended process or a busy loop that wakes
    late admits nothing. Only the runtime's admission clock moves; asyncio's own time stays real."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    _fake_transport(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(r, "time", SimpleNamespace(monotonic=lambda: clock[0], monotonic_ns=time.monotonic_ns))
    monkeypatch.setattr(r, "STARTUP_SECONDS", 1.0)
    monkeypatch.setattr(r, "STARTUP_PAUSES", (0.2, 0.2))
    rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url="unused"))
    launches = []

    async def launch(attempt=None):
        launches.append(attempt)
        if attempt == 1:
            raise PreviewError("unavailable")
        rt.ready, rt.reason = True, None

    rt._launch = launch
    try:
        await rt.start()  # attempt 1 failed; the owner sleeps in its pause
        assert launches == [1] and not rt.starter.done()
        clock[0] = 100.0  # the process was suspended past the 3.4 s admission deadline
        await asyncio.wait_for(asyncio.shield(rt.starter), 5)
        assert launches == [1] and rt.health().reason == "startup_failed"
        assert any("exhausted_deadline" in m for m in _failures(caplog))
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_a_stopped_start_ends_with_one_terminal_line(tmp_path, monkeypatch, caplog):
    """FSR-I-M1: a start stopped during connect says so once — generation, attempt, phase, elapsed — and claims
    no proven end for the cleanup its stop owns."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    entered, release = asyncio.Event(), asyncio.Event()
    _fake_transport(monkeypatch, entered=entered, release=release)
    rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url="unused"))
    starting = asyncio.create_task(rt.start())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await rt.stop()
        await starting
        lines = _stopped_lines(caplog)
        assert len(lines) == 1 and "attempt=1/3" in lines[0] and "phase=connect" in lines[0], lines
        assert "elapsed_ms=" in lines[0] and rt.generation in lines[0]
    finally:
        release.set()
        await rt.stop()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_start_that_ended_with_its_own_outcome_gets_no_second_terminal_line(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    _fast(monkeypatch, attempts=1, budget=0.5)  # a refused connect ends at the startup budget (see _closed_port_url)
    rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url=_closed_port_url()))
    try:
        await rt.start()
        await asyncio.gather(rt.starter, return_exceptions=True)
        await rt.stop()  # a later stop of a start that already said how it ended
        assert len(_failures(caplog)) == 1 and _stopped_lines(caplog) == []
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_every_stop_of_a_runtime_is_one_and_the_same(tmp_path, monkeypatch):
    """A cancelled start, the module's shared stop and any other caller join ONE stop: never a second teardown
    beside it (FSR-I-R1)."""
    _fast(monkeypatch, pauses=(5.0, 5.0))
    rt = r.PreviewRuntime(tmp_path / "preview")
    runs, real = [], rt._stop_once

    async def counted(**kwargs):
        runs.append(1)
        return await real(**kwargs)

    rt._stop_once = counted
    monkeypatch.setattr(r, "runtime", rt)
    monkeypatch.setattr(r, "_stopping", None)

    async def launch(attempt=None):
        raise PreviewError("unavailable")

    rt._launch = launch
    await rt.start()  # it now waits in its first pause
    _, _, proven = await asyncio.gather(rt.stop(), rt.stop(), r.stop_preview_runtime())
    assert proven is True
    assert runs == [1] and rt.closed


@pytest.mark.asyncio
async def test_a_stop_asked_from_inside_an_attempt_neither_waits_for_it_nor_cancels_it(tmp_path):
    import trimesh

    rt = r.PreviewRuntime(tmp_path / "preview")
    await rt.start()
    try:
        async with rt.attempt({"mesh": (trimesh.creation.box().export(file_type="stl"), "stl")}) as result:
            assert result.outcome == "ok"
            await asyncio.wait_for(rt.stop(), 30)  # the attempt's own task: spared, never awaited by its stop
        assert rt.closed and not rt.slot.locked()
    finally:
        await _stopped(rt)


@pytest.mark.asyncio
async def test_a_stop_that_wins_the_race_after_a_final_outcome_adds_no_second_terminal_line(
    tmp_path, monkeypatch, caplog
):
    """White-box: an external stop gets in between the owner's final failure line and its own give-up, so the
    shared stop cancels the owner. The outcome was already said; the cancelled owner says nothing more."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    _fast(monkeypatch, attempts=1, budget=0.5)  # a refused connect ends at the startup budget (see _closed_port_url)
    rt = r.PreviewRuntime(tmp_path, SimpleNamespace(token="t", url=_closed_port_url()))
    real = rt._log_start_failure

    def then_an_external_stop(*args):
        real(*args)
        if args[-1] == "final" or args[-1].startswith("exhausted"):
            rt._stop_task = asyncio.ensure_future(rt._stop_once(spare=None))  # the external stop got in first

    rt._log_start_failure = then_an_external_stop
    try:
        await rt.start()
        await asyncio.gather(rt.starter, return_exceptions=True)
        await rt.stop()
        assert rt.starter.cancelled()  # the race really happened
        assert len(_failures(caplog)) == 1 and _stopped_lines(caplog) == []
    finally:
        await _stopped(rt)
