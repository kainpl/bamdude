"""Integration tests for the server-side slicing flow (Phase 1.D).

Routes under test:
- ``POST /library/files/{id}/slice``  (returns 202 + job_id; bg task does the work)
- ``POST /archives/{id}/slice``        (same shape; result lands in archives table)
- ``GET /slice-jobs/{id}``             (poll for terminal state)

The synchronous validation paths (404 missing source, 400 wrong file type)
are tested directly. The bg-task paths poll until the job finishes and
then assert on the captured state.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections.abc import Callable

import httpx
import pytest
from httpx import AsyncClient

from backend.app.core.config import settings as app_settings
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.local_preset import LocalPreset
from backend.app.models.settings import Settings as SettingsModel
from backend.app.services import slicer_api as slicer_api_module


def _make_3mf_with_settings(settings_payload: dict | None = None) -> bytes:
    """Build a tiny in-memory 3MF zip with all the embedded-config files
    that real-world Bambu Studio / OrcaSlicer 3MFs ship with."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps(settings_payload or {"prime_tower_brim_width": "-1"}),
        )
        zf.writestr("Metadata/model_settings.config", "<config><object id='1'/></config>")
        zf.writestr(
            "Metadata/slice_info.config",
            "<config><plate><metadata key='filament' value='GFL00'/></plate></config>",
        )
    return buf.getvalue()


