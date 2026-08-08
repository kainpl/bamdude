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
import io
import logging
import zipfile
from collections.abc import Callable
from typing import NamedTuple

import httpx

logger = logging.getLogger(__name__)


class SlicerApiError(Exception):
    """Base error from the slicer API sidecar."""


class SlicerApiUnavailableError(SlicerApiError):
    """Sidecar is unreachable (connection error, no response)."""


class SlicerTimeoutError(SlicerApiError):
    """We gave up waiting on a slice that never finished.

    Kept apart from :class:`SlicerApiUnavailableError` because the two call for
    opposite reactions and used to be reported as the same thing: an
    ``httpx.ReadTimeout`` is a subclass of ``RequestError``, so a heavy model
    that simply took a long time surfaced as "Slicer sidecar unreachable" —
    sending the reporter of #2730 off to update a sidecar that was reachable the
    whole time and still slicing when we hung up on it.

    Only raised from the slice calls. On the health and profile probes, which
    carry short timeouts of their own, a read timeout genuinely does mean the
    sidecar is not answering.
    """


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


def _handle_slice_response(response: httpx.Response, *, export_3mf: bool) -> SliceResult:
    """Turn a sidecar ``/slice`` response into a validated ``SliceResult``.

    Shared by ``slice_with_profiles`` / ``slice_without_profiles``: the two had
    byte-identical status handling, and a check added to one of them would
    otherwise silently not apply to the other.

    Beyond the status code, this refuses **HTTP 200 carrying a body that is not
    a slice** (upstream #2671). A stock or misconfigured sidecar, a reverse
    proxy interstitial, a truncated response, or an OrcaSlicer/BambuStudio CLI
    that crashed without writing output all return 200 with a few bytes. Those
    bytes used to be written out as a ``.gcode.3mf``, stored as a valid sliced
    file — the 3MF parse failure was swallowed as "no thumbnail" — then queued
    and FTP'd, so the failure surfaced at the printer rather than at the slice.

    This is the same rule the rest of the pipeline already holds: ``archive.py``
    ZIP-validates a 3MF after copying it, and the virtual printer's ``cmd_STOR``
    ZIP-validates before ACKing an upload. The slice path was the one place that
    took bytes on trust.

    Raises:
        SlicerInputError: 4xx from the sidecar, including a proxy's 413.
        SlicerApiServerError: 5xx, or a 2xx whose body is not a valid 3MF.
    """
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
    if response.status_code == 413:
        # A 413 almost never comes from the slicer itself — it is a reverse
        # proxy capping the multipart upload (model + profiles). Name the layer
        # that actually has the setting, or the user tunes the wrong one.
        raise SlicerInputError(
            "The slice request was rejected as too large (HTTP 413). A reverse proxy "
            "in front of the slicer sidecar is capping the request body — raise "
            "'client_max_body_size' (nginx/SWAG) or the equivalent on the proxy that "
            "sits directly in front of the sidecar, then reload it. If the sidecar is "
            "behind Cloudflare, note its request-size cap."
        )
    if response.status_code >= 500:
        raise SlicerApiServerError(f"Slicer CLI failed ({response.status_code}): {_format_sidecar_error(response)}")
    if response.status_code >= 400:
        raise SlicerInputError(f"Slicer rejected input ({response.status_code}): {_format_sidecar_error(response)}")

    content = response.content
    if export_3mf and not zipfile.is_zipfile(io.BytesIO(content)):
        # Only a small body is worth quoting back; a large non-ZIP blob is
        # noise in a toast and is already in the log above when it was a 4xx.
        detail = _format_sidecar_error(response) if len(content) <= 500 else ""
        raise SlicerApiServerError(
            f"Slicer sidecar returned HTTP {response.status_code} but the body is not a valid "
            f"3MF ({len(content)} bytes). This usually means a misconfigured sidecar, an "
            f"OrcaSlicer/BambuStudio CLI crash producing no output, or a reverse proxy returning "
            f"an error page or truncating the response — verify the sidecar URL and any proxy in "
            f"front of it." + (f" Body: {detail}" if detail else "")
        )

    return SliceResult(
        content=content,
        print_time_seconds=_safe_int(response.headers.get("x-print-time-seconds")),
        filament_used_g=_safe_float(response.headers.get("x-filament-used-g")),
        filament_used_mm=_safe_float(response.headers.get("x-filament-used-mm")),
    )


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
        arrange: bool = False,
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
        if arrange:
            # Sidecar reads truthy strings as True; cross-nozzle-class re-slices
            # (#1493) need --arrange so BS repositions objects for the target
            # bed instead of inheriting the source printer's coordinate layout.
            data["arrange"] = "true"
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
        except httpx.TimeoutException as exc:
            # Distinguished from a refused connection: the slicer was there, we
            # stopped waiting. The bare-float timeout this service was built with
            # applies to connect, read, write and pool alike, so on one long
            # request it is a cap on how complex a model may be — not a health
            # check. Naming it honestly is the half of #2730 that does not need a
            # liveness poller; see the audit for the rest.
            raise SlicerTimeoutError(
                f"Slicing exceeded the {self.timeout_seconds:.0f}s limit. The slicer was still responding — "
                f"this is a time limit, not a connection problem."
            ) from exc
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        finally:
            if progress_task is not None:
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass  # Polling errors must not fail the slice.

        return _handle_slice_response(response, export_3mf=export_3mf)

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
        except httpx.TimeoutException as exc:
            # Distinguished from a refused connection: the slicer was there, we
            # stopped waiting. The bare-float timeout this service was built with
            # applies to connect, read, write and pool alike, so on one long
            # request it is a cap on how complex a model may be — not a health
            # check. Naming it honestly is the half of #2730 that does not need a
            # liveness poller; see the audit for the rest.
            raise SlicerTimeoutError(
                f"Slicing exceeded the {self.timeout_seconds:.0f}s limit. The slicer was still responding — "
                f"this is a time limit, not a connection problem."
            ) from exc
        except httpx.RequestError as exc:
            raise SlicerApiUnavailableError(f"Slicer sidecar unreachable: {exc}") from exc
        finally:
            if progress_task is not None:
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass

        return _handle_slice_response(response, export_3mf=export_3mf)


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
