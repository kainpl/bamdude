"""HTTP client for an OrcaSlicer / BambuStudio API sidecar (Phase 1 of
the 0.5.x slicer cycle).

BamDude stores user printer/process/filament profiles itself (cloud-synced
or locally imported), so the slice flow always sends the model file plus an
explicit JSON profile triplet to the sidecar's ``/slice`` endpoint. The
sidecar shape mirrors ``AFKFelix/orca-slicer-api`` (multipart upload,
``--load-settings`` under the hood, response body is raw G-code or 3MF
with metadata in the ``X-Print-Time-Seconds`` / ``X-Filament-Used-G`` /
``X-Filament-Used-Mm`` headers).

Pinned to BamDude's ``kainpl/orca-slicer-api`` fork on the
``bamdude/profile-resolver`` branch — patches OrcaSlicer / BambuStudio CLI
quirks the official ``AFKFelix/orca-slicer-api`` upstream hasn't merged
yet (inherits-chain resolver, sentinel-value strip, multi-filament input,
``--pipe`` live progress).
"""

import asyncio
import logging
from collections.abc import Callable
from typing import NamedTuple

import httpx

logger = logging.getLogger(__name__)


class SlicerApiError(Exception):
    """Base error from the slicer API sidecar."""


class SlicerApiUnavailableError(SlicerApiError):
    """Sidecar is unreachable (connection error, no response)."""


class SlicerApiServerError(SlicerApiError):
    """Sidecar responded with a 5xx — usually the wrapped slicer CLI exited
    non-zero (range-validation reject, segfault on complex models, etc.).
    Distinguished from :class:`SlicerApiUnavailableError` so the caller can
    decide whether to retry with a different request shape (e.g. a 3MF
    embedded-settings fallback)."""


class SlicerInputError(SlicerApiError):
    """Sidecar rejected the input as invalid (4xx)."""


class SliceResult(NamedTuple):
    """Result of a slice operation."""

    content: bytes
    print_time_seconds: int
    filament_used_g: float
    filament_used_mm: float


class BundleSummary(NamedTuple):
    """Sidecar's view of a stored Printer Preset Bundle (.bbscfg).

    Mirrors the JSON shape returned by ``/profiles/bundle(s)`` on the
    sidecar — ``printer``, ``process``, ``filament`` are each a list of
    preset names available within the bundle (without the ``.json``
    extension and without the BambuStudio "# " user-clone prefix; the
    sidecar accepts both forms when looking them up at slice time).
    """

    id: str
    printer_preset_name: str
    printer: list[str]
    process: list[str]
    filament: list[str]
    version: str | None


class BundleNotFoundError(SlicerApiError):
    """Sidecar returned 404 for the bundle id (deleted, never imported)."""


def _parse_bundle_summary(payload: dict) -> BundleSummary:
    """Build a BundleSummary from the sidecar's JSON. Tolerant of missing
    optional fields so a sidecar that adds keys later doesn't break parsing.
    """
    return BundleSummary(
        id=str(payload.get("id") or ""),
        printer_preset_name=str(payload.get("printer_preset_name") or ""),
        printer=list(payload.get("printer") or []),
        process=list(payload.get("process") or []),
        filament=list(payload.get("filament") or []),
        version=payload.get("version"),
    )


_shared_http_client: httpx.AsyncClient | None = None


def set_shared_http_client(client: httpx.AsyncClient | None) -> None:
    """Register an app-scoped client so per-request services can pool transport.

    Slicing uses a 300 s default timeout (vs cloud's 30 s) so a dedicated
    pool is wired in lifespan with the bigger budget. Per-request callers
    can also pass their own client; the shared one is just the default.
    """
    global _shared_http_client
    _shared_http_client = client


