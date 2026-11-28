"""Optional per-printer WLED preset integration (N02, ported from upstream Bambuddy #3124).

A WLED controller beside a printer shows its state: BamDude switches the presets the operator
saved in WLED (``POST /json/state {"ps": id}``) and never sets colours, effects or segments.
One manager owns the HTTP calls, per-printer deduplication, the 30 s retry window and the
Finished timer; every send is a background task, so the status callback never waits on WLED.

Where BamDude differs from upstream (spec ``upstream-n02-wled-spec`` r2):

* **A1 — plate clear is the printer's own flag.** «Awaiting plate clear» needs the printer's
  *Require plate clear* AND the gate, as the dispatcher reads them; the manager keeps the flag per
  printer (``set_require_plate_clear``) where upstream keeps one global setting. The gate alone is
  not enough: an order's auto-eject mode and recovery arm it on a printer whose flag is off.
* **A2 — every fault the card counts lights the HMS state.** ``state.hms_errors`` already
  excludes muted faults; our card counts the rest. Muting a code on a printer quiets the card, the
  camera wall, notifications and the light together.
* **A3 — the deletion registry owns removal.** ``_configs``, ``_require_plate_clear`` and
  ``_runtime`` are in ``printer_deletion._ID_KEYED``; a committed deletion calls each dropped
  runtime's ``cleanup()``, which cancels its send and its Finished timer.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from backend.app.core.tasks import spawn_background_task
from backend.app.schemas.printer import WLEDConfig
from backend.app.services.bambu_mqtt import PrinterState

logger = logging.getLogger(__name__)

# While WLED does not answer, the same preset is retried at most this often
# instead of on every printer status update.
WLED_RETRY_SECONDS = 30.0


class WLEDResponseError(ValueError):
    """WLED returned JSON that does not match the expected API shape."""


def effective_wled_status(
    state: PrinterState, *, awaiting_plate_clear: bool = False, require_plate_clear: bool = False
) -> str | None:
    """Map BamDude's printer state to a WLED mapping key."""
    statuses = _applicable_wled_statuses(
        state, awaiting_plate_clear=awaiting_plate_clear, require_plate_clear=require_plate_clear
    )
    return statuses[0] if statuses else None


def _applicable_wled_statuses(
    state: PrinterState, *, awaiting_plate_clear: bool = False, require_plate_clear: bool = False
) -> list[str]:
    """Return applicable states in priority order for mapped-preset fallback."""
    if not state.connected:
        return ["offline"]

    printer_state = (state.state or "").upper()
    statuses = []
    if printer_state == "PAUSE" and state.stg_cur == 6:
        statuses.append("filament_problem")
    # A2: every fault the card counts (muted ones are already gone from ``hms_errors``).
    if state.hms_errors:
        statuses.append("hms_error")
    if require_plate_clear and awaiting_plate_clear and printer_state in {"IDLE", "FINISH", "FAILED"}:
        statuses.append("queue_waiting")
    if printer_state == "FAILED":
        statuses.append("error")
    elif printer_state == "PAUSE":
        statuses.append("paused")
    elif printer_state == "FINISH":
        statuses.append("finished")
    elif printer_state in {"RUNNING", "PRINTING"}:
        statuses.append("printing")
    elif printer_state in {"PREPARE", "SLICING"}:
        statuses.append("prepare")
    elif printer_state == "IDLE":
        statuses.append("idle")
    return statuses


@dataclass
class _WLEDRuntime:
    status: str | None = None
    preset_id: int | None = None
    target: tuple[str | None, int | None] | None = None
    generation: int = 0
    confirmed_generation: int | None = None
    finished_timeout_expired: bool = False
    # Monotonic time before which a failed send is not retried, and whether
    # the last automatic send failed (so an outage logs once, not per retry).
    retry_after: float = 0.0
    unreachable: bool = False
    send_task: asyncio.Task | None = None
    finished_task: asyncio.Task | None = None

    def cleanup(self) -> None:
        """Cancel this printer's pending send and Finished timer (A3).

        Upstream's ``_cancel_runtime`` body for one runtime: what the manager calls when it drops or
        replaces a runtime, and what the deletion registry calls on a committed deletion.
        """
        self.generation += 1
        for task in (self.send_task, self.finished_task):
            if task and not task.done():
                task.cancel()


