"""Virtual Printer Manager - coordinates SSDP, MQTT, and FTP services.

Each virtual printer runs its own independent services (FTP, MQTT, SSDP, Bind)
bound to its dedicated IP address, regardless of mode.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from backend.app.core.config import settings as app_settings
from backend.app.services.virtual_printer.bind_server import BindServer
from backend.app.services.virtual_printer.certificate import CertificateService
from backend.app.services.virtual_printer.ftp_server import VirtualPrinterFTPServer, compute_passive_port_slice
from backend.app.services.virtual_printer.mqtt_bridge import MQTTBridge
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer
from backend.app.services.virtual_printer.ssdp_server import SSDPProxy, VirtualPrinterSSDPServer
from backend.app.services.virtual_printer.tcp_proxy import SlicerProxyManager, TCPProxy

if TYPE_CHECKING:
    from backend.app.services.printer_manager import PrinterManager

logger = logging.getLogger(__name__)


# Mapping of SSDP model codes to display names
# These are the codes that slicers expect during discovery
# Sources:
#   - https://gist.github.com/Alex-Schaefer/72a9e2491a42da2ef99fb87601955cc3
#   - https://github.com/psychoticbeef/BambuLabOrcaSlicerDiscovery
VIRTUAL_PRINTER_MODELS = {
    # X1 Series
    "BL-P001": "X1C",  # X1 Carbon
    "BL-P002": "X1",  # X1
    "C13": "X1E",  # X1E
    # X2 Series
    "N6": "X2D",  # X2D
    # A2 Series (single-FDM + integrated cutter/plotter)
    "N9": "A2L",  # A2L
    # P Series
    "C11": "P1P",  # P1P
    "C12": "P1S",  # P1S
    "N7": "P2S",  # P2S
    # A1 Series
    "N2S": "A1",  # A1
    "N1": "A1 Mini",  # A1 Mini
    # H2 Series
    "O1D": "H2D",  # H2D
    # H2D Pro (upstream Bambuddy v0.2.4.5, experimental — codes transcribed from
    # the model-codes reference, not yet confirmed against a live H2D Pro SSDP
    # response). Distinct code from H2D's O1D so BambuStudio tells them apart.
    "O1E": "H2D Pro",
    "O2D": "H2D Pro",
    "O1C": "H2C",  # H2C
    "O1C2": "H2C",  # H2C (dual nozzle variant)
    "O1S": "H2S",  # H2S
}

# Serial number prefixes for each model (based on Bambu Lab serial number format)
# Format: MMM??RYMDDUUUUU (15 chars total)
#   MMM = Model prefix (3 chars)
#   ?? = Unknown/revision code (2 chars)
#   R = Revision letter (1 char)
#   Y = Year digit (1 char)
#   M = Month (1 char, hex: 1-9, A=Oct, B=Nov, C=Dec)
#   DD = Day (2 chars)
#   UUUUU = Unit number (5 chars)
MODEL_SERIAL_PREFIXES = {
    # X1 Series
    "BL-P001": "00M00A",  # X1C
    "BL-P002": "00M00A",  # X1
    "C13": "03W00A",  # X1E
    # X2 Series
    "N6": "20P90A",  # X2D (first 4 chars "20P9" match real serials)
    # A2 Series
    "N9": "26A19A",  # A2L (first 5 chars "26A19" match real serials)
    # P Series
    "C11": "01S00A",  # P1P
    "C12": "01P00A",  # P1S
    "N7": "22E00A",  # P2S
    # A1 Series
    "N2S": "03900A",  # A1
    "N1": "03000A",  # A1 Mini
    # H2 Series
    "O1D": "09400A",  # H2D
    "O1E": "09400A",  # H2D Pro (same prefix family as H2D)
    "O2D": "09400A",  # H2D Pro
    "O1C": "09400A",  # H2C
    "O1C2": "09400A",  # H2C (dual nozzle variant)
    "O1S": "09400A",  # H2S
}

# Reverse mapping: display name → SSDP model code (for auto-inheriting from printer model)
DISPLAY_NAME_TO_MODEL_CODE = {v: k for k, v in VIRTUAL_PRINTER_MODELS.items()}

# Default model
DEFAULT_VIRTUAL_PRINTER_MODEL = "BL-P001"  # X1C

# Bound on the per-instance ``_slicer_print_options`` cache. Each print_queue
# ``project_file`` command stashes one entry keyed by filename; the queue path
# pops it on consume, but a project_file whose file is never uploaded/queued
# (slicer cancels, filename mismatch) would linger forever. FIFO-evict the
# oldest when over the cap so the dict stays bounded (upstream v0.2.4.5).
_SLICER_OPTIONS_CACHE_LIMIT = 128

# How long ``_add_to_print_queue`` waits for the slicer's MQTT ``project_file``
# after the FTP upload completes (#1780 round 3). Bambu Studio sends FTP first,
# then the MQTT command immediately after — but on wireless / loaded-Pi setups
# the MQTT command can land 2+ s after FTP, which used to time the wait out and
# silently drop ``nozzle_mapping`` + the other slicer-driven flags. The bumped
# window covers the observed worst case in the field; the late-MQTT fallback in
# ``on_print_command`` covers the rest.
_SLICER_OPTIONS_WAIT_TIMEOUT = 5.0

# How long ``on_print_command`` will retroactively stamp slicer fields onto a
# recently-committed queue item when the MQTT print command arrives after
# ``_SLICER_OPTIONS_WAIT_TIMEOUT`` expired. Covers extra-late MQTT (slow
# wireless slicer, NIC drop+retry) and the scheduler tick interval before
# dispatch picks the item up.
_RECENT_QUEUE_ITEM_TTL = 30.0


def _get_serial_for_model(model: str, serial_suffix: str) -> str:
    """Get serial number for the given model and suffix."""
    prefix = MODEL_SERIAL_PREFIXES.get(model, "00M09A")
    return f"{prefix}{serial_suffix}"


def _coerce_flag(v: object) -> bool:
    """Coerce a print-option value to a bool.

    Handles the slicer's bool / int (0/1) shapes AND the tri-state calibration
    string that now rides in saved preferences (``'on'`` → True, ``'off'`` /
    ``'auto'`` → False). Critically NOT ``bool(v)`` — ``bool('off')`` is True.
    The VP has no ``*_mode`` column, so ``'auto'`` degrades to its bool mirror
    (secondary-path behaviour per the SAFE spec §3.5).
    """
    if isinstance(v, str):
        return v.strip().lower() == "on"
    return bool(v)


def _resolve_print_option(
    slicer_opts: dict | None,
    system_opts: dict | None,
    slicer_key: str,
    pref_key: str,
    column_default: bool,
) -> bool:
    """Resolve one queue-item print-option flag for a slicer→VP submission.

    Precedence (upstream Bambuddy #1403 + #1235):
        slicer-sent value → system fallback row → model column default.

    MQTT field naming differs from our column / preference names — the slicer
    sends ``bed_leveling`` (single L) while we store ``bed_levelling`` (double
    L). Slicer values arrive as bool/int; a saved calibration preference now
    arrives as a tri-state string — ``_coerce_flag`` handles both (a plain
    ``bool()`` would read the string ``'off'`` as True).
    """
    if slicer_opts is not None and slicer_key in slicer_opts:
        return _coerce_flag(slicer_opts[slicer_key])
    if system_opts is not None and pref_key in system_opts:
        return _coerce_flag(system_opts[pref_key])
    return column_default


class VirtualPrinterInstance:
    """Per-printer state and file handling logic.

    Each instance represents one virtual printer with its own config,
    upload directory, certificates, and file handling mode.
    """

    def __init__(
        self,
        *,
        vp_id: int,
        name: str,
        mode: str,
        model: str,
        access_code: str,
        serial_suffix: str,
        target_printer_ip: str = "",
        target_printer_serial: str = "",
        target_printer_id: int | None = None,
        target_folder_id: int | None = None,
        auto_dispatch: bool = True,
        queue_force_color_match: bool = False,
        gcode_injection: bool = False,
        save_ams_mapping: bool = False,
        bind_ip: str = "",
        remote_interface_ip: str = "",
        tailscale_disabled: bool = True,
        base_dir: Path,
        session_factory: Callable | None = None,
        printer_manager: "PrinterManager | None" = None,
    ):
        self.id = vp_id
        self.name = name
        self.mode = mode
        self.model = model
        self.access_code = access_code
        self.serial_suffix = serial_suffix
        self.target_printer_ip = target_printer_ip
        self.target_printer_serial = target_printer_serial
        self.target_printer_id = target_printer_id
        # Library folder where incoming files land. None = library root.
        # Forwarded to ``_save_to_library`` for every file the VP receives.
        self.target_folder_id = target_folder_id
        self.auto_dispatch = auto_dispatch
        self.queue_force_color_match = queue_force_color_match
        self.gcode_injection = gcode_injection
        self.save_ams_mapping = save_ams_mapping
        self.bind_ip = bind_ip
        self.remote_interface_ip = remote_interface_ip
        self.tailscale_disabled = tailscale_disabled
        self._session_factory = session_factory
        self._printer_manager = printer_manager

        # Directories
        self.upload_dir = base_dir / "uploads" / str(vp_id)
        self.cert_dir = base_dir / "certs" / str(vp_id)
        shared_ca_dir = base_dir / "certs"

        # Ensure directories exist
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.upload_dir / "cache").mkdir(exist_ok=True)
        self.cert_dir.mkdir(parents=True, exist_ok=True)

        # Certificate service (shared CA, per-instance printer cert)
        self._cert_service = CertificateService(
            cert_dir=self.cert_dir,
            serial=self.serial,
            shared_ca_dir=shared_ca_dir,
        )

        # Pending files for MQTT correlation
        self._pending_files: dict[str, Path] = {}

        # Slicer-side print options captured from the MQTT ``project_file``
        # command, keyed by filename. Used by ``_add_to_print_queue`` so the
        # queue item inherits the user's slicer-chosen timelapse /
        # bed_leveling / flow_cali / layer_inspect / use_ams toggles
        # rather than falling back to the model's column defaults
        # (upstream Bambuddy #1403). FTP completes a few hundred ms
        # before the slicer's MQTT ``project_file`` arrives, so the
        # queue-add path waits briefly on the event below before
        # reading the dict. Events are popped along with the options
        # so the dict stays bounded.
        self._slicer_print_options: dict[str, dict] = {}
        self._slicer_print_options_events: dict[str, asyncio.Event] = {}

        # Queue items recently committed by ``_add_to_print_queue``, keyed by
        # FTP filename. Used by ``on_print_command`` to retroactively stamp the
        # slicer's ``nozzle_mapping`` (and the other slicer-driven flags) onto a
        # queue item when the MQTT ``project_file`` arrives after the queue-add
        # wait timed out — the #1780 round-3 race. Value is
        # (queue_item_ids, monotonic_committed_at); entries older than
        # ``_RECENT_QUEUE_ITEM_TTL`` are evicted opportunistically on each
        # queue-add.
        self._recent_queue_items: dict[str, tuple[list[int], float]] = {}

        # Per-instance services
        self._proxy: SlicerProxyManager | None = None
        self._ftp: VirtualPrinterFTPServer | None = None
        self._mqtt: SimpleMQTTServer | None = None
        self._mqtt_bridge: MQTTBridge | None = None
        self._rtsp_proxy: TCPProxy | None = None
        self._bind: BindServer | None = None
        self._ssdp: VirtualPrinterSSDPServer | None = None
        self._ssdp_proxy: SSDPProxy | None = None
        self._tasks: list[asyncio.Task] = []

        # Pending timer that re-fires gcode_state=FINISH after a project_file
        # ack. See ``_schedule_finish_release`` for the #1658 rationale.
        self._finish_release_task: asyncio.Task | None = None

    @property
    def serial(self) -> str:
        """Full serial number for this virtual printer."""
        return _get_serial_for_model(self.model or DEFAULT_VIRTUAL_PRINTER_MODEL, self.serial_suffix)

    @property
    def cert_path(self) -> Path:
        return self._cert_service.cert_path

    @property
    def key_path(self) -> Path:
        return self._cert_service.key_path

    @property
    def is_proxy(self) -> bool:
        return self.mode == "proxy"

    @property
    def is_running(self) -> bool:
        return len(self._tasks) > 0 and all(not t.done() for t in self._tasks)

    def generate_certificates(self) -> tuple[Path, Path]:
        """Generate certificates for this instance."""
        self._cert_service.serial = self.serial if not self.is_proxy else (self.target_printer_serial or self.serial)
        additional_ips = [self.remote_interface_ip] if self.remote_interface_ip else None
        if self.bind_ip:
            additional_ips = additional_ips or []
            additional_ips.append(self.bind_ip)
        self._cert_service.delete_printer_certificate()
        return self._cert_service.generate_certificates(additional_ips=additional_ips)

    # -- File handling callbacks --

    async def on_file_received(self, file_path: Path, source_ip: str) -> None:
        """Handle file upload completion from FTP."""
        logger.info("[VP %s] Received file: %s from %s", self.name, file_path.name, source_ip)

        self._pending_files[file_path.name] = file_path

        # Four supported modes (m002 already migrated legacy ``immediate``
        # / ``review`` rows to ``file_manager``, and ``queue`` to
        # ``print_queue``):
        #   * print_queue  — archive + push directly to a per-printer queue
        #                    (specific target_printer_id, or "least busy of
        #                    matching model" via _find_best_queue).
        #   * auto_queue   — archive + drop into the global auto-queue
        #                    layer; AutoQueueScheduler picks an eligible
        #                    printer with full filament/color check.
        #   * file_manager — save to library for review.
        #   * proxy        — bypasses this handler (the TCP proxy passes
        #                    bytes straight through to the real printer).
        # Anything unexpected falls back to file_manager rather than
        # silently dropping the file.
        try:
            if self.mode == "print_queue":
                await self._add_to_print_queue(file_path, source_ip)
            elif self.mode == "auto_queue":
                await self._add_to_auto_queue(file_path, source_ip)
            else:
                await self._save_to_library(file_path, source_ip)
        finally:
            # Drop the pending-file marker here so it's cleared for every mode
            # even if the handler raised partway through — the scattered per-path
            # pops inside the handlers missed the exception paths, leaking Path
            # refs into ``_pending_files`` (inflating the status counter) until a
            # later successful upload of the same name (upstream v0.2.4.5).
            self._pending_files.pop(file_path.name, None)
            # Always resolve the upload state machine to a terminal state, in a
            # ``finally`` so it runs for every mode and even if a handler raised
            # — otherwise the ``PREPARE`` set by the slicer's ``project_file``
            # command sticks and the next pre-flight reads "busy with another
            # print job" (BambuStudio / OrcaSlicer both treat PREPARE as
            # in-printing).
            #
            # Send-flow slicers don't watch the post-upload state, but the Print
            # flow (intended for proxy-mode VPs, though users click it against
            # other modes too — upstream Bambuddy #1280) watches the gcode_state
            # cycle and only releases its in-flight-job lock when it sees
            # ``FINISH``. ``PREPARE → IDLE`` wedges its UI at "Downloading...(0%)";
            # ``PREPARE → FINISH`` satisfies both flows, and
            # ``prepare_percent=100`` unfreezes the "Downloading X%" bar. A
            # non-3MF upload (cover image / slicer junk) carries no job, so we
            # only clear any leftover PREPARE rather than advertising a phantom
            # finished print.
            if self._mqtt:
                if file_path.suffix.lower() == ".3mf":
                    self._mqtt.set_gcode_state("FINISH", filename=file_path.name, prepare_percent="100")
                else:
                    self._mqtt.resolve_stale_prepare()

    async def on_print_command(self, filename: str, data: dict) -> None:
        """Handle print command from MQTT.

        Captures the slicer's ``project_file`` options (``timelapse``,
        ``bed_leveling``, ``flow_cali``, ``layer_inspect``, ``use_ams``,
        plus the H2C rack-pick ``nozzle_mapping``) so the VP-queue path
        can inherit them when adding the item to the queue, rather than
        falling back to the model's column defaults (upstream Bambuddy
        #1403, #1780). Only ``print_queue`` mode consumes the capture;
        other modes ignore the print command, so we skip the stash there
        to keep the dict from accumulating one entry per print over the
        VP's uptime.

        Also schedules the #1658 follow-up that re-fires gcode_state=FINISH a
        moment after the synthetic project_file ack — for every non-proxy mode
        — so the slicer's "Downloading" UI releases on Bambu Studio 2.7.x's
        FTP-first-then-MQTT send order.

        ``filename`` is the slicer's ``subtask_name`` (bare model name, no
        extension) — used verbatim for ``_schedule_finish_release`` because
        push_status echoes it back to the slicer as gcode_file / subtask_name.
        The queue-side stash key is derived from ``data["file"]`` (the FTP
        filename WITH extension) so ``_add_to_print_queue``'s ``file_path.name``
        lookup matches; falls back to ``filename`` when ``data["file"]`` is
        absent (legacy slicers / non-3MF uploads). Stash/lookup mismatch was
        the #1780 root cause — every captured field silently fell back to
        settings defaults on every Bambu Studio "Send".
        """
        logger.info("[VP %s] Print command for: %s", self.name, filename)
        if not self.is_proxy and filename and self._mqtt is not None:
            self._schedule_finish_release(filename)
        if self.mode != "print_queue":
            return
        # Stash key must match ``_add_to_print_queue``'s lookup, which uses
        # ``file_path.name`` (the FTP filename WITH extension). The slicer's
        # ``subtask_name`` (== this method's ``filename`` arg) is the bare
        # model name, no extension — using it as the stash key silently
        # dropped every captured slicer field (#1780 root cause). The FTP
        # filename rides in ``data["file"]``; fall back to ``filename`` for
        # legacy slicers / non-3MF uploads that omit it.
        stash_key = data.get("file") or filename
        # FIFO-evict the oldest entry when at the cap so a stream of
        # never-consumed project_file captures can't grow unbounded. Dicts
        # preserve insertion order, so iter() yields the oldest key first.
        while len(self._slicer_print_options) >= _SLICER_OPTIONS_CACHE_LIMIT:
            stale_key = next(iter(self._slicer_print_options))
            self._slicer_print_options.pop(stale_key, None)
            self._slicer_print_options_events.pop(stale_key, None)
        self._slicer_print_options[stash_key] = dict(data)
        event = self._slicer_print_options_events.get(stash_key)
        if event:
            event.set()
            return
        # No consumer waiting: ``_add_to_print_queue`` either already gave up
        # (wait_for timed out) or hasn't started yet (FTP still uploading). If
        # a queue item was committed within the last ``_RECENT_QUEUE_ITEM_TTL``,
        # the wait timed out and the row holds settings defaults instead of the
        # slicer's pick — retroactively stamp the slicer-driven fields so the
        # dispatcher honours the user's choice. Covers the #1780 round-3 race
        # where Bambu Studio's MQTT lands just past the bumped wait ceiling.
        await self._restamp_recent_queue_item(stash_key, data)

    async def _restamp_recent_queue_item(self, stash_key: str, data: dict) -> None:
        """Patch slicer-driven fields onto a queue item the MQTT command missed.

        ``_add_to_print_queue`` waits up to ``_SLICER_OPTIONS_WAIT_TIMEOUT``
        for the slicer's MQTT ``project_file`` before committing the queue
        item. If the MQTT command arrives after that window — observed in the
        field at ~2.1 s on H2C / wireless setups (#1780 round 3) — the row was
        already written with settings defaults. This method runs on the late
        MQTT path: it looks up the most recent queue items committed for this
        filename and patches in the slicer's ``nozzle_mapping`` + workflow
        flags, but only while the items are still ``pending`` (the scheduler
        hasn't dispatched them yet), so we never race the dispatcher.
        """
        if not self._session_factory:
            return
        entry = self._recent_queue_items.get(stash_key)
        if entry is None:
            return
        queue_item_ids, committed_at = entry
        if time.monotonic() - committed_at > _RECENT_QUEUE_ITEM_TTL:
            self._recent_queue_items.pop(stash_key, None)
            return

        import json

        # Mirror the field set ``_add_to_print_queue`` reads off slicer_opts.
        # MQTT sends ``bed_leveling`` (single L); our column is ``bed_levelling``.
        # ``vibration_cali`` is intentionally NOT stamped — PrintQueueItem has
        # no such column (dispatch hardcodes vibration_cali=False in
        # bambu_mqtt.start_print), so writing it would raise.
        patch: dict = {}
        for mqtt_field, column in (
            ("bed_leveling", "bed_levelling"),
            ("flow_cali", "flow_cali"),
            ("layer_inspect", "layer_inspect"),
            ("timelapse", "timelapse"),
            ("use_ams", "use_ams"),
        ):
            if mqtt_field in data:
                patch[column] = bool(data[mqtt_field])

        # Where the slicer asked the recording to go — ``cfg`` bit 2, the same
        # per-print pick BambuStudio offers behind the folder icon beside its
        # timelapse checkbox. Same reasoning as ``nozzle_mapping`` below: the
        # operator chose it in the Send dialog, and dropping it silently sends
        # the print somewhere they did not ask for.
        #
        # ⚠️ **Only the set bit is a choice.** A clear bit 2 is NOT "external":
        # Studio sends ``"0"`` for External, for a timelapse that is off, and
        # for a machine with no internal storage at all — three different things
        # wearing one value. Reading zero as a pick would stamp "external" on
        # every print any slicer ever sent us. Left NULL, it means what it is:
        # nobody said, so nothing is claimed.
        _cfg = data.get("cfg")
        if _cfg is not None:
            try:
                if int(str(_cfg), 16) & 0x4:
                    patch["timelapse_storage"] = "internal"
            except (TypeError, ValueError):
                pass

        raw = data.get("nozzle_mapping")
        if raw is not None:
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "[VP %s] Late MQTT nozzle_mapping is unparseable JSON, dropping: %r",
                        self.name,
                        raw,
                    )
                    raw = None
            if raw is not None:
                patch["nozzle_mapping"] = json.dumps(raw)

        if not patch:
            self._recent_queue_items.pop(stash_key, None)
            return

        from sqlalchemy import select, update

        from backend.app.models.print_queue import PrintQueueItem

        try:
            async with self._session_factory() as db:
                # Only stamp items still pending; once the scheduler has picked
                # the row up we can't safely race the dispatcher.
                result = await db.execute(
                    select(PrintQueueItem.id).where(
                        PrintQueueItem.id.in_(queue_item_ids),
                        PrintQueueItem.status == "pending",
                    )
                )
                eligible_ids = [row[0] for row in result.all()]
                if not eligible_ids:
                    self._recent_queue_items.pop(stash_key, None)
                    return
                await db.execute(update(PrintQueueItem).where(PrintQueueItem.id.in_(eligible_ids)).values(**patch))
                await db.commit()
                logger.info(
                    "[VP %s] Late slicer MQTT for %s — retroactively stamped %s onto queue item(s) %s",
                    self.name,
                    stash_key,
                    sorted(patch.keys()),
                    eligible_ids,
                )
        except Exception as e:
            logger.error(
                "[VP %s] Failed to retroactively stamp queue item(s) %s for %s: %s",
                self.name,
                queue_item_ids,
                stash_key,
                e,
            )
        finally:
            self._recent_queue_items.pop(stash_key, None)

    def _schedule_finish_release(self, filename: str, delay: float = 1.5) -> None:
        """Re-set gcode_state=FINISH on the VP after the project_file ack.

        #1280 set FINISH after the FTP upload completes — correct for the
        slicer flow at the time (MQTT project_file → FTP → done). Bambu Studio
        2.7.x flipped the order to FTP → FTP → MQTT project_file, so the
        synthetic ``project_file`` ack now runs *after* the FINISH set in
        ``on_file_received`` and overwrites the state back to PREPARE. The
        slicer's 1 Hz status stream then carries PREPARE forever and the send
        modal sits at "Downloading" until the VP is restarted (#1658).

        Re-firing FINISH after a short delay closes the gap: the slicer sees the
        synthetic PREPARE in the project_file ack (and likely one PREPARE push
        on the 1 Hz cycle), then the next push carries FINISH and the modal
        releases. Proxy mode is exempt — there the real printer drives the state
        through the bridge and a synthetic FINISH would clobber a real
        PREPARE/RUNNING transition coming back from the printer.

        Cancels any in-flight timer before scheduling a new one so a slicer that
        fires project_file twice in quick succession only ends in one FINISH.
        """
        if self._mqtt is None:
            return
        if self._finish_release_task is not None and not self._finish_release_task.done():
            self._finish_release_task.cancel()
        self._finish_release_task = asyncio.create_task(
            self._delayed_finish_release(filename, delay),
            name=f"vp-{self.id}-finish-release",
        )

    async def _delayed_finish_release(self, filename: str, delay: float) -> None:
        """Sleep, then set gcode_state=FINISH. Used by ``_schedule_finish_release``."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._mqtt is None:
            return
        self._mqtt.set_gcode_state("FINISH", filename=filename, prepare_percent="100")
        logger.debug("[VP %s] Re-set gcode_state=FINISH after project_file ack (%s)", self.name, filename)

    def on_upload_start(self) -> None:
        """FTP STOR began — flag the MQTT side so it advertises the real
        in-flight ``PREPARE`` rather than downgrading it to IDLE."""
        if self._mqtt:
            self._mqtt.upload_started()

    def on_upload_end(self) -> None:
        """FTP STOR ended (success OR failure). A failed upload leaves the
        ``PREPARE`` set by ``project_file`` behind; once no upload is in flight
        the MQTT side reports IDLE so the slicer isn't stuck on "busy"."""
        if self._mqtt:
            self._mqtt.upload_finished()

    async def _save_to_library(self, file_path: Path, source_ip: str):  # noqa: ARG002
        """Save file to the File Manager library.

        Returns the persisted ``LibraryFile`` row (or ``None`` on failure /
        non-3MF input). The Audit-2 redesign in 0.4.2 made this the single
        ingestion entry-point for **all** VP modes (not just file_manager) —
        ``_add_to_print_queue`` and ``_add_to_auto_queue`` now call it first
        and queue against the resulting ``library_file_id`` instead of
        pre-creating a placeholder ``status='archived'`` archive row.

        ``source_ip`` is currently informational only (preserved in the
        argument list so callers don't have to know it became a no-op).
        """
        if not self._session_factory:
            logger.error("Cannot save to library: no database session factory configured")
            return None

        if file_path.suffix.lower() != ".3mf":
            logger.debug("Skipping non-3MF file: %s", file_path.name)
            self._pending_files.pop(file_path.name, None)
            try:
                file_path.unlink()
            except OSError:
                pass
            return None

        try:
            import hashlib
            import uuid
            import zipfile

            from backend.app.api.routes.library import (
                get_library_files_dir,
                get_library_thumbnails_dir,
                to_relative_path,
            )
            from backend.app.models.library import LibraryFile
            from backend.app.services.archive import ThreeMFParser
            from backend.app.services.library_helpers import (
                detect_file_type,
                skip_objects_supported_from_metadata,
                sync_system_tags,
            )
            from backend.app.services.library_ingest import find_reusable_row

            async with self._session_factory() as db_session:
                filename = file_path.name
                # Bambu Studio's "Send to printer" lands files via VP FTP
                # under their bare ``.3mf`` name even when the bytes are a
                # sliced gcode-3mf container. Without promoting the suffix,
                # ``detect_file_type`` returns ``"3mf"`` and the row is
                # tagged as a project, missing the ``sliced`` badge — same
                # mismatch that hit the printer-FTP import path. Probe the
                # zip for ``Metadata/plate_*.gcode`` and rewrite the
                # apparent filename when present, so both the library row
                # and the on-disk copy end up in the canonical sliced
                # shape (``{stem}.gcode.3mf``).
                detected_source_type: str | None = None
                lower = filename.lower()
                if lower.endswith(".3mf") and not lower.endswith(".gcode.3mf"):
                    try:
                        with zipfile.ZipFile(str(file_path), "r") as _probe:
                            if any(n.startswith("Metadata/plate_") and n.endswith(".gcode") for n in _probe.namelist()):
                                filename = f"{filename[:-4]}.gcode.3mf"
                                detected_source_type = "sliced"
                    except (zipfile.BadZipFile, KeyError):
                        pass

                # On-disk extension follows the (possibly promoted) filename
                # so a row with ``filename = "X.gcode.3mf"`` keeps a
                # ``{uuid}.gcode.3mf`` copy on disk — matches the regular
                # library upload path.
                ext = ".gcode.3mf" if filename.lower().endswith(".gcode.3mf") else file_path.suffix.lower()
                file_type = detect_file_type(filename)

                library_files_dir = get_library_files_dir()
                unique_filename = f"{uuid.uuid4().hex}{ext}"
                dest_path = (
                    library_files_dir / unique_filename
                )  # SEC-PATH-OK: unique_filename = uuid4().hex + the extension of an already-basename'd name

                import shutil

                shutil.copy2(str(file_path), str(dest_path))

                sha256_hash = hashlib.sha256()
                with open(dest_path, "rb") as f:
                    for block in iter(lambda: f.read(4096), b""):
                        sha256_hash.update(block)
                file_hash = sha256_hash.hexdigest()

                metadata = None
                thumbnail_path = None

                try:
                    parser = ThreeMFParser(str(dest_path))
                    raw_metadata = parser.parse()

                    thumbnail_data = raw_metadata.get("_thumbnail_data")
                    thumbnail_ext = raw_metadata.get("_thumbnail_ext", ".png")

                    if thumbnail_data:
                        thumbnails_dir = get_library_thumbnails_dir()
                        thumb_filename = f"{uuid.uuid4().hex}{thumbnail_ext}"
                        thumb_path = (
                            thumbnails_dir / thumb_filename
                        )  # SEC-PATH-OK: thumb_filename = uuid4().hex + a hardcoded .png extension
                        with open(thumb_path, "wb") as f:
                            f.write(thumbnail_data)
                        thumbnail_path = str(thumb_path)

                    def clean_metadata(obj):
                        if isinstance(obj, dict):
                            return {
                                k: clean_metadata(v)
                                for k, v in obj.items()
                                if not isinstance(v, bytes) and k not in ("_thumbnail_data", "_thumbnail_ext")
                            }
                        elif isinstance(obj, list):
                            return [clean_metadata(i) for i in obj if not isinstance(i, bytes)]
                        elif isinstance(obj, bytes):
                            return None
                        return obj

                    metadata = clean_metadata(raw_metadata)

                    # Per-plate cache (matches the regular library upload path
                    # in routes/library.py::upload_file). Without this, VP-saved
                    # library entries miss the gallery on the file-manager UI
                    # because ``is_multi_plate`` is unset.
                    try:
                        import zipfile as _zf

                        from backend.app.services.archive import parse_plates_from_3mf

                        with _zf.ZipFile(str(dest_path), "r") as _zfh:
                            plates_payload = parse_plates_from_3mf(_zfh)
                        if plates_payload and metadata is not None:
                            metadata["plates"] = plates_payload
                            metadata["is_multi_plate"] = len(plates_payload) > 1
                    except Exception as _pe:
                        logger.debug("[VP %s] per-plate parse failed (non-critical): %s", self.name, _pe)
                except Exception as e:
                    logger.warning("[VP %s] Failed to parse 3MF metadata: %s", self.name, e)

                library_file = LibraryFile(
                    folder_id=self.target_folder_id,
                    filename=filename,
                    file_path=to_relative_path(dest_path),
                    file_type=file_type,
                    skip_objects_supported=skip_objects_supported_from_metadata(metadata),
                    file_size=file_path.stat().st_size,
                    file_hash=file_hash,
                    thumbnail_path=to_relative_path(thumbnail_path) if thumbnail_path else None,
                    file_metadata=metadata,
                    source_type=detected_source_type,
                )
                # The slicer sends the same plate again more often than any
                # other path — "Send to printer", tweak nothing, send again.
                # ``find_reusable_row`` is the one place that decides.
                reusable = await find_reusable_row(db_session, content_hash=file_hash)
                if reusable is not None and reusable[1]:
                    # ⚠️ Clean up exactly what the success path below cleans up:
                    # the copy written into the library dir, and the staged
                    # upload. Otherwise every resend leaves two orphans.
                    dest_path.unlink(missing_ok=True)
                    try:
                        file_path.unlink()
                    except OSError:
                        pass
                    self._pending_files.pop(file_path.name, None)
                    logger.info(
                        "[VP %s] %s is already in the library as %r (id=%s) — using that row",
                        self.name,
                        filename,
                        reusable[0].filename,
                        reusable[0].id,
                    )
                    return reusable[0]

                db_session.add(library_file)
                # ⚠️ Flush before the sync, and sync before the commit: the
                # system-tag associations key off ``library_file.id``. This site
                # kept the pre-m128 form — ``file_tags=`` in the constructor —
                # which writes the cache column and no associations, so every
                # file the slicer sent through the Virtual Printer rendered its
                # badges correctly and was missing from every server-side tag
                # filter.
                await db_session.flush()
                await sync_system_tags(db_session, library_file)
                await db_session.commit()
                await db_session.refresh(library_file)
                logger.info(
                    "[VP %s] Saved to library: %s (id=%s, folder=%s)",
                    self.name,
                    filename,
                    library_file.id,
                    self.target_folder_id,
                )

                # Notify frontend to refresh File Manager
                try:
                    from backend.app.core.websocket import ws_manager

                    await ws_manager.send_library_file_added({"id": library_file.id, "filename": filename})
                except Exception:
                    pass

                try:
                    file_path.unlink()
                except OSError:
                    pass
                self._pending_files.pop(file_path.name, None)
                return library_file
        except Exception as e:
            logger.error("Error saving to library: %s", e)
            return None

    async def _add_to_print_queue(self, file_path: Path, source_ip: str) -> None:
        """Save file to library and add to a per-printer queue.

        Audit-2 redesign (0.4.2): the file lands in the library FIRST
        (single source-of-truth for "files we have"), then the queue item
        is created with ``library_file_id``. The dispatcher's
        ``_run_print_library_file`` path creates the archive at print-start
        with ``status='printing'`` — we no longer pre-create a synthetic
        ``status='archived'`` placeholder.
        """
        if not self._session_factory:
            logger.error("Cannot add to print queue: no database session factory configured")
            return

        if file_path.suffix.lower() != ".3mf":
            self._pending_files.pop(file_path.name, None)
            try:
                file_path.unlink()
            except OSError:
                pass
            return

        # Wait briefly for the slicer's MQTT ``project_file`` command so
        # the queue item can inherit the slicer-side print options the
        # user picked (timelapse, bed_leveling, etc.). Slicers send the
        # FTP upload first and the MQTT command immediately after, so the
        # typical lag is a few hundred ms. The window is generous enough
        # to absorb wireless / loaded-Pi jitter without making every
        # VP-queue add visibly slow — the observed worst case in #1780
        # round 3 was 2.085 s, just past the previous 2.0 s ceiling. Falls
        # back to the model's column defaults if MQTT doesn't arrive in
        # time (legacy behaviour for users on a slicer that doesn't send a
        # print command — upstream Bambuddy #1403); a late MQTT that lands
        # after the wait is caught by ``on_print_command``'s retroactive
        # stamp path. The wait is skipped when there's no MQTT server
        # attached — covers unit tests that invoke ``_add_to_print_queue``
        # directly without going through ``on_print_command``, so they
        # don't pay the wait tax.
        slicer_opts = self._slicer_print_options.pop(file_path.name, None)
        if slicer_opts is None and self._mqtt is not None:
            event = asyncio.Event()
            self._slicer_print_options_events[file_path.name] = event
            try:
                await asyncio.wait_for(event.wait(), timeout=_SLICER_OPTIONS_WAIT_TIMEOUT)
                slicer_opts = self._slicer_print_options.pop(file_path.name, None)
            except TimeoutError:
                slicer_opts = None
            finally:
                self._slicer_print_options_events.pop(file_path.name, None)
        # If the cache still misses, queued workflow flags / nozzle pick will
        # silently fall back to settings defaults. Surface the missed key so a
        # future stash/lookup mismatch (the #1780 root cause) is obvious in the
        # log instead of needing a wire capture to diagnose.
        if slicer_opts is None:
            logger.debug(
                "[VP %s] No slicer options cached for %r (cache keys: %s); "
                "workflow flags + nozzle pick will fall back to settings defaults.",
                self.name,
                file_path.name,
                sorted(self._slicer_print_options.keys()),
            )

        try:
            # Step 1: save to library (handles file copy + metadata extraction
            # + cleanup of source temp + WS broadcast). Returns the persisted
            # ``LibraryFile`` row or None on failure.
            library_file = await self._save_to_library(file_path, source_ip)
            if not library_file:
                logger.error("[VP %s] Failed to save to library: %s", self.name, file_path.name)
                return

            # Step 2: pick a queue based on the library row's metadata and
            # link a queue item to it.
            from backend.app.models.print_queue import PrintQueueItem

            sliced_model = None
            if isinstance(library_file.file_metadata, dict):
                sliced_model = library_file.file_metadata.get("sliced_for_model")

            async with self._session_factory() as db:
                queue = await self._find_best_queue(db, sliced_model)
                if not queue:
                    # File already in library; just no auto-queue placement.
                    # Operator can pick it up from the File Manager and queue
                    # manually. Same UX as ``mode='file_manager'`` from here on.
                    logger.info(
                        "[VP %s] No matching printer queue for %s — file stays in library only",
                        self.name,
                        library_file.filename,
                    )
                    return

                # #1733: a multi-plate "Send All" upload ships every plate in
                # one 3MF, so ``file_metadata['plates']`` lists each plate with
                # its own index. Enqueue one queue item per plate so the
                # scheduler runs each separately. A single-plate "Send" yields a
                # one-element list, preserving the prior one-item-per-upload
                # behaviour (and ``[None]`` when metadata is missing → the
                # dispatcher defaults plate_id to 1).
                plate_ids = self._extract_plate_indices_from_metadata(library_file.file_metadata)

                # System fallback row (user_id IS NULL) for the target
                # printer's model — fills any flag the slicer omitted. There's
                # no user in a slicer→VP handshake, so the per-user preference
                # rows can't serve here (upstream Bambuddy #1235).
                system_opts = await self._load_system_print_options(db, queue.printer_id)

                # Append at the tail: MAX(position) for this queue, then hand
                # consecutive positions to each plate so a Send All keeps
                # plate-order execution. A hardcoded position=1 stacked every
                # VP-queued print at the head, so two slicer Sends in a row
                # collided on position 1 and their order became arbitrary
                # (upstream Bambuddy v0.2.4.5).
                from sqlalchemy import func as sa_func, select as sa_select

                max_pos = await db.scalar(
                    sa_select(sa_func.coalesce(sa_func.max(PrintQueueItem.position), 0)).where(
                        PrintQueueItem.queue_id == queue.id
                    )
                )
                base_pos = max_pos or 0

                # Precedence per flag: slicer value → system fallback → column
                # default (see ``_resolve_print_option``). Resolved once — only
                # plate_id + position vary per plate.
                bed_levelling = _resolve_print_option(slicer_opts, system_opts, "bed_leveling", "bed_levelling", True)
                flow_cali = _resolve_print_option(slicer_opts, system_opts, "flow_cali", "flow_cali", True)
                layer_inspect = _resolve_print_option(slicer_opts, system_opts, "layer_inspect", "layer_inspect", False)
                timelapse = _resolve_print_option(slicer_opts, system_opts, "timelapse", "timelapse", False)
                # use_ams has no system-preference field (the saved PrintModal
                # toggles don't carry it) — slicer value or column default only.
                use_ams = bool(slicer_opts["use_ams"]) if slicer_opts and "use_ams" in slicer_opts else True
                # Recording medium, captured from the slicer's ``cfg`` bit 2 in
                # ``on_print_command``. No system fallback and no default: NULL
                # means nobody chose, and the dispatcher leaves the machine to
                # its own devices — see the note there on why a clear bit is not
                # a pick.
                timelapse_storage = (slicer_opts or {}).get("timelapse_storage")

                # H2C dual-nozzle-rack slicer-pick preservation (#1780).
                # BambuStudio's project_file MQTT command for rack-swap models
                # (O1C2 today) carries ``nozzle_mapping`` — the slicer's
                # per-filament array of physical nozzle position IDs
                # (``list[int]``) straight off the wire. Forward it verbatim
                # onto the queue item so the dispatcher can replay it in its
                # own project_file command; without it the H2C firmware falls
                # back to "last matching nozzle" auto-pick and ignores the
                # user's Bambu Studio choice. Every other model has it absent
                # from slicer_opts, so this is a transparent no-op there.
                #
                # DISTINCT from ``utils/threemf_tools.extract_nozzle_mapping_from_3mf``
                # (a server-derived slot→physical-extruder ``dict[int, int]``
                # that feeds per-filament nozzle_id and never reaches
                # command["print"]). Do not conflate the two.
                import json

                # The slicer's own resolved AMS pick (#2700). Captured only
                # when this VP asks for it: storing a mapping on the queue item
                # makes ``_ensure_ams_mapping`` return early, which skips
                # prefer_lowest_filament, the AMS-Backup gate, the
                # inventory-remain overrides and FTS routing. Off is today's
                # behaviour, exactly.
                ams_mapping_json: str | None = None
                if self.save_ams_mapping and slicer_opts is not None:
                    raw_ams_mapping = slicer_opts.get("ams_mapping")
                    if isinstance(raw_ams_mapping, str):
                        try:
                            raw_ams_mapping = json.loads(raw_ams_mapping)
                        except json.JSONDecodeError:
                            logger.warning(
                                "[VP %s] Slicer ams_mapping is unparseable JSON, dropping: %r",
                                self.name,
                                raw_ams_mapping,
                            )
                            raw_ams_mapping = None
                    # An all-[-1] mapping is the slicer saying "unresolved", not
                    # a pick — storing it would be read downstream as an explicit
                    # external-spool selection and print against an empty feed.
                    if isinstance(raw_ams_mapping, list) and any(
                        isinstance(v, int) and v >= 0 for v in raw_ams_mapping
                    ):
                        ams_mapping_json = json.dumps(raw_ams_mapping)

                nozzle_mapping_json: str | None = None
                if slicer_opts is not None:
                    raw_nozzle_mapping = slicer_opts.get("nozzle_mapping")
                    if raw_nozzle_mapping is not None:
                        # BambuStudio's NetworkAgent embeds this as parsed JSON
                        # in the project_file body (matching the ams_mapping
                        # shape we already consume as list[int]). Accept a
                        # JSON-encoded string defensively; fail-open (drop) on
                        # bad JSON so the firmware auto-picks rather than the
                        # dispatch breaking.
                        if isinstance(raw_nozzle_mapping, str):
                            try:
                                raw_nozzle_mapping = json.loads(raw_nozzle_mapping)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "[VP %s] Slicer nozzle_mapping is unparseable JSON, dropping: %r",
                                    self.name,
                                    raw_nozzle_mapping,
                                )
                                raw_nozzle_mapping = None
                        if raw_nozzle_mapping is not None:
                            nozzle_mapping_json = json.dumps(raw_nozzle_mapping)

                queue_item_ids: list[int] = []
                # ⚠️ No ``created_by_id`` here, and that is the answer rather
                # than a gap. A VirtualPrinter carries no owner, and the obvious
                # substitute is wrong rather than incomplete: one admin
                # typically configures the VP while everyone slices through it,
                # so crediting these to that admin would make the "added by"
                # column lie and drop other people's jobs into their queue.
                # Every other path that builds a queue item DOES set it —
                # ``queue:read_own`` filters on it — so a missing value here has
                # to stay a deliberate, documented one.
                for offset, plate_id in enumerate(plate_ids, start=1):
                    queue_item = PrintQueueItem(
                        queue_id=queue.id,
                        library_file_id=library_file.id,
                        archive_id=None,  # archive created at print-start by _run_print_library_file
                        plate_id=plate_id,
                        position=base_pos + offset,
                        status="pending",
                        manual_start=not self.auto_dispatch,
                        bed_levelling=bed_levelling,
                        flow_cali=flow_cali,
                        layer_inspect=layer_inspect,
                        timelapse=timelapse,
                        timelapse_storage=timelapse_storage,
                        use_ams=use_ams,
                        # Stamp the slicer's H2C rack nozzle pick on every plate
                        # of a multi-plate Send All so each plate replays the
                        # same pick (mirrors the #1697 / #1733 per-plate loop).
                        nozzle_mapping=nozzle_mapping_json,
                        # None unless this VP opted in — see above.
                        ams_mapping=ams_mapping_json,
                        # Per-VP opt-in for auto-print G-code injection (#1516).
                        # Default off; when on, the dispatcher still no-ops unless
                        # gcode_snippets are configured for the target model, so
                        # it's effectively "inject when enabled AND snippets exist".
                        gcode_injection=self.gcode_injection,
                    )
                    db.add(queue_item)
                    await db.flush()  # populate queue_item.id before logging
                    queue_item_ids.append(queue_item.id)
                await db.commit()
                # Track the freshly-committed queue items so ``on_print_command``
                # can retroactively stamp slicer-side fields if the MQTT
                # ``project_file`` lands AFTER the ``_SLICER_OPTIONS_WAIT_TIMEOUT``
                # window expired — the #1780 round-3 race. Eviction of stale
                # entries here keeps the dict bounded; the queue path is the only
                # writer, so doing it on commit is enough.
                now = time.monotonic()
                cutoff = now - _RECENT_QUEUE_ITEM_TTL
                self._recent_queue_items = {k: v for k, v in self._recent_queue_items.items() if v[1] > cutoff}
                self._recent_queue_items[file_path.name] = (list(queue_item_ids), now)
                # Last-chance check: MQTT for this filename could have arrived
                # during ANY await between the initial pop and now — wait_for
                # itself, _save_to_library, db.flush, db.commit. In all those
                # cases ``on_print_command`` stashed its data but neither the
                # event-signal path nor the retroactive ``_recent_queue_items``
                # path was in place to consume it. Pop any late stash and apply
                # inline so the late MQTT never leaks past the queue-add.
                late_opts = self._slicer_print_options.pop(file_path.name, None)
                if late_opts is not None:
                    logger.info(
                        "[VP %s] Late slicer MQTT detected for %s during queue-add — "
                        "applying inline (race vs commit/flush/save yield)",
                        self.name,
                        file_path.name,
                    )
                    await self._restamp_recent_queue_item(file_path.name, late_opts)
                if len(queue_item_ids) == 1:
                    logger.info(
                        "[VP %s] Added to queue %s (printer %s): item %s, library_file=%s",
                        self.name,
                        queue.id,
                        queue.printer_id,
                        queue_item_ids[0],
                        library_file.id,
                    )
                else:
                    logger.info(
                        "[VP %s] Added %d queue items for multi-plate upload (plates %s) to queue %s (printer %s): %s, library_file=%s",
                        self.name,
                        len(queue_item_ids),
                        plate_ids,
                        queue.id,
                        queue.printer_id,
                        queue_item_ids,
                        library_file.id,
                    )
        except Exception as e:
            logger.exception("[VP %s] Error adding to print queue: %s", self.name, e)

    @staticmethod
    async def _load_system_print_options(db, printer_id: int | None) -> dict | None:
        """Return the system fallback ``print_options`` dict for ``printer_id``'s model.

        The system row is a ``PrintOptionsPreference`` with ``user_id IS NULL``
        keyed by printer model (upstream Bambuddy #1235). Returns None when the
        printer / model is unknown or no system row is configured — callers
        then fall back to the model column defaults.
        """
        if not printer_id:
            return None
        from sqlalchemy import select as sa_select

        from backend.app.models.print_options_preference import PrintOptionsPreference
        from backend.app.models.printer import Printer

        model = await db.scalar(sa_select(Printer.model).where(Printer.id == printer_id))
        if not model:
            return None
        pref = await db.scalar(
            sa_select(PrintOptionsPreference).where(
                PrintOptionsPreference.user_id.is_(None),
                PrintOptionsPreference.printer_model == model,
            )
        )
        if pref is None or not isinstance(pref.options, dict):
            return None
        print_options = pref.options.get("print_options")
        return print_options if isinstance(print_options, dict) else None

    async def _add_to_auto_queue(self, file_path: Path, source_ip: str) -> None:
        """Save file to library and drop it into the global auto-queue layer.

        Audit-2 redesign (0.4.2): mirrors ``_add_to_print_queue`` — file
        lands in the library first, then the auto-queue row carries
        ``library_file_id`` instead of pre-creating an
        ``status='archived'`` placeholder archive. Unlike
        ``_add_to_print_queue`` this never picks a printer up-front;
        the AutoQueueScheduler later assigns the item to whichever idle
        printer matches model + filaments + colors. ``target_printer_id``
        on the VP config is intentionally ignored in this mode (use
        ``print_queue`` mode for specific-printer routing).
        """
        if not self._session_factory:
            logger.error("Cannot add to auto-queue: no database session factory configured")
            return

        if file_path.suffix.lower() != ".3mf":
            self._pending_files.pop(file_path.name, None)
            try:
                file_path.unlink()
            except OSError:
                pass
            return

        try:
            # Step 1: save to library. Returns the persisted LibraryFile or
            # None on failure (handles file copy + metadata + cleanup).
            library_file = await self._save_to_library(file_path, source_ip)
            if not library_file:
                logger.error("[VP %s] Failed to save to library: %s", self.name, file_path.name)
                return

            import json

            from sqlalchemy import func as sa_func, select as sa_select

            from backend.app.models.auto_queue import AutoQueueItem
            from backend.app.services.auto_queue_threemf import extract_auto_queue_requirements
            from backend.app.services.filament_requirements import extract_filament_requirements

            plate_id = self._extract_plate_id_from_metadata(library_file.file_metadata)

            # Re-extract requirements from the saved library file so the
            # auto-queue's per-slot filament info matches the bytes we'll
            # actually upload to the printer (the original upload temp is
            # gone after _save_to_library cleans it up).
            on_disk = Path(app_settings.base_dir) / library_file.file_path
            requirements = extract_auto_queue_requirements(on_disk, plate_id=plate_id)

            # When the per-VP toggle is on, also lift per-slot type+color from
            # the 3MF and persist them as ``force_color_match=True`` overrides
            # so the eligibility scheduler refuses printers loaded with the
            # right material in the wrong colours (#1188). The eligibility
            # path's `_get_missing_force_color_slots` validates the same JSON
            # shape we build here. Default-off preserves legacy types-only
            # routing for upgraders who don't want the colour filter.
            filament_overrides_json: list[dict] | None = None
            if self.queue_force_color_match:
                per_slot = extract_filament_requirements(on_disk, plate_id=plate_id)
                overrides = [
                    {
                        "slot_id": slot["slot_id"],
                        "type": slot["type"],
                        "color": slot.get("color", ""),
                        # Carry the slicer's spool identity so force_color_match
                        # can tell Bambu's PLA variants apart (#2650): Basic,
                        # Matte and Silk all report tray_type "PLA" and differ
                        # only here. A blank idx (custom or third-party spool)
                        # means "no variant constraint" and eligibility falls
                        # back to type+colour.
                        "tray_info_idx": slot.get("tray_info_idx", ""),
                        "force_color_match": True,
                    }
                    for slot in per_slot
                    if slot.get("type") and slot.get("color")
                ]
                if overrides:
                    filament_overrides_json = overrides

            sliced_model: str | None = None
            if isinstance(library_file.file_metadata, dict):
                sliced_model = library_file.file_metadata.get("sliced_for_model")

            async with self._session_factory() as db:
                # Position at the end of pending items so VP-uploads don't
                # jump ahead of UI submissions.
                max_pos = await db.scalar(
                    sa_select(sa_func.coalesce(sa_func.max(AutoQueueItem.position), 0)).where(
                        AutoQueueItem.status == "pending"
                    )
                )
                next_pos = (max_pos or 0) + 1

                # Serialise filament fields as JSON strings — the column is
                # ``Text`` and the eligibility scheduler reads via
                # ``json.loads`` in ``auto_queue_eligibility.py``. Storing the
                # raw Python list works on SQLite (silently stringifies via
                # str(list) → e.g. ``"['PLA']"``) but breaks the eligibility
                # parser, which then treats the row as if it had no
                # requirements. The /auto-queue/ POST route already does this
                # right (`auto_queue.py:268`); this aligns the VP path.
                required_types_json = (
                    json.dumps(list(requirements.required_filament_types))
                    if requirements.required_filament_types
                    else None
                )
                item = AutoQueueItem(
                    library_file_id=library_file.id,
                    archive_id=None,  # archive created at print-start by _run_print_library_file
                    target_model=requirements.target_model or sliced_model,
                    required_filament_types=required_types_json,
                    filament_overrides=(json.dumps(filament_overrides_json) if filament_overrides_json else None),
                    plate_id=plate_id,
                    position=next_pos,
                    status="pending",
                    manual_start=not self.auto_dispatch,
                    # ⚠️ No ``timelapse_storage`` here, and that is not an
                    # oversight: this path reads no slicer options at all. Unlike
                    # ``_add_to_print_queue`` it never pops
                    # ``_slicer_print_options``, so bed levelling, flow cali,
                    # layer inspect and the timelapse switch itself all come
                    # from column defaults too. Adding one of the six here would
                    # need that plumbing and would look like the other five
                    # already worked.
                    # Per-VP auto-print G-code injection opt-in (#1516). Copied
                    # onto the per-printer print_queue item when the scheduler
                    # promotes this router row. No-op unless snippets exist.
                    gcode_injection=self.gcode_injection,
                )
                db.add(item)
                await db.commit()
                logger.info(
                    "[VP %s] Added to auto-queue (item %s, library_file=%s, target_model=%s, "
                    "filaments=%s, force_color=%s)",
                    self.name,
                    item.id,
                    library_file.id,
                    item.target_model,
                    item.required_filament_types,
                    bool(filament_overrides_json),
                )
        except Exception as e:
            logger.exception("[VP %s] Error adding to auto-queue: %s", self.name, e)

    @staticmethod
    def _extract_plate_id(file_path: Path) -> int | None:
        """Extract plate index from 3MF slice_info.config (on-disk fallback).

        Kept as a fallback for code paths that don't yet have a parsed
        ``library_file.file_metadata`` to read from. The Audit-2 redesign
        prefers ``_extract_plate_id_from_metadata`` so VP file ingestion
        doesn't reopen the ZIP a second time.
        """
        try:
            import xml.etree.ElementTree as ET
            import zipfile

            with zipfile.ZipFile(file_path, "r") as zf:
                if "Metadata/slice_info.config" in zf.namelist():
                    content = zf.read("Metadata/slice_info.config").decode()
                    root = ET.fromstring(content)  # noqa: S314  # nosec B314
                    plate = root.find(".//plate")
                    if plate is not None:
                        for meta in plate.findall("metadata"):
                            if meta.get("key") == "index" and meta.get("value"):
                                return int(meta.get("value"))
        except Exception as e:
            # Log at debug rather than swallowing silently — a malformed 3MF
            # otherwise produced a wrong-plate dispatch with no diagnostic trail
            # in the support bundle (upstream Bambuddy v0.2.4.5).
            logger.debug("[VP] _extract_plate_id failed for %s: %s", file_path.name, e)
            return None
        return None

    @staticmethod
    def _extract_plate_id_from_metadata(file_metadata: dict | None) -> int | None:
        """Pick a plate index out of an already-parsed ``file_metadata`` dict.

        Audit-2 redesign helper: ``_save_to_library`` already opens the
        3MF and caches per-plate info under ``file_metadata['plates']``
        (each entry has an ``index`` field). The slicer's "Send to
        printer" workflow exports ONE plate per upload, so a sliced
        ``.gcode.3mf`` arriving via VP should have a single-entry
        plates list whose index is the user-picked plate.

        Returns ``None`` when the metadata is missing / multi-plate /
        malformed; the dispatcher then defaults plate_id to 1, which is
        the existing behaviour for queue items uploaded without plate
        context.
        """
        if not isinstance(file_metadata, dict):
            return None
        plates = file_metadata.get("plates")
        if isinstance(plates, list) and len(plates) == 1:
            idx = plates[0].get("index") if isinstance(plates[0], dict) else None
            if isinstance(idx, int) and idx >= 1:
                return idx
        return None

    @staticmethod
    def _extract_plate_indices_from_metadata(file_metadata: dict | None) -> list[int | None]:
        """Return every plate index cached in ``file_metadata['plates']`` (#1733).

        A multi-plate "Send All" 3MF ships every plate in one upload — the
        plates list has one entry per plate, each with its own ``index``.
        Returns one index per plate so the caller can enqueue one queue item
        each. A single-plate "Send" yields a one-element list (prior
        behaviour). Returns ``[None]`` when the metadata is missing / malformed
        / carries no valid index, so the caller still creates exactly one item
        (the dispatcher then defaults plate_id to 1).
        """
        if isinstance(file_metadata, dict):
            plates = file_metadata.get("plates")
            if isinstance(plates, list) and plates:
                indices = [
                    p["index"]
                    for p in plates
                    if isinstance(p, dict) and isinstance(p.get("index"), int) and p["index"] >= 1
                ]
                if indices:
                    return indices
        return [None]

    async def _find_best_queue(self, db, sliced_model: str | None):
        """Find the best printer queue for this job.

        If target_printer_id is set and online → use it.
        Otherwise, find the least busy online printer matching ``sliced_model``.
        Returns None if no matching printer is available (file should go
        to library only).

        "Least busy" = lowest total queue time (current print remaining +
        sum of pending print times). Pending-time accounting walks both
        archive- and library-file-backed queue items so the redesigned
        ingestion path (Audit-2 0.4.2) doesn't make the queue look
        artificially empty.
        """
        from sqlalchemy import select as sa_select
        from sqlalchemy.sql import func as sa_func

        from backend.app.models.archive import PrintArchive
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer import Printer
        from backend.app.models.printer_queue import PrinterQueue
        from backend.app.services.printer_manager import printer_manager

        # If explicit target is set and printer is online, use it directly
        if self.target_printer_id:
            state = printer_manager.get_status(self.target_printer_id)
            if state and state.connected:
                result = await db.execute(
                    sa_select(PrinterQueue).where(PrinterQueue.printer_id == self.target_printer_id)
                )
                queue = result.scalar_one_or_none()
                if queue:
                    return queue
            logger.info(
                "[VP %s] Target printer %s not available, searching alternatives", self.name, self.target_printer_id
            )

        if not sliced_model:
            logger.info("[VP %s] No sliced_for_model on library file, cannot auto-assign to queue", self.name)
            return None

        # Get online printers matching the model
        result = await db.execute(
            sa_select(PrinterQueue, Printer.model)
            .join(Printer, Printer.id == PrinterQueue.printer_id)
            .where(Printer.is_active.is_(True), Printer.archived.is_(False), Printer.model == sliced_model)
        )
        matching_queues = result.all()

        # Filter to online and calculate total queue time
        candidates: list[tuple[PrinterQueue, int]] = []
        for queue, _model in matching_queues:
            state = printer_manager.get_status(queue.printer_id)
            if not state or not state.connected:
                continue

            # Current print remaining (minutes → seconds)
            current_remaining = state.remaining_time * 60 if state.remaining_time > 0 else 0

            # Pending items: COALESCE(archive.print_time_seconds,
            # library_file.file_metadata->>'print_time_seconds') so a
            # queue full of library-file dispatches still contributes
            # to the busy-score. JSON path syntax differs between
            # SQLite and Postgres — fall back to summing in Python so
            # the comparator stays portable.
            pending_rows = await db.execute(
                sa_select(
                    sa_func.coalesce(PrintArchive.print_time_seconds, 0),
                    LibraryFile.file_metadata,
                )
                .select_from(PrintQueueItem)
                .join(PrintArchive, PrintArchive.id == PrintQueueItem.archive_id, isouter=True)
                .join(LibraryFile, LibraryFile.id == PrintQueueItem.library_file_id, isouter=True)
                .where(PrintQueueItem.queue_id == queue.id, PrintQueueItem.status == "pending")
            )
            pending_time = 0
            for archive_secs, lib_meta in pending_rows.all():
                if archive_secs:
                    pending_time += int(archive_secs)
                    continue
                if isinstance(lib_meta, dict):
                    secs = lib_meta.get("print_time_seconds")
                    if isinstance(secs, int) and secs > 0:
                        pending_time += secs
            candidates.append((queue, current_remaining + pending_time))

        if not candidates:
            return None

        # Least busy first, then by printer_id for determinism
        candidates.sort(key=lambda c: (c[1], c[0].printer_id))
        return candidates[0][0]

    # -- Cert + advertise (#1070 post-rip-out) --

    def _resolve_cert_and_advertise(self) -> tuple[Path, Path, str]:
        """Return (cert_path, key_path, advertise_address) for TLS services.

        Always uses the self-signed CA flow — BambuStudio / OrcaSlicer's
        printer-MQTT trust path validates only against the bundled BBL CA
        (not the system trust store), so a publicly-trusted Let's Encrypt
        cert via ``tailscale cert`` would never validate. The Tailscale
        toggle is now informational only — when ON the VP card surfaces
        the host's Tailscale IP / MagicDNS hostname so users know which
        endpoint to paste into the slicer; the cert itself is unaffected.
        """
        cert_path, key_path = self.generate_certificates()
        advertise = self.remote_interface_ip or self.bind_ip or ""
        return cert_path, key_path, advertise

    # -- Service lifecycle --

    async def start_server(self) -> None:
        """Start server-mode services (FTP, MQTT, SSDP, Bind) on this VP's bind_ip."""
        logger.info("[VP %s] Starting server-mode services on %s", self.name, self.bind_ip)

        cert_path, key_path, advertise_addr = self._resolve_cert_and_advertise()
        bind_addr = self.bind_ip or "0.0.0.0"  # nosec B104

        async def run_with_logging(coro, svc_name):
            try:
                await coro
            except Exception as e:
                logger.error("[VP %s] %s failed: %s", self.name, svc_name, e)

        self._tasks = []

        # FTP server. Each VP gets a non-overlapping passive-mode port slice
        # derived from its DB id so bridge-mode Docker users only have to expose
        # a narrow range (#1646). Default slice is 10 ports per VP; see
        # ftp_server.compute_passive_port_slice for the wrap-around behaviour on
        # installs with very high VP ids.
        passive_port_min, passive_port_max = compute_passive_port_slice(self.id)
        self._ftp = VirtualPrinterFTPServer(
            upload_dir=self.upload_dir,
            access_code=self.access_code,
            cert_path=cert_path,
            key_path=key_path,
            on_file_received=self.on_file_received,
            bind_address=bind_addr,
            vp_name=self.name,
            on_upload_start=self.on_upload_start,
            on_upload_end=self.on_upload_end,
            passive_port_min=passive_port_min,
            passive_port_max=passive_port_max,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._ftp.start(), "FTP"),
                name=f"vp_{self.id}_ftp",
            )
        )

        # MQTT server
        self._mqtt = SimpleMQTTServer(
            serial=self.serial,
            access_code=self.access_code,
            cert_path=cert_path,
            key_path=key_path,
            on_print_command=self.on_print_command,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            bind_address=bind_addr,
            vp_name=self.name,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._mqtt.start(), "MQTT"),
                name=f"vp_{self.id}_mqtt",
            )
        )

        # MQTT bridge — fans out the target printer's pushes to slicers
        # connected to this VP and forwards their commands back to the
        # printer. Only meaningful when a target printer is configured AND
        # printer_manager was injected (it always is at runtime; tests may
        # omit it).
        if self.target_printer_id is not None and self._printer_manager is not None:
            self._mqtt_bridge = MQTTBridge(
                vp_id=self.id,
                vp_name=self.name,
                vp_serial=self.serial,
                target_printer_id=self.target_printer_id,
                mqtt_server=self._mqtt,
                printer_manager=self._printer_manager,
            )
            self._mqtt.set_bridge(self._mqtt_bridge)
            await self._mqtt_bridge.start()

            # Camera passthrough. BambuStudio / OrcaSlicer connect the "camera"
            # button to the device IP they bound on (the VP), not the IP in the
            # printer's ``ipcam.rtsp_url``. Without a listener the slicer gets
            # connection refused → "LAN connection failed" (RTSP models) or
            # OrcaSlicer error ``[2:-10061]`` (chamber-image models, #1868).
            # Same raw TCP pass-through used by SlicerProxyManager in proxy mode.
            #
            # The port depends on the TARGET printer's model:
            #   RTSPS (X1/X2/H2/P2S)        → 322
            #   chamber-image (A1/P1P/P1S)  → 6000
            #
            # ``get_camera_port()`` is the same source of truth used by
            # ``routes/camera.py``, so slicer and BamDude UI agree. Model comes
            # from the physical printer's client, NOT ``self.model`` — the VP's
            # spoofed identity has no bearing on how the real device serves its
            # camera. The ``_rtsp_proxy`` attribute name is kept for a tight diff;
            # it doubles as chamber-image passthrough on A1/P1.
            target_client = self._printer_manager.get_client(self.target_printer_id)
            target_ip = getattr(target_client, "ip_address", None) if target_client else None
            target_model = getattr(target_client, "model", None) if target_client else None
            if target_ip:
                from backend.app.services.camera import get_camera_port

                camera_port = get_camera_port(target_model)
                self._rtsp_proxy = TCPProxy(
                    name=f"Camera-{camera_port}",
                    listen_port=camera_port,
                    target_host=target_ip,
                    target_port=camera_port,
                    bind_address=bind_addr,
                )
                self._tasks.append(
                    asyncio.create_task(
                        run_with_logging(self._rtsp_proxy.start(), f"Camera-{camera_port}"),
                        name=f"vp_{self.id}_camera",
                    )
                )

        # Bind server
        self._bind = BindServer(
            serial=self.serial,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            name=self.name,
            bind_address=bind_addr,
            cert_path=cert_path,
            key_path=key_path,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._bind.start(), "Bind"),
                name=f"vp_{self.id}_bind",
            )
        )

        # SSDP server. ``advertise_addr`` is always an IP (remote_interface_ip
        # or bind_ip) — see ``_prepare_tls``. It is NEVER the tailnet FQDN: the
        # Tailscale LE-cert path was removed (#1070 post-rip-out) because the
        # slicers validate MQTT only against the bundled BBL CA, and their Add
        # Printer dialog takes an IP anyway. Tailscale is network reach only.
        self._ssdp = VirtualPrinterSSDPServer(
            name=self.name,
            serial=self.serial,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            advertise_ip=advertise_addr,
            bind_ip=bind_addr,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._ssdp.start(), "SSDP"),
                name=f"vp_{self.id}_ssdp",
            )
        )

        # Wait briefly for every child service to actually bind its socket so
        # ``is_running`` doesn't report ready while ports are still in the gap
        # between task creation and ``asyncio.start_server`` returning — a caller
        # racing the start (diagnostic route, VP-card poll, integration test)
        # would otherwise see running=True but port checks fail. Bounded 5 s: if
        # a child hangs binding we log and continue, and the existing task
        # tracking still catches the failure on the next iteration (V6, upstream
        # Bambuddy v0.2.4.5).
        ready_targets = [
            ("FTP", self._ftp.ready),
            ("MQTT", self._mqtt.ready),
            ("Bind", self._bind.ready),
            ("SSDP", self._ssdp.ready),
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(e.wait() for _, e in ready_targets)),
                timeout=5.0,
            )
        except TimeoutError:
            not_ready = [name for name, e in ready_targets if not e.is_set()]
            logger.warning(
                "[VP %s] Sub-service(s) didn't bind within 5s: %s — continuing anyway",
                self.name,
                ", ".join(not_ready) or "(none)",
            )

        logger.info("[VP %s] Server-mode services started on %s", self.name, bind_addr)

    async def stop_server(self) -> None:
        """Stop server-mode services."""
        if self._finish_release_task is not None and not self._finish_release_task.done():
            self._finish_release_task.cancel()
            self._finish_release_task = None
        if self._mqtt_bridge:
            try:
                await self._mqtt_bridge.stop()
            except Exception:
                logger.exception("[VP %s] MQTT bridge stop failed", self.name)
            if self._mqtt:
                self._mqtt.set_bridge(None)
            self._mqtt_bridge = None
        if self._rtsp_proxy:
            try:
                await self._rtsp_proxy.stop()
            except Exception:
                logger.exception("[VP %s] Camera proxy stop failed", self.name)
            self._rtsp_proxy = None
        if self._ftp:
            await self._ftp.stop()
            self._ftp = None
        if self._mqtt:
            await self._mqtt.stop()
            self._mqtt = None
        if self._bind:
            await self._bind.stop()
            self._bind = None
        if self._ssdp:
            await self._ssdp.stop()
            self._ssdp = None
        await self._cancel_tasks()

    async def start_proxy(self) -> None:
        """Start proxy mode services for this instance."""
        logger.info("[VP %s] Starting proxy mode to %s", self.name, self.target_printer_ip)

        # Self-signed cert path; the advertise_addr is unused by proxy SSDP
        # (it has its own advertise_ip wired below).
        cert_path, key_path, _ = self._resolve_cert_and_advertise()

        self._proxy = SlicerProxyManager(
            target_host=self.target_printer_ip,
            cert_path=cert_path,
            key_path=key_path,
            on_activity=lambda n, m: logger.info("[VP %s] Proxy %s: %s", self.name, n, m),
            bind_address=self.bind_ip or "0.0.0.0",  # nosec B104
            bind_identity={
                "serial": self.target_printer_serial or self.serial,
                "model": self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                "name": self.name,
                "version": "01.00.00.00",
            },
        )

        async def run_with_logging(coro, svc_name):
            try:
                await coro
            except Exception as e:
                logger.error("[VP %s] %s failed: %s", self.name, svc_name, e)

        self._tasks = []

        # SSDP for proxy
        proxy_serial = self.target_printer_serial or self.serial
        if self.remote_interface_ip:
            from backend.app.services.network_utils import find_interface_for_ip

            local_iface = find_interface_for_ip(self.target_printer_ip)
            if local_iface:
                self._ssdp_proxy = SSDPProxy(
                    local_interface_ip=local_iface["ip"],
                    remote_interface_ip=self.remote_interface_ip,
                    target_printer_ip=self.target_printer_ip,
                    name=self.name,
                )
                self._tasks.append(
                    asyncio.create_task(
                        run_with_logging(self._ssdp_proxy.start(), "SSDP Proxy"),
                        name=f"vp_{self.id}_ssdp_proxy",
                    )
                )
            else:
                self._start_fallback_ssdp(proxy_serial, run_with_logging)
        else:
            self._start_fallback_ssdp(proxy_serial, run_with_logging)

        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._proxy.start(), "Proxy"),
                name=f"vp_{self.id}_proxy",
            )
        )

    def _start_fallback_ssdp(self, proxy_serial: str, run_with_logging) -> None:
        """Start single-interface SSDP server as fallback for proxy mode."""
        self._ssdp = VirtualPrinterSSDPServer(
            name=f"{self.name} (Proxy)",
            serial=proxy_serial,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            advertise_ip=self.bind_ip or "",
            bind_ip=self.bind_ip or "",
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._ssdp.start(), "SSDP"),
                name=f"vp_{self.id}_ssdp",
            )
        )

    async def stop_proxy(self) -> None:
        """Stop proxy mode services for this instance."""
        if self._proxy:
            await self._proxy.stop()
            self._proxy = None
        if self._ssdp:
            await self._ssdp.stop()
            self._ssdp = None
        if self._ssdp_proxy:
            await self._ssdp_proxy.stop()
            self._ssdp_proxy = None
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        """Cancel all running tasks and wait for cleanup."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout=1.0)
            except TimeoutError:
                pass
        self._tasks = []

    def get_status(self) -> dict:
        """Get status for this instance."""
        status: dict = {
            "running": self.is_running,
            "pending_files": len(self._pending_files),
            "tailscale_disabled": self.tailscale_disabled,
        }
        if self.is_proxy and self._proxy:
            status["proxy"] = self._proxy.get_status()
        return status


class VirtualPrinterManager:
    """Multi-instance virtual printer registry and orchestrator.

    Every VP runs its own independent services on a dedicated bind IP.
    """

    def __init__(self):
        self._session_factory: Callable | None = None
        self._printer_manager: PrinterManager | None = None
        self._instances: dict[int, VirtualPrinterInstance] = {}
        # Serialises sync_from_db — concurrent PUT /virtual-printers/{id} routes
        # all reconcile through here; without the lock a start/stop sequence
        # races and can leave duplicate sub-services bound to the same port or
        # orphan still-running tasks (upstream Bambuddy v0.2.4.5).
        self._sync_lock = asyncio.Lock()

        # Directories
        self._base_dir = app_settings.base_dir / "virtual_printer"

        # Ensure base directories exist
        self._ensure_base_directories()

    def _ensure_base_directories(self) -> None:
        """Create base directories at startup."""
        for dir_path in [self._base_dir, self._base_dir / "uploads", self._base_dir / "certs"]:
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.error(
                    f"Cannot create directory {dir_path}: Permission denied. "
                    f"For Docker: ensure the data volume is writable by the container user. "
                    f"For bare metal: run 'sudo chown -R $(whoami) {self._base_dir}'"
                )

    def set_session_factory(self, session_factory: Callable) -> None:
        """Set the database session factory."""
        self._session_factory = session_factory

    def set_printer_manager(self, printer_manager: "PrinterManager") -> None:
        """Inject the global ``printer_manager`` so non-proxy VPs can mirror their target's MQTT stream."""
        self._printer_manager = printer_manager

    def get_ca_certificate_info(self) -> dict:
        """Return the shared virtual-printer CA certificate for slicer-trust import.

        The CA is shared by every VP (one import covers all of them). It is
        generated on demand here if no VP has triggered cert generation yet,
        so the "copy/download certificate" UI works even before the first VP
        is enabled.
        """
        certs_dir = self._base_dir / "certs"
        cert_service = CertificateService(cert_dir=certs_dir, shared_ca_dir=certs_dir)
        return cert_service.get_ca_certificate_info()

    @property
    def is_enabled(self) -> bool:
        """Check if any virtual printer is running."""
        return len(self._instances) > 0

    async def sync_from_db(self) -> None:
        """Load all VPs from DB, reconcile running state.

        Serialised by ``self._sync_lock`` — concurrent PUT /virtual-printers/{id}
        routes all reconcile here; without the lock the start/stop sequence
        races and can leave duplicate sub-services bound to the same port or
        orphan still-running tasks.
        """
        if not self._session_factory:
            logger.warning("Cannot sync virtual printers: no session factory")
            return

        async with self._sync_lock:
            await self._sync_from_db_locked()

    async def _sync_from_db_locked(self) -> None:
        """Inner sync body — caller holds ``self._sync_lock``."""
        from sqlalchemy import select

        from backend.app.models.printer import Printer
        from backend.app.models.virtual_printer import VirtualPrinter

        async with self._session_factory() as db:
            result = await db.execute(
                select(VirtualPrinter).where(VirtualPrinter.enabled == True).order_by(VirtualPrinter.position)  # noqa: E712
            )
            enabled_vps = result.scalars().all()

        # Stop instances that are no longer enabled or changed mode
        enabled_ids = {vp.id for vp in enabled_vps}
        for vp_id in list(self._instances.keys()):
            if vp_id not in enabled_ids:
                await self.remove_instance(vp_id)

        # Look up printer IPs for proxy VPs
        proxy_vps = [vp for vp in enabled_vps if vp.mode == "proxy"]
        proxy_ips: dict[int, tuple[str, str]] = {}
        if proxy_vps:
            async with self._session_factory() as db:
                for pvp in proxy_vps:
                    if pvp.target_printer_id:
                        result = await db.execute(select(Printer).where(Printer.id == pvp.target_printer_id))
                        printer = result.scalar_one_or_none()
                        if printer:
                            proxy_ips[pvp.id] = (printer.ip_address, printer.serial_number)

        # Detect config changes on running instances and restart if needed
        for vp in enabled_vps:
            instance = self._instances.get(vp.id)
            if not instance:
                continue

            # Proxy mode: detect a target-printer IP / serial change picked up by
            # the ``proxy_ips`` resolve above. Without this a DHCP renewal that
            # hands the target a new IP leaves the running proxy forwarding to
            # the stale address until the user manually toggles the VP (#1552
            # follow-up family, upstream v0.2.4.5). ``target_printer_id`` alone
            # doesn't catch it — the config still points at the same printer.
            proxy_target_changed = False
            if vp.mode == "proxy":
                fresh = proxy_ips.get(vp.id)
                if fresh is not None:
                    fresh_ip, fresh_serial = fresh
                    if instance.target_printer_ip != fresh_ip or instance.target_printer_serial != fresh_serial:
                        proxy_target_changed = True

            changed = (
                instance.mode != vp.mode
                or instance.model != (vp.model or DEFAULT_VIRTUAL_PRINTER_MODEL)
                or instance.access_code != (vp.access_code or "")
                or instance.bind_ip != (vp.bind_ip or "")
                or instance.remote_interface_ip != (vp.remote_interface_ip or "")
                or instance.target_printer_id != vp.target_printer_id
                or instance.target_folder_id != vp.target_folder_id
                or instance.auto_dispatch != vp.auto_dispatch
                or instance.queue_force_color_match != vp.queue_force_color_match
                or instance.gcode_injection != vp.gcode_injection
                or instance.save_ams_mapping != vp.save_ams_mapping
                or proxy_target_changed
                # tailscale_disabled is informational only (#1070 post-rip-out):
                # the toggle decides whether the VP card surfaces the host's
                # Tailscale IP / hostname, but does NOT change the cert path
                # (always self-signed) or the bind / advertise behaviour, so a
                # flip doesn't need a service restart. Keep the field on the
                # instance + on get_status, just don't diff it here.
            )

            if changed:
                logger.info(
                    "VP %s config changed (mode: %s→%s), restarting",
                    instance.name,
                    instance.mode,
                    vp.mode,
                )
                await self.remove_instance(vp.id)

        # Start instances for all enabled VPs (skip already running)
        for vp in enabled_vps:
            if vp.id in self._instances:
                continue

            if vp.mode == "proxy":
                ip_info = proxy_ips.get(vp.id)
                if not ip_info:
                    logger.warning("Proxy VP %s: target printer not found, skipping", vp.name)
                    continue
                target_ip, target_serial = ip_info
                instance = VirtualPrinterInstance(
                    vp_id=vp.id,
                    name=vp.name,
                    mode=vp.mode,
                    model=vp.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                    access_code=vp.access_code or "",
                    serial_suffix=vp.serial_suffix,
                    target_printer_ip=target_ip,
                    target_printer_serial=target_serial,
                    auto_dispatch=vp.auto_dispatch,
                    queue_force_color_match=vp.queue_force_color_match,
                    gcode_injection=vp.gcode_injection,
                    save_ams_mapping=vp.save_ams_mapping,
                    bind_ip=vp.bind_ip or "",
                    remote_interface_ip=vp.remote_interface_ip or "",
                    tailscale_disabled=vp.tailscale_disabled,
                    base_dir=self._base_dir,
                    session_factory=self._session_factory,
                    printer_manager=self._printer_manager,
                )
                self._instances[vp.id] = instance
                await instance.start_proxy()
                logger.info("Started proxy VP: %s → %s (bind=%s)", instance.name, target_ip, instance.bind_ip)
            else:
                instance = VirtualPrinterInstance(
                    vp_id=vp.id,
                    name=vp.name,
                    mode=vp.mode,
                    model=vp.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                    access_code=vp.access_code or "",
                    serial_suffix=vp.serial_suffix,
                    target_printer_id=vp.target_printer_id,
                    target_folder_id=vp.target_folder_id,
                    auto_dispatch=vp.auto_dispatch,
                    queue_force_color_match=vp.queue_force_color_match,
                    gcode_injection=vp.gcode_injection,
                    save_ams_mapping=vp.save_ams_mapping,
                    bind_ip=vp.bind_ip or "",
                    remote_interface_ip=vp.remote_interface_ip or "",
                    tailscale_disabled=vp.tailscale_disabled,
                    base_dir=self._base_dir,
                    session_factory=self._session_factory,
                    printer_manager=self._printer_manager,
                )
                self._instances[vp.id] = instance
                await instance.start_server()
                logger.info("Started server-mode VP: %s on %s", instance.name, vp.bind_ip)

    async def remove_instance(self, vp_id: int) -> None:
        """Stop and remove a single VP instance."""
        instance = self._instances.pop(vp_id, None)
        if instance:
            if instance.is_proxy:
                await instance.stop_proxy()
            else:
                await instance.stop_server()
            logger.info("Removed VP instance: %s", instance.name)

    async def stop_all(self) -> None:
        """Shutdown all virtual printer services."""
        logger.info("Stopping all virtual printer services...")

        for vp_id in list(self._instances.keys()):
            await self.remove_instance(vp_id)

        logger.info("All virtual printer services stopped")

    def get_instance(self, vp_id: int) -> VirtualPrinterInstance | None:
        """Get a running instance by ID."""
        return self._instances.get(vp_id)

    def get_all_status(self) -> list[dict]:
        """Get status for all running instances."""
        return [
            {
                "id": inst.id,
                "name": inst.name,
                "mode": inst.mode,
                **inst.get_status(),
            }
            for inst in self._instances.values()
        ]

    # -- Legacy single-printer compat --

    def get_status(self) -> dict:
        """Get status for first virtual printer (backward compat)."""
        if self._instances:
            first = next(iter(self._instances.values()))
            return {
                "enabled": True,
                "running": first.is_running,
                "mode": first.mode,
                "name": first.name,
                "serial": first.serial,
                "model": first.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                "model_name": VIRTUAL_PRINTER_MODELS.get(
                    first.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                    first.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                ),
                "pending_files": first.get_status().get("pending_files", 0),
                **({"target_printer_ip": first.target_printer_ip} if first.is_proxy else {}),
                **({"proxy": first.get_status().get("proxy", {})} if first.is_proxy else {}),
            }
        return {
            "enabled": False,
            "running": False,
            "mode": "file_manager",
            "name": "BamDude",
            "serial": "",
            "model": DEFAULT_VIRTUAL_PRINTER_MODEL,
            "model_name": VIRTUAL_PRINTER_MODELS[DEFAULT_VIRTUAL_PRINTER_MODEL],
            "pending_files": 0,
        }

    async def configure(
        self,
        enabled: bool,
        access_code: str = "",
        mode: str = "file_manager",
        model: str = "",
        target_printer_ip: str = "",
        target_printer_serial: str = "",
        remote_interface_ip: str = "",
    ) -> None:
        """Legacy single-printer configure. Delegates to sync_from_db()."""
        # This method is kept for backward compat with the settings endpoint.
        # The actual work is done by sync_from_db() which reads from the DB.
        await self.sync_from_db()


# Global instance
virtual_printer_manager = VirtualPrinterManager()
