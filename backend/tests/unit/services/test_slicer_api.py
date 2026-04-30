"""Tests for SlicerApiService — Phase 1 of the 0.5.x slicer cycle.

Ported verbatim from upstream Bambuddy v0.2.4b1 (HTTP bridge module is
generic — no BamDude-specific assertions).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.app.services.slicer_api import (
    SlicerApiServerError,
    SlicerApiService,
    SlicerApiUnavailableError,
    SliceResult,
    SlicerInputError,
    _guess_model_content_type,
)


def _mock_client(handler) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient that routes every request through ``handler``.

    handler signature: ``(httpx.Request) -> httpx.Response``.
    """
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=10.0)


class TestGuessModelContentType:
    """The sidecar's multer middleware rejects octet-stream for STL uploads,
    so we guess by extension."""

    def test_stl(self):
        assert _guess_model_content_type("Cube.stl") == "model/stl"

    def test_3mf(self):
        assert _guess_model_content_type("Bank.3mf") == "model/3mf"

    def test_3mf_uppercase(self):
        assert _guess_model_content_type("Bank.3MF") == "model/3mf"

    def test_step(self):
        assert _guess_model_content_type("Cube.step") == "model/step"

    def test_stp(self):
        assert _guess_model_content_type("Cube.stp") == "model/step"

    def test_unknown(self):
        assert _guess_model_content_type("foo.bar") == "application/octet-stream"