class WLEDManager:
    """Own WLED HTTP calls, per-printer deduplication, and finished timers."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns_client = client is None
        # A1: the printer's own *Require plate clear*, kept per printer.
        self._require_plate_clear: dict[int, bool] = {}
        self.retry_seconds = WLED_RETRY_SECONDS
        self._configs: dict[int, WLEDConfig] = {}
        self._runtime: dict[int, _WLEDRuntime] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(3.0, connect=2.0),
                follow_redirects=False,
            )
        return self._client

    def configure_printer(self, printer_id: int, config: dict | WLEDConfig | None) -> None:
        self._cancel_runtime(printer_id, remove=True)
        try:
            self._configs[printer_id] = WLEDConfig.model_validate(config or {})
        except ValueError:
            self._configs[printer_id] = WLEDConfig()
            logger.warning("Ignoring invalid WLED configuration for printer %d", printer_id)

    def set_require_plate_clear(self, printer_id: int, value: bool) -> None:
        """The printer's own *Require plate clear* (A1). Like upstream's settings hook, nothing is re-applied:
        the next status push reads it."""
        self._require_plate_clear[printer_id] = bool(value)

    def remove_printer(self, printer_id: int) -> None:
        self._configs.pop(printer_id, None)
        self._require_plate_clear.pop(printer_id, None)
        self._cancel_runtime(printer_id, remove=True)

    def handle_status(
        self,
        printer_id: int,
        state: PrinterState,
        *,
        awaiting_plate_clear: bool = False,
    ) -> None:
        """Schedule any needed WLED work without delaying printer handling."""
        config = self._configs.get(printer_id)
        if not config or not config.enabled or not config.base_url:
            self._cancel_runtime(printer_id)
            return

        status = next(
            (
                candidate
                for candidate in _applicable_wled_statuses(
                    state,
                    awaiting_plate_clear=awaiting_plate_clear,
                    require_plate_clear=self._require_plate_clear.get(printer_id, False),
                )
                if getattr(config.presets, candidate) is not None
            ),
            None,
        )
        preset_id = getattr(config.presets, status, None) if status else None
        if state.connected and (state.state or "").upper() in {"PREPARE", "SLICING", "RUNNING", "PRINTING"}:
            self._runtime.setdefault(printer_id, _WLEDRuntime()).finished_timeout_expired = False
        self._apply_status(printer_id, config, status, preset_id)

    def handle_offline(self, printer_id: int) -> None:
        """Apply the offline mapping for an explicit printer lifecycle transition."""
        config = self._configs.get(printer_id)
        if not config or not config.enabled or not config.base_url:
            return
        preset_id = config.presets.offline
        self._apply_status(printer_id, config, "offline" if preset_id is not None else None, preset_id)

    def _apply_status(self, printer_id: int, config: WLEDConfig, status: str | None, preset_id: int | None) -> None:
        base_url = config.base_url
        if not base_url:
            return
        idle_preset_id = config.presets.idle
        runtime = self._runtime.setdefault(printer_id, _WLEDRuntime())
        expired_finished = runtime.finished_timeout_expired and status == "finished" and idle_preset_id is not None
        target = (status, preset_id)
        if runtime.target == target:
            if expired_finished:
                if runtime.finished_task is not None and not runtime.finished_task.done():
                    return
            elif (
                preset_id is None
                or runtime.confirmed_generation == runtime.generation
                or (runtime.send_task is not None and not runtime.send_task.done())
            ):
                return
            if time.monotonic() < runtime.retry_after:
                return
        else:
            runtime.generation += 1
            self._cancel_task(runtime.finished_task)
            runtime.finished_task = None
            self._cancel_task(runtime.send_task)
            runtime.target = target

        generation = runtime.generation
        if expired_finished and idle_preset_id is not None:
            if runtime.status == "idle" and runtime.preset_id == idle_preset_id:
                return
            runtime.finished_task = spawn_background_task(
                self._send_finished_idle(printer_id, generation, base_url, idle_preset_id),
                name=f"wled-finished-idle-{printer_id}",
            )
            return
        if preset_id is not None:
            runtime.send_task = spawn_background_task(
                self._send_status(printer_id, base_url, status, preset_id, generation),
                name=f"wled-preset-{printer_id}",
            )

        if (
            status == "finished"
            and config.finished_timeout_seconds
            and idle_preset_id is not None
            and runtime.finished_task is None
        ):
            runtime.finished_task = spawn_background_task(
                self._finish_timeout(
                    printer_id,
                    generation,
                    base_url,
                    idle_preset_id,
                    config.finished_timeout_seconds,
                ),
                name=f"wled-finished-timeout-{printer_id}",
            )

    async def _send_status(
        self, printer_id: int, base_url: str, status: str | None, preset_id: int, generation: int
    ) -> None:
        ok = await self._send_automatic(printer_id, base_url, preset_id, status)
        if ok:
            runtime = self._runtime.get(printer_id)
            if (
                runtime
                and runtime.generation == generation
                and runtime.target == (status, preset_id)
                and runtime.send_task is asyncio.current_task()
            ):
                runtime.status = status
                runtime.preset_id = preset_id
                runtime.confirmed_generation = generation

    async def _send_finished_idle(self, printer_id: int, generation: int, base_url: str, idle_preset_id: int) -> None:
        if await self._send_automatic(printer_id, base_url, idle_preset_id, "idle"):
            runtime = self._runtime.get(printer_id)
            if (
                runtime
                and runtime.generation == generation
                and runtime.finished_timeout_expired
                and runtime.target
                and runtime.target[0] == "finished"
            ):
                runtime.status = "idle"
                runtime.preset_id = idle_preset_id
                runtime.confirmed_generation = generation

    async def _send_automatic(self, printer_id: int, base_url: str, preset_id: int, status: str | None) -> bool:
        """Send a mapped preset; on failure hold retries back and log once per outage."""
        runtime = self._runtime.get(printer_id)
        quiet = runtime is not None and runtime.unreachable
        ok = await self.send_preset(printer_id, base_url, preset_id, status=status, quiet=quiet)
        if runtime is None or self._runtime.get(printer_id) is not runtime:
            return ok
        if ok:
            runtime.retry_after = 0.0
            if runtime.unreachable:
                runtime.unreachable = False
                logger.info("[WLED] Printer %d: WLED answers again", printer_id)
        else:
            runtime.retry_after = time.monotonic() + self.retry_seconds
            if not runtime.unreachable:
                runtime.unreachable = True
                logger.info(
                    "[WLED] Printer %d: retrying every %ds until WLED answers", printer_id, int(self.retry_seconds)
                )
        return ok

    async def test_preset(self, printer_id: int, base_url: str, preset_id: int) -> bool:
        """Test a preset while forcing the next status update to restore automatic control."""
        self._invalidate_automatic(printer_id)
        try:
            return await self.send_preset(printer_id, base_url, preset_id)
        finally:
            self._invalidate_automatic(printer_id)

    def _invalidate_automatic(self, printer_id: int) -> None:
        runtime = self._runtime.get(printer_id)
        if runtime:
            self._cancel_task(runtime.send_task)
            runtime.send_task = None
            runtime.status = None
            runtime.preset_id = None
            runtime.confirmed_generation = None

    async def send_preset(
        self, printer_id: int, base_url: str, preset_id: int, *, status: str | None = None, quiet: bool = False
    ) -> bool:
        """Activate a preset. Failures are isolated from all printer work.

        ``quiet`` logs a failure at debug level: a retry during an outage that
        was already reported.
        """
        log_failure = logger.debug if quiet else logger.warning
        try:
            response = await self.client.post(f"{base_url}/json/state", json={"ps": preset_id})
            response.raise_for_status()
            if not isinstance(response.json(), dict):
                raise WLEDResponseError("state response must be a JSON object")
            logger.info("[WLED] Printer %d state=%s -> preset=%d", printer_id, status or "manual", preset_id)
            return True
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            log_failure(
                "[WLED] Printer %d state=%s -> preset=%d failed (HTTP %d)",
                printer_id,
                status or "manual",
                preset_id,
                exc.response.status_code,
            )
        except (httpx.HTTPError, ValueError):
            log_failure("[WLED] Printer %d state=%s -> preset=%d failed", printer_id, status or "manual", preset_id)
        return False

    async def list_presets(self, base_url: str) -> list[dict[str, int | str]]:
        """Return WLED preset names and IDs without modifying WLED state."""
        response = await self.client.get(f"{base_url}/presets.json")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise WLEDResponseError("presets response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise WLEDResponseError("presets response must be a JSON object")

        presets = []
        for raw_id, value in payload.items():
            try:
                preset_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not 1 <= preset_id <= 250 or not isinstance(value, dict):
                continue
            name = value.get("n")
            presets.append(
                {"id": preset_id, "name": name.strip() if isinstance(name, str) and name.strip() else str(preset_id)}
            )
        return sorted(presets, key=lambda item: int(item["id"]))

    async def get_info(self, base_url: str) -> dict[str, str | None]:
        """Return the optional name and version reported by WLED."""
        response = await self.client.get(f"{base_url}/json/info")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise WLEDResponseError("info response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise WLEDResponseError("info response must be a JSON object")

        def clean(value) -> str | None:
            return value.strip() if isinstance(value, str) and value.strip() else None

        return {"name": clean(payload.get("name")), "version": clean(payload.get("ver"))}

    async def shutdown(self) -> None:
        tasks: list[asyncio.Task] = []
        for runtime in self._runtime.values():
            for task in (runtime.send_task, runtime.finished_task):
                if task and not task.done():
                    task.cancel()
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runtime.clear()
        self._configs.clear()
        self._require_plate_clear.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def _finish_timeout(
        self,
        printer_id: int,
        generation: int,
        base_url: str,
        idle_preset_id: int,
        timeout_seconds: int,
    ) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
            runtime = self._runtime.get(printer_id)
            if not runtime or runtime.generation != generation or not runtime.target or runtime.target[0] != "finished":
                return
            runtime.finished_timeout_expired = True
            await self._send_finished_idle(printer_id, generation, base_url, idle_preset_id)
        except asyncio.CancelledError:
            return

    def _cancel_runtime(self, printer_id: int, *, remove: bool = False) -> None:
        runtime = self._runtime.get(printer_id)
        if runtime:
            runtime.cleanup()
        if remove:
            self._runtime.pop(printer_id, None)

    @staticmethod
    def _cancel_task(task: asyncio.Task | None) -> None:
        if task and not task.done():
            task.cancel()


wled_manager = WLEDManager()