def _format_sidecar_error(response: httpx.Response) -> str:
    """Build a human-readable error string from a slicer-API 4xx/5xx response.

    Tries known JSON shapes in order, then falls back to a stripped
    text/HTML body. Limits to 500 chars so a CLI stderr dump can't blow up
    a notification toast.

    JSON shapes handled:

    - ``AppError`` middleware (our own): ``{"message": "...", "details": "..."}``.
      ``details`` carries the CLI stderr / ``error_string`` for slice
      failures and is the actual cause; ``message`` is the user-facing
      headline. Both are joined with " — " when present.
    - Express default 404 / generic: ``{"error": "..."}`` or ``{"detail": "..."}``.
    - Validator errors: ``{"errors": [...]}`` joined with "; ".

    Non-JSON shapes:

    - HTML (Express default ``Cannot POST /...`` page): tags stripped,
      whitespace collapsed.
    - Plain text: passed through.
    """
    try:
        payload = response.json()
    except Exception:
        return _strip_html(response.text)[:500] or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        message = (payload.get("message") or "").strip()
        details = (payload.get("details") or "").strip()
        if message and details:
            return f"{message} — {details}"[:500]
        if message or details:
            return (message or details)[:500]
        for key in ("error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts = [str(e).strip() for e in errors if str(e).strip()]
            if parts:
                return "; ".join(parts)[:500]
        return f"HTTP {response.status_code}"
    if isinstance(payload, list) and payload:
        return "; ".join(str(p) for p in payload)[:500]
    return str(payload)[:500] or f"HTTP {response.status_code}"


def _strip_html(text: str) -> str:
    """Crude HTML-to-text for the few Express default pages the sidecar emits.

    Express's missing-route handler returns an HTML page; passing that raw
    string into a UI toast looks broken. The pages are simple enough that
    a tag-strip + whitespace-collapse is sufficient — we don't pull in a
    real parser for an unhappy-path one-liner.
    """
    import re

    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def _guess_model_content_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".stl"):
        return "model/stl"
    if lower.endswith(".3mf") or lower.endswith(".gcode.3mf"):
        return "model/3mf"
    if lower.endswith(".step") or lower.endswith(".stp"):
        return "model/step"
    return "application/octet-stream"


