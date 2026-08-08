"""A slice is bounded by silence, not by how long the model takes (#2730).

A heavy MakerWorld model — one Bambu Studio also takes a long time over — failed
after five minutes with **"Slicer sidecar unreachable"**. The sidecar was
reachable throughout and still slicing when we hung up on it. Two faults, fixed
in two passes:

1. ``httpx.ReadTimeout`` is a subclass of ``RequestError``, so expiry landed in
   the same handler as a refused connection and was *reported* as an unreachable
   sidecar. Shipped first, as :class:`SlicerTimeoutError`.
2. The limit itself was wall-clock, which cannot tell a slow model from a
   stalled one. The information to do better was already being collected: the
   progress poller runs once a second alongside the blocking POST. So the read
   timeout comes off the HTTP call entirely and the poller supervises instead.

Consequence worth stating, because it inverts what the first pass tested: with
no read timeout on the slice POST, the only ``TimeoutException`` that can still
reach us there is a **connect** timeout — and a sidecar that will not accept a
connection really is unreachable, so that is what it now says.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.app.services.slicer_api import (
    DEFAULT_SLICE_STALL_TIMEOUT_SECONDS,
    SlicerApiError,
    SlicerApiService,
    SlicerApiUnavailableError,
    SlicerTimeoutError,
    _Liveness,
    get_stall_timeout_seconds,
)

# Compressed so the suite does not actually wait out a stall window.
_TICK = 0.01
_WINDOW = 0.08


def _service(handler, *, window: float = _WINDOW) -> SlicerApiService:
    service = SlicerApiService(
        "http://slicer.lan:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        timeout_seconds=window,
    )
    service.progress_poll_interval = _TICK
    return service


def _slice_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"G1 X0 Y0")


async def _never_finishes(_request: httpx.Request) -> httpx.Response:
    # Long relative to the stall window but still bounded: if the watchdog ever
    # stops firing, the test should FAIL in a couple of seconds rather than hang
    # the suite. (It does stop firing if the changed-payload rule is dropped —
    # the poller then pings its own deadline forever.)
    await asyncio.sleep(_WINDOW * 60)
    return httpx.Response(200, content=b"G1 X0 Y0")


async def _slice(service: SlicerApiService, *, request_id: str | None = "req-1"):
    return await service.slice_without_profiles(model_bytes=b"solid x", model_filename="x.stl", request_id=request_id)


@pytest.mark.asyncio
class TestSilenceEndsTheWait:
    async def test_a_slicer_that_says_nothing_is_abandoned(self) -> None:
        """No progress endpoint at all: nothing to judge liveness by, so the
        window bounds total elapsed time — the old behaviour, kept honest."""

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                return httpx.Response(404)
            return await _never_finishes(request)

        with pytest.raises(SlicerTimeoutError) as excinfo:
            await _slice(_service(handler))

        assert "does not report progress" in str(excinfo.value)

    async def test_a_slicer_that_keeps_reporting_runs_to_completion(self) -> None:
        """The whole point: the slice takes several times the stall window and
        must not be cut off, because it was visibly working throughout."""
        ticks = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                ticks["n"] += 1
                return httpx.Response(200, json={"percent": ticks["n"]})
            await asyncio.sleep(_WINDOW * 4)
            return _slice_ok(request)

        result = await _slice(_service(handler))

        assert result.content == b"G1 X0 Y0"
        assert ticks["n"] > 1, "the poller must have been running for this to mean anything"

    async def test_a_repeated_payload_is_not_a_sign_of_life(self) -> None:
        """The sidecar re-serves its last snapshot on every poll. Counting a
        repeat as progress would leave the watchdog unable to detect a stall at
        all — it would be pinged by its own polling forever."""

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                return httpx.Response(200, json={"percent": 42})
            return await _never_finishes(request)

        with pytest.raises(SlicerTimeoutError) as excinfo:
            await _slice(_service(handler))

        # The message must name the case that actually happened: progress was
        # available and then stopped, not "this sidecar cannot report".
        assert "stopped reporting progress" in str(excinfo.value)

    async def test_progress_is_polled_even_when_nobody_asked_for_callbacks(self) -> None:
        """The poll is what makes stall detection possible, so it runs on the
        request_id alone. One GET a second is cheaper than a cancelled slice."""
        polled = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                polled["n"] += 1
                return httpx.Response(200, json={"percent": polled["n"]})
            await asyncio.sleep(_WINDOW * 2)
            return _slice_ok(request)

        # on_progress is None — the pre-#2730 code started no poller at all here.
        await _slice(_service(handler))

        assert polled["n"] > 0


@pytest.mark.asyncio
class TestTheHttpCallItself:
    async def test_the_slice_post_carries_no_read_timeout(self) -> None:
        """If a read timeout survived on this call it would still cap model
        complexity, and the watchdog above would never get to decide."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                return httpx.Response(404)
            seen.update(request.extensions.get("timeout") or {})
            return _slice_ok(request)

        await _slice(_service(handler))

        assert seen["read"] is None
        assert seen["write"] is None
        # Connect and pool keep short limits: a sidecar that will not accept a
        # connection is unreachable and should say so quickly.
        assert seen["connect"] == 30.0
        assert seen["pool"] == 30.0

    async def test_a_refused_connection_still_reads_as_unreachable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=None)

        with pytest.raises(SlicerApiUnavailableError) as excinfo:
            await _slice(_service(handler), request_id=None)

        assert "unreachable" in str(excinfo.value).lower()

    async def test_a_connect_timeout_now_reads_as_unreachable(self) -> None:
        """Inverted by this change, deliberately. With no read timeout left on
        the slice POST, a ``TimeoutException`` here can only mean the connection
        was never established — which is the unreachable case, not the slow one."""

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=None)

        with pytest.raises(SlicerApiUnavailableError):
            await _slice(_service(handler), request_id=None)