def _install_mock_sidecar(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Pin a MockTransport-backed httpx client onto the slicer_api singleton
    so per-request ``SlicerApiService`` instances reuse it instead of opening
    a real connection."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
    slicer_api_module.set_shared_http_client(client)
    return client


async def _wait_for_job(client: AsyncClient, job_id: int, timeout: float = 5.0) -> dict:
    """Poll ``GET /slice-jobs/{id}`` until the job hits a terminal state."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/api/v1/slice-jobs/{job_id}")
        if r.status_code != 200:
            raise AssertionError(f"slice-jobs poll failed: {r.status_code} {r.text}")
        body = r.json()
        if body["status"] in ("completed", "failed"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"slice job {job_id} did not finish in {timeout}s")


@pytest.fixture
async def slice_test_setup(db_session, tmp_path):
    """Source LibraryFile + 3 LocalPresets + ``preferred_slicer=orcaslicer``."""
    storage_dir = tmp_path / "library" / "files"
    storage_dir.mkdir(parents=True, exist_ok=True)
    src_path = storage_dir / "Cube.stl"
    src_path.write_bytes(b"solid Cube\nendsolid\n")

    original_base_dir = app_settings.base_dir
    original_archive_dir = app_settings.archive_dir
    app_settings.base_dir = tmp_path
    app_settings.archive_dir = tmp_path / "archive"
    app_settings.archive_dir.mkdir(parents=True, exist_ok=True)

    src_file = LibraryFile(
        filename="Cube.stl",
        file_path=str(src_path.relative_to(tmp_path)),
        file_type="stl",
        file_size=src_path.stat().st_size,
    )
    db_session.add(src_file)

    presets = {}
    for kind in ("printer", "process", "filament"):
        p = LocalPreset(
            name=f"Test {kind}",
            preset_type=kind,
            source="orcaslicer",
            setting=json.dumps({"name": f"Test {kind}", "type": kind}),
        )
        db_session.add(p)
        presets[kind] = p

    db_session.add(SettingsModel(key="preferred_slicer", value="orcaslicer"))
    await db_session.commit()

    for p in presets.values():
        await db_session.refresh(p)
    await db_session.refresh(src_file)

    yield {
        "src_file_id": src_file.id,
        "printer_id": presets["printer"].id,
        "process_id": presets["process"].id,
        "filament_id": presets["filament"].id,
        "tmp_path": tmp_path,
    }

    app_settings.base_dir = original_base_dir
    app_settings.archive_dir = original_archive_dir
    slicer_api_module.set_shared_http_client(None)


# ---------------------------------------------------------------------------
# POST /library/files/{id}/slice — synchronous validation paths
# ---------------------------------------------------------------------------


class TestSliceValidation:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_404_when_source_missing(self, async_client: AsyncClient, slice_test_setup):
        _install_mock_sidecar(lambda r: httpx.Response(200, content=b""))
        response = await async_client.post(
            "/api/v1/library/files/999999/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_400_for_wrong_file_type(self, async_client: AsyncClient, db_session, slice_test_setup):
        gcode_path = slice_test_setup["tmp_path"] / "library" / "files" / "out.gcode"
        gcode_path.write_bytes(b"; gcode\n")
        gfile = LibraryFile(
            filename="out.gcode",
            file_path=str(gcode_path.relative_to(slice_test_setup["tmp_path"])),
            file_type="gcode",
            file_size=10,
        )
        db_session.add(gfile)
        await db_session.commit()
        await db_session.refresh(gfile)

        _install_mock_sidecar(lambda r: httpx.Response(200, content=b""))
        response = await async_client.post(
            f"/api/v1/library/files/{gfile.id}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 400
        assert "STL, 3MF, or STEP" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /library/files/{id}/slice — async dispatch + bg job
# ---------------------------------------------------------------------------


class TestSliceLibraryFile:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_happy_path_returns_202_then_job_completes_with_library_file(
        self, async_client: AsyncClient, slice_test_setup
    ):
        # Sidecar emits a valid 3MF zip back so the parser can extract metadata.
        sliced_3mf = _make_3mf_with_settings()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                content=sliced_3mf,
                headers={
                    "x-print-time-seconds": "1234",
                    "x-filament-used-g": "12.5",
                    "x-filament-used-mm": "4567.8",
                },
            )

        _install_mock_sidecar(handler)

        response = await async_client.post(
            f"/api/v1/library/files/{slice_test_setup['src_file_id']}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 202
        body = response.json()
        assert "job_id" in body
        assert body["status"] in ("pending", "running")
        assert body["status_url"].endswith(f"/slice-jobs/{body['job_id']}")

        terminal = await _wait_for_job(async_client, body["job_id"])
        assert terminal["status"] == "completed", terminal
        result = terminal["result"]
        assert result["print_time_seconds"] == 1234
        assert result["filament_used_g"] == 12.5
        assert result["library_file_id"] is not None
        assert result["name"].endswith(".gcode.3mf")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sidecar_unavailable_marks_job_failed_with_502(self, async_client: AsyncClient, slice_test_setup):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        _install_mock_sidecar(handler)
        response = await async_client.post(
            f"/api/v1/library/files/{slice_test_setup['src_file_id']}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 202
        terminal = await _wait_for_job(async_client, response.json()["job_id"])
        assert terminal["status"] == "failed"
        assert terminal["error_status"] == 502

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_invalid_preset_id_marks_job_failed_with_400(self, async_client: AsyncClient, slice_test_setup):
        _install_mock_sidecar(lambda r: httpx.Response(200, content=b""))
        response = await async_client.post(
            f"/api/v1/library/files/{slice_test_setup['src_file_id']}/slice",
            json={
                "printer_preset_id": 999_999,  # missing
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 202
        terminal = await _wait_for_job(async_client, response.json()["job_id"])
        assert terminal["status"] == "failed"
        assert terminal["error_status"] == 400


# ---------------------------------------------------------------------------
# 3MF embedded-settings fallback when --load-settings 5xx
# ---------------------------------------------------------------------------


class TestEmbeddedSettingsFallback:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_3mf_5xx_retries_without_profiles_and_succeeds(
        self, async_client: AsyncClient, db_session, slice_test_setup
    ):
        """When the slicer-with-profiles call returns 5xx for a 3MF input,
        ``_run_slicer_with_fallback`` retries via ``slice_without_profiles``
        and surfaces the success — with ``used_embedded_settings: true``
        so the UI can flag the fallback."""
        # Replace the source with a real 3MF.
        src_path = slice_test_setup["tmp_path"] / "library" / "files" / "Cube.3mf"
        src_path.write_bytes(_make_3mf_with_settings())
        threemf = LibraryFile(
            filename="Cube.3mf",
            file_path=str(src_path.relative_to(slice_test_setup["tmp_path"])),
            file_type="3mf",
            file_size=src_path.stat().st_size,
        )
        db_session.add(threemf)
        await db_session.commit()
        await db_session.refresh(threemf)

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                # Since #2730 a slice is supervised by the progress poller, so
                # the sidecar sees GETs alongside the POST. Only slice attempts
                # are being counted here.
                return httpx.Response(404)
            call_count["n"] += 1
            body = request.content
            if call_count["n"] == 1:
                # First call carries profile parts → 5xx.
                assert b'name="printerProfile"' in body
                return httpx.Response(
                    status_code=500,
                    json={"message": "Failed to slice", "details": "bad config"},
                )
            # Second call is the fallback — no profile parts.
            assert b'name="printerProfile"' not in body
            return httpx.Response(
                status_code=200,
                content=_make_3mf_with_settings(),
                headers={
                    "x-print-time-seconds": "300",
                    "x-filament-used-g": "5.0",
                    "x-filament-used-mm": "1500",
                },
            )

        _install_mock_sidecar(handler)

        response = await async_client.post(
            f"/api/v1/library/files/{threemf.id}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 202
        terminal = await _wait_for_job(async_client, response.json()["job_id"])
        assert terminal["status"] == "completed"
        assert terminal["result"]["used_embedded_settings"] is True
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stl_5xx_no_fallback_marks_failed(self, async_client: AsyncClient, slice_test_setup):
        """STL inputs have no embedded settings to fall back to — a 5xx is
        terminal, not retryable."""
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                # Since #2730 a slice is supervised by the progress poller, so
                # the sidecar sees GETs alongside the POST. Only slice attempts
                # are being counted here.
                return httpx.Response(404)
            call_count["n"] += 1
            return httpx.Response(
                status_code=500,
                json={"message": "Failed to slice", "details": "STL bad"},
            )

        _install_mock_sidecar(handler)
        response = await async_client.post(
            f"/api/v1/library/files/{slice_test_setup['src_file_id']}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 202
        terminal = await _wait_for_job(async_client, response.json()["job_id"])
        assert terminal["status"] == "failed"
        assert terminal["error_status"] == 502
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_3mf_content_rejection_does_not_fall_back(
        self, async_client: AsyncClient, db_session, slice_test_setup
    ):
        """#1851: when the CLI ran and *rejected* the job for a content reason
        (marker present), the chosen presets were applied — falling back to the
        3MF's embedded settings would silently re-slice for the source's
        original printer. The job must fail (400) with the mined CLI reason and
        NOT retry via ``slice_without_profiles``."""
        src_path = slice_test_setup["tmp_path"] / "library" / "files" / "Reject.3mf"
        src_path.write_bytes(_make_3mf_with_settings())
        threemf = LibraryFile(
            filename="Reject.3mf",
            file_path=str(src_path.relative_to(slice_test_setup["tmp_path"])),
            file_type="3mf",
            file_size=src_path.stat().st_size,
        )
        db_session.add(threemf)
        await db_session.commit()
        await db_session.refresh(threemf)

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                # Since #2730 a slice is supervised by the progress poller, so
                # the sidecar sees GETs alongside the POST. Only slice attempts
                # are being counted here.
                return httpx.Response(404)
            call_count["n"] += 1
            return httpx.Response(
                status_code=500,
                json={
                    "message": (
                        "Slicing failed with error from slicer: The input preset file is invalid and can not be parsed."
                    ),
                    "details": (
                        "Slicer process failed (exit code 251)\n"
                        "stdout: [2026-06-29 04:12:12.175810] [error] run 3008: filament preset "
                        "Generic PLA @BBL H2C (slot 1) is not compatible with printer "
                        "Bambu Lab A1 0.4 nozzle.\n"
                        "run found error, return -5, exit..."
                    ),
                },
            )

        _install_mock_sidecar(handler)
        response = await async_client.post(
            f"/api/v1/library/files/{threemf.id}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 202
        terminal = await _wait_for_job(async_client, response.json()["job_id"])
        assert terminal["status"] == "failed"
        assert terminal["error_status"] == 400
        # The generic placeholder is replaced with the real CLI diagnostic.
        assert "not compatible with printer Bambu Lab A1" in terminal["error_detail"]
        # No embedded-settings fallback — one sidecar call only.
        assert call_count["n"] == 1


class TestSlicerRejectionMessage:
    """Unit coverage for ``_slicer_rejection_message`` — the CLI-rejection
    surfacing (#1851). Fed the ``str(SlicerApiServerError)`` shape our
    ``_format_sidecar_error`` produces."""

    @staticmethod
    def _reject(text: str):
        from backend.app.api.routes.library import _slicer_rejection_message

        return _slicer_rejection_message(text)

    def test_returns_none_without_marker(self):
        # No "Slicing failed with error from slicer:" marker → not a content
        # rejection, so the caller's embedded-settings fallback still fires.
        assert self._reject("") is None
        assert self._reject("Slicer sidecar unreachable: connection reset") is None
        assert self._reject("Slicer CLI failed (500): Failed to slice — bad config") is None

    def test_replaces_input_preset_invalid_placeholder_with_cli_error_line(self):
        # The CLI emits its catch-all "input preset file is invalid" placeholder
        # for every -5 exit, including real preset-vs-printer compatibility
        # rejections. The actual diagnostic only appears in the stdout
        # ``[error] run NNNN:`` line; the function must prefer that.
        text = (
            "Slicer CLI failed (500): Slicing failed with error from slicer: "
            "The input preset file is invalid and can not be parsed.: "
            "Slicer process failed (exit code 251)\n"
            "stdout: [2026-06-29 04:12:11.952784] [trace] Initializing StaticPrintConfigs\n"
            "[2026-06-29 04:12:12.175810] [error] run 3008: filament preset "
            "Generic PLA @BBL H2C (slot 1) is not compatible with printer "
            "Bambu Lab A1 0.4 nozzle.\n"
            "run found error, return -5, exit..."
        )
        assert self._reject(text) == (
            "filament preset Generic PLA @BBL H2C (slot 1) is not compatible with printer Bambu Lab A1 0.4 nozzle."
        )

    def test_replaces_placeholder_with_bamdude_dash_joined_shape(self):
        # BamDude's ``_format_sidecar_error`` joins the sidecar ``message`` /
        # ``details`` fields with " — ". The placeholder + junk still resolves
        # to the mined CLI diagnostic through the " — " separator.
        text = (
            "Slicer CLI failed (500): Slicing failed with error from slicer: "
            "The input preset file is invalid and can not be parsed. — "
            "Slicer process failed (exit code 251)\n"
            "stdout: [2026-06-29 04:12:12.175810] [error] run 3008: filament preset "
            "Generic PLA @BBL H2C (slot 1) is not compatible with printer "
            "Bambu Lab A1 0.4 nozzle.\n"
            "run found error, return -5, exit..."
        )
        assert self._reject(text) == (
            "filament preset Generic PLA @BBL H2C (slot 1) is not compatible with printer Bambu Lab A1 0.4 nozzle."
        )

    def test_keeps_meaningful_reason_even_when_cli_error_line_present(self):
        # When the headline error_string is already a useful reason (here the
        # bed-boundary rejection), don't override it with a generic ``[error]``
        # line that may just restate the same message. Avoids double-text.
        text = (
            "Slicer CLI failed (500): Slicing failed with error from slicer: "
            "Some objects are located over the boundary of the heated bed.: "
            "Slicer process failed (exit code 204)\n"
            "stdout: [error] some unrelated stdout chatter"
        )
        assert self._reject(text) == "Some objects are located over the boundary of the heated bed."

    def test_cli_error_line_without_run_prefix(self):
        # The CLI sometimes logs ``[error] <msg>`` without the ``run NNNN:``
        # prefix (different code paths). The regex must still pick it up.
        text = (
            "Slicer CLI failed (500): Slicing failed with error from slicer: "
            "The input preset file is invalid and can not be parsed.: "
            "Slicer process failed (exit code 251)\n"
            "stdout: [2026-06-29 12:00:00.000000] [error] Configuration parse failed: "
            "missing key 'printer_settings_id'"
        )
        assert self._reject(text) == "Configuration parse failed: missing key 'printer_settings_id'"


# ---------------------------------------------------------------------------
# POST /archives/{id}/slice
# ---------------------------------------------------------------------------


class TestSliceArchive:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_slice_creates_new_archive_row(self, async_client: AsyncClient, db_session, slice_test_setup):
        archive_dir = slice_test_setup["tmp_path"] / "archive" / "1" / "20260101_010101_Cube"
        archive_dir.mkdir(parents=True, exist_ok=True)
        src_3mf = archive_dir / "Cube.3mf"
        src_3mf.write_bytes(_make_3mf_with_settings())
        archive = PrintArchive(
            printer_id=1,
            filename="Cube.3mf",
            file_path=str(src_3mf.relative_to(slice_test_setup["tmp_path"])),
            file_size=src_3mf.stat().st_size,
            content_hash="x" * 64,
            print_name="Test Cube",
        )
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)

        sliced_3mf = _make_3mf_with_settings()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                content=sliced_3mf,
                headers={
                    "x-print-time-seconds": "777",
                    "x-filament-used-g": "9.0",
                    "x-filament-used-mm": "2000",
                },
            )

        _install_mock_sidecar(handler)
        response = await async_client.post(
            f"/api/v1/archives/{archive.id}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
            },
        )
        assert response.status_code == 202
        terminal = await _wait_for_job(async_client, response.json()["job_id"])
        assert terminal["status"] == "completed", terminal
        assert terminal["kind"] == "archive"
        result = terminal["result"]
        assert result["print_time_seconds"] == 777
        assert result["archive_id"] is not None
        assert "(re-sliced)" in result["name"]


# ---------------------------------------------------------------------------
# GET /slice-jobs/{id} — 404 for unknown ids
# ---------------------------------------------------------------------------


class TestSliceJobsPoll:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unknown_job_returns_404(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/slice-jobs/999999")
        assert response.status_code == 404