class TestSliceWithProfiles:
    @pytest.mark.asyncio
    async def test_happy_path_returns_gcode_and_metadata(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body_len"] = len(request.content)
            captured["content_type"] = request.headers.get("content-type", "")
            return httpx.Response(
                status_code=200,
                content=b"; G-CODE START\nG28\n",
                headers={
                    "content-type": "application/octet-stream",
                    "x-print-time-seconds": "656",
                    "x-filament-used-g": "0.94",
                    "x-filament-used-mm": "302.5",
                },
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))

        result = await service.slice_with_profiles(
            model_bytes=b"solid Cube\n",
            model_filename="Cube.stl",
            printer_profile_json='{"name": "p"}',
            process_profile_json='{"name": "pr"}',
            filament_profile_jsons=['{"name": "f"}'],
        )

        assert isinstance(result, SliceResult)
        assert result.content == b"; G-CODE START\nG28\n"
        assert result.print_time_seconds == 656
        assert result.filament_used_g == 0.94
        assert result.filament_used_mm == 302.5
        assert captured["url"].endswith("/slice")
        assert captured["content_type"].startswith("multipart/form-data")
        assert captured["body_len"] > 0

    @pytest.mark.asyncio
    async def test_4xx_raises_slicer_input_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=400,
                json={"message": "Invalid file type for printerProfile."},
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerInputError) as exc_info:
            await service.slice_with_profiles(
                model_bytes=b"x",
                model_filename="Cube.stl",
                printer_profile_json="{}",
                process_profile_json="{}",
                filament_profile_jsons=["{}"],
            )
        assert "Invalid file type" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_5xx_raises_server_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=500,
                json={"message": "Failed to slice the model"},
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiServerError) as exc_info:
            await service.slice_with_profiles(
                model_bytes=b"x",
                model_filename="Cube.stl",
                printer_profile_json="{}",
                process_profile_json="{}",
                filament_profile_jsons=["{}"],
            )
        assert "Failed to slice the model" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_5xx_includes_sidecar_details_field(self):
        """Sidecar's AppError emits ``{message, details}`` — both must end up
        in the raised error so logs carry the actual CLI rejection reason
        instead of just the generic outer message."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=500,
                json={
                    "message": "Failed to slice the model",
                    "details": "prime_tower_brim_width: -1 not in range [0, 100]",
                },
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiServerError) as exc_info:
            await service.slice_with_profiles(
                model_bytes=b"x",
                model_filename="Cube.stl",
                printer_profile_json="{}",
                process_profile_json="{}",
                filament_profile_jsons=["{}"],
            )
        msg = str(exc_info.value)
        assert "Failed to slice the model" in msg
        assert "prime_tower_brim_width: -1" in msg

    @pytest.mark.asyncio
    async def test_5xx_with_only_details_still_surfaces(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=500,
                json={"details": "Slicer killed by SIGSEGV"},
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiServerError) as exc_info:
            await service.slice_with_profiles(
                model_bytes=b"x",
                model_filename="Cube.stl",
                printer_profile_json="{}",
                process_profile_json="{}",
                filament_profile_jsons=["{}"],
            )
        assert "SIGSEGV" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_5xx_with_non_json_body_falls_back_to_text(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=502, content=b"Bad Gateway")

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiServerError) as exc_info:
            await service.slice_with_profiles(
                model_bytes=b"x",
                model_filename="Cube.stl",
                printer_profile_json="{}",
                process_profile_json="{}",
                filament_profile_jsons=["{}"],
            )
        assert "Bad Gateway" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_connection_error_raises_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiUnavailableError) as exc_info:
            await service.slice_with_profiles(
                model_bytes=b"x",
                model_filename="Cube.stl",
                printer_profile_json="{}",
                process_profile_json="{}",
                filament_profile_jsons=["{}"],
            )
        assert "unreachable" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_passes_plate_and_export_3mf_options(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(
                status_code=200,
                content=b"3MF-BYTES",
                headers={
                    "x-print-time-seconds": "0",
                    "x-filament-used-g": "0",
                    "x-filament-used-mm": "0",
                },
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        await service.slice_with_profiles(
            model_bytes=b"x",
            model_filename="Cube.stl",
            printer_profile_json="{}",
            process_profile_json="{}",
            filament_profile_jsons=["{}"],
            plate=2,
            export_3mf=True,
        )

        body = captured["body"]
        assert b'name="plate"' in body
        assert b"\r\n2\r\n" in body or b'name="plate"\r\n\r\n2' in body
        assert b'name="exportType"' in body
        assert b"3mf" in body

    @pytest.mark.asyncio
    async def test_multi_filament_sends_one_part_per_profile(self):
        # Multi-color slicing requires N filament profiles, in plate-slot
        # order, sent as N repeated multipart `filamentProfile` parts (NOT a
        # single concatenated value). The CLI joins their resulting paths
        # with `;` for --load-filaments. A future regression to a dict-shaped
        # `files=` would silently keep prior tests green but ship only the
        # last filament — pin the wire shape.
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(
                status_code=200,
                content=b"3MF-BYTES",
                headers={
                    "x-print-time-seconds": "0",
                    "x-filament-used-g": "0",
                    "x-filament-used-mm": "0",
                },
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        await service.slice_with_profiles(
            model_bytes=b"x",
            model_filename="Cube.3mf",
            printer_profile_json="{}",
            process_profile_json="{}",
            filament_profile_jsons=['{"a":1}', '{"b":2}', '{"c":3}'],
        )

        body = captured["body"]
        assert body.count(b'name="filamentProfile"') == 3
        assert b'{"a":1}' in body and b'{"b":2}' in body and b'{"c":3}' in body
        assert body.index(b'{"a":1}') < body.index(b'{"b":2}') < body.index(b'{"c":3}')

    @pytest.mark.asyncio
    async def test_missing_metadata_headers_default_to_zero(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=200, content=b"; gcode")

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        result = await service.slice_with_profiles(
            model_bytes=b"x",
            model_filename="Cube.stl",
            printer_profile_json="{}",
            process_profile_json="{}",
            filament_profile_jsons=["{}"],
        )
        assert result.print_time_seconds == 0
        assert result.filament_used_g == 0.0
        assert result.filament_used_mm == 0.0


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"status": "healthy", "checks": {"orcaslicer": {"available": True}}},
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        body = await service.health()
        assert body["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_unreachable_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiUnavailableError):
            await service.health()


class TestSliceWithProfilesProgress:
    """Live-progress wiring for slice_with_profiles."""

    @pytest.mark.asyncio
    async def test_request_id_forwarded_as_form_field(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/slice":
                captured["body"] = request.content
                return httpx.Response(
                    status_code=200,
                    content=b"PK\x03\x04 fake",
                    headers={
                        "x-print-time-seconds": "1",
                        "x-filament-used-g": "0",
                        "x-filament-used-mm": "0",
                    },
                )
            return httpx.Response(status_code=404, json={"error": "not_found"})

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        await service.slice_with_profiles(
            model_bytes=b"x",
            model_filename="Cube.stl",
            printer_profile_json="{}",
            process_profile_json="{}",
            filament_profile_jsons=["{}"],
            request_id="abc-123",
            on_progress=lambda _snap: None,
        )
        body = captured["body"].decode("utf-8", errors="ignore")
        assert "requestId" in body
        assert "abc-123" in body

    @pytest.mark.asyncio
    async def test_progress_404_does_not_crash_or_stop_polling(self):
        """A 404 from /slice/progress/:id is expected during the early
        race window (POST fired before sidecar's progressStore.start()
        ran) and from older sidecars without progress support. Neither
        should crash the slice or block the response."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/slice":
                return httpx.Response(
                    status_code=200,
                    content=b"PK\x03\x04",
                    headers={
                        "x-print-time-seconds": "1",
                        "x-filament-used-g": "0",
                        "x-filament-used-mm": "0",
                    },
                )
            return httpx.Response(status_code=404, json={"error": "not_found"})

        snapshots: list[dict] = []
        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        result = await service.slice_with_profiles(
            model_bytes=b"x",
            model_filename="Cube.stl",
            printer_profile_json="{}",
            process_profile_json="{}",
            filament_profile_jsons=["{}"],
            request_id="legacy-sidecar",
            on_progress=lambda snap: snapshots.append(snap),
        )
        assert result is not None
        assert snapshots == []