class TestTheLivenessClock:
    def test_the_window_is_floored_at_three_poll_intervals(self) -> None:
        """Liveness can only be observed as fast as the poller ticks, so a
        shorter window would expire between two polls and fail every slice
        instantly, however healthy."""
        assert _Liveness(0.001, poll_interval=1.0).window_seconds == 3.0

    def test_an_unsupported_progress_channel_measures_from_the_start(self) -> None:
        liveness = _Liveness(60.0, poll_interval=1.0)
        assert liveness.deadline == pytest.approx(liveness.started_at + 60.0)

    def test_a_supported_channel_measures_from_the_last_sign_of_life(self) -> None:
        liveness = _Liveness(60.0, poll_interval=1.0)
        liveness.saw_progress_endpoint()
        liveness.mark_alive()
        assert liveness.deadline > liveness.started_at + 60.0 - 1

    def test_both_messages_name_where_to_change_the_setting(self) -> None:
        """A timeout the user cannot act on is a bug report we then have to
        answer by hand."""
        silent = _Liveness(60.0)
        assert "Settings" in silent.timeout_message()
        silent.saw_progress_endpoint()
        assert "Settings" in silent.timeout_message()


@pytest.mark.asyncio
class TestTheSetting:
    class _FakeDb:
        pass

    async def _read(self, monkeypatch, value):
        async def fake_get_setting(_db, _key):
            if isinstance(value, Exception):
                raise value
            return value

        from backend.app.api.routes import settings as settings_routes

        monkeypatch.setattr(settings_routes, "get_setting", fake_get_setting)
        return await get_stall_timeout_seconds(self._FakeDb())

    async def test_a_configured_value_is_minutes(self, monkeypatch) -> None:
        assert await self._read(monkeypatch, "30") == 30 * 60.0

    async def test_an_unset_value_falls_back(self, monkeypatch) -> None:
        assert await self._read(monkeypatch, None) == DEFAULT_SLICE_STALL_TIMEOUT_SECONDS

    async def test_nonsense_falls_back_rather_than_failing_the_slice(self, monkeypatch) -> None:
        """A settings row nobody can explain must not be why a print does not
        happen."""
        assert await self._read(monkeypatch, "soon") == DEFAULT_SLICE_STALL_TIMEOUT_SECONDS

    async def test_zero_falls_back(self, monkeypatch) -> None:
        """Zero would floor to three poll intervals and fail every slice."""
        assert await self._read(monkeypatch, "0") == DEFAULT_SLICE_STALL_TIMEOUT_SECONDS

    async def test_a_broken_settings_read_falls_back(self, monkeypatch) -> None:
        assert await self._read(monkeypatch, RuntimeError("db gone")) == DEFAULT_SLICE_STALL_TIMEOUT_SECONDS


class TestTheContractsAroundIt:
    def test_the_new_error_is_still_a_slicer_error(self) -> None:
        """Callers with only a base handler keep catching it, rather than the
        change turning a handled failure into a 500."""
        assert issubclass(SlicerTimeoutError, SlicerApiError)
        assert not issubclass(SlicerTimeoutError, SlicerApiUnavailableError)

    def test_the_library_slice_route_handles_the_timeout_explicitly(self) -> None:
        """That route has no base ``SlicerApiError`` handler, so without its own
        branch the timeout would escape as a 500 with no message."""
        import inspect

        from backend.app.api.routes import library

        source = inspect.getsource(library)
        assert "except SlicerTimeoutError" in source

    def test_every_slice_path_reads_the_setting(self) -> None:
        """A construction site left on the default is a path where the user's
        number silently does not apply — and there are five of them."""
        import inspect

        from backend.app.api.routes import filament_calibration, library
        from backend.app.services import calibration_service

        for module in (library, filament_calibration, calibration_service):
            assert "get_stall_timeout_seconds" in inspect.getsource(module), module.__name__
