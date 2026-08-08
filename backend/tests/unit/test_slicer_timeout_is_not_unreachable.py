"""A slow slice is not an unreachable sidecar (upstream #2730).

A heavy MakerWorld model — one Bambu Studio also takes a long time over — failed
after five minutes with **"Slicer sidecar unreachable"**. The sidecar was
reachable throughout and still slicing when we hung up on it.

``httpx.ReadTimeout`` is a subclass of ``RequestError``, so expiry landed in the
same handler as a refused connection. The reporter went and updated a sidecar
that was fine.

The timeout is also a bare float, which httpx applies to connect, read, write and
pool alike — so on one long request it is a cap on how complex a model may be,
not a health check. Naming it honestly is the half of this that does not need a
liveness poller; the rest is recorded as a follow-up in the audit.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.services.slicer_api import (
    SlicerApiError,
    SlicerApiService,
    SlicerApiUnavailableError,
    SlicerTimeoutError,
)


def _service_raising(exc: Exception) -> SlicerApiService:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return SlicerApiService(
        "http://slicer.lan:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        timeout_seconds=300.0,
    )


async def _slice(service: SlicerApiService):
    return await service.slice_without_profiles(model_bytes=b"solid x", model_filename="x.stl")


@pytest.mark.asyncio
class TestTimeoutIsNotUnreachable:
    async def test_a_read_timeout_is_reported_as_a_time_limit(self) -> None:
        """The reported case: five minutes of real slicing, then a message about
        a connection that was never broken."""
        service = _service_raising(httpx.ReadTimeout("timed out", request=None))

        with pytest.raises(SlicerTimeoutError) as excinfo:
            await _slice(service)

        message = str(excinfo.value).lower()
        assert "unreachable" not in message
        assert "time limit" in message
        assert "300" in str(excinfo.value), "the limit that was hit should be named"

    async def test_a_refused_connection_still_reads_as_unreachable(self) -> None:
        service = _service_raising(httpx.ConnectError("connection refused", request=None))

        with pytest.raises(SlicerApiUnavailableError) as excinfo:
            await _slice(service)

        assert "unreachable" in str(excinfo.value).lower()

    async def test_a_connect_timeout_counts_as_a_timeout_too(self) -> None:
        """``ConnectTimeout`` is also a ``TimeoutException``. Calling it a time
        limit is still truer than calling it unreachable — we did stop waiting."""
        service = _service_raising(httpx.ConnectTimeout("timed out", request=None))

        with pytest.raises(SlicerTimeoutError):
            await _slice(service)

    async def test_the_new_error_is_still_a_slicer_error(self) -> None:
        """Callers with only a base handler keep catching it, rather than the
        change turning a handled failure into a 500."""
        assert issubclass(SlicerTimeoutError, SlicerApiError)
        assert not issubclass(SlicerTimeoutError, SlicerApiUnavailableError)


class TestTheRoutesDistinguishThem:
    def test_the_library_slice_route_handles_the_timeout_explicitly(self) -> None:
        """That route has no base ``SlicerApiError`` handler, so without its own
        branch the timeout would escape as a 500 with no message."""
        import inspect

        from backend.app.api.routes import library

        source = inspect.getsource(library)
        assert "except SlicerTimeoutError" in source