class TestSliceWithoutProfilesEmbeddedSettings:
    """3MF embedded-settings fallback path — used when slice_with_profiles
    triggers a CLI segfault on complex H2D models."""

    @pytest.mark.asyncio
    async def test_minimal_request_no_profile_parts(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(
                status_code=200,
                content=b"; gcode-from-embedded",
                headers={
                    "x-print-time-seconds": "100",
                    "x-filament-used-g": "5",
                    "x-filament-used-mm": "1500",
                },
            )

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        result = await service.slice_without_profiles(
            model_bytes=b"PK\x03\x04",
            model_filename="model.3mf",
            plate=1,
            export_3mf=True,
        )
        assert result.print_time_seconds == 100
        body = captured["body"]
        # No printer/process/filament profile parts when falling back to
        # embedded settings.
        assert b'name="printerProfile"' not in body
        assert b'name="presetProfile"' not in body
        assert b'name="filamentProfile"' not in body
        # Plate + exportType still threaded through.
        assert b'name="plate"' in body
        assert b'name="exportType"' in body

    @pytest.mark.asyncio
    async def test_5xx_still_classified_as_server_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=500, json={"message": "embedded slice failed"})

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiServerError):
            await service.slice_without_profiles(model_bytes=b"PK\x03\x04", model_filename="model.3mf")

    @pytest.mark.asyncio
    async def test_connection_error_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns")

        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        with pytest.raises(SlicerApiUnavailableError):
            await service.slice_without_profiles(model_bytes=b"PK\x03\x04", model_filename="model.3mf")


class TestProgressPollerLoop:
    """Direct unit test for ``_poll_progress`` cancellation semantics —
    without the surrounding slice POST that normally drives it."""

    @pytest.mark.asyncio
    async def test_cancellation_returns_cleanly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"stage": "Generating G-code", "total_percent": 50},
            )

        snapshots: list[dict] = []
        service = SlicerApiService("http://sidecar:3000", client=_mock_client(handler))
        task = asyncio.create_task(service._poll_progress("rid", lambda s: snapshots.append(s)))
        # Give the loop time to make at least one call.
        await asyncio.sleep(0.05)
        task.cancel()
        # Must complete cleanly, not raise CancelledError out.
        await asyncio.gather(task, return_exceptions=True)
        assert snapshots, "poller never forwarded a snapshot"
        assert snapshots[0]["stage"] == "Generating G-code"