class SlicerApiService:
    """Talks to an OrcaSlicer / BambuStudio API sidecar."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        if client is not None:
            self._client = client
            self._owns_client = False
        elif _shared_http_client is not None:
            self._client = _shared_http_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(timeout=timeout_seconds)
            self._owns_client = True

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "SlicerApiService":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def health(self) -> dict:
        """``GET /health`` — used to surface a clear "sidecar offline" error
        before accepting a slice request from the user."""
        try:
            response = await self._client.get(f"{self.base_url}/health", timeout=10.0)
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise SlicerApiUnavailableError(f"Slicer sidecar /health returned {response.status_code}")
        return response.json()

    async def list_bundled_profiles(self) -> dict:
        """``GET /profiles/bundled`` — return the slicer's stock profiles by slot.

        Powers the "Standard" tier of BamDude's SliceModal preset dropdowns.
        The sidecar walks the slicer's read-only ``resources/profiles/BBL/``
        tree and returns ``{printer, process, filament}`` arrays of
        ``{name, base_id}`` (alphabetised, instantiable presets only —
        abstract bases like ``fdm_filament_pla`` are filtered out by the
        sidecar).

        Returns an empty-shaped dict when the sidecar is unreachable so the
        unified-presets endpoint can degrade to "no standard tier" without
        crashing the modal — cloud + local-imported profiles still render.
        """
        try:
            response = await self._client.get(f"{self.base_url}/profiles/bundled", timeout=10.0)
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise SlicerApiUnavailableError(f"Slicer sidecar /profiles/bundled returned {response.status_code}")
        return response.json()

    async def import_bundle(
        self,
        zip_bytes: bytes,
        *,
        filename: str = "bundle.bbscfg",
    ) -> BundleSummary:
        """``POST /profiles/bundle`` — upload a BambuStudio Printer Preset Bundle.

        Idempotent on the sidecar side: re-uploading the same file yields the
        same id (deterministic SHA-256 prefix of the zip content) and the
        sidecar reuses its existing extracted directory, so re-importing is
        always safe.

        Raises:
            SlicerInputError: 4xx — bundle isn't a valid .bbscfg, or fails the
                sidecar's path-traversal / manifest validation.
            SlicerApiUnavailableError: connection error or 5xx.
        """
        files = {"file": (filename, zip_bytes, "application/zip")}
        try:
            response = await self._client.post(
                f"{self.base_url}/profiles/bundle",
                files=files,
                timeout=60.0,
            )
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        if response.status_code >= 500:
            raise SlicerApiServerError(
                f"Slicer API /profiles/bundle failed ({response.status_code}): {_format_sidecar_error(response)}",
            )
        if response.status_code >= 400:
            raise SlicerInputError(
                f"Slicer API rejected bundle ({response.status_code}): {_format_sidecar_error(response)}",
            )
        return _parse_bundle_summary(response.json())

    async def list_bundles(self) -> list[BundleSummary]:
        """``GET /profiles/bundles`` — list every imported bundle and its presets.

        Returns an empty list when the sidecar's bundle store is empty (the
        sidecar returns ``[]`` rather than 404 in that case). Network errors
        and 5xx surface as ``SlicerApiUnavailableError`` so callers can
        decide whether to render an empty UI or a "sidecar offline" banner.
        """
        try:
            response = await self._client.get(f"{self.base_url}/profiles/bundles", timeout=10.0)
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise SlicerApiUnavailableError(
                f"Slicer sidecar /profiles/bundles returned {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise SlicerApiServerError("Slicer sidecar returned non-array bundle list")
        return [_parse_bundle_summary(b) for b in payload if isinstance(b, dict)]

    async def get_bundle(self, bundle_id: str) -> BundleSummary:
        """``GET /profiles/bundles/<id>`` — single bundle summary.

        Raises:
            BundleNotFoundError: 404 — id does not exist on the sidecar.
            SlicerApiUnavailableError: connection error or 5xx.
        """
        try:
            response = await self._client.get(
                f"{self.base_url}/profiles/bundles/{bundle_id}",
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        if response.status_code == 404:
            raise BundleNotFoundError(f"Bundle {bundle_id!r} not found on sidecar")
        if response.status_code >= 400:
            raise SlicerApiUnavailableError(
                f"Slicer sidecar /profiles/bundles/{bundle_id} returned {response.status_code}",
            )
        return _parse_bundle_summary(response.json())

    async def delete_bundle(self, bundle_id: str) -> None:
        """``DELETE /profiles/bundles/<id>`` — remove a stored bundle."""
        try:
            response = await self._client.delete(
                f"{self.base_url}/profiles/bundles/{bundle_id}",
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        if response.status_code == 404:
            raise BundleNotFoundError(f"Bundle {bundle_id!r} not found on sidecar")
        if response.status_code >= 400:
            raise SlicerApiUnavailableError(
                f"Slicer sidecar DELETE /profiles/bundles/{bundle_id} returned {response.status_code}",
            )

    async def _poll_progress(
        self,
        request_id: str,
        on_progress: Callable[[dict], None],
    ) -> None:
        """Poll the sidecar's progress endpoint at ~1 Hz and forward each
        snapshot to ``on_progress``. Runs until cancelled.

        4xx is NOT treated as terminal: the FIRST poll fires the moment
        the slice POST is sent, which can be milliseconds before the
        request actually lands on the sidecar and ``progressStore.start()``
        runs — so a fresh request legitimately returns 404 for the first
        tick or two. Bailing on the first 404 (the original implementation)
        meant we'd quit before progress could ever arrive. The polling
        task is cancelled by the outer slice request anyway, so a
        sustained 404 (older sidecar without progress support, or post-
        slice grace expiry) just costs a few wasted GETs that the cancel
        will stop. Network errors and non-JSON 5xx are swallowed; the
        next tick retries.
        """
        url = f"{self.base_url}/slice/progress/{request_id}"
        while True:
            try:
                response = await self._client.get(url, timeout=5.0)
                if response.status_code == 200:
                    payload = response.json()
                    if isinstance(payload, dict):
                        on_progress(payload)
                # 404 / other 4xx = no progress available (yet, or ever
                # for older sidecars). Keep polling — the outer slice
                # request will cancel this task on completion.
            except (httpx.RequestError, ValueError):
                # ValueError covers JSONDecodeError when the sidecar
                # returns a non-JSON 5xx. Don't crash the poller.
                pass
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return

    async def slice_with_profiles(
        self,
        *,
        model_bytes: bytes,
        model_filename: str,
        printer_profile_json: str,
        process_profile_json: str,
        filament_profile_jsons: list[str],
        plate: int | None = None,
        export_3mf: bool = False,
        bed_type: str | None = None,
        request_id: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> SliceResult:
        """``POST /slice`` with model + printer/process/filament profiles.

        ``filament_profile_jsons`` is plate-slot-ordered: index 0 is the
        profile for slot 1, etc. Single-color callers pass a one-element
        list. Multiple ``filamentProfile`` parts are sent as a repeated
        form field — the sidecar's route declares ``maxCount: 16`` and the
        slicing service joins them as semicolon-separated
        ``--load-filaments`` for the OrcaSlicer / BambuStudio CLI.

        ``request_id``: when supplied, the sidecar wires ``--pipe`` to a
        per-request FIFO and publishes structured JSON progress events to
        its in-memory ProgressStore under this id. BamDude's slice
        dispatch polls ``GET /slice/progress/{request_id}`` in parallel
        to drive the live-progress toast.

        Raises:
            SlicerInputError: 4xx from sidecar (caller-supplied input is bad).
            SlicerApiServerError: 5xx from sidecar (slicer CLI failure).
            SlicerApiUnavailableError: connection error.
        """
        # httpx supports repeated multipart fields when ``files`` is a list of
        # tuples — using the dict form would silently overwrite duplicate
        # keys and ship only the last filament profile.
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("file", (model_filename, model_bytes, _guess_model_content_type(model_filename))),
            (
                "printerProfile",
                ("printer.json", printer_profile_json.encode("utf-8"), "application/json"),
            ),
            (
                "presetProfile",
                ("preset.json", process_profile_json.encode("utf-8"), "application/json"),
            ),
        ]
        for idx, fjson in enumerate(filament_profile_jsons):
            files.append(
                (
                    "filamentProfile",
                    (f"filament_{idx + 1}.json", fjson.encode("utf-8"), "application/json"),
                )
            )

        data: dict[str, str] = {}
        if plate is not None:
            data["plate"] = str(plate)
        if export_3mf:
            data["exportType"] = "3mf"
        if bed_type is not None:
            # Sidecar's ``SlicingSettings.bedType`` → ``--curr-bed-type`` CLI
            # arg. Empty string falls back to slicer-internal default, so we
            # only set the field when the caller passed a real value.
            data["bedType"] = bed_type
        if request_id is not None:
            data["requestId"] = request_id

        # When the caller supplied a request_id, kick off a parallel poller
        # that reads the sidecar's --pipe-fed progress endpoint and surfaces
        # structured updates via on_progress. Uses a short-tick poll (1 s)
        # since the slicer emits stage changes several times per minute on
        # complex models.
        progress_task: asyncio.Task | None = None
        if request_id is not None and on_progress is not None:
            progress_task = asyncio.create_task(
                self._poll_progress(request_id, on_progress),
                name=f"slicer-progress-{request_id}",
            )

        try:
            response = await self._client.post(
                f"{self.base_url}/slice",
                files=files,
                data=data,
                timeout=self.timeout_seconds,
            )
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        finally:
            if progress_task is not None:
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass  # Polling errors must not fail the slice.

        if response.status_code >= 400:
            # The toast-facing message is capped at 500 chars by
            # _format_sidecar_error, which buries the real CLI cause when
            # the slicer dumps a long stdout. Log the full body here so
            # the backend console always has the un-truncated failure.
            logger.error(
                "slicer sidecar %d body (full): %s",
                response.status_code,
                response.text[:8000],
            )
        if response.status_code >= 500:
            raise SlicerApiServerError(f"Slicer CLI failed ({response.status_code}): {_format_sidecar_error(response)}")
        if response.status_code >= 400:
            raise SlicerInputError(f"Slicer rejected input ({response.status_code}): {_format_sidecar_error(response)}")

        return SliceResult(
            content=response.content,
            print_time_seconds=_safe_int(response.headers.get("x-print-time-seconds")),
            filament_used_g=_safe_float(response.headers.get("x-filament-used-g")),
            filament_used_mm=_safe_float(response.headers.get("x-filament-used-mm")),
        )

    async def slice_with_bundle(
        self,
        *,
        model_bytes: bytes,
        model_filename: str,
        bundle_id: str,
        printer_name: str,
        process_name: str,
        filament_names: list[str],
        plate: int | None = None,
        export_3mf: bool = False,
        request_id: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> SliceResult:
        """``POST /slice`` with bundle id + per-category preset names.

        Asks the sidecar to materialize the printer / process / filament
        JSONs from a previously-imported `.bbscfg`, instead of accepting
        them as multipart attachments. Equivalent to
        :meth:`slice_with_profiles` from the user's perspective — same
        return shape, same 4xx/5xx semantics, same progress-poll wiring —
        but the sidecar saves the round-trip of re-uploading the JSONs
        every time a user kicks off a slice with the same bundle.

        ``filament_names`` is plate-slot-ordered: index 0 is slot 1, etc.
        Single-color callers pass a one-element list. The sidecar joins
        them as semicolon-separated `--load-filaments` for the CLI.

        Raises:
            SlicerInputError: 4xx — bundle / preset name not found, etc.
            SlicerApiServerError: sidecar 5xx (CLI failure on resolved
                triplet — same conditions that fail slice_with_profiles).
            SlicerApiUnavailableError: connection error.
        """
        files = {
            "file": (model_filename, model_bytes, _guess_model_content_type(model_filename)),
        }
        data: dict[str, str | list[str]] = {
            "bundle": bundle_id,
            "printerName": printer_name,
            "processName": process_name,
        }
        # The sidecar's SlicingSettings supports both `filamentName` (single
        # legacy field, kept for clients that pre-date multi-color) and
        # `filamentNames` (semicolon/comma-separated, matches multi-color
        # uploads). Always send the array form so a single-slot case still
        # ends up in the same code path on the sidecar.
        data["filamentNames"] = ";".join(filament_names)
        if plate is not None:
            data["plate"] = str(plate)
        if export_3mf:
            data["exportType"] = "3mf"
        if request_id is not None:
            data["requestId"] = request_id

        progress_task: asyncio.Task | None = None
        if request_id is not None and on_progress is not None:
            progress_task = asyncio.create_task(
                self._poll_progress(request_id, on_progress),
                name=f"slicer-progress-{request_id}",
            )

        try:
            response = await self._client.post(
                f"{self.base_url}/slice",
                files=files,
                data=data,
                timeout=self.timeout_seconds,
            )
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        finally:
            if progress_task is not None:
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass

        if response.status_code >= 400:
            # The toast-facing message is capped at 500 chars by
            # _format_sidecar_error, which buries the real CLI cause when
            # the slicer dumps a long stdout. Log the full body here so
            # the backend console always has the un-truncated failure.
            logger.error(
                "slicer sidecar %d body (full): %s",
                response.status_code,
                response.text[:8000],
            )
        if response.status_code >= 500:
            raise SlicerApiServerError(f"Slicer CLI failed ({response.status_code}): {_format_sidecar_error(response)}")
        if response.status_code >= 400:
            raise SlicerInputError(f"Slicer rejected input ({response.status_code}): {_format_sidecar_error(response)}")

        return SliceResult(
            content=response.content,
            print_time_seconds=_safe_int(response.headers.get("x-print-time-seconds")),
            filament_used_g=_safe_float(response.headers.get("x-filament-used-g")),
            filament_used_mm=_safe_float(response.headers.get("x-filament-used-mm")),
        )

    async def slice_without_profiles(
        self,
        *,
        model_bytes: bytes,
        model_filename: str,
        plate: int | None = None,
        export_3mf: bool = False,
        bed_type: str | None = None,
        request_id: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> SliceResult:
        """``POST /slice`` with only the model file and no profile triplet.

        For 3MF inputs this lets the slicer fall back on the file's embedded
        ``Metadata/project_settings.config``. Used as a fallback when
        :meth:`slice_with_profiles` triggers a CLI segfault or other 5xx —
        complex H2D / multi-extruder models hit upstream bugs in both the
        OrcaSlicer and BambuStudio CLIs when invoked via ``--load-settings``.

        Also used by the SliceModal's per-plate filament discovery path:
        for an unsliced project file we run a real preview slice via the
        sidecar to find which AMS slots the picked plate consumes. The
        ``request_id`` parameter routes the sidecar's --pipe progress
        events to the ProgressStore so the modal's inline spinner +
        toast can show "Generating G-code (75%)" for that preview as
        well.
        """
        files = {
            "file": (model_filename, model_bytes, _guess_model_content_type(model_filename)),
        }
        data: dict[str, str] = {}
        if plate is not None:
            data["plate"] = str(plate)
        if export_3mf:
            data["exportType"] = "3mf"
        if bed_type is not None:
            data["bedType"] = bed_type
        if request_id is not None:
            data["requestId"] = request_id

        progress_task: asyncio.Task | None = None
        if request_id is not None and on_progress is not None:
            progress_task = asyncio.create_task(
                self._poll_progress(request_id, on_progress),
                name=f"slicer-progress-{request_id}",
            )

        try:
            response = await self._client.post(
                f"{self.base_url}/slice",
                files=files,
                data=data,
                timeout=self.timeout_seconds,
            )
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        finally:
            if progress_task is not None:
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass

        if response.status_code >= 400:
            # The toast-facing message is capped at 500 chars by
            # _format_sidecar_error, which buries the real CLI cause when
            # the slicer dumps a long stdout. Log the full body here so
            # the backend console always has the un-truncated failure.
            logger.error(
                "slicer sidecar %d body (full): %s",
                response.status_code,
                response.text[:8000],
            )
        if response.status_code >= 500:
            raise SlicerApiServerError(f"Slicer CLI failed ({response.status_code}): {_format_sidecar_error(response)}")
        if response.status_code >= 400:
            raise SlicerInputError(f"Slicer rejected input ({response.status_code}): {_format_sidecar_error(response)}")

        return SliceResult(
            content=response.content,
            print_time_seconds=_safe_int(response.headers.get("x-print-time-seconds")),
            filament_used_g=_safe_float(response.headers.get("x-filament-used-g")),
            filament_used_mm=_safe_float(response.headers.get("x-filament-used-mm")),
        )


def _safe_int(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
