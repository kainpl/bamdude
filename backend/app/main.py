import asyncio
import inspect
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, or_, select, text, update

from backend.app.api.routes import (
    ams_history,
    ams_settings as ams_settings_routes,
    api_keys,
    archive_purge,
    archives,
    auth,
    auto_queue,
    background_dispatch as background_dispatch_routes,
    bug_report,
    camera,
    cameras,
    camwall,
    cloud,
    cloud_link,
    customers,
    delivery_methods,
    discovery,
    external_links,
    filament_calibration as filament_calibration_routes,
    filament_families as filament_families_routes,
    firmware,
    git_backup,
    groups,
    ha_sensors,
    hms as hms_routes,
    inbox,
    inventory,
    kprofiles,
    label_devices,
    label_templates,
    labels,
    library,
    library_assembly,
    library_notes,
    library_photos,
    library_tags,
    library_thumbnail_jobs,
    library_trash,
    local_backup,
    local_presets,
    location_ha_sensors,
    macros,
    maintenance,
    makerworld,
    measurement_history,
    metrics,
    mfa,
    model_sources,
    monitor,
    notification_templates,
    notifications,
    obico,
    octoeverywhere,
    orca_cloud,
    overlay_branding,
    part_images as part_images_routes,
    print_options_preferences,
    print_queue,
    print_state_recovery,
    printer_locations,
    printer_queues,
    printer_sensor_history,
    printer_settings as printer_settings_routes,
    printer_tags,
    printers,
    product_categories,
    products,
    projects,
    scheduled_drying as scheduled_drying_routes,
    settings as settings_routes,
    slice_jobs,
    slicer_pipelines,
    slicer_presets,
    smart_plugs,
    spoolman,
    spoolman_inventory,
    spoolman_usage,
    statistics,
    stock,
    stock_issues,
    support,
    system,
    telegram,
    updates,
    user_notifications,
    users,
    virtual_printers,
    webhook,
    websocket,
    zigbee,
)
from backend.app.api.routes.maintenance import _get_printer_maintenance_internal, ensure_default_types
from backend.app.api.routes.support import init_debug_logging
from backend.app.core import process_gc, process_resources, query_timing
from backend.app.core.config import APP_VERSION, settings as app_settings
from backend.app.core.database import async_session, engine, init_db
from backend.app.core.tasks import spawn_background_task
from backend.app.core.websocket import ws_manager
from backend.app.i18n.api_errors import install as install_api_error_translation
from backend.app.models.smart_plug import SmartPlug
from backend.app.services import swap_compatibility
from backend.app.services.ams_slot_presence import spool_present
from backend.app.services.archive import ArchiveService
from backend.app.services.auto_queue_scheduler import auto_queue_scheduler
from backend.app.services.background_dispatch import background_dispatch, delete_internal_by_name
from backend.app.services.bambu_mqtt import HMS_SEVERITY_NOTIFY_THRESHOLD, HMSError, PrinterState
from backend.app.services.forecast_engine import ForecastSourceUnavailable
from backend.app.services.git_backup import git_backup_service
from backend.app.services.ha_sensor_manager import ha_sensor_manager
from backend.app.services.hms_catalogue import device_of as hms_device_of
from backend.app.services.local_backup import local_backup_service
from backend.app.services.location_ha_sensor_manager import location_ha_sensor_manager
from backend.app.services.mqtt_relay import mqtt_relay
from backend.app.services.mqtt_smart_plug import mqtt_smart_plug_service
from backend.app.services.notification_service import notification_service
from backend.app.services.notify_live_activities import PrintSnapshot as NotifyPrintSnapshot, notify_live_activities
from backend.app.services.notify_widgets import notify_widgets
from backend.app.services.print_scheduler import scheduler as print_scheduler
from backend.app.services.printer_manager import (
    init_printer_connections,
    parse_plate_id,
    printer_manager,
    printer_state_to_dict,
)
from backend.app.services.smart_plug_manager import smart_plug_manager
from backend.app.services.spool_assignment_notifications import (
    notify_missing_spool_assignments_on_print_start,
)
from backend.app.services.spoolman import (
    ArchivedTagIndex,
    close_spoolman_client,
    get_spoolman_client,
    init_spoolman_client,
)
from backend.app.services.spoolman_tracking import (
    cleanup_tracking as _cleanup_spoolman_tracking,
    report_usage as _report_spoolman_usage,
    store_print_data as _store_spoolman_print_data,
)
from backend.app.services.stock_forecast_alerts import stock_forecast_alerts

# ⚠️ One home for finding AND reading a printer's recordings. There were three
# copies of "walk these directories over FTP" — here and twice in
# routes/archives.py — and two copies of the download beside them. Consolidating
# only the listing was worse than useless for a while: the auto-scan found the
# recording in internal storage and then tried to fetch it over FTP.
from backend.app.services.timelapse_files import (
    last_listing_answered as _last_listing_answered,
    last_listing_media as _last_listing_media,
    last_recording_path as _last_recording_path,
    list_timelapse_videos as _list_timelapse_videos,
    pick_new_recording as _pick_new_recording,
    read_timelapse_video,
    recording_key as _recording_key,
    remote_source,
    remove_recording_after_attach,
)
from backend.app.services.wled import wled_manager
from backend.app.utils import tray_ids
from backend.app.utils.ams_humidity import ams_humidity_percent
from backend.app.utils.failure_reasons import OTHER as FAILURE_OTHER, USER_CANCELLED
from backend.app.utils.filament_remaining import grams_used
from backend.app.utils.filament_types import support_slot_type
from backend.app.utils.print_jobs import is_internal_printer_job


# =============================================================================
# Dependency Check - runs before other imports to give helpful error messages
# =============================================================================
def _start_error_server(missing_packages: list):
    """Start a minimal HTTP server to display dependency errors in browser."""
    import os
    import signal
    from http.server import BaseHTTPRequestHandler, HTTPServer

    packages_html = "".join(f"<li><code>{p}</code></li>" for p in missing_packages)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>BamDude - Setup Required</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a; color: #e2e8f0;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box;
        }}
        .container {{
            background: #1e293b; border-radius: 12px; padding: 40px;
            max-width: 600px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }}
        h1 {{ color: #f87171; margin-bottom: 10px; }}
        h2 {{ color: #94a3b8; font-weight: normal; margin-top: 0; }}
        .packages {{
            background: #0f172a; border-radius: 8px; padding: 20px;
            margin: 20px 0; text-align: left;
        }}
        .packages ul {{ margin: 0; padding-left: 20px; }}
        .packages li {{ color: #fbbf24; margin: 8px 0; }}
        .command {{
            background: #0f172a; border-radius: 8px; padding: 15px 20px;
            margin: 15px 0; font-family: monospace; color: #4ade80;
            text-align: left; overflow-x: auto;
        }}
        .note {{ color: #94a3b8; font-size: 14px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Setup Required</h1>
        <h2>Missing Python packages</h2>
        <div class="packages"><ul>{packages_html}</ul></div>
        <p>To fix, run this command on your server:</p>
        <div class="command">pip install -r requirements.txt</div>
        <p>Or if using a virtual environment:</p>
        <div class="command">./venv/bin/pip install -r requirements.txt</div>
        <p class="note">After installing, restart BamDude:<br>
        <code>sudo systemctl restart bamdude</code></p>
    </div>
</body>
</html>"""

    class ErrorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, format, *args):
            print(f"[Error Server] {args[0]}")

    port = int(os.environ.get("PORT", 8000))
    print(f"\nStarting error server on http://0.0.0.0:{port}")
    print("Visit this URL in your browser to see the error details.\n")

    server = HTTPServer(("0.0.0.0", port), ErrorHandler)  # nosec B104

    def shutdown(signum, frame):
        print("\nShutting down error server...")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()


def check_dependencies():
    """Check that all required packages are installed."""
    missing = []

    # Map of import name -> package name (for pip install)
    required = {
        "jwt": "PyJWT",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "sqlalchemy": "sqlalchemy",
        "aiosqlite": "aiosqlite",
        "pydantic": "pydantic",
        "paho.mqtt": "paho-mqtt",
    }

    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print("\n" + "=" * 60)
        print("ERROR: Missing required Python packages!")
        print("=" * 60)
        print(f"\nMissing packages: {', '.join(missing)}")
        print("\nTo fix, run:")
        print("  pip install -r requirements.txt")
        print("\nOr if using a virtual environment:")
        print("  ./venv/bin/pip install -r requirements.txt")
        print("=" * 60 + "\n")
        _start_error_server(missing)


check_dependencies()
# =============================================================================


# Import settings first for logging configuration

# Configure logging - LOG_LEVEL env var controls the level directly
log_level_str = app_settings.log_level.upper()
log_level = getattr(logging, log_level_str, logging.INFO)
# Trace ID column ([-] when no request scope is active — startup, MQTT
# callbacks, scheduled tasks not chained from a request — so the column
# stays visually aligned and missing values are obvious in grep). See
# backend/app/core/trace.py for the ContextVar that feeds this slot.
log_format = "%(asctime)s %(levelname)s [%(name)s] [%(trace_id)s] %(message)s"

# Create root logger
root_logger = logging.getLogger()
root_logger.setLevel(log_level)

# Trace-ID injection: this filter populates record.trace_id from the
# per-request ContextVar so the format string above can reference it.
# Attached to each HANDLER (not the root logger) because Python's
# logging semantics only invoke a logger's filters on records that
# *originated* at that logger — records propagated up from child
# loggers (every named logger in the app) never trigger root's filter.
# Putting it on the handlers means every record any handler emits gets
# trace_id injected just before the formatter runs, regardless of which
# logger created the record. Without this, the formatter raises
# KeyError on every child-logger record and the stdlib logging machinery
# silently drops it — exactly the "logs/bamdude.log only shows logs
# partially" failure mode (audit A.28). See backend/app/core/trace.py.
from backend.app.core.logging_filters import CredentialSafeFormatter  # noqa: E402
from backend.app.core.trace import TraceIDFilter  # noqa: E402

_trace_id_filter = TraceIDFilter()


def _protect_uvicorn_log_handlers() -> None:
    # Uvicorn can own non-propagating console handlers and configure them again
    # after importing the app. Preserve their formatters, sanitize final output.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(CredentialSafeFormatter.wrap(handler.formatter))


_protect_uvicorn_log_handlers()

# Console handler - always enabled
console_handler = logging.StreamHandler()
console_handler.setLevel(log_level)
console_handler.setFormatter(CredentialSafeFormatter(log_format))
console_handler.addFilter(_trace_id_filter)
root_logger.addHandler(console_handler)

# File handler - only in production or if explicitly enabled.
# Daily rotation at midnight (operator local time). Live file is
# ``logs/bamdude.log``; rotated archives land as
# ``bamdude-YYYY-MM-DD.log`` (date-in-stem via custom namer, see
# ``logging_state.app_log_filename_namer``). The bootstrap retention
# of 7 days is overridden at lifespan startup from the DB-backed
# ``log_retention_days`` setting (Settings page → Data Management),
# so the operator-facing knob lives where they'd expect it.
if app_settings.log_to_file:
    from backend.app.core.logging_state import (  # noqa: E402
        WindowsSafeTimedRotatingFileHandler,
        app_log_filename_namer,
        set_app_log_handler,
    )

    log_file = app_settings.log_dir / "bamdude.log"
    file_handler = WindowsSafeTimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        # Bootstrap value — actual retention is read from DB at lifespan
        # startup. Hard-coded fallback covers the boot window before the
        # DB is ready, plus fresh installs that never set the value.
        backupCount=7,
        encoding="utf-8",
        utc=False,
    )
    # Suffix override — stdlib defaults to ``%Y-%m-%d_%H-%M-%S`` which is
    # noisier than midnight-only rotations need. Date-only suffix +
    # custom namer produce ``bamdude-YYYY-MM-DD.log`` (date-in-stem,
    # ``.log`` extension preserved).
    file_handler.suffix = "%Y-%m-%d"
    file_handler.namer = app_log_filename_namer
    file_handler.setLevel(log_level)
    file_handler.setFormatter(CredentialSafeFormatter(log_format))
    file_handler.addFilter(_trace_id_filter)
    root_logger.addHandler(file_handler)
    set_app_log_handler(file_handler)
    logging.info("Logging to file: %s (rotated daily at midnight)", log_file)

    # Pipe uvicorn's HTTP access log to bamdude.log too. Uvicorn ships its
    # access logger with propagate=False by default, so without this attach
    # there is no on-disk record of which endpoint triggered a server-state
    # change — leaving incident triage to eyeball-correlate timestamps
    # across separate streams. Filtered to write methods only
    # (POST/PUT/PATCH/DELETE) so the high-volume status-poll GETs from the
    # frontend don't churn the rotation window faster than it's useful.
    #
    # The filter goes on the FILE HANDLER, not on the access logger. A
    # logger-level filter runs before any handler, so on the logger it also
    # stripped GETs from the console — which does not rotate, and where
    # someone watching the server wants to see them. It cost real diagnostic
    # time once: an empty stretch of access lines read as "the server served
    # nothing", when the GET polling behind it was simply invisible by
    # design. On the handler the file stays trimmed and the console stays
    # complete.
    from backend.app.core.logging_filters import WriteRequestsOnlyFilter  # noqa: E402

    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addHandler(file_handler)
    file_handler.addFilter(WriteRequestsOnlyFilter())
    # Uvicorn's access logger has propagate=False (its own default), so
    # the root-attached TraceIDFilter never sees these records. Attach a
    # second filter instance directly to the access logger so HTTP access
    # lines carry the same trace ID column as the application logs they
    # correlate with.
    uvicorn_access_logger.addFilter(TraceIDFilter())

# The native download door carries a one-time grant in its path (G10 D). Masked on the access
# LOGGER whatever the configuration: a console-only install logs the same line.
from backend.app.core.logging_filters import DownloadGrantFilter  # noqa: E402

logging.getLogger("uvicorn.access").addFilter(DownloadGrantFilter())

# HTTP wire logs contain opaque capability paths/headers that a generic
# formatter cannot recognize, even in debug mode. Adapters retain safe
# destination/status diagnostics instead.
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Reduce other third-party noise in production
if not app_settings.debug:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("paho.mqtt").setLevel(logging.WARNING)

# Drop SQLAlchemy pool noise driven by Starlette's BaseHTTPMiddleware
# cancellation propagation on client disconnect (#1112 follow-up).
from backend.app.core.logging_filters import CancelledPoolNoiseFilter  # noqa: E402

logging.getLogger("sqlalchemy.pool").addFilter(CancelledPoolNoiseFilter())

logging.info("BamDude starting - debug=%s, log_level=%s", app_settings.debug, log_level_str)


# Track active prints: {(printer_id, filename): archive_id}
_active_prints: dict[tuple[int, str], int] = {}
_active_print_identities: dict[tuple[int, str], str] = {}


def _remember_active_print(key, archive):
    _active_prints[key] = archive.id
    _active_print_identities[key] = archive.recovery_identity


def _forget_active_print(archive_id, identity):
    for key in [
        key
        for key, value in _active_prints.items()
        if value == archive_id and _active_print_identities.get(key) == identity
    ]:
        _active_prints.pop(key, None)
        _active_print_identities.pop(key, None)


# #1721: stage-22 pre-captured finish photo bytes per printer. on_finish_photo_moment
# fires when stg_cur enters 22 ("Filament unloading") at end-of-print — toolhead
# parked, bed not yet dropped — and grabs a single camera frame into this cache.
# `_background_finish_photo` (inside on_print_complete) consumes the cached bytes
# instead of running its own grab-now chain when present, so the finish photo
# captures the better-framed pre-bed-drop moment without us having to force
# timelapse on at dispatch (the #1397 mechanism that caused #1721's per-layer
# nozzle parking on slicer profiles with Timelapse Type = Smooth).
#
# The bytes in here are ALWAYS already rotated by the printer's
# ``camera_rotation``. ``on_finish_photo_moment`` owns that, because one of its
# sources (the #1867 in-print bank) is rotated before it ever reaches the bank
# and the others are not — so a consumer cannot tell them apart and must not
# rotate again (#2708).
_stage22_finish_frames: dict[tuple[int, int], bytes] = {}

# #1790: per-printer producer-done event. Set by `on_finish_photo_moment` in its
# `finally` (whether or not a frame was captured). The consumer in
# `_background_finish_photo` waits on it before reading `_stage22_finish_frames`, so
# the FINISH-state fallback path (moment + completion dispatched back-to-back) can't
# race past the producer with an empty pop, and the consumer's RTSP fallback can't
# collide with the producer's in-flight grab (Bambu allows one RTSP client).
_stage22_finish_in_flight: dict[tuple[int, int], asyncio.Event] = {}

# #1867 follow-on: rolling "last in-print camera frame" per printer. Refreshed on
# layer change while the model is still printing, then consumed by the
# FINISH-state finish-photo path. Firmware that never emits ``stg_cur=22``
# (A1 Mini, confirmed) only reaches ``on_finish_photo_moment`` at the
# gcode_state=FINISH transition — which Bambu reports AFTER the user End G-code
# (e.g. a SwapMod plate swap) has run, so a live grab there captures the swapped
# or empty plate. The last-layer edge in bambu_mqtt.py is the right window but
# depends on catching one transient MQTT packet. Banking is layer-driven, so it
# freezes at the final object layer: the End G-code emits no further layer_num
# increases, so the last banked frame is the finished print.
_inprint_frame_bank: dict[tuple[int, int], bytes] = {}
# Monotonic timestamp of the last banking ATTEMPT per printer — throttles banking
# so tall prints don't add a camera grab on every layer, and so a disabled or
# failing capture backs off instead of retrying on every layer.
_inprint_frame_bank_ts: dict[tuple[int, int], float] = {}
# Printers with a banking grab in flight — a second concurrent grab would open a
# competing camera socket (single-client RTSP / port-6000 chamber stream).
_inprint_bank_in_flight: set[tuple[int, int]] = set()
# Minimum seconds between banking attempts, except the final object layer which
# always refreshes for the best framing.
_INPRINT_BANK_MIN_INTERVAL = 25.0

# Track expected prints from reprint/scheduled (skip auto-archiving for these)
# {(printer_id, filename): archive_id}
_expected_prints: dict[tuple[int, str], int] = {}

# Track AMS mapping for prints: {archive_id: [global_tray_id_per_slot]}
# Used by usage tracker to map 3MF slots to physical AMS trays
_print_ams_mappings: dict[int, list[int]] = {}
_print_ams_mapping_identities: dict[int, str] = {}

# Swap-macro configuration for active prints: {printer_id: {"swap_macro_events": [...]}}
# Populated at dispatch time; consumed + cleaned up in on_print_complete.
# Only one print is active per printer at a time, so printer_id is a safe key.
_active_swap_config: dict[int, dict] = {}

# Which macros the operator ticked for the print currently on each printer.
# ``{printer_id: [macro_id, ...]}``. Registered at dispatch, read by the macro
# triggers, popped in on_print_complete.
#
# This exists because the archive copy cannot cover the whole window:
# ``fire_event_macros("print_started")`` fires several hundred lines before
# ``on_print_start`` gets round to creating or adopting the archive, so an
# archive-only lookup would find nothing and print_started macros would never
# run at all.
_active_macro_selection: dict[int, list[int]] = {}

# Track progress milestones for notifications: {printer_id: last_milestone_notified}
# Milestones are 25, 50, 75. Value of 0 means no milestone notified yet for current print.
_last_progress_milestone: dict[int, int] = {}


def _estimated_total_print_minutes(progress: int, remaining_minutes: int | None) -> float | None:
    """Whole-print duration estimated at a progress point, in minutes.

    ``remaining / (1 - progress/100)`` — at 25% with 90 min left the print is
    a 120-minute job. Stateless on purpose (#28): capturing the total at print
    start would need a per-print store that must survive restarts, while this
    is answerable at every milestone crossing from the live state alone, and
    for a "longer than N minutes" gate its precision is ample. ``None`` when
    the inputs can't answer (no remaining time reported, or progress at an
    edge) — callers treat that as "don't gate".
    """
    if not remaining_minutes or remaining_minutes < 0 or progress <= 0 or progress >= 100:
        return None
    return remaining_minutes / (1.0 - progress / 100.0)


# Track whether first layer complete notification has been sent for current print.
# A cache: the fact itself is the run's (``services/first_layer_announcement``), claimed on its execution archive
# before the send and seeded back here where a run is bound, so a restart cannot announce a print twice.
_first_layer_notified: dict[int, bool] = {}
# The restart recoveries (``on_print_running_observed``) still looking their run up, per printer. A layer edge
# that overtakes one waits for it: until the run is bound nothing can say whether it was announced.
_running_print_adoptions: dict[int, list[asyncio.Event]] = {}
_RUN_ADOPTION_WAIT_SECONDS = 30.0

# HMS faults already announced, with when each was last seen:
# {printer_id: {fault key: timestamp}}. Kept per FAULT, not per printer: one set
# for the whole stack was forgotten only when the stack emptied, so a fault that
# flickers beside one that stays was announced on every return (upstream #3226).
# The window is long because measured gaps reach 64 s. A restored standing fault
# has no last-seen timestamp until the printer reports its source.
_notified_hms_errors: dict[int, dict[str, float | None]] = {}
_HMS_CLEAR_GRACE_SECONDS = 600.0
# The print state each printer was last seen in, and the faults recorded before
# it last changed -- those are forgotten as soon as they are gone, without the
# window (see ``_take_new_hms_fault_keys``).
_hms_print_state: dict[int, str] = {}
_hms_forget_when_gone: dict[int, set[str]] = {}

# How long post-print housekeeping on the printer may take before it is
# abandoned. Generous enough for a slow A1 over FTP, short enough that a
# machine which stops answering cannot leave the sweep running into the next
# print on the same printer — where the names it deletes by would belong to
# that job. Deleting nothing is a file left behind; deleting late is a file
# taken from somebody else's print.
_POST_PRINT_CLEANUP_BUDGET = 120.0

# Track timelapse file baselines at print start: {printer_id: set of video filenames}
# Used for snapshot-diff detection at print completion
_timelapse_baselines: dict[int, set[str]] = {}
# Printers whose baseline above was taken off a card that did not answer (audit
# D6 part 4): recorded all the same — the usual card holds one recording at
# completion, and an empty baseline resolves it — but the completion scan then
# refuses to guess between several new recordings.
_untrusted_timelapse_baselines: set[int] = set()
# Whose baseline that is: (archive incarnation, process storage epoch) of the
# capture. A numeric printer id alone would hand one print's list to the next
# print — or to the next printer — that got the id (G10 C3).
_timelapse_baseline_owners: dict[int, tuple[str | None, int]] = {}

# Track active bed cooldown monitoring tasks: {printer_id: asyncio.Task}
_bed_cooldown_tasks: dict[int, asyncio.Task] = {}


def has_bed_cooldown(printer_id: int) -> bool:
    """Read-only: a completion's bed-cooldown watch is still running for this printer."""
    task = _bed_cooldown_tasks.get(printer_id)
    return task is not None and not task.done()


async def stop_bed_cooldown_tasks(printer_id: int | None = None) -> None:
    tasks = {task for owner, task in _bed_cooldown_tasks.items() if printer_id is None or owner == printer_id}
    for task in tasks:
        task.cancel()
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=5)
        for task in pending:
            task.cancel()


# Track an explicit queue-UI stop by its execution archive when the manager has
# one.  A bare printer-id flag used to let terminal A reclassify a later B as
# ``cancelled`` after a rapid restart on the same printer.
_user_stopped_printers: dict[int, int | None] = {}


# HMS short-code → human-readable failure reason. Used by on_print_complete when
# status="failed" to label the print's failure_reason in archives.
#
# Earlier code matched on `module` alone (e.g. "any module 0x0C HMS → Layer shift"),
# which was wrong on two counts:
#   1. Real layer-shift codes live in module 0x03 (per Bambu wiki), not 0x0C.
#   2. Module 0x0C is "Motion Controller" — a broad category that also covers
#      cameras, visual markers, AND the H2D firmware emits 0x0C HMS codes
#      (e.g. 0C00_001B) as part of its user-cancel sequence. Matching on the
#      module alone caused user-cancellations to be archived as "Layer shift"
#      failures.
# We now match by full short code only — anything not in this map leaves
# failure_reason=None rather than guessing.
#
# Values are KEYS from utils/failure_reasons, not display labels (upstream
# #2974): the Failure Analysis widget groups on the raw value, so a label here
# was a second bucket beside the key the editor stores, and could never be
# translated.
_HMS_FAILURE_REASONS: dict[str, str] = {
    # Layer shift / step loss
    "0300_4057": "layerShift",
    "0300_4068": "layerShift",
    "0300_800C": "layerShift",
    # Filament runout (printer-side & per-AMS-slot)
    "0300_8004": "filamentRunout",
    "0700_8011": "filamentRunout",
    "0701_8011": "filamentRunout",
    "0702_8011": "filamentRunout",
    "0703_8011": "filamentRunout",
    "0704_8011": "filamentRunout",
    "0705_8011": "filamentRunout",
    "0706_8011": "filamentRunout",
    "0707_8011": "filamentRunout",
    "07FF_8011": "filamentRunout",
    # Clogged nozzle / extruder
    "0300_4006": "cloggedNozzle",
    "0300_8016": "cloggedNozzle",
    "0300_801C": "cloggedNozzle",
    "0700_8003": "cloggedNozzle",
    "0700_8007": "cloggedNozzle",
    "0700_8013": "cloggedNozzle",
    "0701_8003": "cloggedNozzle",
    "0701_8007": "cloggedNozzle",
    "0701_8013": "cloggedNozzle",
    "0702_8003": "cloggedNozzle",
    # The printer's own AI monitor halting the print: spaghetti / the model off
    # the plate (upstream a6ce26205). ``spaghettiDetached`` is a key the archive
    # editor already offers. 0C00_8042 is a FULL short code with a documented
    # meaning, so the module-0x0C warning above does not apply (the H2D cancel
    # echo is 0C00_001B). 0300_8003 arrives with the print already paused,
    # offering only "resume with defects" / "stop".
    # Left out on purpose: 0C00_C004 ("possible spaghetti" -- a warning about a
    # print still running) and 0300_800A (a pile-up in the waste chute -- not
    # the print failing).
    "0300_8003": "spaghettiDetached",
    "0C00_8042": "spaghettiDetached",
}


def _hms_short_code(attr: int, code: int | str) -> str:
    """Build the canonical "MMMM_CCCC" HMS short code from raw attr/code values."""
    if isinstance(code, str):
        code_int = int(code.replace("0x", ""), 16) if code else 0
    else:
        code_int = int(code or 0)
    attr_int = int(attr or 0)
    return f"{(attr_int >> 16) & 0xFFFF:04X}_{code_int & 0xFFFF:04X}"


def _hms_notify_key(error) -> str:
    """What identifies a fault for the announcement memory: its full code.

    ``attr`` alone is unique for a ``print_error``, but for an ``hms[]`` fault it
    is only the module and the part, so two faults on one part shared a key and
    the second was never announced -- and a quiet one that came first silenced a
    fatal one (upstream #2728).
    """
    full_code = getattr(error, "full_code", "") or ""
    if full_code:
        return full_code.upper()
    return f"{int(error.attr or 0):08X}:{error.code}"


def _take_new_hms_fault_keys(
    printer_id: int,
    errors: Sequence[HMSError],
    print_state: str | None = None,
    *,
    reported: tuple[bool, bool] = (True, True),
    cleared: set[str] | None = None,
    now: float | None = None,
) -> set[str]:
    """The faults on this printer not announced yet; records them as announced.

    The memory is written before anything is sent, so two status callbacks
    cannot announce one fault twice. A fault is forgotten once it has been gone
    for ``_HMS_CLEAR_GRACE_SECONDS``: one that flickers is announced once, one
    that returns much later is announced again.

    When ``print_state`` changes (a print resumed, started, finished), every
    fault recorded so far is forgotten the first time it is gone, without the
    wait -- a runout fixed before resuming must be announced when it happens
    again a minute later. Not only the faults gone at the change itself: the
    frame that flips the state can still carry the old fault.
    """
    now = time.time() if now is None else now
    current = {_hms_notify_key(e) for e in errors}
    seen = _notified_hms_errors.setdefault(printer_id, {})
    forget_when_gone = _hms_forget_when_gone.setdefault(printer_id, set())

    if print_state:
        print_state = print_state.upper()
        if _hms_print_state.get(printer_id, print_state) != print_state:
            forget_when_gone.update(seen)
        _hms_print_state[printer_id] = print_state

    for key, last_seen in list(seen.items()):
        if key not in current and not reported[len(key) == 8]:
            continue
        if last_seen is None:
            last_seen = seen[key] = now
        # A gap between reports does not clear a fault known to be standing.
        absent = key not in current
        expired = now - last_seen >= _HMS_CLEAR_GRACE_SECONDS and (absent or cleared is None or key in cleared)
        if expired or (absent and key in forget_when_gone):
            del seen[key]
            forget_when_gone.discard(key)

    new = current - seen.keys()
    for key in current:
        seen[key] = now
    if not seen:
        _notified_hms_errors.pop(printer_id, None)
    if not forget_when_gone:
        _hms_forget_when_gone.pop(printer_id, None)
    return new


@dataclass
class _HmsNotificationStorage:
    lock: asyncio.Lock
    recorded: dict[str, float | None] | None = None


_hms_notification_storage: dict[int, _HmsNotificationStorage] = {}


async def _record_hms_faults(
    printer_id: int,
    errors: Sequence[HMSError],
    print_state: str | None,
    reported: tuple[bool, bool],
    *,
    muted_errors: Sequence[HMSError] = (),
    now: float | None = None,
) -> set[str]:
    """Keep announced faults across a restart without treating partial reports as clears."""
    if not printer_manager.session_still_lives(printer_id) or not (errors or any(reported)):
        return set()

    from backend.app.models.printer import Printer

    life = printer_manager.printer_life(printer_id)
    storage = _hms_notification_storage.get(printer_id)
    if storage is None:
        storage = _hms_notification_storage[printer_id] = _HmsNotificationStorage(asyncio.Lock())
    async with storage.lock:
        if not printer_manager.session_still_lives(printer_id) or not printer_manager.life_holds(printer_id, life):
            return set()
        if storage.recorded is None:
            try:
                async with async_session() as db:
                    row = (
                        await db.execute(select(Printer.hms_notified_faults).where(Printer.id == printer_id))
                    ).one_or_none()
                if not printer_manager.life_holds(printer_id, life):
                    return set()
                if row is None:
                    return set()
                storage.recorded = row[0] or {}
                if storage.recorded:
                    seen = _notified_hms_errors.setdefault(printer_id, {})
                    for key, observed_at in storage.recorded.items():
                        seen.setdefault(key, observed_at)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Could not load HMS notification state for printer %s", printer_id, exc_info=True
                )
                if not printer_manager.life_holds(printer_id, life):
                    return set()

        now = time.time() if now is None else now
        known = _notified_hms_errors.get(printer_id, {})
        if muted_errors and reported[0]:
            # Hiding a standing fault is not a clear. A fault first seen
            # muted stays eligible if the operator later chooses to show it.
            errors = [*errors, *(error for error in muted_errors if _hms_notify_key(error) in known)]
        stored = storage.recorded or {}
        cleared = {key for key, last_seen in stored.items() if last_seen is not None}
        new = _take_new_hms_fault_keys(printer_id, errors, print_state, reported=reported, cleared=cleared, now=now)
        current = {_hms_notify_key(error) for error in errors}
        # A standing fault has no expiry. Once its source reports it absent,
        # retain its last-seen time through a restart for the existing grace.
        active = _notified_hms_errors.get(printer_id, {})
        saved = {
            key: None if key in current else last_seen if reported[len(key) == 8] else stored.get(key)
            for key, last_seen in active.items()
        }
        if saved != stored:
            try:
                async with async_session() as db:
                    if not await printer_manager.hold_record(db, printer_id, life=life):
                        return set()
                    await db.execute(
                        update(Printer)
                        .where(Printer.id == printer_id)
                        .values(hms_notified_faults=saved, updated_at=Printer.updated_at)
                    )
                    await db.commit()
                storage.recorded = saved
            except Exception:
                # Preserve in-process deduplication and retry persistence on a later report.
                logging.getLogger(__name__).warning(
                    "Could not persist HMS notification state for printer %s", printer_id, exc_info=True
                )
        return new if printer_manager.life_holds(printer_id, life) else set()


def _hms_label(full_code: str | None, short_code: str) -> str:
    """How a fault is named to a person.

    An ``hms[]`` fault is named by its full code in the printer screen's four
    groups: its short form drops the part and the level, so ``0500_0070`` read
    as a code nobody could look up. A ``print_error`` keeps the short form, which
    IS what the printer shows for it.
    """
    full_code = full_code or ""
    if len(full_code) == 16:
        return "-".join(full_code[i : i + 4] for i in range(0, 16, 4)).upper()
    return short_code


def derive_failure_reason(
    status: str, hms_errors: list[dict] | None, *, cancelled_on_printer: bool = False
) -> str | None:
    """Derive the failure_reason KEY for an archived print (see utils/failure_reasons).

    Returns "userCancelled" for cancelled/aborted prints; for failed prints,
    returns the first matching reason from _HMS_FAILURE_REASONS, or None when
    no HMS code matches (don't guess — null is honest).

    ``cancelled_on_printer``: the firmware said a person cancelled this print on the printer
    (``bambu_mqtt``'s cancel echo, no fault beside it). The print stays ``failed``; this is its reason.
    """
    if status in ("aborted", "cancelled"):
        return USER_CANCELLED
    if status != "failed":
        return None
    if cancelled_on_printer:
        return USER_CANCELLED
    for err in hms_errors or []:
        short_code = _hms_short_code(err.get("attr", 0), err.get("code", 0))
        if short_code in _HMS_FAILURE_REASONS:
            return _HMS_FAILURE_REASONS[short_code]
    return None


# Track created_by_id for expected prints so the user email can be sent even when
# the archive itself doesn't have created_by_id set (e.g. library-file-based prints).
# {(printer_id, filename): created_by_id}
_expected_print_creators: dict[tuple[int, str], int] = {}

# TTL for expected-print entries: evict registrations older than this to prevent
# unbounded growth when a print is registered but never starts (e.g. printer
# disconnect, app restart, print started from the printer panel).
_EXPECTED_PRINT_TTL_SECONDS: int = 2 * 60 * 60  # 2 hours

# Registration timestamps used for TTL eviction: {(printer_id, filename): monotonic_time}
_expected_print_registered_at: dict[tuple[int, str], float] = {}

# Cleanup loop interval
_EXPECTED_PRINT_CLEANUP_INTERVAL: int = 15 * 60  # 15 minutes
_expected_prints_cleanup_task: asyncio.Task | None = None
_label_reclaim_task: asyncio.Task | None = None


# Per-printer lock that serialises the spool-assignment block of
# `on_ams_change` (auto-unlink stale + auto-assign new) when MQTT bursts
# deliver multiple AMS updates for the same printer in quick succession
# (~30 ms apart, observed in the wild on H2D + dual AMS).
#
# Without this serialisation, two concurrent on_ams_change callbacks each
# read "no assignment for (printer, ams, tray)", each call auto_assign_spool,
# and the second commit hits
#   IntegrityError: duplicate key value violates unique constraint
#                   "spool_assignment_printer_id_ams_id_tray_id_key"
# SQLite's WAL serial-write semantics had been silently swallowing the race
# until optional Postgres support landed (asyncpg allows true concurrent
# transactions and surfaces the constraint violation).
#
# Scope is intentionally narrow: only the auto-assign block is inside the
# lock. The Spoolman sync (network-bound, idempotent) stays outside.
# Per-printer scope keeps unrelated printers fully parallel.
_ams_assignment_locks: dict[int, asyncio.Lock] = {}


def _get_ams_assignment_lock(printer_id: int) -> asyncio.Lock:
    """Return the per-printer assignment lock, creating it on first use."""
    lock = _ams_assignment_locks.get(printer_id)
    if lock is None:
        lock = asyncio.Lock()
        _ams_assignment_locks[printer_id] = lock
    return lock


# Per-printer dedup for unknown_tag WS broadcasts. Keyed by
# (ams_id, tray_id) -> (tag_uid, tray_uuid); we only re-broadcast when the
# tag tuple changes for the slot. Cleared when the slot is reported empty
# so remove + reinsert reliably re-prompts the UI.
_unknown_tag_last_broadcast: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}


async def _broadcast_unknown_tag(
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    tag_uid: str,
    tray_uuid: str,
    tray_type: str | None = None,
    tray_color: str | None = None,
    tray_sub_brands: str | None = None,
    tray_count: int | None = None,
) -> None:
    """Broadcast unknown_tag, deduped so repeated MQTT pushes for the same slot+tag don't spam the UI."""
    _logger = logging.getLogger(__name__)
    slot_key = (ams_id, tray_id)
    tag_key = (tag_uid or "", tray_uuid or "")
    per_printer = _unknown_tag_last_broadcast.setdefault(printer_id, {})
    if per_printer.get(slot_key) == tag_key:
        _logger.debug(
            "unknown_tag deduped for printer=%d AMS=%d slot=%d tag=%s",
            printer_id,
            ams_id,
            tray_id,
            tag_key[0][:8] or tag_key[1][:8] or "(none)",
        )
        return
    _logger.info(
        "unknown_tag broadcast: printer=%d AMS=%d slot=%d type=%r color=%r tag=%s",
        printer_id,
        ams_id,
        tray_id,
        tray_type,
        tray_color,
        tag_key[0][:8] or tag_key[1][:8] or "(none)",
    )
    # Broadcast first; only commit the dedup if the WS write succeeds.
    # If broadcast raises, the next MQTT push retries instead of being
    # permanently silenced by a poisoned dedup entry.
    await ws_manager.broadcast(
        {
            "type": "unknown_tag",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
            "tag_uid": tag_uid,
            "tray_uuid": tray_uuid,
            "tray_type": tray_type,
            "tray_color": tray_color,
            "tray_sub_brands": tray_sub_brands,
            "tray_count": tray_count,
        }
    )
    per_printer[slot_key] = tag_key


def _clear_unknown_tag_dedup(printer_id: int, ams_id: int, tray_id: int) -> None:
    """Drop the cached last-broadcast tag for a slot (called when slot reports empty or gets matched)."""
    per_printer = _unknown_tag_last_broadcast.get(printer_id)
    if per_printer is None:
        return
    per_printer.pop((ams_id, tray_id), None)


async def _get_plug_energy(plug, db, *, force_read: bool = False) -> dict | None:
    """Energy for one plug, through whichever driver the manager resolves.

    This was a hand-rolled ``if/elif`` chain ending in ``else:
    tasmota_service``. That default is what made it dangerous rather than merely
    incomplete: a plug type the chain predated did not fail, it silently got
    Tasmota's answer — an HTTP poll against an IP that a Zigbee plug does not
    have. Since this feeds per-print energy, the result would have been a wrong
    number rather than a missing one.

    ``SmartPlugManager.get_service_for_plug`` is the single resolver, so a new
    plug type is one line there and every caller follows. It also configures the
    Home Assistant service from settings, which is why dropping the HA branch
    here loses nothing.

    The driver each type returns is unchanged — only the route to it.

    ``force_read`` is for the two ends of a per-print measurement, where a
    reading that is merely recent is not good enough. Only a driver that answers
    from a cache has anything to force: Tasmota, REST and Home Assistant make a
    live HTTP call per question, and the MQTT driver holds state the plug pushes
    at it, so for those it is already as fresh as the device is willing to be.
    The flag is honoured through ``reads_from_a_cache`` rather than by asking
    which plug type this is — that if/elif chain is the one this function exists
    to have removed.
    """
    service = await smart_plug_manager.get_service_for_plug(plug, db)
    if force_read and getattr(service, "reads_from_a_cache", False):
        # No timeout of our own: this runs in a background task, and the driver
        # already bounds the wait and shares one read between callers.
        await service.refresh(plug)
    return await service.get_energy(plug)


async def _record_print_energy(
    archive_id: int,
    printer_id: int,
    *,
    approximate: bool = False,
    may_read=None,
    ended_at: datetime | None = None,
    expected_identity: str | None = None,
    expected_epoch: int | None = None,
) -> None:
    """Calculate and save energy usage in background.

    Reads the starting kWh from the archive row (#941: persisted so a mid-print
    backend restart no longer loses per-print energy data).

    Lifted out of ``on_print_complete`` so it can be tested: what it does that
    is easy to get silently wrong — asking the plug for a fresh reading rather
    than accepting the cache — fails by producing a slightly small number, which
    is the one kind of wrong nobody notices.

    ``approximate`` is set by the reconciliation sweep, which reaches a print
    that ended while the process was down. The counter has climbed since, so
    the printer's idle draw over the gap is inside the figure; the archive is
    flagged rather than left empty, because "no energy recorded" reads as "this
    print used nothing".

    The counter is not always believed (spec plug-meter-watchdog W8-W10). When
    the measured window meets a proven flat series of the same measurement
    source (``meter_watch``), the whole window is estimated once from the power
    history, marked as such -- or, without enough history, or with an end that
    is not known, left unknown with its reason, never written as a measured
    zero. A source that changed under the print makes the counter's difference
    meaningless too. A healthy counter stays the measurement; a figure this
    writer finalised is never overwritten by a second callback.
    """
    from backend.app.services import queue_sources

    epoch = queue_sources.current_epoch() if expected_epoch is None else expected_epoch
    if queue_sources.current_epoch() != epoch:
        return
    logger = logging.getLogger(__name__)
    try:
        logger.info("[ENERGY-BG] Starting energy calculation for archive %s", archive_id)
        async with async_session() as db:
            from backend.app.models.archive import PrintArchive

            archive = await db.get(PrintArchive, archive_id)
            if archive is None or (expected_identity is not None and archive.recovery_identity != expected_identity):
                logger.warning("[ENERGY-BG] Archive %s no longer exists", archive_id)
                return
            expected_identity = archive.recovery_identity
            starting_kwh = archive.energy_start_kwh
            if starting_kwh is None:
                logger.info("[ENERGY-BG] No start kWh recorded for archive %s", archive_id)
                return
            started = archive.extra_data or {}
            if _energy_settled(started):
                logger.info("[ENERGY-BG] Archive %s already has its final energy figure", archive_id)
                return
            start_source = started.get(ENERGY_SOURCE_KEY)
            start_at = _utc_or_none(started.get(ENERGY_START_AT_KEY))
            end_at = _utc_or_none(archive.completed_at) or _utc_or_none(ended_at)

            # The meter the start was read from — never another one: two
            # counters make a plausible, wrong delta rather than a missing one.
            # An archive started before the plug was remembered keeps the old
            # rule, the ranked first plug, which is what its start was read on.
            remembered = (archive.extra_data or {}).get(ENERGY_PLUG_KEY)
            if remembered is not None:
                plug = await db.get(SmartPlug, remembered)
                if plug is None or plug.printer_id != printer_id:
                    logger.info(
                        "[ENERGY-BG] Plug %s that archive %s started on is no longer on printer %s",
                        remembered,
                        archive_id,
                        printer_id,
                    )
                    return
            else:
                plug = await _energy_plug_for_printer(printer_id, db)
            if plug is None:
                logger.info("[ENERGY-BG] No smart plug for printer %s", printer_id)
                return

            # Ask the plug itself rather than accept what is cached. The
            # counter only moves when the plug reports it, and it is asked
            # to report at most every 30 s — so a print that has just
            # stopped drawing 200 W leaves up to half a minute of it
            # unrecorded, every time, in the same direction. The start
            # reading has the same lag over an idle printer, where it is
            # worth a fraction of a watt-hour and cannot pay for a radio
            # read on the dispatch path.
            if may_read is not None and not may_read():
                return
            token_before = plug.measurement_source_id
            energy = await _get_plug_energy(plug, db, force_read=True)
            if may_read is not None and not may_read():
                return
            logger.info("[ENERGY-BG] Energy response: %s", energy)
            if not energy or energy.get("total") is None:
                logger.warning("[ENERGY-BG] No 'total' in energy response")
                return
            read_at = datetime.now(timezone.utc)

            # Recorded before the sanity check below, not after: a counter
            # that went backwards is precisely when the range report most
            # needs a fresh baseline, and the reading itself is true even
            # though the per-print delta derived from it is not. Only as
            # the source it was read from (spec plug-meter-watchdog W2).
            from backend.app.services.measurement_buffer import measurement_buffer
            from backend.app.services.meter_source import admits, read_source, source_guard

            async with source_guard():
                if measurement_buffer.retired:
                    # A restore replaced the storage while the plug was read: nothing of this print is its.
                    logger.info("[ENERGY-BG] Archive %s: storage replaced by a restore; nothing written", archive_id)
                    return
                source = await read_source(db, plug.id)
                end_admitted = admits(source, token_before, read_at)
                if end_admitted:
                    await smart_plug_manager.record_energy_snapshot(db, plug.id, energy["total"], at=read_at)
                await db.commit()
            current_source = source[0] if source is not None else None
            source_changed = current_source != token_before or (
                start_source is not None and current_source != start_source
            )

            energy_used = round(energy["total"] - starting_kwh, 4)
            logger.info("[ENERGY-BG] Per-print energy: %s kWh", energy_used)

            async def left_unknown(reason: str) -> None:
                await _write_print_energy(
                    db,
                    archive_id,
                    kind="unavailable",
                    kwh=None,
                    cost_per_kwh=None,
                    approximate=approximate,
                    record={"version": 1, "kind": "unavailable", "unavailable_reason": reason, "final": True},
                    expected_identity=expected_identity,
                    expected_epoch=epoch,
                )
                logger.info("[ENERGY-BG] Archive %s: energy unknown (%s)", archive_id, reason)

            # Changed under the end's own read, or since the start was read: the difference of two
            # readings is not one meter's (A24). A start from before m211 knows no source; only a
            # change under its end read can be proven then.
            if source_changed:
                await left_unknown("source_changed")
                return
            if not end_admitted:
                # The same source, but read at or before its cutoff: the clock went back after the
                # source began, and this reading is not inside its window (review r1 MW-R5).
                await left_unknown("clock_before_source")
                return
            if energy_used < 0:
                logging.getLogger(__name__).warning(
                    "[ENERGY-BG] Negative energy delta for archive %s (start=%s, end=%s) - counter reset?",
                    archive_id,
                    starting_kwh,
                    energy["total"],
                )
                # Commit the snapshot even though no per-print figure is
                # written — otherwise the session unwinds and the one
                # reading that could re-baseline the report is thrown away.
                await db.commit()
                return

            # The price of its own time (m245), from the database alone -- no network on this path.
            # ``cost_per_kwh`` is the average price the figure is costed at: along the counter of
            # this print's own source, each step at the price in effect when the step started.
            from backend.app.services import energy_price

            price_history = await energy_price.load_history(db)
            cost_per_kwh, tariff_method = await energy_price.print_price(
                db,
                plug.id,
                price_history,
                start_at=start_at,
                start_kwh=starting_kwh,
                end_at=read_at,
                end_kwh=energy["total"],
                source_since=source[1] if source is not None else None,
            )

            # Every figure this writer settles is final, a measured one too (W9, review r1 MW-R4):
            # a later callback reads a later counter -- idle draw or the next job, never this print.
            kind, kwh = "measured", energy_used
            record = {
                "version": 1,
                "kind": "measured",
                "plug_id": plug.id,
                "source_id": token_before,
                "counter_delta_kwh": round(energy_used, 6),
                "tariff": cost_per_kwh,
                "tariff_method": tariff_method,
                "final": True,
            }
            if start_source is not None and start_at is not None:
                from backend.app.services import meter_watch as watch
                from backend.app.services.measurement_history import resolve_sample_seconds

                await db.refresh(plug)
                if plug.measurement_source_id != token_before:
                    # Rotated after the end read's check: its history now starts at the new cutoff,
                    # and an empty window would read as a healthy counter (plan T6).
                    await left_unknown("source_changed")
                    return
                window_end = end_at or read_at
                assessment = await watch.assess_print_window(
                    async_session,
                    plug,
                    start_at=start_at,
                    end_at=window_end,
                    start_total=starting_kwh,
                    end_total=energy["total"],
                    end_known=not approximate and end_at is not None,
                    sample_seconds=await resolve_sample_seconds(db),
                )
                if assessment.kind == "storage_replaced":
                    logger.info("[ENERGY-BG] Archive %s: storage replaced by a restore; nothing written", archive_id)
                    return
                if assessment.kind == "measured":
                    record = watch.provenance(
                        assessment,
                        plug,
                        start_at=start_at,
                        end_at=window_end,
                        counter_delta=energy_used,
                        tariff=cost_per_kwh,
                    )
                    record["tariff_method"] = tariff_method
                    record["final"] = True
                    if started.get("energy_start_partial"):
                        record["start_partial"] = True
                else:
                    if assessment.incident_id is None and assessment.detection is not None:
                        # Best effort: the episode and its warning are the sweep's to retry, the
                        # print's figure is not (spec §9 — a failed inbox write stops nothing else).
                        try:
                            assessment.incident_id = await watch.open_incident_from_print(
                                async_session, plug, assessment.detection, read_at
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[ENERGY-BG] Archive %s: stall episode not recorded: %s", archive_id, exc)
                    kind = assessment.kind
                    kwh = round(assessment.kwh, 4) if assessment.kwh is not None else None
                    if kind == "estimated":
                        # The counter stood still, so there are no steps to follow: an estimate is
                        # costed at the time-weighted price of the window it was integrated over.
                        cost_per_kwh = energy_price.average_price(price_history, start_at, window_end)
                        tariff_method = energy_price.METHOD_WINDOW_AVERAGE
                    record = watch.provenance(
                        assessment,
                        plug,
                        start_at=start_at,
                        end_at=window_end,
                        counter_delta=energy_used,
                        tariff=cost_per_kwh,
                    )
                    if kind == "estimated":
                        record["tariff_method"] = tariff_method
                    record["final"] = True
                    if started.get("energy_start_partial"):
                        record["start_partial"] = True
            await _write_print_energy(
                db,
                archive_id,
                kind=kind,
                kwh=kwh,
                cost_per_kwh=cost_per_kwh,
                approximate=approximate,
                record=record,
                plug_id=plug.id,
                expected_source=token_before,
                expected_identity=expected_identity,
                expected_epoch=epoch,
            )
    except Exception as e:
        logger.warning("[ENERGY-BG] Failed: %s", e)


#: ``PrintArchive.extra_data`` keys of the energy measurement (spec plug-meter-watchdog W2, W9).
ENERGY_SOURCE_KEY = "energy_source_id"
ENERGY_START_AT_KEY = "energy_start_at"
ENERGY_PROVENANCE_KEY = "energy_provenance"


def _utc_or_none(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _energy_settled(extra: dict | None) -> bool:
    """A figure no later callback may change (W9, review r1 MW-R4): one this writer finalised, or an
    operator's own reconstruction -- marked ``energy_reconstructed_from`` without any provenance.

    A bare number from before the watch is neither and stays writable, as it always was.
    """
    extra = extra or {}
    if (extra.get(ENERGY_PROVENANCE_KEY) or {}).get("final"):
        return True
    return ENERGY_PROVENANCE_KEY not in extra and extra.get("energy_reconstructed_from") is not None


async def _write_print_energy(
    db,
    archive_id: int,
    *,
    kind: str,
    kwh,
    cost_per_kwh,
    approximate: bool,
    record,
    plug_id: int | None = None,
    expected_source: str | None = None,
    expected_identity: str | None = None,
    expected_epoch: int | None = None,
) -> None:
    """Write one print's energy figure and where it came from (W9).

    The row was read before the plug's round-trip, and the 3MF may have landed
    meanwhile -- its attach writes the same metadata. So the snapshot commits
    first, and the archive is written from a fresh read under its own write
    scope, merging only what this writer adds (swap-source-lifecycle spec r4
    §7). No I/O under the scope, no notification under it.

    A figure read off a plug (``plug_id`` + ``expected_source``) is written only
    while that source is still the plug's: the check and the write run under
    ``source_guard``, the lock a rotation holds, so a reconfiguration during the
    assessment cannot slip in between (review r1 MW-R2). The guard is taken
    before the archive's locks and SQLite's writer -- the order the flush and a
    rotation keep too -- and nothing under it waits on the network.
    """
    from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.meter_source import read_source, source_guard

    logger = logging.getLogger(__name__)
    await db.commit()
    async with source_guard(), archive_write_scope(db, archive_id):
        if measurement_buffer.retired:
            await db.rollback()
            logger.info("[ENERGY-BG] Archive %s: storage replaced by a restore; nothing written", archive_id)
            return
        fresh = await load_active_archive_for_write(db, archive_id)
        from backend.app.services import queue_sources

        if (
            fresh is None
            or (expected_identity is not None and fresh.recovery_identity != expected_identity)
            or (expected_epoch is not None and queue_sources.current_epoch() != expected_epoch)
            or "noncurrent_execution" in (fresh.extra_data or {})
        ):
            await db.rollback()
            logger.warning("[ENERGY-BG] Archive %s no longer exists", archive_id)
            return
        extra = dict(fresh.extra_data or {})
        if _energy_settled(extra):
            await db.rollback()
            return  # finalised by an earlier callback, or the operator's own: never overwritten
        if plug_id is not None and kind != "unavailable":
            source = await read_source(db, plug_id)
            if source is None or source[0] != expected_source:
                kind, kwh = "unavailable", None
                record = {"version": 1, "kind": "unavailable", "unavailable_reason": "source_changed", "final": True}
        if kind == "unavailable":
            fresh.energy_kwh = None
            fresh.energy_cost = None
        else:
            fresh.energy_kwh = kwh
            fresh.energy_cost = round(kwh * cost_per_kwh, 3)
        if approximate or kind == "estimated":
            extra["energy_is_approximate"] = True
        if kind == "estimated":
            extra["energy_reconstructed_from"] = "power_history"
        if record is not None:
            extra[ENERGY_PROVENANCE_KEY] = record
        # Reassign the dict so SQLAlchemy flags the JSON column dirty.
        fresh.extra_data = extra
        await db.commit()
    logger.info(
        "[ENERGY-BG] Saved archive %s energy: %s (%s)%s",
        archive_id,
        kwh,
        kind,
        " (approximate — recovered print)" if approximate else "",
    )


async def _default_queue_id_for_printer(db, printer_id: int) -> int | None:
    """Resolve a printer's single PrinterQueue row → its id, creating the row
    when the printer has none (see ``services/printer_queues``).

    External / direct-dispatch archives use this to fill ``archive.queue_id``
    so the post-m019 archive-driven counters in ``GET /printer-queues/``
    include them under the printer's default queue.
    """
    from backend.app.services.printer_queues import ensure_printer_queue

    return (await ensure_printer_queue(db, printer_id)).id


#: ``PrintArchive.extra_data`` key: the plug a print's energy start was read from.
ENERGY_PLUG_KEY = "energy_plug_id"


async def _energy_plugs_for_printer(printer_id: int, db) -> list[SmartPlug]:
    """Every plug on the printer, in the order energy tries them (see below)."""
    result = await db.execute(
        select(SmartPlug)
        .where(SmartPlug.printer_id == printer_id)
        .order_by(SmartPlug.controls_printer_power.desc(), SmartPlug.id.asc())
    )
    return list(result.scalars().all())


async def _energy_plug_for_printer(printer_id: int, db) -> SmartPlug | None:
    """The one plug a printer's energy is measured with.

    Both ends of a per-print measurement go through here so they cannot pick
    different rows. They used to run their own ``scalar_one_or_none()``, which
    raises on two plugs and — worse — guarantees nothing about picking the same
    one twice: a start read from one meter and an end read from another produce a
    plausible, wrong delta instead of a missing one.

    A printer legitimately has several plugs. ``controls_printer_power`` (#2629)
    marks the mains feed as opposed to an accessory on the same machine, so it
    wins; ``id`` breaks any remaining tie so the answer is stable.
    """
    result = await db.execute(
        select(SmartPlug)
        .where(SmartPlug.printer_id == printer_id)
        .order_by(SmartPlug.controls_printer_power.desc(), SmartPlug.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _record_energy_start(archive, printer_id: int, db, *, context: str = "") -> bool:
    """Capture the smart plug lifetime counter on the archive at print start.

    Persists `energy_start_kwh` on the archive row (upstream #941) so per-print
    energy tracking survives a backend restart mid-print. The print-end handler
    reads this value back from the DB and computes the delta against the current
    plug counter.
    """
    from backend.app.services import queue_sources

    archive_id, expected_identity = archive.id, archive.recovery_identity
    epoch = queue_sources.current_epoch()
    _logger = logging.getLogger(__name__)
    try:
        plugs = await _energy_plugs_for_printer(printer_id, db)
        if not plugs:
            _logger.info("[ENERGY] No smart plug for printer %s (archive %s)", printer_id, archive.id)
            return False
        # The first plug, in the ranked order, that actually reports a counter
        # (upstream #2859): a mains switch with no power sensor must not stand
        # in front of the plug that meters the printer. Ranking rather than
        # filtering — a printer whose only plug is an accessory or disabled is
        # still measured by it when it answers.
        plug = energy = token = None
        for candidate in plugs:
            # The source is captured BEFORE the read (spec plug-meter-watchdog W2).
            candidate_token = candidate.measurement_source_id
            reading = await _get_plug_energy(candidate, db)
            if reading and reading.get("total") is not None:
                plug, energy, token = candidate, reading, candidate_token
                break
        if plug is None:
            _logger.warning(
                "[ENERGY] None of printer %s's plugs reports an energy counter for archive %s (tried: %s)",
                printer_id,
                archive.id,
                ", ".join(p.name for p in plugs),
            )
            return False
        read_at = datetime.now(timezone.utc)
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.meter_source import admits, read_source, source_guard

        # This function commits the caller's session anyway; doing it before the lock means no
        # holder of SQLite's writer ever waits on it (the flush holds it and then wants the writer).
        await db.commit()
        from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

        async with source_guard(), archive_write_scope(db, archive_id):
            if measurement_buffer.retired:
                _logger.info("[ENERGY] Storage replaced by a restore; no start recorded for archive %s", archive.id)
                return False
            source = await read_source(db, plug.id)
            if not admits(source, token, read_at):
                # Reconfigured while it was being read, or read at or before the source's cutoff (the
                # clock went back): the counter is not this source's. No baseline, never a borrowed one.
                _logger.info("[ENERGY] Plug %s: start reading outside its source (archive %s)", plug.id, archive.id)
                return False
            fresh = await load_active_archive_for_write(db, archive_id)
            if (
                fresh is None
                or fresh.recovery_identity != expected_identity
                or queue_sources.current_epoch() != epoch
                or "noncurrent_execution" in (fresh.extra_data or {})
            ):
                await db.rollback()
                return False
            archive = fresh
            current_source = source[0]
            archive.energy_start_kwh = float(energy["total"])
            # The end reads THIS meter (``_record_print_energy``), of THIS measurement
            # source, and the estimate's window starts at THIS reading (W2, W10).
            # Reassign the dict so SQLAlchemy flags the JSON column dirty.
            archive.extra_data = {
                **(archive.extra_data or {}),
                ENERGY_PLUG_KEY: plug.id,
                ENERGY_SOURCE_KEY: current_source,
                ENERGY_START_AT_KEY: read_at.isoformat(),
            }
            # Same reading, also kept as a snapshot: it marks the true start of this
            # print in the range report instead of leaving the nearest boundary on
            # whichever hour the snapshot loop last fired.
            await smart_plug_manager.record_energy_snapshot(db, plug.id, energy["total"], at=read_at)
            await db.commit()
        _logger.info(
            "[ENERGY] Recorded starting energy%s for archive %s: %s kWh",
            f" ({context})" if context else "",
            archive.id,
            energy["total"],
        )
        return True
    except Exception as e:
        _logger.warning("[ENERGY] Failed to record starting energy for archive %s: %s", archive_id, e)
        return False


def register_expected_print(
    printer_id: int,
    filename: str,
    archive_id: int,
    ams_mapping: list[int] | None = None,
    created_by_id: int | None = None,
    archive_identity: str | None = None,
):
    """Register an expected print from reprint/scheduled so we don't create duplicate archives."""
    # Store with multiple filename variations to catch different naming patterns
    _expected_prints[(printer_id, filename)] = archive_id
    # Also store without .3mf extension if present
    if filename.endswith(".3mf"):
        base = filename[:-4]
        _expected_prints[(printer_id, base)] = archive_id
        _expected_prints[(printer_id, f"{base}.gcode")] = archive_id
    # Store AMS mapping for usage tracking at print completion
    if ams_mapping is not None:
        _print_ams_mappings[archive_id] = ams_mapping
        if archive_identity is not None:
            _print_ams_mapping_identities[archive_id] = archive_identity
        else:
            _print_ams_mapping_identities.pop(archive_id, None)
    # Store created_by_id so the user start email can be sent even when the archive
    # itself has no created_by_id (e.g. library-file-based queue prints)
    if created_by_id is not None:
        _expected_print_creators[(printer_id, filename)] = created_by_id
        if filename.endswith(".3mf"):
            base = filename[:-4]
            _expected_print_creators[(printer_id, base)] = created_by_id
            _expected_print_creators[(printer_id, f"{base}.gcode")] = created_by_id
    # Record registration time for TTL-based eviction
    _registered_at = time.monotonic()
    _expected_print_registered_at[(printer_id, filename)] = _registered_at
    if filename.endswith(".3mf"):
        base = filename[:-4]
        _expected_print_registered_at[(printer_id, base)] = _registered_at
        _expected_print_registered_at[(printer_id, f"{base}.gcode")] = _registered_at
    logging.getLogger(__name__).info(
        f"Registered expected print: printer={printer_id}, file={filename}, archive={archive_id}, ams_mapping={ams_mapping}"
    )


def withdraw_expected_print(printer_id: int, filename: str, expected_archive_id: int | None = None) -> None:
    """Undo a registration whose print command never went out (upstream #2702 follow-up).

    ``register_expected_print`` runs *before* the print command — it has to, so
    the entry exists by the time the printer's first report arrives. Everything
    between the two can still fail: a cancel, the strict-stagger refusal, a swap
    macro, the calibration write, a dropped MQTT session, or ``start_print``
    simply returning False.

    Without this, that stale entry survives for the full
    ``_EXPECTED_PRINT_TTL_SECONDS`` (two hours). And ``_expected_prints`` is a
    **name-match adoption table**: ``on_print_start`` pops ``(printer_id,
    filename)`` and attaches the print to that archive. So a leftover does not
    merely leak — for two hours it adopts *the next print of the same file on
    that printer* into the wrong archive, whichever way that print was started.
    On a farm that repeats one file, that is a normal Tuesday.

    The TTL sweeper stays as the backstop it was meant to be, rather than the
    only mechanism. Removes every filename variant ``register_expected_print``
    wrote, so a partial withdrawal cannot leave one of them behind to match on.
    """
    keys = [(printer_id, filename)]
    if filename.endswith(".3mf"):
        base = filename[:-4]
        keys.append((printer_id, base))
        keys.append((printer_id, f"{base}.gcode"))

    if expected_archive_id is not None:
        keys = [key for key in keys if _expected_prints.get(key) == expected_archive_id]
    archive_ids = {aid for key in keys if (aid := _expected_prints.pop(key, None)) is not None}
    if expected_archive_id is not None:
        archive_ids.add(expected_archive_id)
    for key in keys:
        _expected_print_creators.pop(key, None)
        _expected_print_registered_at.pop(key, None)

    # The AMS mapping is keyed by archive, not by filename, so it is only safe to
    # drop once no variant still points at that archive.
    for archive_id in archive_ids:
        if archive_id not in _expected_prints.values():
            _print_ams_mappings.pop(archive_id, None)
            _print_ams_mapping_identities.pop(archive_id, None)

    if archive_ids:
        logging.getLogger(__name__).info(
            "Withdrew expected print: printer=%s, file=%s, archive=%s (command never sent)",
            printer_id,
            filename,
            ", ".join(str(a) for a in sorted(archive_ids)),
        )


def is_expected_print_file(printer_id: int, remote_path: str) -> bool:
    """True when this path names the file a just-dispatched print is about to run.

    Post-print cleanup and the next dispatch of the SAME file overlap in time
    and in every identity check there is: same name, and — the file being the
    same 3MF — byte-identical content, so the content proof passes too. Live
    incident 2026-08-28 (X2D, batch copies): cleanup of the finished job
    deleted ``/X.3mf`` 0.7 s after the next copy's print command referenced
    it, and the printer answered 0500_4002 "Unsupported print file path".
    ``register_expected_print`` runs before the print command goes out, so at
    the moment cleanup reaches for a file this registry already knows whether
    that name belongs to the NEXT job. Checked per destructive call, not at
    candidate-list time — the registration can land mid-cleanup.
    """
    return expected_print_archive(printer_id, remote_path) is not None


def expected_print_archive(printer_id: int, remote_path: str) -> int | None:
    """The archive of the dispatch that registered this path as its next print, else ``None``.

    The dispatcher that made the registration may clear its own file; nobody
    else may (``services/printer_file_guard``).
    """
    name = remote_path.rsplit("/", 1)[-1]
    forms = [name]
    for suffix in (".gcode.3mf", ".3mf"):
        if name.endswith(suffix):
            forms.append(name[: -len(suffix)])
    for form in forms:
        archive_id = _expected_prints.get((printer_id, form))
        if archive_id is not None:
            return archive_id
    return None


def printer_has_expected_print(printer_id: int) -> bool:
    """Whether any dispatch has registered a print that has not started yet on this printer."""
    return any(key[0] == printer_id for key in tuple(_expected_prints))


def register_macro_selection(printer_id: int, options: dict) -> None:
    """Register the per-print macro selection for *printer_id*.

    Called from dispatch after a successful ``start_print``, next to
    :func:`register_swap_config`. Unlike that one there is no master toggle to
    gate on: an empty list is a real answer — the operator ticked nothing —
    and is registered as such. Only a missing key, meaning a dispatch path
    that never carried a selection at all, registers nothing.
    """
    ids = options.get("selected_macro_ids")
    if ids is None:
        return
    _active_macro_selection[printer_id] = [int(i) for i in ids]
    logging.getLogger(__name__).info("[MACRO-SEL] Registered %d selected macro(s) for printer %s", len(ids), printer_id)


def clear_macro_selection(printer_id: int) -> None:
    """Forget the selection — the print it belonged to is over."""
    _active_macro_selection.pop(printer_id, None)


def run_config_registrations(printer_id: int) -> dict[str, tuple[bool, object]]:
    """Read-only: what dispatch registered for the print on *printer_id*, as ``(present, the object itself)``.

    The objects, not copies: an addressed repair compares their identity and content before it removes
    them (spec printer-runtime-stall-diagnostics r3 R7a). An empty selection is present — it is an answer.
    """
    return {
        "macro_selection": (printer_id in _active_macro_selection, _active_macro_selection.get(printer_id)),
        "swap_config": (printer_id in _active_swap_config, _active_swap_config.get(printer_id)),
    }


def capture_run_config(printer_id: int) -> tuple:
    """Presence, the object and a deep copy of both registrations, for :func:`run_config_unchanged`."""
    from copy import deepcopy

    return tuple(
        (printer_id in registry, registry.get(printer_id), deepcopy(registry.get(printer_id)))
        for registry in (_active_macro_selection, _active_swap_config)
    )


def run_config_unchanged(printer_id: int, expected: tuple) -> bool:
    """Whether both registrations are still the very objects, with the very content, that were checked."""
    for registry, (present, value, content) in zip(
        (_active_macro_selection, _active_swap_config), expected, strict=True
    ):
        if (printer_id in registry) != present:
            return False
        if present and (registry[printer_id] is not value or registry[printer_id] != content):
            return False
    return True


def release_run_config(printer_id: int, expected: tuple) -> None:
    """Forget what dispatch registered for a finished run -- call only right after :func:`run_config_unchanged`
    answered True. Its finish macros are not run: this is a selection, not an owed effect."""
    for registry, (present, _value, _content) in zip(
        (_active_macro_selection, _active_swap_config), expected, strict=True
    ):
        if present:
            registry.pop(printer_id, None)


def register_swap_config(printer_id: int, options: dict):
    """Register in-memory swap-macro config for the current print on *printer_id*.

    Called from dispatch AFTER a successful ``start_print``. Stores only
    the in-memory fast path; persistence to ``archive.extra_data`` is
    handled separately by the dispatch caller (in the same db session
    that's already open for archive creation / lookup, before the FTP
    upload and start_print fire). Two reasons for the split:

    * Library-file dispatch — the swap intent is folded into
      ``archive_print()``'s INSERT (parameter
      ``swap_macro_events_pending``), so no extra UPDATE is needed.
    * Reprint dispatch — the existing archive row is updated in the
      already-open session before any other DB writes (runtime tracker,
      AMS history, etc.) start contending for the SQLite writer.

    The previous design made ``register_swap_config`` async and ran a
    standalone UPDATE after ``start_print``. That UPDATE raced the
    runtime-tracker UPDATE on ``printers`` (both writers under SQLite's
    single-writer rule) and timed out under ``busy_timeout``, leaving
    ``swap_macro_events_pending`` not persisted — so a backend restart
    mid-print still couldn't recover the change_table intent.

    ``on_print_complete`` reads + pops this dict; it falls back to
    ``archive.extra_data["swap_macro_events_pending"]`` for restart
    recovery, then clears that key after firing the macro for
    idempotency.
    """
    if not options.get("execute_swap_macros"):
        return
    events = options.get("swap_macro_events") or []
    if not events:
        return
    _active_swap_config[printer_id] = {"swap_macro_events": list(events)}
    logging.getLogger(__name__).info("[SWAP] Registered swap config for printer %s: events=%s", printer_id, events)


async def maybe_register_external_stagger(printer_id: int) -> None:
    """If stagger is enabled and this printer just started heating from
    cold, take a stagger slot so the grid-load cap is respected by any
    subsequent queue dispatches.

    Skips registration when bed is already at target — the print was
    started long ago (BamDude restart during active print) and the
    heating spike is already behind us.
    """
    try:
        state = printer_manager.get_status(printer_id)
        if state is None or not state.connected:
            return
        if state.state not in ("PREPARE", "RUNNING"):
            return
        bed_target = state.temperatures.get("bed_target", 0) if state.temperatures else 0
        bed_cur = state.temperatures.get("bed", 0) if state.temperatures else 0
        if bed_target <= 0 or bed_cur >= bed_target - 2:
            return  # already at target, or no target set — no heating spike
        from backend.app.services.print_scheduler import scheduler as print_scheduler

        async with async_session() as db:
            enabled, _, interval, _ = await print_scheduler._get_stagger_settings(db)
        if not enabled:
            return
        print_scheduler._register_stagger_start(printer_id, interval)
    except Exception as e:
        logging.getLogger(__name__).debug("External stagger registration failed for printer %s: %s", printer_id, e)


async def mark_queue_printing_for_printer(
    printer_id: int,
    item_id: int | None = None,
    *,
    archive_id: int | None = None,
    options: dict | None = None,
    expected_identity: str | None = None,
):
    """Typed observed claim; every authoritative read is inside its writer lane."""
    from backend.app.services import queue_sources
    from backend.app.services.print_claims import observe_claim
    from backend.app.services.queue_ops import claim_lanes

    async with (
        queue_sources.storage_mutation(),
        async_session() as db,
        claim_lanes(db, [printer_id], archive_id=archive_id),
    ):
        result = await observe_claim(
            db, printer_id, item_id, archive_id=archive_id, options=options, expected_identity=expected_identity
        )
        await db.commit()
        return result


def _get_start_ams_mapping(data: dict, archive_id: int | None, archive_identity: str | None = None) -> list[int] | None:
    """Resolve AMS mapping for print start without consuming stored queue/reprint state."""
    stored_ams_mapping = data.get("ams_mapping")
    if (
        not stored_ams_mapping
        and archive_id
        and archive_identity is not None
        and _print_ams_mapping_identities.get(archive_id) == archive_identity
    ):
        stored_ams_mapping = _print_ams_mappings.get(archive_id)
    return stored_ams_mapping


def _take_completion_ams_mapping(data: dict, archive_id: int | None, archive_identity: str | None) -> list[int] | None:
    """The mapping a completion charges by, draining this execution's registered one either way (G03 E-9).

    The terminal frame's own mapping (captured from the request topic, every print source) wins; the mapping
    registered at dispatch (queue / reprint, before the print started) is the fallback. ⚠️ The registered entry
    is removed whenever its identity matches, used or not: it was popped only when the frame carried none, and
    our own dispatch's ``project_file`` echo is captured into nearly every terminal frame, so the entry stayed in
    memory for the life of the process (the TTL sweep cannot see it once the start consumed its expected keys).
    The identity guard stays: a reused archive id never reads, nor drains, another execution's mapping.
    """
    registered = None
    if archive_id and _print_ams_mapping_identities.get(archive_id) == archive_identity:
        registered = _print_ams_mappings.pop(archive_id, None)
        _print_ams_mapping_identities.pop(archive_id, None)
    return data.get("ams_mapping") or registered


def _printer_reported_options(
    data: dict, archive_id: int | None, plate_id: int | None, archive_identity: str | None = None
) -> dict:
    """The print options a print BamDude did not send can still be known from.

    A print started from BambuStudio or the printer's screen gets a queue row
    like any other, and that row is repeatable — so an option dropped here is
    silently wrong on the repeat. Reported from a farm: the row came out with
    both columns empty.

    ⚠️ Empty does not mean the repeat fails, which is why this went unnoticed.
    A row with no mapping is routed from scratch at dispatch — preflight resolves
    a plan against whatever is loaded on the printer *now*, and that is the weaker
    source the filament-attribution rule exists to rank last: it charges the
    repeat to today's spools rather than to the slots the print was actually
    sliced for.

    ⚠️ **Both can still come back None here, and often do.** Measured on a
    BambuStudio-launched print (2026-08-21): the printer announced it as
    ``Cube_slicer.gcode.3mf`` with no ``gcode_file``, so the plate was
    unparseable; and no mapping existed at all — the request-topic interceptor
    never saw a ``project_file`` command (Studio had not gone through the LAN
    broker), and the live ``mapping`` field was absent too. That is not a fault
    to paper over:

    * the **plate** is knowable later, from the file itself, and
      ``ArchiveService.attach_3mf_to_archive`` carries it to the row when the
      3MF lands;
    * the **mapping** is not knowable at all in that case. It stays None, and
      the recompute above is the honest answer. ⚠️ Do NOT fill it late from the
      live ``mapping`` field: by then AMS filament backup may have rewritten it
      to a substitute, and ``PrintQueueItem.ams_mapping`` is ranked ABOVE the
      live field by the attribution chain — writing it there would launder the
      weakest source into the second-strongest slot.

    Exactly two things are recoverable: the slicer's slot-per-filament mapping,
    which the printer echoes in the print payload, and the plate parsed from the
    filename.

    ⚠️ Nothing else is guessed. The calibration flags, the macros, the preheat
    override and the gcode injection are parameters of a ``project_file``
    command we never sent and the printer does not report back — a plausible
    default invented here would be indistinguishable from one the operator
    chose.
    """
    return {"ams_mapping": _get_start_ams_mapping(data, archive_id, archive_identity), "plate_id": plate_id}


def mqtt_tray_color_to_hex(value) -> str:
    """The printer's ``RRGGBBAA`` tray colour as the archive stores colours: ``#RRGGBB``.

    Every other writer of ``PrintArchive.filament_color`` (the 3MF, the loaded
    spool) stores the ``#``-prefixed six digits, and the Archives page uses the
    token as a CSS colour — a bare ``FF8800FF`` painted an empty chip (upstream
    879acdbf1). Anything that is not a hex colour is no colour.
    """
    hex_color = str(value or "").strip().lstrip("#").upper()[:6]
    return f"#{hex_color}" if re.fullmatch(r"[0-9A-F]{6}", hex_color) else ""


def _extract_filament_data_from_mqtt(data: dict, ams_mapping: list[int] | None = None) -> dict[str, str]:
    """Best-effort filament metadata from the MQTT print-start snapshot.

    Used when the 3MF can't be downloaded (P1S/A1/P2S firmwares lock the file
    during print, see #1533) so the fallback PrintArchive still has enough
    filament info to support the inventory views and AMS-expansion planning the
    operator opens it for. Returns a dict with optional ``filament_type`` and
    ``filament_color`` keys in the same comma-separated format the 3MF extractor
    produces, so the rest of the codebase treats the fallback archive identically
    to a normal one.

    ``ams_mapping`` is the slicer's slot-per-print-filament list captured from
    the MQTT print payload (global tray IDs, possibly -1 for VT-tray entries).
    When supplied, only the slots actually consumed by this print contribute.
    Without it the function falls back to every loaded AMS slot.

    Accepts both the raw inner payload (``{"ams": {"ams": [...]}, ...}``) that
    the unit tests pass directly, AND the on_print_start callback shape
    (``{"raw_data": {"ams": {"ams": [...]}, ...}, ...}``) the bambu_mqtt service
    hands to main.py at runtime — the inner-only lookup shipped in #1533 silently
    returned ``{}`` for every real print start (#1533 follow-up).
    """
    result: dict[str, str] = {}
    # Look at the on_print_start wrapper first, then the inner shape.
    raw_data = (data or {}).get("raw_data")
    ams_root = (raw_data or {}).get("ams") if isinstance(raw_data, dict) else None
    if not isinstance(ams_root, dict):
        ams_root = (data or {}).get("ams") or {}
    ams_units = ams_root.get("ams") if isinstance(ams_root, dict) else None
    if not isinstance(ams_units, list) or not ams_units:
        return result

    from backend.app.services.usage_tracker import _global_tray_id

    # Map global tray id → (type, color), addressed as the print's ams_mapping
    # addresses it: unit * 4 + tray, or the bare unit id for an AMS-HT (from
    # 128, one tray). ``unit * 4`` for an HT put its spool at 512 while the
    # mapping names 128, so an HT-fed print got no filament at all (upstream
    # 879acdbf1).
    loaded: dict[int, tuple[str, str]] = {}
    for unit in ams_units:
        if not isinstance(unit, dict):
            continue
        try:
            unit_id = int(unit.get("id", 0))
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            try:
                tray_id = int(tray.get("id", 0))
            except (TypeError, ValueError):
                continue
            ttype = (tray.get("tray_type") or "").strip()
            tcolor = mqtt_tray_color_to_hex(tray.get("tray_color"))
            if not ttype:
                continue  # Empty / unloaded slot.
            loaded[_global_tray_id(unit_id, tray_id)] = (ttype, tcolor)

    if not loaded:
        return result

    if ams_mapping:
        used_ids = [int(x) for x in ams_mapping if isinstance(x, (int, float)) and int(x) >= 0]
        filaments = [loaded[g] for g in used_ids if g in loaded]
        if not filaments:
            return result  # Mapping points entirely at slots we have no data for.
    else:
        filaments = [loaded[g] for g in sorted(loaded.keys())]

    # Comma + space, each material once — the form every other writer of the
    # column uses (a two-slot PLA job is "PLA", not "PLA,PLA").
    types_joined = ", ".join(dict.fromkeys(f[0] for f in filaments))
    # Column limits per backend/app/models/archive.py: filament_type=50,
    # filament_color=50 (upstream's column is 200 — ours is 50, or PostgreSQL
    # raises on a long multi-filament color string). Whole colours only: a
    # string cut at 50 characters ended in half a colour.
    colors: list[str] = []
    for color in (f[1] for f in filaments):
        if color and len(",".join([*colors, color])) <= 50:
            colors.append(color)
    if types_joined:
        result["filament_type"] = types_joined[:50]
    if colors:
        result["filament_color"] = ",".join(colors)
    return result


async def _bump_library_file_usage(db, library_file_id: int | None) -> None:
    """Increment LibraryFile.print_count and stamp last_printed_at.

    Call this on successful print completion only (caller gates on status).
    Caller is responsible for committing the session. No-op when there's no
    linked library file (e.g. external prints or reprints from archive) or
    the library row has since been deleted. See #1008.
    """
    if library_file_id is None:
        return
    from backend.app.models.library import LibraryFile

    lib_file = await db.scalar(select(LibraryFile).where(LibraryFile.id == library_file_id))
    if lib_file is None:
        return
    lib_file.print_count = (lib_file.print_count or 0) + 1
    lib_file.last_printed_at = datetime.now(timezone.utc)


def _format_hms_error_summary(hms_errors: list[dict], device: str = "") -> str | None:
    """Build a human-readable failure reason from MQTT hms_errors for
    PrintQueueItem.error_message (#1111).

    Each entry has keys: code ('0x4038'), attr (32-bit int), module, severity.
    The short code used for the hms_errors.py lookup table is 'MMMM_EEEE' —
    module from attr bits 16-31, error from the numeric part of code. Falls
    back to the raw short code when no description is on file. Returns None
    for an empty list so callers can leave error_message unset.
    """
    if not hms_errors:
        return None
    from backend.app.services.hms_catalogue import describe

    parts: list[str] = []
    for err in hms_errors:
        try:
            # ⚠️ Through the one derivation that masks the error to 16 bits: a
            # fault from the hms[] array carries its alert level in the code's
            # high half, and unmasked it read 0500_24038 — no such code, and
            # no catalogue key, so the description was lost too (upstream
            # 6988a30e). It also takes the raw integer the payload may carry.
            short_code = _hms_short_code(err.get("attr", 0), err.get("code", ""))
        except (TypeError, ValueError):
            continue
        # ⚠️ The short code goes in WITHOUT its separator: that is how both
        # halves of Bambu's catalogue are keyed, and how hms_actions is called.
        full_code = str(err.get("full_code") or "")
        description = describe(device, full_code or None, short_code.replace("_", ""))
        label = _hms_label(full_code, short_code)
        parts.append(f"[{label}] {description}" if description else f"[{label}]")
    return "; ".join(parts) if parts else None


async def _bump_library_file_usage_if_completed(db, item, queue_status: str) -> None:
    """Queue-branch wrapper: bump usage only for status='completed' items with
    a library_file_id. Preserved so the on_print_complete queue branch reads
    naturally; new direct-print path calls ``_bump_library_file_usage``
    directly after resolving the archive's ``library_file_id``.
    """
    if queue_status != "completed":
        return
    await _bump_library_file_usage(db, item.library_file_id)


# Which print run (its archive id) each printer's milestone memory belongs to.
_progress_milestone_run: dict[int, int] = {}


def _progress_milestone_to_notify(
    progress: float, *, layer_num: int, total_layers: int, stg_cur: int, last: int
) -> int:
    """The 25 / 50 / 75 milestone to announce now, or 0.

    Nothing counts before the first layer: some printers report a large
    percentage in the first RUNNING frame while still heating (an A1 mini says
    85), which sent "75 %" beside "print started" and made 25 and 50
    unreachable. A printer that reports no layers at all is held back only
    while its stage says it is preparing. Read off the printer's own counters,
    never the file's total.
    """
    if progress <= 0:
        return 0
    if (layer_num or 0) < 1 and ((total_layers or 0) > 0 or stg_cur not in (0, -1, 255)):
        return 0
    reached = 75 if progress >= 75 else 50 if progress >= 50 else 25 if progress >= 25 else 0
    return reached if reached > last else 0


def _milestones_follow_run(printer_id: int, archive_id: int) -> None:
    """Start the milestone memory over when the printer is bound to ANOTHER run.

    A print that follows one finished at 100 % may never show a frame under
    5 %, so the old run's 75 stayed and the new run got no milestone. The same
    run bound again -- a reconnect -- keeps what it has already announced, and so
    does the first run this process binds: after a restart in the middle of a
    print the frames before the bind may already have announced a milestone.
    """
    previous = _progress_milestone_run.get(printer_id)
    _progress_milestone_run[printer_id] = archive_id
    if previous is not None and previous != archive_id:
        _last_progress_milestone[printer_id] = 0


def _take_expected_print(expected_keys: list[tuple[int, str]]) -> tuple[int | None, int | None]:
    """Take a started print's expectation: its archive id and who queued it.

    All three registries are emptied for every alias of the file name, together.
    The creator used to be removed only on the path that sent the start
    notification for a found archive; on any other path it stayed, the TTL sweep
    could not see it (it walks the registration times, already gone), and a later
    print of the same name was attributed to that user (upstream dad7fea37).
    """
    archive_id: int | None = None
    creator: int | None = None
    for key in expected_keys:
        taken = _expected_prints.pop(key, None)
        _expected_print_registered_at.pop(key, None)
        taken_creator = _expected_print_creators.pop(key, None)
        if archive_id is None and taken:
            archive_id = taken
        if creator is None:
            creator = taken_creator
    return archive_id, creator


def mark_printer_stopped_by_user(printer_id: int, *, unless_idle: bool = False) -> None:
    """Mark that the active print on this printer was stopped by the user from the queue UI.

    When on_print_complete fires with status 'failed' for a printer in this set we
    reclassify it as 'cancelled' so the correct 'print stopped' notification is sent
    rather than a 'print failed' notification.

    ``unless_idle`` is the printer card's Stop: on a printer that REPORTS a state
    with no print loaded the stop stops nothing, and its mark would wait for the
    failure of some later print (upstream ab5ccc108). An unknown state is not
    idle and still marks. The queue's Stop has a row behind it and always marks.
    """
    from backend.app.services.print_run_binding import current_print_run

    if unless_idle:
        status = printer_manager.get_status(printer_id)
        state = getattr(status, "state", None)
        if state and state != "unknown" and state not in printer_manager.ACTIVE_PRINT_STATES:
            logging.getLogger(__name__).info(
                "Stop on printer %s in state %s: nothing is printing, no user-stopped mark", printer_id, state
            )
            return

    run = current_print_run(printer_manager, printer_id)
    _user_stopped_printers[printer_id] = run.archive_id if run is not None else None
    logging.getLogger(__name__).info(
        "Marked printer %s as user-stopped from queue (archive=%s)",
        printer_id,
        _user_stopped_printers[printer_id],
    )


def _forget_unbound_user_stop(printer_id: int) -> None:
    """A mark that could name no run does not outlive the next run bound to the printer.

    ``None`` matches any archive, so it ends before a later print can meet it;
    a mark that names its archive is that run's own and stays.
    """
    if printer_id in _user_stopped_printers and _user_stopped_printers[printer_id] is None:
        _user_stopped_printers.pop(printer_id, None)


def _normalise_user_stopped_terminal(printer_id: int, archive_id: int | None, data: dict) -> dict:
    """Apply a queue-UI stop only to the execution archive it stopped."""
    stopped_archive_id = _user_stopped_printers.get(printer_id)
    raw_status = data.get("status", "completed")
    if (
        printer_id in _user_stopped_printers
        and (stopped_archive_id is None or stopped_archive_id == archive_id)
        and raw_status in ("failed", "aborted")
    ):
        logging.getLogger(__name__).info(
            "[CALLBACK] Overriding status '%s' -> 'cancelled' for printer %s (print was stopped from queue by user)",
            raw_status,
            printer_id,
        )
        data = {**data, "status": "cancelled"}
    if stopped_archive_id is None or stopped_archive_id == archive_id:
        _user_stopped_printers.pop(printer_id, None)
    return data


_last_status_broadcast: dict[int, str] = {}
# Track printers where we've updated nozzle_count
_nozzle_count_updated: set[int] = set()

# Pause/resume edge tracking — last observed gcode_state per printer, the
# wall-clock at which the current pause started, and a one-shot reason hint
# planted by internal pause-trigger paths (plate-detect today; future: any
# server-initiated pause that wants its own reason instead of the generic
# HMS classification). The reason hint is consumed + cleared by the next
# pause edge so a subsequent unrelated user-pause doesn't inherit it.
_last_printer_state: dict[int, str] = {}
_last_printer_state_generation: dict[int, int] = {}
_pause_started_at: dict[int, float] = {}
_expected_pause_reasons: dict[int, str] = {}

# Offline-notification edge (#1752): fire on_printer_offline exactly once on the
# connected→disconnected transition. _printer_last_connected holds the previous
# observation (only True→False notifies; False→False repeats + an initial startup
# False do not). _printer_offline_notify_tasks holds the per-printer pending
# debounce task, cancelled if the printer reconnects before the window elapses so
# transient MQTT blips don't flood the user.
_printer_last_connected: dict[int, bool] = {}
_printer_offline_notify_tasks: dict[int, asyncio.Task] = {}
# Sized against the staleness path (bambu_mqtt.py STALE_RECONNECT_COOLDOWN=30s +
# STALE_TIMEOUT=60s) so a single stale-reconnect cooldown can't fire — only an
# offline that survives a reconnect attempt notifies.
_PRINTER_OFFLINE_NOTIFY_DEBOUNCE_SECONDS = 60.0


def set_expected_pause_reason(printer_id: int, reason_code: str) -> None:
    """Plant a reason hint for the next observed RUNNING→PAUSE edge.

    Called by internal pause-trigger paths (currently
    ``plate_detection_loop`` in the same file) immediately before issuing
    ``client.pause_print()``, so the edge handler can label the pause with
    the actual cause instead of falling through to "Paused by user" — Bambu
    firmware fires HMS code ``0300_8001`` (paused by user) for any
    pause-command we send, regardless of motivation. Hints are one-shot:
    consumed + cleared by the next ``RUNNING→PAUSE`` transition or any
    ``PAUSE→RUNNING`` (whichever comes first), so a stale hint never bleeds
    into an unrelated pause.
    """
    _expected_pause_reasons[printer_id] = reason_code


async def _current_tray_frozen(db, printer_id: int, state) -> tuple[int | None, int | None, int | None]:
    """(global_tray, spool_id, spoolman_spool_id) of the currently-loaded tray.

    For journal timeline rows (pause/resume) — forensic context only, never
    accounting, so an unresolvable tray simply yields Nones.
    """
    from backend.app.services.print_usage_journal import freeze_spool_ids
    from backend.app.services.printer_manager import tray_layout_of

    tn = getattr(state, "tray_now", 255)
    # The trays the journal accepts (utils/tray_ids.TrayLayout.is_journal_tray).
    if not tray_layout_of(printer_id, printer_manager).is_journal_tray(tn):
        return None, None, None
    try:
        spool_id, spoolman_spool_id = await freeze_spool_ids(db, printer_id, tn)
    except Exception:
        return tn, None, None
    return tn, spool_id, spoolman_spool_id


async def _handle_pause_edge(printer_id: int, state: PrinterState):
    """Fire on_print_pause notification + WS push on RUNNING→PAUSE.

    Reason resolution:
      1. Internal hint planted by ``set_expected_pause_reason`` wins (e.g.
         plate-detect sets ``"plate_objects"`` before issuing the pause
         command — Bambu firmware responds with HMS ``0300_8001`` "paused
         by user" for any pause-command we send, which would otherwise
         label every internal pause as user-initiated).
      2. Otherwise classify from active HMS codes via
         ``hms_errors.classify_pause_reason``.
      3. Fallback "unknown" only when neither path produced a code.
    """
    from backend.app.services.hms_errors import classify_pause_reason

    try:
        printer_info = printer_manager.get_printer(printer_id)
        printer_name = printer_info.name if printer_info else f"Printer {printer_id}"

        # ``state.hms_errors`` holds ``HMSError`` dataclasses, not dicts. This
        # asked ``isinstance(e, dict)``, which is False for every one of them —
        # so the list was ALWAYS empty and every pause fell through to
        # "unknown". Filament runout and an open door paged the operator with
        # the identical message.
        #
        # ``short_code`` is what ``PAUSE_REASON_CODES`` is keyed by
        # (``0300_8004``); ``HMSError.code`` alone is ``0x8004`` and matches
        # nothing, which is the second half of the same bug.
        hms_codes = [e.short_code for e in (state.hms_errors or []) if getattr(e, "code", None)]
        expected = _expected_pause_reasons.pop(printer_id, None)
        reason_code, reason_label, hms_code = classify_pause_reason(hms_codes, expected, hms_device_of(printer_id))

        # Stash on state so frontend snapshot consumers can render the cause
        # inline without re-querying the HMS table.
        state.pause_reason = reason_code
        state.pause_reason_label = reason_label

        # Track pause start for resume duration calc + frontend live counter.
        # Stored both on the per-printer ``state`` (snapshot-visible — survives
        # F5 on the frontend) and in the module-level dict (read by
        # ``_handle_resume_edge`` even when the snapshot has already been
        # mutated by a subsequent state change).
        now = time.time()
        state.pause_started_at = now
        _pause_started_at[printer_id] = now

        # Timeline entry in the usage journal — attribution-neutral (the
        # accountant ignores pause/resume), but it anchors spool_loaded events
        # and makes runout forensics readable. The current tray + its frozen
        # spool ride along for the same forensic reason: "what was loaded when
        # it paused" is the first question a timeline reader asks.
        try:
            from backend.app.models.print_usage_event import EVENT_PAUSE
            from backend.app.services.print_usage_journal import active_archive_id, journal_lock, record_event

            # The journal lock before the session, as every journal writer takes them: the row
            # lands in the order the printer reported, never ahead of a queued tray or tail fact.
            async with journal_lock(printer_id), async_session() as db:
                archive_id = await active_archive_id(db, printer_id)
                # The id may name the next printer's run by now (spec gh109-o1 r2 R6).
                if archive_id is not None and printer_manager.session_still_lives(printer_id):
                    tray, spool_id, spoolman_spool_id = await _current_tray_frozen(db, printer_id, state)
                    # The tray read awaited: the row is written under the record's hold, proved again under it
                    # (Codex recheck R2-2). The journal lock is already held; no process lock comes after this.
                    if await printer_manager.hold_record(db, printer_id):
                        await record_event(
                            db,
                            printer_id=printer_id,
                            archive_id=archive_id,
                            layer_num=state.layer_num or 0,
                            event=EVENT_PAUSE,
                            global_tray_id=tray,
                            spool_id=spool_id,
                            spoolman_spool_id=spoolman_spool_id,
                        )
        except Exception as e:
            logging.getLogger(__name__).debug("Pause journal entry failed for printer %d: %s", printer_id, e)

        filename = state.subtask_name or state.gcode_file
        ws_data = {
            "filename": filename,
            "reason": reason_label,
            "reason_code": reason_code,
            "hms_code": hms_code,
        }
        if not printer_manager.session_still_lives(printer_id):
            return
        await ws_manager.send_print_paused(printer_id, ws_data)

        async with async_session() as db:
            await notification_service.on_print_pause(
                printer_id=printer_id,
                printer_name=printer_name,
                filename=filename,
                reason_code=reason_code,
                reason_label=reason_label,
                hms_code=hms_code,
                db=db,
            )
    except Exception as e:
        logging.getLogger(__name__).warning("pause edge handler failed for printer %s: %s", printer_id, e)


async def _handle_resume_edge(printer_id: int, state: PrinterState, *, stock_resume_witnessed: bool = False):
    """Fire on_print_resume notification + WS push on PAUSE→RUNNING.

    Computes paused duration from ``_pause_started_at`` (planted by
    ``_handle_pause_edge``); falls back to ``None`` when the resume hits
    without a recorded start (e.g. BamDude restarted while the printer
    was paused).
    """
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    generation = state.connection_generation
    try:
        printer_info = printer_manager.get_printer(printer_id)
        printer_name = printer_info.name if printer_info else f"Printer {printer_id}"

        started_at = _pause_started_at.pop(printer_id, None)
        paused_for_seconds = int(time.time() - started_at) if started_at is not None else None

        # Clear pause-reason + pause-start from state so the snapshot stops
        # carrying stale data after the resume edge.
        state.pause_reason = None
        state.pause_reason_label = None
        state.pause_started_at = None
        # Drop any one-shot reason hint that might have been planted but
        # never consumed (e.g. plate-detect issued the pause + the printer
        # resumed before the MQTT pause edge made it through).
        _expected_pause_reasons.pop(printer_id, None)

        try:
            from backend.app.models.print_usage_event import EVENT_RESUME
            from backend.app.services import spoolman_activity
            from backend.app.services.print_usage_journal import active_archive_id, journal_lock, record_event

            # The journal lock before the session, as the stock insertion takes it: the external
            # claim below holds SQLite's writer and must not wait for it (post-rebuild review). Its
            # Spoolman admission comes before the lane (C047 acceptance A05), and so does its
            # slot-ledger write, first (A10): the claim reads the mode under it (A08).
            async with (
                spoolman_activity.slot_write("external_resume"),
                spoolman_activity.first_evidence("external_resume"),
                _get_ams_assignment_lock(printer_id),
                journal_lock(printer_id),
                async_session() as db,
            ):
                archive_id = await active_archive_id(db, printer_id)
                # The id may name the next printer's run by now (spec gh109-o1 r2 R6).
                if archive_id is not None and printer_manager.session_still_lives(printer_id):
                    from backend.app.services.auto_stock_spool import claim_on_external_resume

                    outcome = (
                        await claim_on_external_resume(
                            db,
                            printer_id=printer_id,
                            event={
                                "observed_at": observed_at,
                                "generation": generation,
                                "printer_model": printer_manager.effective_model_for(printer_id),
                            },
                            manager=printer_manager,
                        )
                        if stock_resume_witnessed
                        else {"reason": "unwitnessed_resume"}
                    )
                    # The claim proves the record under its own hold; what follows it -- memory, the socket, the
                    # journal -- names the printer again after its awaits (Codex recheck R2-2).
                    if not printer_manager.session_still_lives(printer_id):
                        return
                    if outcome["reason"] == "assigned":
                        from backend.app.services import ams_advertised_overlay as overlay

                        overlay.forget(printer_id, 255, outcome["tray_id"])
                        # Resume was reported by the printer, not requested by
                        # us. Keep its in-flight filament configuration intact;
                        # only inventory attribution changes at the runout layer.
                        await ws_manager.broadcast(
                            {"type": "spool_auto_assigned", "printer_id": printer_id, "spool_id": outcome["spool_id"]}
                        )
                    elif outcome["reason"] == "no_full_stock":
                        await ws_manager.broadcast({"type": "stock_spool_unavailable", "printer_id": printer_id})
                    logging.getLogger(__name__).info(
                        "External stock resume decision: printer=%s generation=%s reason=%s",
                        printer_id,
                        generation,
                        outcome["reason"],
                    )
                    if outcome["reason"] == "assigned":
                        tray, spool_id, spoolman_spool_id = (
                            tray_ids.external_tray_id(outcome["tray_id"]),
                            outcome["spool_id"],
                            None,
                        )
                    else:
                        tray, spool_id, spoolman_spool_id = await _current_tray_frozen(db, printer_id, state)
                    # As the pause edge: written under the record's hold, proved again under it.
                    if await printer_manager.hold_record(db, printer_id):
                        await record_event(
                            db,
                            printer_id=printer_id,
                            archive_id=archive_id,
                            layer_num=state.layer_num or 0,
                            event=EVENT_RESUME,
                            global_tray_id=tray,
                            spool_id=spool_id,
                            spoolman_spool_id=spoolman_spool_id,
                        )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Resume stock/journal processing failed for printer %d: %s", printer_id, e
            )

        filename = state.subtask_name or state.gcode_file
        ws_data = {
            "filename": filename,
            "paused_for_seconds": paused_for_seconds,
        }
        if not printer_manager.session_still_lives(printer_id):
            return
        await ws_manager.send_print_resumed(printer_id, ws_data)

        async with async_session() as db:
            await notification_service.on_print_resume(
                printer_id=printer_id,
                printer_name=printer_name,
                filename=filename,
                paused_for_seconds=paused_for_seconds,
                db=db,
            )
    except Exception as e:
        logging.getLogger(__name__).warning("resume edge handler failed for printer %s: %s", printer_id, e)


async def _maybe_notify_printer_offline(printer_id: int) -> None:
    """Wait the debounce window, then fire on_printer_offline if still offline (#1752).

    Scheduled by ``on_printer_status_change`` on the connected→disconnected edge;
    cancelled by the same handler if the printer reconnects within the window, so a
    single MQTT blip + recovery doesn't notify. Both the staleness-detector path
    (``bambu_mqtt.py::check_staleness``) and the smart-plug power-off path
    (``printer_manager.mark_printer_offline``) route through the same status-change
    callback, so this covers both.
    """
    logger = logging.getLogger(__name__)
    try:
        await asyncio.sleep(_PRINTER_OFFLINE_NOTIFY_DEBOUNCE_SECONDS)
        if printer_manager.is_connected(printer_id):
            return
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if printer is None:
                return
            logger.info(
                "[#1752] Dispatching on_printer_offline for printer %s (%s)",
                printer_id,
                printer.name,
            )
            await notification_service.on_printer_offline(printer_id, printer.name, db)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a background notify must never crash the loop
        logger.warning("Printer offline notification failed for printer %s: %s", printer_id, e)
    finally:
        _printer_offline_notify_tasks.pop(printer_id, None)


def _fts_status_key(state: PrinterState) -> tuple:
    """The Filament Track Switch part of the status broadcast dedup key.

    A freshly connected real ``PrinterState`` always carries the default
    FilaSwitchState; partial snapshots during startup are tolerated — absence
    means no FTS evidence, never installed. The inlet bindings and readiness are
    in it because neither lives in the tray part of the key nor in the AMS
    change-hash (tray fields only, it drives Spoolman sync): without them
    "Join IN-B" on the printer screen moved nothing on the page (upstream
    7a42e0a7). Which slot each hotend holds is in it too: it moves on every
    filament change — one push per toolchange, not a stream — and the Load
    dialog reads it live (upstream 9500c046).
    """
    from backend.app.utils.fila_switch import extruder_slots_payload, inlet_bindings, switch_ready

    fila_switch = getattr(state, "fila_switch", None)
    return (
        getattr(fila_switch, "installed", False),
        getattr(state, "fts_pending_confirmation", False),
        tuple(getattr(fila_switch, "in_slots", ())),
        tuple(getattr(fila_switch, "out_extruders", ())),
        getattr(fila_switch, "stat", 0),
        getattr(fila_switch, "info", 0),
        tuple(sorted(inlet_bindings(state).items())),
        switch_ready(state),
        tuple(
            (ext, slot["ams_id"], slot["slot_id"], slot["has_filament"])
            for ext, slot in extruder_slots_payload(state).items()
        ),
    )


async def on_printer_layer_change(printer_id: int, layer_num: int, previous_layer: int, observation=None):
    """Capture timelapse frame on layer change + first layer notification."""
    from backend.app.services.layer_timelapse import on_layer_change as tl_layer_change

    await tl_layer_change(printer_id, layer_num)
    # After each await: a deletion of this printer record meanwhile ends the old session's work here
    # -- the id may already name the next printer and its run (spec gh109-o1 r2 R6).
    if not printer_manager.session_still_lives(printer_id):
        return

    # #1867 follow-on: bank a recent in-print frame so the FINISH-state
    # finish-photo path (firmware that never emits stg_cur=22, e.g. A1 Mini)
    # has a pre-swap image to fall back on instead of a live grab of the
    # swapped plate. Layer-driven, so it freezes at the final object layer.
    # Must sit after tl_layer_change (timelapse capture keeps priority) and
    # before the first-layer block below, which returns early when the
    # printer isn't RUNNING.
    await _maybe_bank_inprint_frame(printer_id, layer_num)
    if not printer_manager.session_still_lives(printer_id):
        return

    # Layer-triggered macros. Sits before the first-layer block below,
    # which returns early when the printer isn't RUNNING.
    from backend.app.services.macro_trigger import fire_layer_macros

    await fire_layer_macros(printer_id, layer_num, previous_layer, async_session, printer_manager)
    if not printer_manager.session_still_lives(printer_id):
        return

    # First-layer-complete notification (layer_num >= 2 means layer 1 is done).
    # Gated on the printer actually PRINTING: some models (e.g. P1S) tick layer_num
    # during pre-print calibration ~30 min early, which fired a bogus first-layer photo
    # of an empty plate. Defer until state == RUNNING with no mc_print_sub_stage
    # (calibration / prep) active, and only mark notified AFTER that check so a
    # premature tick doesn't burn the one-shot. Widened to <=10 to tolerate the extra
    # early ticks the gate now skips (#1837).
    if 2 <= layer_num <= 10 and not _first_layer_notified.get(printer_id, False):
        # A restart recovery still looking this print's run up binds it -- and seeds the cache above from the
        # run's marker -- before anything here can know whether the print was announced already.
        for adoption in list(_running_print_adoptions.get(printer_id, ())):
            try:
                await asyncio.wait_for(adoption.wait(), _RUN_ADOPTION_WAIT_SECONDS)
            except TimeoutError:
                break
        if not printer_manager.session_still_lives(printer_id) or _first_layer_notified.get(printer_id, False):
            return
        client = printer_manager.get_client(printer_id)
        state = client.state if client else None
        if not state or state.state != "RUNNING" or getattr(state, "mc_print_sub_stage", 0) not in (None, 0):
            return
        # Once per RUN, not per process: the run's execution archive is claimed before the send, addressed by
        # its immutable identity. A run with no such address yet keeps the process-memory rule alone.
        from backend.app.services import first_layer_announcement
        from backend.app.services.print_run_binding import current_print_run

        run = current_print_run(printer_manager, printer_id)
        if run is not None and run.archive_identity:
            try:
                ours = await first_layer_announcement.claim(async_session, run.archive_id, run.archive_identity)
            except Exception as e:
                # Outcome unknown: never a second announcement. The cache stays empty, so the next edge asks.
                logging.getLogger(__name__).warning("First layer announcement claim failed: %s", e)
                return
            if not printer_manager.session_still_lives(printer_id):
                return
            _first_layer_notified[printer_id] = True
            current = current_print_run(printer_manager, printer_id)
            if not ours or current is None or current.archive_identity != run.archive_identity:
                return
        else:
            _first_layer_notified[printer_id] = True
        from backend.app.services.layer_projection import capture_first_layer_total, first_layer_total_is_current

        try:
            layer_text = capture_first_layer_total(printer_manager, printer_id, observation)
            async with async_session() as db:
                from backend.app.models.printer import Printer

                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                if not printer:
                    return
                printer_name = printer.name
                filename = state.subtask_name or state.gcode_file or "Unknown"
                total_layers = state.total_layers or 0

                image_data = await _capture_snapshot_for_notification(printer_id, printer, logging.getLogger(__name__))
                if not printer_manager.session_still_lives(printer_id):
                    return
                if first_layer_total_is_current(printer_manager, printer_id, layer_text):
                    total_layers = layer_text[2]
                await notification_service.on_first_layer_complete(
                    printer_id, printer_name, filename, total_layers, db, image_data=image_data
                )
        except Exception as e:
            logging.getLogger(__name__).warning("First layer notification failed: %s", e)


def _printer_status_effect_key(state: PrinterState) -> str:
    # Only broadcast if something meaningful changed (reduce WebSocket spam)
    # Include rounded temperatures to detect meaningful temp changes (within 1 degree)
    temps = state.temperatures or {}
    nozzle_temp = round(temps.get("nozzle", 0))
    bed_temp = round(temps.get("bed", 0))
    nozzle_2_temp = round(temps.get("nozzle_2", 0)) if "nozzle_2" in temps else ""
    chamber_temp = round(temps.get("chamber", 0)) if "chamber" in temps else ""

    # Include target temps for heating phase detection
    bed_target = round(temps.get("bed_target", 0))
    nozzle_target = round(temps.get("nozzle_target", 0))

    # Include tray_now and vt_tray hash so external spool changes trigger broadcasts
    vt_tray_key = hash(str(state.raw_data.get("vt_tray", []))) if state.raw_data else 0
    # Include AMS dry_time and tray state values so drying/slot changes trigger broadcasts.
    # dry_status is in the key too (#2533): a cycle that enters Checking (1) or fails
    # with Error (5) without dry_time ever leaving 0 is invisible on dry_time alone, so
    # the frontend's post-ack drying watch would never see the signal it resolves on and
    # would report "never started" for a unit that did start. Upstream keys on dry_time
    # only and therefore inherits that false positive. dry_sub_status is deliberately
    # left out — it churns mid-cycle and nothing renders it.
    # dry_countdown_stalled is here because it flips exactly when nothing else in
    # this key moves — a frozen countdown is what raises it — so without it the
    # card of an idle printer would never hear that its timer is parked.
    ams_dry_key = (
        tuple(
            (a.get("dry_time", 0), a.get("dry_status", 0), bool(a.get("dry_countdown_stalled")))
            for a in (state.raw_data.get("ams") or [])
        )
        if state.raw_data
        else ()
    )
    # Include tray states so load/unload transitions (state 11→10) trigger broadcasts (#784).
    # ⚠️ ``tray_color`` and ``tray_info_idx`` are in here for the advertised-profile
    # overlay: an entry becomes EFFECTIVE the moment the printer echoes what we
    # published (``ams_advertised_overlay.matches_live``), and that echo moves
    # exactly these two fields and nothing else in this key. Without them the
    # "AMS sees" badge and the real spool behind it waited for the next unrelated
    # push. Both are short, low-churn strings — they change when somebody
    # configures a slot, which is a moment that deserves a broadcast anyway.
    ams_tray_key = (
        tuple(
            (t.get("id"), t.get("tray_type", ""), t.get("state"), t.get("tray_color", ""), t.get("tray_info_idx", ""))
            for a in (state.raw_data.get("ams") or [])
            for t in a.get("tray", [])
        )
        if state.raw_data
        else ()
    )
    # ⚠️ The card draws its fans from ``airduct_fans`` alone, and on a machine
    # with an air duct those speeds live nowhere else — the flat fields above
    # cover only three of them, and not the second auxiliary fan at all. Without
    # this the whole tile updated only when something unrelated moved.
    airduct_key = tuple((pid, (part or {}).get("state")) for pid, part in sorted((state.airduct_parts or {}).items()))
    # Preserve the member order inside every firmware group (it is the printer's
    # reported rotation order), while sorting extruder keys for stable dedup.
    ams_backup_key = (
        tuple(
            (extruder, tuple(tuple(group) for group in groups))
            for extruder, groups in sorted(state.ams_backup_groups.items())
        )
        if state.ams_backup_groups is not None
        else None
    )
    fts_key = _fts_status_key(state)
    nozzles_key = tuple(
        (n.nozzle_type, getattr(n, "nozzle_flow", ""), n.nozzle_diameter)
        for n in (getattr(state, "nozzles", None) or [])
    )
    return (
        f"{state.connected}:{state.state}:{state.progress}:{state.layer_num}:{state.total_layers}:"
        f"{nozzle_temp}:{bed_temp}:{nozzle_2_temp}:{chamber_temp}:"
        f"{state.stg_cur}:{bed_target}:{nozzle_target}:"
        f"{state.cooling_fan_speed}:{state.big_fan1_speed}:{state.big_fan2_speed}:"
        f"{state.chamber_light}:{state.active_extruder}:{state.tray_now}:{vt_tray_key}:"
        # Without these two, changing the air-duct mode changes nothing in
        # this key, so no broadcast is sent and the card keeps the old
        # selection until something else happens to move.
        f"{state.airduct_mode}:{state.airduct_sub_mode}:{airduct_key}:{state.heatbreak_fan_speed}:"
        # Everything else the card draws. All discrete and low-churn, so
        # they cost nothing here — a door opens once, a speed level is
        # chosen once. The noisy fields (progress, temperatures) were
        # already in this key, which is why adding these changes the
        # broadcast rate hardly at all.
        #
        # ⚠️ ``last_ams_update`` is deliberately NOT here. It is
        # ``time.time()`` stamped on every AMS push, so including it
        # would make the key differ every time and defeat the whole
        # early-return. It rides in the WebSocket payload, where a value
        # that changes constantly is harmless, and triggers nothing.
        f"{state.speed_level}:{state.door_open}:{state.sdcard}:{state.sdcard_state}:"
        f"{state.store_to_sdcard}:{state.timelapse}:{state.ipcam}:"
        f"{state.upgrade_kit_supported}:{state.upgrade_kit_installed}:"
        f"{state.firmware_version}:{state.mc_print_sub_stage}:"
        f"{state.firmware_consistency_request}:{state.firmware_force_upgrade}:"
        f"{ams_dry_key}:{ams_tray_key}:{state.ams_auto_switch_filament}:{ams_backup_key}:"
        f"{fts_key}:"
        # The bounds the temperature inputs are drawn with. They arrive late and
        # rarely — a reported range, or the mains-voltage bit that lowers the bed
        # ceiling — and land in no other field here, so without them the browser
        # would keep whatever bounds happened to be true at the first broadcast.
        f"{state.nozzle_temp_range}:{state.bed_temp_range}:{state.bed_temperature_limit}:"
        f"{state.is_220v}:{sorted(state.ext_has_nozzle.items())}:"
        # Homed-ness drives whether the jog controls are offered at all,
        # and it changes exactly when somebody homes the machine — which is
        # otherwise a moment nothing else in this key notices.
        f"{sorted(state.axis_at_home.items())}:{sorted(state.ext_has_filament.items())}:"
        # Whether a timelapse is possible changes when a card is pulled or
        # fills up — moments nothing else in this key notices.
        f"{state.sdcard_state}:{state.has_timelapse_kit}:{sorted(state.timelapse_storage.items())}:"
        # Pushed since the status shapers were made to agree, and each moves
        # exactly when nothing else here does: a nozzle swapped on an idle
        # printer, a macro the server started or finished, developer mode
        # switched on the printer's screen.
        f"{nozzles_key}:{getattr(state, 'macro_executing', None)}:{getattr(state, 'developer_mode', None)}"
    )


@dataclass(frozen=True)
class _HmsStatusObservation:
    errors: tuple[HMSError, ...]
    muted_errors: tuple[HMSError, ...]
    print_state: str
    reported: tuple[bool, bool]
    observed_at: float


async def initialize_wled_printers(db) -> None:
    """Load every printer's WLED mapping and plate flag, and show offline where no session will start (N02).

    Before any printer connects, so the first status push finds its mapping (upstream #3124). An inactive
    printer, and an ARCHIVED one — our startup connects neither (``init_printer_connections``) — shows its
    offline preset (A12). Each printer's own *Require plate clear* replaces upstream's global setting (A1).
    """
    from sqlalchemy import select as _select

    from backend.app.models.printer import Printer

    rows = await db.execute(
        _select(Printer.id, Printer.wled_config, Printer.require_plate_clear, Printer.is_active, Printer.archived)
    )
    for printer_id, config, require_plate_clear, is_active, archived in rows.all():
        wled_manager.configure_printer(printer_id, config)
        wled_manager.set_require_plate_clear(printer_id, bool(require_plate_clear))
        if config and (not is_active or archived):
            wled_manager.handle_offline(printer_id)


def on_printer_status_change(printer_id: int, state: PrinterState) -> Coroutine[None, None, None]:
    # PrinterManager calls this on the producer thread before queuing the
    # coroutine. The next packet can replace any of these fields immediately.
    hms = _HmsStatusObservation(
        errors=tuple(getattr(state, "hms_errors", None) or ()),
        muted_errors=tuple(getattr(state, "hms_muted", None) or ()),
        print_state=state.state,
        reported=(getattr(state, "hms_array_reported", False), getattr(state, "hms_scalar_reported", False)),
        observed_at=time.time(),
    )
    # Notify!'s print snapshot is taken here too, for the same reason (upstream observes in the callback).
    notify = None
    if not notify_live_activities.dormant:
        try:
            notify = NotifyPrintSnapshot.from_state(state, printer_id)
        except Exception:
            logging.getLogger(__name__).exception("Notify Live Activity snapshot failed for printer %s", printer_id)
    return _handle_printer_status_change(printer_id, state, hms, notify)


async def _handle_printer_status_change(
    printer_id: int, state: PrinterState, hms: _HmsStatusObservation, notify: NotifyPrintSnapshot | None = None
) -> None:
    """Handle printer status changes - broadcast via WebSocket."""
    # Before the dedup below: a printer whose interrupted-dispatch recheck waits for a fresh
    # idle status gets it even when nothing on its card changes (post-rebuild I-R3).
    from backend.app.services import interrupted_dispatch

    interrupted_dispatch.on_status(printer_id, state)
    if notify is not None:
        try:
            notify_live_activities.observe_snapshot(printer_id, notify)
        except Exception:
            logging.getLogger(__name__).exception("Notify Live Activity status hook failed for printer %s", printer_id)

    # Enter the per-printer HMS lock before this task can yield to the next
    # queued report. Notification delivery happens after releasing that lock.
    new_error_codes = await _record_hms_faults(
        printer_id,
        hms.errors,
        hms.print_state,
        hms.reported,
        muted_errors=hms.muted_errors,
        now=hms.observed_at,
    )
    if not printer_manager.session_still_lives(printer_id):
        return

    temps = state.temperatures or {}
    # Auto-detect dual-nozzle printers from MQTT temperature data
    if "nozzle_2" in temps and printer_id not in _nozzle_count_updated:
        _nozzle_count_updated.add(printer_id)
        # Update nozzle_count in database
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if printer and printer.nozzle_count != 2 and printer_manager.session_still_lives(printer_id):
                printer.nozzle_count = 2
                await db.commit()
                logging.getLogger(__name__).info(
                    f"Auto-detected dual-nozzle printer {printer_id}, updated nozzle_count=2"
                )

    status_key = _printer_status_effect_key(state)

    # A session of a deleted printer record stops here: what follows -- relay, pause edges, notifications,
    # the card -- addresses the printer by id, and the awaits above may have spanned the deletion (spec
    # gh109-o1 r2 R6).
    if not printer_manager.session_still_lives(printer_id):
        return

    # The "pause coming up" heads-up: a look at memory on every status, before the
    # dedup below (the time to a pause moves when nothing on the card does). It
    # reads the proven run's forecast and sends nothing to the printer.
    from backend.app.services import pause_warning

    try:
        pause_warning.observe(printer_manager, printer_id)
    except Exception as exc:  # noqa: BLE001 -- never fail the status callback over a heads-up
        logging.getLogger(__name__).warning("Pause warning check failed for printer %s: %s", printer_id, exc)

    # MQTT relay - publish status (before dedup check - always publish to MQTT)
    try:
        printer_info = printer_manager.get_printer(printer_id)
        if printer_info:
            await mqtt_relay.on_printer_status(
                printer_id,
                state,
                printer_info.name,
                printer_info.serial_number,
                awaiting_plate_clear=printer_manager.is_awaiting_plate_clear(printer_id),
            )
    except Exception:
        pass  # Don't fail status callback if MQTT fails

    # WLED (N02, upstream #3124): beside the relay — after the session proof above, before the dedupe, so every
    # push reaches it; it only schedules background sends. A WLED fault never fails the status callback (A4).
    try:
        wled_manager.handle_status(
            printer_id, state, awaiting_plate_clear=printer_manager.is_awaiting_plate_clear(printer_id)
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("WLED status hook failed for printer %s", printer_id, exc_info=True)

    # Every await below may span a deletion of this printer record; what follows each one writes registries
    # or reaches the printer by id, so the session is proved again first (spec gh109-o1 r2 R6).
    if not printer_manager.session_still_lives(printer_id):
        return

    # Pause / resume edge detection — runs BEFORE the dedup early-return so
    # a state-only change (e.g. RUNNING→PAUSE with same temps + progress)
    # still fires the event when the dedup key would otherwise skip it. Edge
    # is computed against ``_last_printer_state`` rather than the snapshot
    # broadcast key because we care about gcode_state transitions, not
    # arbitrary status churn.
    prev_state = _last_printer_state.get(printer_id)
    current_state = state.state
    if prev_state is not None and prev_state != current_state:
        if prev_state == "RUNNING" and current_state == "PAUSE":
            await _handle_pause_edge(printer_id, state)
        elif prev_state == "PAUSE" and current_state == "RUNNING":
            await _handle_resume_edge(
                printer_id,
                state,
                stock_resume_witnessed=(
                    _printer_last_connected.get(printer_id) is True
                    and state.connected
                    and _last_printer_state_generation.get(printer_id) == state.connection_generation
                ),
            )
    if not printer_manager.session_still_lives(printer_id):
        return
    _last_printer_state[printer_id] = current_state
    _last_printer_state_generation[printer_id] = state.connection_generation

    # Offline-notification edge (#1752): schedule on_printer_offline on the
    # connected→disconnected transition. "Back online" is already covered by the
    # print-failure notification on the firmware FAILED report, so no symmetric
    # online event is fired here. Uses spawn_background_task (the only sanctioned
    # create_task wrapper, #1648); the lifespan shutdown cancels any pending task.
    prev_connected = _printer_last_connected.get(printer_id)
    _printer_last_connected[printer_id] = state.connected

    # The MQTT relay gets BOTH edges, unlike the notification above. An
    # integrator subscribed to `.../online` is asking "is it reachable now",
    # which only makes sense as a pair — and the reason there is no online
    # notification (a print-failure report already covers the human case)
    # says nothing about a machine consumer.
    if prev_connected is not None and prev_connected != state.connected:
        try:
            relay_info = printer_manager.get_printer(printer_id)
            if relay_info:
                emit = mqtt_relay.on_printer_online if state.connected else mqtt_relay.on_printer_offline
                await emit(printer_id, relay_info.name, relay_info.serial_number)
        except Exception:
            pass  # A relay failure must never break the status callback.
    if not printer_manager.session_still_lives(printer_id):
        return

    if prev_connected is True and not state.connected:
        existing = _printer_offline_notify_tasks.get(printer_id)
        if existing is None or existing.done():
            _printer_offline_notify_tasks[printer_id] = spawn_background_task(
                _maybe_notify_printer_offline(printer_id),
                name=f"printer-offline-notify-{printer_id}",
            )
    elif state.connected:
        pending = _printer_offline_notify_tasks.pop(printer_id, None)
        if pending is not None and not pending.done():
            pending.cancel()

    if _last_status_broadcast.get(printer_id) == status_key:
        await _broadcast_printer_status_projection(printer_id, state)
        await _deliver_hms_notifications(printer_id, hms.errors, new_error_codes)
        return

    _last_status_broadcast[printer_id] = status_key

    # Check for progress milestone notifications (25%, 50%, 75%)
    progress = state.progress or 0
    is_printing = state.state in ("RUNNING", "PRINTING")

    if is_printing and progress > 0:
        current_milestone = _progress_milestone_to_notify(
            progress,
            layer_num=state.layer_num,
            total_layers=state.total_layers,
            stg_cur=state.stg_cur,
            last=_last_progress_milestone.get(printer_id, 0),
        )

        # If we've crossed a new milestone, send notification
        if current_milestone:
            _last_progress_milestone[printer_id] = current_milestone
            try:
                from backend.app.models.printer import Printer

                # Read the printer in a short session and release the connection
                # BEFORE the ~15s camera snapshot below — holding it across the grab
                # pinned a pooled connection per milestone, per printer (#2572).
                async with async_session() as db:
                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()

                printer_name = printer.name if printer else f"Printer {printer_id}"
                filename = state.subtask_name or state.gcode_file or "Unknown"
                # remaining_time is in minutes, convert to seconds for notification
                remaining_time_seconds = state.remaining_time * 60 if state.remaining_time else None

                # #28: whole-print duration estimated statelessly at the
                # crossing — the duration floors (per provider, per telegram
                # chat) are applied inside on_print_progress, per recipient.
                # The snapshot is handed over as a supplier so a fully muted
                # milestone never pays for the ~15 s grab.
                estimated_minutes = _estimated_total_print_minutes(progress, state.remaining_time)

                async def _milestone_snapshot() -> bytes | None:
                    return await _capture_snapshot_for_notification(printer_id, printer, logging.getLogger(__name__))

                if not printer_manager.session_still_lives(printer_id):
                    return
                # Notification send needs a session (provider/template lookups).
                async with async_session() as db:
                    await notification_service.on_print_progress(
                        printer_id,
                        printer_name,
                        filename,
                        current_milestone,
                        db,
                        remaining_time_seconds,
                        estimated_minutes=estimated_minutes,
                        image_supplier=_milestone_snapshot,
                    )
            except Exception as e:
                logging.getLogger(__name__).warning(f"Progress milestone notification failed: {e}")
    elif progress < 5:
        # Reset milestone tracking when print restarts or new print begins
        _last_progress_milestone[printer_id] = 0
        _first_layer_notified[printer_id] = False
        from backend.app.services.macro_trigger import clear_fired_layer_macros

        clear_fired_layer_macros(printer_id)

    if not printer_manager.session_still_lives(printer_id):
        return

    await _deliver_hms_notifications(printer_id, hms.errors, new_error_codes)

    if not printer_manager.session_still_lives(printer_id):
        return
    repeat_available = await _repeat_available_for(printer_id)
    if printer_manager.session_still_lives(printer_id):
        _projection_repeat_available[printer_id] = repeat_available
        # A display-only publication may have peeked this frame with the old
        # Repeat value. It must not consume this real status's fresh payload.
        await _broadcast_printer_status_projection(printer_id, state, deduplicate=False)


async def _deliver_hms_notifications(
    printer_id: int, current_hms_errors: Sequence[HMSError], new_error_codes: set[str]
) -> None:
    if not current_hms_errors or not new_error_codes or not printer_manager.session_still_lives(printer_id):
        return

    # HMS error codes that should not trigger notifications even though they
    # have known descriptions (e.g. user-initiated actions, not real errors).
    _HMS_NOTIFICATION_SUPPRESS = {
        "0500_400E",  # Printing was cancelled (user action, not an error)
    }

    # Get the actual new errors for the notification
    # Notify on FATAL (1) and SERIOUS (2) only. **Lower is worse** in
    # BS's HMSMessageLevel, so the old ``>= 2`` had this exactly
    # backwards: it dropped every FATAL and kept every INFO. The
    # frontend has always used ``<= 2`` for its red pip, so the two
    # halves of the product disagreed about which faults mattered.
    new_errors = [
        e
        for e in current_hms_errors
        if _hms_notify_key(e) in new_error_codes and e.severity <= HMS_SEVERITY_NOTIFY_THRESHOLD
    ]

    try:
        from backend.app.models.printer import Printer

        # Read the printer in a short session and release the connection
        # BEFORE the ~15s camera snapshot below (#2572).
        async with async_session() as db:
            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()

        printer_name = printer.name if printer else f"Printer {printer_id}"

        # ⚠️ The notification title used to name the module — a table of
        # five guesses. Measured against Bambu's catalogue, four were
        # wrong: 0x03 is bed and heating (not Print/Task), 0x05 is media,
        # camera, USB and cloud (not Nozzle/Extruder), 0x0C is the
        # toolhead camera (not the motion controller), 0x12 is AMS slot
        # motors (not the chamber). Only 0x07 was right, and two modules
        # bigger than most of the table were absent — AMS-HT and the X2D
        # hotend rack. That is how "Device is busy" reached an operator
        # titled "Nozzle/Extruder Error" and sent them to look at a
        # nozzle.
        #
        # Renaming the five would fix today's list and break on the next
        # model, because the table guesses what the catalogue KNOWS. The
        # title now carries the code, which is exact and searchable, and
        # the body carries the catalogue's own description — which
        # already names its subsystem ("Toolhead Camera is offline").
        from backend.app.services.hms_catalogue import describe

        hms_device = hms_device_of(printer_id)

        # Capture camera snapshot once for all error notifications (no DB held).
        error_image_data = await _capture_snapshot_for_notification(printer_id, printer, logging.getLogger(__name__))

        if not printer_manager.session_still_lives(printer_id):
            return
        # The send loop + MQTT relay run in a fresh session (the read one
        # was released before the snapshot).
        async with async_session() as db:
            sent_count = 0
            for error in new_errors:
                # Build short code like "0700_8010"
                # Mask to 16 bits to handle printers that send larger values
                error_code_int = int(error.code.replace("0x", ""), 16) if error.code else 0
                error_code_masked = error_code_int & 0xFFFF
                short_code = f"{(error.attr >> 16) & 0xFFFF:04X}_{error_code_masked:04X}"

                # ⚠️ Severity decides whether to notify, and it already
                # did — ``new_errors`` above is filtered on
                # HMS_SEVERITY_NOTIFY_THRESHOLD. The second gate here
                # was "do we have a description", which is the same
                # mistake the printer card made: "we have text" is not
                # "this is real". A fatal fault fully documented by
                # Bambu stayed silent because our smaller table had
                # never heard of it.
                if short_code in _HMS_NOTIFICATION_SUPPRESS:
                    continue

                description = describe(hms_device, getattr(error, "full_code", None), short_code.replace("_", ""))
                error_type = _hms_label(getattr(error, "full_code", None), short_code)
                # ⚠️ Not `description or short_code`. The title already
                # carries the code, so falling back to it printed the
                # same eight characters twice with the printer name
                # between them and told the operator nothing:
                #
                #     Помилка принтера: 12FF_0001
                #     3DP-030-102
                #     12FF_0001
                #
                # An uncatalogued code gets a place to look it up
                # instead. It is rare now that a code missing from the
                # model's own catalogue falls through to the consensus
                # of the others.
                error_detail = description or "https://wiki.bambulab.com/en/hms/home"

                await notification_service.on_printer_error(
                    printer_id, printer_name, error_type, db, error_detail, image_data=error_image_data
                )
                sent_count += 1

            if sent_count:
                logging.getLogger(__name__).info(
                    f"[HMS] Sent notification for {sent_count} error(s) on printer {printer_id}"
                )

            # Also publish to MQTT relay
            printer_info = printer_manager.get_printer(printer_id)
            if printer_info:
                errors_data = [
                    {
                        "code": e.code,
                        "attr": e.attr,
                        "module": e.module,
                        "severity": e.severity,
                    }
                    for e in new_errors
                ]
                await mqtt_relay.on_printer_error(
                    printer_id, printer_info.name, printer_info.serial_number, errors_data
                )

    except Exception as e:
        logging.getLogger(__name__).warning(f"HMS error notification failed: {e}")


_last_projection_broadcast: dict[int, tuple] = {}
_projection_repeat_available: dict[int, bool] = {}


async def _broadcast_printer_status_projection(printer_id: int, state=None, *, deduplicate: bool = True) -> None:
    """Display only: never run MQTT relay, lifecycle edges or notifications."""
    from backend.app.services.layer_projection import resolve_layer_pair

    if not printer_manager.session_still_lives(printer_id):
        return
    if state is None:
        state, _, _ = printer_manager.peek_status(printer_id)
    if state is None:
        return
    pair = resolve_layer_pair(printer_manager, printer_id, state)
    # The next slicer pause appears when the print's file has been analysed, and
    # nothing else in this key moves then.
    from backend.app.services.pause_forecast import forecast_next_pause

    key = (_printer_status_effect_key(state), pair, forecast_next_pause(printer_manager, printer_id))
    if deduplicate and _last_projection_broadcast.get(printer_id) == key:
        return
    payload = printer_state_to_dict(
        state,
        printer_id,
        printer_manager.get_model(printer_id),
        printer_manager.get_drying_targets(printer_id),
        repeat_available=_projection_repeat_available.get(printer_id, False),
        layer_pair=pair,
    )
    _last_projection_broadcast[printer_id] = key
    await ws_manager.send_printer_status(printer_id, payload)


def _schedule_layer_projection(printer_id: int) -> None:
    spawn_background_task(_broadcast_printer_status_projection(printer_id), name=f"layer-projection-{printer_id}")


printer_manager._layer_projection_callback = _schedule_layer_projection


async def _repeat_available_for(printer_id: int) -> bool:
    """``plate_hold.repeat_available`` for the status broadcasts — never a
    reason for a broadcast to fail. Cheap: no query unless the gate is armed."""
    try:
        from backend.app.services.plate_hold import repeat_available

        return await repeat_available(printer_id)
    except Exception as e:  # noqa: BLE001 — a status push must go out regardless
        logging.getLogger(__name__).debug("repeat_available lookup failed for printer %s: %s", printer_id, e)
        return False


def _is_bambu_uuid(tray_uuid: str) -> bool:
    """Check if a tray UUID looks like a valid Bambu Lab RFID UUID (non-empty, non-zero)."""
    return bool(tray_uuid) and tray_uuid not in ("", "0" * len(tray_uuid))


# The firmware's explicit "this slot is empty" codes. ``apply_tray_exist_bits``
# stamps state=9 on every slot the feed-hall sensor reports vacant, which is the
# same signal BambuStudio draws its empty slots from (``is_exists`` off
# ``tray_exist_bits``, DevFilaSystem.cpp).
_FIRMWARE_EMPTY_STATES = (9, 10)


def slot_reported_no_filament(tray_type: str, tray_state: object) -> bool:
    """True when the slot told us **nothing** — as opposed to told us it is empty.

    The distinction decides whether a spool assignment survives. The
    reconciliation in :func:`on_ams_change` compares the slot's filament against
    the assignment's fingerprint and unlinks on any difference — and ``""``
    differs from every material name, so a slot reporting nothing read as a slot
    reporting *something else*, and healthy links were deleted. Four times in a
    week, each with a fingerprint matching its spool exactly. The clearest came
    two seconds after the user assigned a spool mid-print:

        Auto-unlink: spool 145 AMS255-T0 - fingerprint mismatch
          (cur=50543DFF/  fp=000000FF/PETG  spool=50543DFF/PETG)

    The tray had already come back carrying the NEW spool's colour — the
    assignment had plainly taken, and BambuStudio showed the slot correctly
    configured throughout. Only ``tray_type`` was missing from a partial report,
    and that alone deleted the link. Nothing automatic restores an ``ams_id=255``
    link either, because auto-assign runs inside the ``ams`` loop that the
    external slot never enters.

    ⚠️ **A blank is authoritative only when the firmware says so.** With one of
    :data:`_FIRMWARE_EMPTY_STATES` the slot really is empty and the caller should
    go on to unlink. Without one, a blank type means a partial report, a state
    code our clearing rule does not recognise (an X2D was seen reporting 26), or
    an external slot, which has no exist-bits at all — in every case we were told
    nothing, and being told nothing must change nothing.
    """
    return not (tray_type or "").strip() and tray_state not in _FIRMWARE_EMPTY_STATES


_stock_insertion_seen: dict[tuple[int, int, int], tuple[int, int]] = {}


async def on_stock_spool_inserted(printer_id: int, event: dict):
    logger = logging.getLogger(__name__)
    from sqlalchemy.orm import selectinload

    from backend.app.services import spoolman_activity
    from backend.app.services.auto_stock_spool import claim_on_insertion
    from backend.app.services.print_usage_journal import journal_lock

    # The journal lock BEFORE the session: the claim takes SQLite's writer at once and must
    # never wait for an in-process lock while it holds it (post-rebuild review). The Spoolman
    # admission of its journal facts comes before the lane (C047 acceptance A05), and so does
    # its slot-ledger write, first (A10): the claim reads the mode under it (A08).
    async with (
        spoolman_activity.slot_write("stock_insertion"),
        spoolman_activity.first_evidence("stock_insertion"),
        _get_ams_assignment_lock(printer_id),
        journal_lock(printer_id),
    ):
        key = (printer_id, event["ams_id"], event["tray_id"])
        token = (event["generation"], event["sequence"])
        if token <= _stock_insertion_seen.get(key, (-1, -1)):
            return
        _stock_insertion_seen[key] = token
        try:
            async with async_session() as db:
                outcome = await claim_on_insertion(db, printer_id=printer_id, event=event, manager=printer_manager)
                logger.info(
                    "Stock insertion decision: printer=%s AMS%s-T%s generation=%s sequence=%s reason=%s",
                    printer_id,
                    event["ams_id"],
                    event["tray_id"],
                    event["generation"],
                    event["sequence"],
                    outcome["reason"],
                )
                if outcome["reason"] == "assigned":
                    from backend.app.api.routes.inventory import apply_spool_to_slot_via_mqtt
                    from backend.app.models.spool import Spool
                    from backend.app.services import ams_advertised_overlay as overlay

                    # The replacement has a new identity. A failed publish must
                    # not leave routing looking through the outgoing spool's mask.
                    overlay.forget(printer_id, event["ams_id"], event["tray_id"])

                    spool = (
                        await db.execute(
                            select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == outcome["spool_id"])
                        )
                    ).scalar_one()
                    # Same publisher/projection as manual assignments. Neither
                    # queue colour/profile overrides nor the dispatched mapping
                    # are changed by loading a replacement.
                    published = False
                    try:
                        published = await apply_spool_to_slot_via_mqtt(
                            db=db,
                            current_user=None,
                            spool=spool,
                            printer_id=printer_id,
                            ams_id=event["ams_id"],
                            tray_id=event["tray_id"],
                        )
                    except Exception:
                        logger.exception("Stock assignment configuration failed for printer %s", printer_id)
                    if not published:
                        logger.warning(
                            "Stock spool assigned, but slot configuration was not published for printer %s", printer_id
                        )
                        await ws_manager.broadcast({"type": "stock_spool_config_failed", "printer_id": printer_id})
                    await ws_manager.broadcast(
                        {
                            "type": "spool_auto_assigned",
                            "printer_id": printer_id,
                            "ams_id": event["ams_id"],
                            "tray_id": event["tray_id"],
                            "spool_id": spool.id,
                        }
                    )
                elif outcome["reason"] == "no_full_stock":
                    await ws_manager.broadcast(
                        {
                            "type": "stock_spool_unavailable",
                            "printer_id": printer_id,
                            "ams_id": event["ams_id"],
                            "tray_id": event["tray_id"],
                        }
                    )
        except Exception:
            logger.exception("Stock insertion assignment failed for printer %s", printer_id)


def _unit_reported(ams_data: list, ams_id: int) -> bool:
    """Whether the AMS list names unit ``ams_id`` at all (its trays aside)."""
    for unit in ams_data or []:
        if not isinstance(unit, dict):
            continue
        try:
            if int(unit.get("id", -1)) == ams_id:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _slot_feeds_print(status, ams_id: int, tray_id: int) -> bool:
    """Whether this slot is one the printer is feeding from right now.

    ``tray_now`` names one slot for the whole printer, so on a dual-nozzle
    machine each hotend's own fed slot is asked as well. Meaningful only while
    a print runs; the caller checks that.
    """
    if status is None:
        return False
    global_id = tray_ids.global_tray_id(ams_id, tray_id)
    if getattr(status, "tray_now", None) == global_id:
        return True
    slots = getattr(status, "extruder_slots", None) or {}
    return any(
        getattr(slot, "has_filament", False) and slot.holds(ams_id, tray_id)
        for slot in (slots.values() if isinstance(slots, dict) else [])
    )


async def _recheck_held_unlinks(printer_id: int) -> None:
    """Run the AMS pass again for a printer with a held slot unlink (``slot_unlink_hold``).

    The pass itself decides — under the same lock, from the state the printer
    reports now. A printer that is gone or offline is left alone: cached state
    confirms nothing, and a reconnect restarts the clock anyway.
    """
    state = printer_manager.get_status(printer_id)
    if state is None or not getattr(state, "connected", False):
        return
    current = (getattr(state, "raw_data", None) or {}).get("ams")
    if isinstance(current, list):
        await on_ams_change(printer_id, current)


async def _spoolman_mode_on() -> bool:
    """The Spoolman mode now, from a session of its own (no transaction carried on).

    Unreadable counts as the built-in inventory: the caller then takes the admission —
    the side that can only wait, never the side that writes unguarded.
    """
    from backend.app.api.routes.settings import get_setting

    try:
        async with async_session() as db:
            return (await get_setting(db, "spoolman_enabled") or "").strip().lower() == "true"
    except Exception:  # noqa: BLE001 - see the docstring
        logging.getLogger(__name__).debug("Spoolman mode unreadable before the AMS pass", exc_info=True)
        return False


async def on_ams_change(printer_id: int, ams_data: list):
    # Unlink/replay and insertion assignment must share the same lock. Otherwise
    # a delayed empty-slot callback could delete the freshly assigned refill.
    #
    # The built-in inventory's RFID pass may journal a slot's reel — a first Spoolman fact when
    # a Spoolman binding is still on file — so in that mode the admission is waited for before
    # the lane (C047 acceptance A07); the pass reads the mode again under it. In Spoolman mode
    # the pass does no RFID assignment and takes no admission: an AMS update is no reason to
    # hold back a Spoolman delete. Whatever reaches the journal either way is guarded there
    # (``print_usage_journal._spoolman_fact``).
    #
    # The built-in pass also writes the built-in slot ledger, so it holds a slot-ledger write
    # too, which a switch to Spoolman waits for before it empties that ledger (A08), taken before
    # the admission (A10). A pass that began in Spoolman mode holds neither and leaves the
    # built-in work to the next pass, even when the mode it reads under the lane is the built-in
    # one by then.
    from backend.app.services import spoolman_activity

    built_in = not await _spoolman_mode_on()
    async with AsyncExitStack() as admission:
        if built_in:
            await admission.enter_async_context(spoolman_activity.slot_write("ams_change_rfid"))
            await admission.enter_async_context(spoolman_activity.first_evidence("ams_change_rfid"))
        await admission.enter_async_context(_get_ams_assignment_lock(printer_id))
        state = printer_manager.get_status(printer_id)
        current = getattr(state, "raw_data", {}).get("ams") if state else None
        await _on_ams_change(printer_id, current if isinstance(current, list) else ams_data, built_in_owned=built_in)
    # A spool sits in one slot: what the pass above put into a slot of this
    # printer may still have rows on OTHER printers. Those are deleted under
    # their own printers' locks, one at a time, with this one released.
    from backend.app.services import spool_single_slot

    await spool_single_slot.release_elsewhere(printer_id)


async def _on_ams_change(printer_id: int, ams_data: list, *, built_in_owned: bool = True):
    """Handle AMS data changes - sync to Spoolman if enabled and auto mode.

    ``built_in_owned`` — the caller holds the built-in pass's slot-ledger write (C047
    acceptance A08); without it the built-in inventory's work waits for the next pass.
    """
    logger = logging.getLogger(__name__)

    # The advertised-profile overlay is memory only (spec
    # ams-backup-compatibility §6.3), so a restart would let routing read an
    # advertised black slot as black. Rebuild it from the assignment
    # registries here and not at startup: this callback runs after the whole
    # pushall is parsed, so the device's real capabilities and trays are
    # known — at startup they are not, and the derived entries would never
    # match the printer's echo. Once per printer per process, best-effort.
    try:
        from backend.app.services.ams_backup_compatibility_apply import rebuild_once

        await rebuild_once(printer_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("overlay rebuild skipped for printer %s: %s", printer_id, e)

    # Check if a print is actively running on this printer - if so, skip AMS
    # weight sync to avoid double-deducting spool weight (the usage tracker
    # handles weight deduction precisely during prints via 3MF/G-code data).
    from backend.app.services import usage_tracker as _usage_tracker
    from backend.app.services.ams_sync_debounce import debounce as _ams_decrease_debounce
    from backend.app.services.usage_tracker import _active_sessions, print_is_unbooked

    _print_active = printer_id in _active_sessions

    # A slot that reports empty while a print is running is a filament runout,
    # not a spool swap: the spool is still physically in the AMS, just consumed.
    # Dropping the slot link there loses the only record of which spool fed the
    # print, so the completion path can't charge the runout segment to anything.
    #
    # ⚠️ Read from the printer's state, NOT from ``_print_active``: with Spoolman
    # owning usage there is deliberately no in-memory session, and those users
    # have the same runout.
    _unlink_state = printer_manager.get_status(printer_id)
    printing_now = (getattr(_unlink_state, "state", "") or "").upper() in ("RUNNING", "PAUSE")

    # The remain% weight sync asks one question, in both inventory backends: is a print on this printer
    # still to be charged? A registered session, a printer that reports RUNNING/PAUSE (a restart may have
    # restored no session) and a completion still closing the print out all answer yes -- syncing then
    # writes the print's filament once from the AMS reading and the tracker adds it a second time.
    _weight_sync_held = print_is_unbooked(printer_id, _unlink_state)

    # MQTT relay - publish AMS change
    try:
        printer_info = printer_manager.get_printer(printer_id)
        if printer_info:
            await mqtt_relay.on_ams_change(printer_id, printer_info.name, printer_info.serial_number, ams_data)
    except Exception:
        pass  # Don't fail AMS callback if MQTT fails

    # Broadcast AMS change via WebSocket (bypasses status_key deduplication)
    # This ensures frontend gets immediate updates when AMS slots are configured
    try:
        state = printer_manager.get_status(printer_id)
        if state:
            logger.info("[Printer %s] Broadcasting AMS change via WebSocket", printer_id)
            await ws_manager.send_printer_status(
                printer_id,
                printer_state_to_dict(
                    state,
                    printer_id,
                    printer_manager.get_model(printer_id),
                    printer_manager.get_drying_targets(printer_id),
                    repeat_available=await _repeat_available_for(printer_id),
                ),
            )
    except Exception as e:
        logger.warning("Failed to broadcast AMS change for printer %s: %s", printer_id, e)

    # The low-filament threshold (``services/filament_low.py``): a slot that
    # crossed it announces once. Best-effort — never in the way of the sync.
    try:
        from backend.app.services.filament_low import check_printer as _check_filament_low

        async with async_session() as db:
            await _check_filament_low(db, printer_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("filament_low check skipped for printer %s: %s", printer_id, e)

    from backend.app.utils.color_utils import colors_similar as _colors_similar

    # Auto-unlink spool assignments with stale fingerprints
    try:
        async with async_session() as db:
            from sqlalchemy.orm import selectinload

            from backend.app.api.routes.inventory import _find_tray_in_ams_data, tray_holds_filament
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.printer import Printer as _Printer
            from backend.app.models.spool import Spool as _Spool
            from backend.app.models.spool_assignment import SpoolAssignment as SA
            from backend.app.services.auto_stock_spool import policy_for
            from backend.app.services.spool_tag_matcher import is_valid_tag

            stock_printer = await db.get(_Printer, printer_id)
            protect_partial_returns = (
                stock_printer is not None
                and stock_printer.is_active
                and not stock_printer.archived
                and policy_for(stock_printer).enabled
                and (await get_setting(db, "spoolman_enabled") or "").lower() != "true"
            )

            result = await db.execute(
                select(SA)
                .where(SA.printer_id == printer_id)
                .options(selectinload(SA.spool).selectinload(_Spool.k_profiles))
            )
            stale = []
            # (ams_id, tray_id) of every row we end up deleting — snapshotted so the
            # WS fan-out below can run after the session is released.
            unlinked_slots: list[tuple[int, int]] = []
            # Slots whose "presence bit off while idle" unlink is waiting for a
            # second report (services/slot_unlink_hold, owner decision Р8).
            from backend.app.services import slot_unlink_hold as _unlink_hold

            held_slots: set[tuple[int, int, int]] = set()
            _hold_generation = getattr(_unlink_state, "connection_generation", None)
            if not isinstance(_hold_generation, int) or isinstance(_hold_generation, bool):
                _hold_generation = None
            _hold_report = getattr(_unlink_state, "last_ams_update", None)
            if not isinstance(_hold_report, (int, float)) or isinstance(_hold_report, bool):
                _hold_report = None
            for assignment in result.scalars().all():
                known = assignment.spool
                partial_return = (
                    protect_partial_returns
                    and assignment.ams_id != tray_ids.EXTERNAL_AMS_ID
                    and known is not None
                    and ((known.weight_used or 0) > 0 or known.last_used is not None or known.added_full is False)
                )
                # External spool assignments (ams_id=255) live in vt_tray, not AMS data
                if assignment.ams_id == 255:
                    ps = printer_manager.get_status(printer_id)
                    vt_tray_raw = ps.raw_data.get("vt_tray", []) if ps else []
                    ext_id = tray_ids.external_tray_id(assignment.tray_id)  # 0→254, 1→255, 2..8→247..253
                    current_tray = None
                    for vt in vt_tray_raw:
                        if isinstance(vt, dict) and int(vt.get("id", 254)) == ext_id:
                            current_tray = vt
                            break
                    if not current_tray:
                        # vt_tray data may not have arrived yet - keep assignment
                        continue
                else:
                    current_tray = _find_tray_in_ams_data(ams_data, assignment.ams_id, assignment.tray_id)
                if not current_tray:
                    if assignment.ams_id != 255 and not _unit_reported(ams_data, assignment.ams_id):
                        # The whole unit is absent — unplugged, or not reported
                        # yet. That is no word about its slots: the spools stay
                        # (owner decision Р9). They feed and route nothing while
                        # the unit is away; back on, the same reels are found on
                        # it and a different one is caught by the checks below.
                        # A hold opened for such a slot is not renewed here, so
                        # the unit going away never confirms it.
                        logger.info(
                            "Auto-unlink skipped: spool %d AMS%d-T%d - the unit is not attached",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                        )
                        continue
                    if printing_now or partial_return:
                        # A running print may have run out. With stock loading
                        # enabled, keep a known partial spool too: removal alone
                        # cannot distinguish its return from a full replacement.
                        logger.info(
                            "Auto-unlink skipped: spool %d AMS%d-T%d - empty slot retains known identity "
                            "(printing=%s partial-return=%s)",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                            printing_now,
                            partial_return,
                        )
                        continue
                    logger.info(
                        "Auto-unlink: spool %d AMS%d-T%d - tray not found in AMS data (slot empty?)",
                        assignment.spool_id,
                        assignment.ams_id,
                        assignment.tray_id,
                    )
                    stale.append(assignment)  # Slot empty
                elif _is_bambu_uuid(current_tray.get("tray_uuid", "")):
                    # A Bambu Lab spool is in this slot - check if it's the same spool
                    # that's currently assigned. If yes, keep the assignment (avoids
                    # unnecessary unlink/re-assign/ams_filament_setting cycle that clears
                    # the printer's filament preset on every startup).
                    tray_uuid = current_tray.get("tray_uuid", "")
                    tag_uid = current_tray.get("tag_uid", "")
                    spool = assignment.spool
                    spool_matches = False
                    if spool:
                        if (spool.tray_uuid and spool.tray_uuid.upper() == tray_uuid.upper()) or (
                            spool.tag_uid
                            and tag_uid
                            and tag_uid != "0000000000000000"
                            and spool.tag_uid.upper() == tag_uid.upper()
                        ):
                            spool_matches = True
                    if spool_matches:
                        # Same BL spool still in slot - keep assignment, update fingerprint if needed
                        cur_color = (current_tray.get("tray_color") or "").strip()
                        raw_type = (current_tray.get("tray_type") or "").strip()
                        fp_color = assignment.fingerprint_color or ""
                        fp_type = assignment.fingerprint_type or ""
                        cur_type = support_slot_type(
                            raw_type, current_tray.get("tray_info_idx"), fingerprint_type=fp_type
                        )
                        color_changed = bool(cur_color) and cur_color.upper() != fp_color.upper()
                        type_changed = bool(cur_type) and cur_type.upper() != fp_type.upper()
                        if color_changed or type_changed:
                            if color_changed:
                                assignment.fingerprint_color = cur_color
                            if type_changed:
                                assignment.fingerprint_type = cur_type
                            logger.debug(
                                "Auto-unlink: spool %d AMS%d-T%d - same BL spool, updated fingerprint",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                        continue
                    # Different BL spool or unrecognized - unlink so auto-assign can match
                    logger.info(
                        "Auto-unlink: spool %d AMS%d-T%d - different Bambu Lab spool detected (uuid=%s)",
                        assignment.spool_id,
                        assignment.ams_id,
                        assignment.tray_id,
                        tray_uuid,
                    )
                    stale.append(assignment)
                else:
                    cur_color = (current_tray.get("tray_color") or "").strip()
                    raw_type = current_tray.get("tray_type") or ""
                    cur_state = current_tray.get("state")
                    fp_color = assignment.fingerprint_color or ""
                    fp_type = assignment.fingerprint_type or ""
                    cur_type = support_slot_type(raw_type, current_tray.get("tray_info_idx"), fingerprint_type=fp_type)

                    # Pre-config replay: empty fingerprint_type means the slot
                    # was empty when the user pre-assigned (Bambu firmware
                    # drops ams_filament_setting on empty slots, so MQTT was
                    # deferred). The moment any filament gets inserted —
                    # Bambu RFID, 3rd-party tag, or even an existing-but-now-
                    # reconfigured spool — fire the deferred configuration.
                    # The "loaded" signal is ``state == 11`` (Bambu's
                    # "filament fed to extruder" code) OR, on firmwares that
                    # don't use the state enum meaningfully, a non-empty
                    # ``tray_type`` when state is NOT one of the firmware's
                    # explicit empty signals (9, 10). state-only was wrong
                    # for firmwares that never set 11 — A1 Mini BMCU
                    # 01.07.02.00 and P1S Standard AMS 00.00.06.75 both
                    # always report state=3 — so the replay never fired for
                    # them (upstream Bambuddy #1322 / f45aaea9). The
                    # ``state ∉ {9, 10}`` guard keeps the firmware's explicit
                    # "empty" signals authoritative over any stale tray_type
                    # that might survive the relay's auto-clearing.
                    loaded = tray_holds_filament(current_tray)
                    # Not into the slot a running print feeds from (upstream
                    # 1cc7f4f18): a reel put in after a runout leaves that slot
                    # loaded and blank, and this went out on every AMS change for
                    # the rest of the print. Held for THAT slot only; the first
                    # change after the print sends it. Any other slot is
                    # configured at once, as before.
                    if (
                        not fp_type.strip()
                        and loaded
                        and assignment.spool
                        and printing_now
                        and _slot_feeds_print(_unlink_state, assignment.ams_id, assignment.tray_id)
                    ):
                        logger.debug(
                            "Pre-config held for spool %d AMS%d-T%d: the slot is feeding the print",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                        )
                        continue
                    if not fp_type.strip() and loaded and assignment.spool:
                        try:
                            from backend.app.api.routes.inventory import (
                                apply_spool_to_slot_via_mqtt,
                            )

                            await apply_spool_to_slot_via_mqtt(
                                db=db,
                                current_user=None,
                                spool=assignment.spool,
                                printer_id=printer_id,
                                ams_id=assignment.ams_id,
                                tray_id=assignment.tray_id,
                                current_tray_info_idx=current_tray.get("tray_info_idx", ""),
                                current_tray_type=cur_type,
                                preset_owner_identity=assignment.preset_owner_identity,
                            )
                            logger.info(
                                "Pre-config applied on insert: spool %d → printer %d AMS%d-T%d",
                                assignment.spool_id,
                                printer_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                        except Exception:
                            logger.exception(
                                "Pre-config apply failed for spool %d on printer %d AMS%d-T%d",
                                assignment.spool_id,
                                printer_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                        if cur_color:
                            assignment.fingerprint_color = cur_color
                        assignment.fingerprint_type = cur_type
                        continue

                    # Being told nothing must change nothing. Deliberately placed
                    # AFTER the pre-config replay above: that branch fires on
                    # state==11 with a still-blank type and is about *gaining* a
                    # filament, not losing one.
                    if slot_reported_no_filament(raw_type, cur_state):
                        logger.info(
                            "Auto-unlink skipped: spool %d AMS%d-T%d reported no filament type "
                            "(state=%s colour=%r fp=%s/%s) — absence is not a swap",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                            cur_state,
                            cur_color,
                            fp_color,
                            fp_type,
                        )
                        continue

                    if not raw_type.strip():
                        # The firmware DID say the slot is empty — one of
                        # ``_FIRMWARE_EMPTY_STATES`` with cleared content is the
                        # only blank that gets past the guard above. Empty means
                        # two different things depending on when it is said:
                        # mid-print it is a runout (the reel is consumed, not
                        # removed — same rule as the tray-not-found branch),
                        # while idle it is the user taking the spool out.
                        # Letting the blank fall through to the fingerprint
                        # compare below turned a runout into "spool changed"
                        # (X2D, 2026-08-23) and the runout row froze spool=None.
                        if printing_now or partial_return:
                            logger.info(
                                "Auto-unlink skipped: spool %d AMS%d-T%d - empty slot retains known identity "
                                "(printing=%s partial-return=%s)",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                                printing_now,
                                partial_return,
                            )
                            continue
                        # Off a print too, on firmware's own say-so: a blank report
                        # from a slot whose presence bit is set describes a spool the
                        # AMS cannot identify (non-RFID, or a reset slot), not one
                        # that was taken out — the 9 is our own stale stamp. Deleting
                        # the row threw away the identity the user had supplied
                        # (upstream #3100). A slot the bit calls empty, or one with no
                        # bit, still unlinks.
                        _hold_key = (printer_id, assignment.ams_id, assignment.tray_id)
                        _present = spool_present(current_tray)
                        if _present is True and not _unlink_hold.is_open(_hold_key, row_id=assignment.id):
                            logger.info(
                                "Auto-unlink skipped: spool %d AMS%d-T%d - slot still occupied, "
                                "tray reports no filament data yet",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                            continue
                        # One "empty" report is not a reel taken out (owner
                        # decision Р8, upstream 3497cf0d4): an idle printer was
                        # seen to report a whole unit empty for a single push,
                        # and nothing restores the assignment of an untagged
                        # reel. The unlink of a slot whose presence bit reads 0
                        # waits for a later report that still says so. A slot
                        # that was reported empty and is now occupied-but-blank
                        # keeps that clock running — a different, unreadable
                        # reel may be in there and must not inherit the row.
                        # A slot with no presence bit at all unlinks as before.
                        if _present is not None and not _unlink_hold.offer(
                            _hold_key, row_id=assignment.id, generation=_hold_generation, report_stamp=_hold_report
                        ):
                            held_slots.add(_hold_key)
                            logger.info(
                                "Auto-unlink held: spool %d AMS%d-T%d reports no spool — waiting %.0fs "
                                "for a second report",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                                _unlink_hold.HOLD_SECONDS,
                            )
                            continue
                        logger.info(
                            "Auto-unlink: spool %d AMS%d-T%d - slot reports empty (spool removed)",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                        )
                        stale.append(assignment)
                        continue

                    # A partial report may have a material without a color.
                    # Only a reported color can differ from the fingerprint.
                    color_changed = bool(cur_color) and not _colors_similar(cur_color, fp_color)
                    if color_changed or cur_type.upper() != fp_type.upper():
                        # The tray may have been reconfigured to what WE
                        # advertised (backup-compatibility emulation): under
                        # colour mode the printer echoes the canonical colour,
                        # which matches neither the pre-publish fingerprint nor
                        # the spool — and unlinking here would delete the
                        # assignment, forget the overlay and leave routing
                        # reading the masked slot as the mask. The overlay's own
                        # ``matches_live`` is the proof it was us: it compares
                        # the live tray against the advertised plan we recorded
                        # after a successful publish.
                        from backend.app.services import ams_advertised_overlay as _overlay

                        _entry = _overlay.entries_for(printer_id).get(
                            _overlay.slot_key(assignment.ams_id, assignment.tray_id)
                        )
                        if _entry is not None and _overlay.matches_live(_entry, current_tray):
                            logger.info(
                                "Auto-unlink: spool %d AMS%d-T%d - tray matches the advertised profile "
                                "(%s/%s) — keeping assignment",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                                _entry.advertised_variant,
                                _entry.advertised_color,
                            )
                            if cur_color:
                                assignment.fingerprint_color = cur_color
                            assignment.fingerprint_type = cur_type
                            continue
                        # Fingerprint mismatch - but check if tray now matches the
                        # assigned spool (e.g. auto-configure changed the tray).
                        # The fingerprint is the slot BEFORE our publish, so this
                        # is the check every fresh non-RFID assignment passes
                        # through — and "matches the spool" means the type WE
                        # wrote, which the slot plan decides (family type, or the
                        # generic's), not the material column (#2902).
                        spool = assignment.spool
                        if spool:
                            from backend.app.api.routes.inventory import tray_types_written_for

                            spool_color = (spool.rgba or "FFFFFFFF").upper()
                            spool_types = await tray_types_written_for(
                                db,
                                spool,
                                printer_id,
                                assignment.ams_id,
                                assignment.tray_id,
                                preset_owner_identity=assignment.preset_owner_identity,
                            )
                            if cur_type.upper() in spool_types and (
                                not cur_color or _colors_similar(cur_color, spool_color)
                            ):
                                # Tray was reconfigured to match the spool - update fingerprint
                                logger.info(
                                    "Auto-unlink: spool %d AMS%d-T%d - fingerprint mismatch but tray matches spool, updating fp",
                                    assignment.spool_id,
                                    assignment.ams_id,
                                    assignment.tray_id,
                                )
                                if cur_color:
                                    assignment.fingerprint_color = cur_color
                                assignment.fingerprint_type = cur_type
                                continue
                        logger.info(
                            "Auto-unlink: spool %d AMS%d-T%d - fingerprint mismatch (cur=%s/%s fp=%s/%s spool=%s/%s)",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                            cur_color,
                            cur_type,
                            fp_color,
                            fp_type,
                            spool.rgba if spool else "?",
                            spool.material if spool else "?",
                        )
                        if partial_return and not is_valid_tag(
                            current_tray.get("tag_uid"), current_tray.get("tray_uuid")
                        ):
                            # Untagged remove/reinsert or firmware metadata reset
                            # cannot prove that a used spool became a full one.
                            # Keep its existing persistent identity: the stock
                            # selector still requires a confirmed runout to
                            # replace it, or the operator assigns another spool.
                            logger.info(
                                "Auto-unlink skipped: known partial spool %d needs an explicit replacement",
                                assignment.spool_id,
                            )
                            continue
                        stale.append(assignment)  # Spool changed
            # A hold lives only while its slot keeps reporting empty: every slot
            # this pass did not renew is dropped (the reel is back, a print
            # started, the row changed). What is still waiting asks again by
            # itself — a slot that stays empty produces no AMS change of its own.
            _unlink_hold.keep_only(printer_id, held_slots)
            if held_slots:
                _unlink_hold.schedule_recheck(printer_id, lambda: _recheck_held_unlinks(printer_id))
            # Snapshot the slots before the delete — ORM attribute access after the
            # commit would refresh against a deleted row.
            unlinked_slots = [(a.ams_id, a.tray_id) for a in stale]
            for a in stale:
                await db.delete(a)
            if stale:
                logger.info("Auto-unlinked %d stale spool assignments for printer %d", len(stale), printer_id)
            # Commit any changes (stale deletions and/or fingerprint updates)
            await db.commit()
        # Tell open browsers the assignment is gone (#2575). Only the manual REST
        # assign/unassign endpoints broadcast this event, and the frontend's
        # spool-assignments cache is invalidated by nothing else — so without it
        # every open tab keeps rendering the unlinked spool on the slot until an
        # unrelated refetch, which reads exactly like "the fix didn't work" even
        # though the DB is already correct.
        #
        # Deliberately OUTSIDE the `async with async_session()` block: the WS
        # fan-out is network I/O to every connected browser and must not hold a
        # pooled DB connection (#2572 discipline). Still inside the `try`, so a
        # failed commit above skips the broadcast entirely.
        from backend.app.services import ams_advertised_overlay as overlay

        for ams_id, tray_id in unlinked_slots:
            # The slot no longer holds the spool we advertised a profile for,
            # so there is nothing left to look through the mask at.
            overlay.forget(printer_id, ams_id, tray_id)
            await ws_manager.broadcast(
                {
                    "type": "spool_assignment_changed",
                    "printer_id": printer_id,
                    "ams_id": ams_id,
                    "tray_id": tray_id,
                }
            )
    except Exception as e:
        logger.warning("Spool assignment cleanup failed: %s", e, exc_info=True)

    # Auto-manage inventory spools from AMS tray data (skip if Spoolman
    # manages AMS). Serialised per-printer via _ams_assignment_locks: MQTT
    # bursts can deliver two AMS pushes ~30 ms apart, and without the lock
    # both callbacks read "no existing assignment" for the same
    # (printer, ams, tray) and race to INSERT, hitting the
    # spool_assignment_printer_id_ams_id_tray_id_key unique constraint on
    # Postgres. SQLite's WAL serialises writes so the bug stayed latent
    # there. See _ams_assignment_locks comment for details.
    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.spool import Spool as _Spool2
            from backend.app.models.spool_assignment import SpoolAssignment as SA
            from backend.app.services.spool_tag_matcher import (
                auto_assign_spool,
                create_spool_from_tray,
                find_matching_untagged_spool,
                get_archived_spool_by_tag,
                get_spool_by_tag,
                is_bambu_tag,
                is_valid_tag,
                link_tag_to_inventory_spool,
            )

            _spoolman_on = await get_setting(db, "spoolman_enabled")
            _auto_add_raw = await get_setting(db, "auto_add_unknown_rfid")
            _auto_add_unknown = _auto_add_raw is None or _auto_add_raw.lower() == "true"
            if built_in_owned and (not _spoolman_on or _spoolman_on.lower() != "true"):
                # Rows that were double before "a spool sits in one slot" was a
                # rule are settled as printers report in (owner decision Р36):
                # the slot that reports the tag keeps the spool, else the newest
                # row. Nothing here touches a journal or a session.
                try:
                    from backend.app.services import spool_single_slot as _single_slot

                    _settled = await _single_slot.resolve_duplicates(db, printer_id)
                    # A spool archived before archiving vacated its slots still holds
                    # its row here; it goes once the printer reports that it is idle.
                    _retired = await _single_slot.release_archived(db, printer_id)
                    await db.commit()
                    if _settled.vacated or _retired.vacated:
                        _single_slot._pending_announce.setdefault(printer_id, []).extend(
                            _settled.vacated + _retired.vacated
                        )
                except Exception:  # noqa: BLE001 — a duplicate row survives until the next pass
                    await db.rollback()
                    logger.exception("Could not settle doubled spool assignments for printer %s", printer_id)
                for ams_unit in ams_data:
                    if not isinstance(ams_unit, dict):
                        continue
                    ams_id = int(ams_unit.get("id", 0))
                    for tray in ams_unit.get("tray", []):
                        if not isinstance(tray, dict):
                            continue
                        tray_id = int(tray.get("id", 0))
                        tag_uid = tray.get("tag_uid", "")
                        tray_uuid = tray.get("tray_uuid", "")
                        tray_info_idx = tray.get("tray_info_idx", "")
                        if not tray.get("tray_type"):
                            # Slot reported empty — drop any cached unknown-tag
                            # broadcast so reinserting the same spool re-prompts.
                            _clear_unknown_tag_dedup(printer_id, ams_id, tray_id)
                            continue  # Empty slot
                        # Check if assignment already exists for this slot
                        existing = await db.execute(
                            select(SA)
                            .options(selectinload(SA.spool).selectinload(_Spool2.k_profiles))
                            .where(SA.printer_id == printer_id, SA.ams_id == ams_id, SA.tray_id == tray_id)
                        )
                        existing_assignment = existing.scalar_one_or_none()
                        if existing_assignment:
                            # Skip AMS weight sync while a print is active - the usage
                            # tracker deducts weight precisely from 3MF/G-code data.
                            # Syncing the coarse AMS remain% at the same time would
                            # cause double-deduction of filament weight.
                            if _print_active:
                                continue
                            # Sync spool weight_used from AMS remain - only INCREASE, never decrease.
                            # The AMS remain% is low-resolution (integer %, i.e. 10g steps for 1kg spool)
                            # and must not overwrite precise values from the usage tracker (3MF/G-code).
                            #
                            # ⚠️ Which readings are usable at all is ``grams_used``'s question, not
                            # this loop's — in particular a zero from either field is "nothing to
                            # tell you", never an empty spool. Do not re-add a range check here; the
                            # last one drifted to accept 0 and spent three 1 kg spools in 16 ms.
                            remain_raw = tray.get("remain")
                            if (
                                remain_raw is not None
                                and not _weight_sync_held
                                and existing_assignment.spool
                                and not existing_assignment.spool.weight_locked
                            ):
                                try:
                                    remain_val = int(remain_raw)
                                except (TypeError, ValueError):
                                    remain_val = -1
                                _new_used = grams_used(
                                    tray.get("remain_g"), remain_val, existing_assignment.spool.label_weight
                                )
                                if _new_used is not None:
                                    new_used = _new_used
                                    current_used = existing_assignment.spool.weight_used or 0
                                    if new_used > current_used + 1:
                                        logger.info(
                                            "Weight sync: spool %d weight_used %s -> %s (remain=%d, remain_g=%s)",
                                            existing_assignment.spool_id,
                                            current_used,
                                            new_used,
                                            remain_val,
                                            tray.get("remain_g"),
                                        )
                                        await _usage_tracker.sync_spool_weight_from_ams(
                                            db,
                                            existing_assignment.spool,
                                            printer_id=printer_id,
                                            ams_id=ams_id,
                                            tray_id=tray_id,
                                            new_used=new_used,
                                        )
                                        await db.commit()
                                    elif new_used < current_used - 1 and not printing_now:
                                        # Downward correction — BS-mirror parity for
                                        # RFID spools only, and never on one reading:
                                        # the same value must repeat across two pushes
                                        # ≥60 s apart (reconnect bursts cannot pass;
                                        # zeros never got this far — grams_used refuses
                                        # them). record_ams_sync_usage's decrease branch
                                        # does the bookkeeping: no history row, baseline
                                        # pulled, low-stock re-armed.
                                        _bidir_raw = await get_setting(db, "ams_sync_bidirectional")
                                        if (
                                            (_bidir_raw is None or _bidir_raw.lower() != "false")
                                            and is_bambu_tag(tag_uid, tray_uuid, tray_info_idx)
                                            and _ams_decrease_debounce.offer(
                                                (printer_id, ams_id, tray_id), new_used, str(tray_uuid or "")
                                            )
                                        ):
                                            logger.info(
                                                "Weight sync (down): spool %d weight_used %s -> %s (debounced, tagged)",
                                                existing_assignment.spool_id,
                                                current_used,
                                                new_used,
                                            )
                                            await _usage_tracker.sync_spool_weight_from_ams(
                                                db,
                                                existing_assignment.spool,
                                                printer_id=printer_id,
                                                ams_id=ams_id,
                                                tray_id=tray_id,
                                                new_used=new_used,
                                            )
                                            await db.commit()

                            # Re-bind the K-profile of a tagged spool that has
                            # a K link, when the slot is not on it: the binder
                            # resolves link → active calibration → its LIVE
                            # index, and is handed the index this very push
                            # reports for the tray, so a slot already on the
                            # resolved profile gets nothing. Before that
                            # comparison the block published on every AMS change
                            # while idle — a remain% tick included — whatever
                            # the slot held. It catches "reset slot → re-read"
                            # and any other loss of the selection while the
                            # SpoolAssignment row persists. Upstream removed its
                            # counterpart (f45cc599f); ours stays: a profile
                            # picked for the slot in Bambu Studio is replaced by
                            # the linked one at the next AMS change, as before.
                            try:
                                spool = existing_assignment.spool
                                if (
                                    spool is not None
                                    and is_bambu_tag(tag_uid, tray_uuid, tray_info_idx)
                                    and spool.k_profiles
                                ):
                                    from backend.app.services.calibration_service import (
                                        apply_active_calibration_to_slot,
                                        derive_effective_filament_id,
                                    )
                                    from backend.app.utils.slot_nozzle import slot_nozzle

                                    state = printer_manager.get_status(printer_id)
                                    nozzle = slot_nozzle(state, ams_id, tray_id)

                                    effective_filament_id = await derive_effective_filament_id(
                                        spool=spool, slot_tray_info_idx=tray_info_idx or None, db=db
                                    )
                                    if effective_filament_id:
                                        # The tray's index from the SAME data this
                                        # callback was handed — a cached snapshot
                                        # could be a push behind and skip a bind
                                        # that is needed. Unreported is not "equal".
                                        try:
                                            bound_cali_idx = int(tray.get("cali_idx"))
                                        except (TypeError, ValueError):
                                            bound_cali_idx = None
                                        try:
                                            bound_k = float(tray.get("k"))
                                        except (TypeError, ValueError):
                                            bound_k = None
                                        fired, fc = await apply_active_calibration_to_slot(
                                            db=db,
                                            printer_id=printer_id,
                                            ams_id=ams_id,
                                            slot_id=tray_id,
                                            filament_id=effective_filament_id,
                                            nozzle_diameter=nozzle.diameter_float,
                                            nozzle_volume_type=nozzle.flow_or_standard,
                                            extruder_id=nozzle.extruder_or_default,
                                            spool_id=spool.id,
                                            bound_cali_idx=bound_cali_idx,
                                            bound_k=bound_k,
                                        )
                                        if fired and fc:
                                            logger.info(
                                                "Re-applied K-profile (k=%.3f) for spool %d on printer %d "
                                                "AMS%d-T%d (drift detected)",
                                                fc.pa_k_value or 0,
                                                spool.id,
                                                printer_id,
                                                ams_id,
                                                tray_id,
                                            )
                            except Exception:
                                logger.exception(
                                    "K-profile re-apply failed for printer %d AMS%d-T%d",
                                    printer_id,
                                    ams_id,
                                    tray_id,
                                )
                            continue

                        if is_bambu_tag(tag_uid, tray_uuid, tray_info_idx):
                            # BL spool with RFID tag: auto-match → inventory match → auto-create
                            spool = await get_spool_by_tag(db, tag_uid, tray_uuid)
                            if not spool and await get_archived_spool_by_tag(db, tag_uid, tray_uuid):
                                # An archived spool's reel still in the slot — typically
                                # the one the runout close-out just retired after an
                                # auto-switch. Not unknown: no create, no tag moved onto
                                # another spool, no "+ Add" prompt.
                                continue
                            if not spool:
                                # Try matching an untagged inventory spool (same material/color)
                                spool = await find_matching_untagged_spool(db, tray)
                                if spool:
                                    await link_tag_to_inventory_spool(db, spool, tray)
                                elif _auto_add_unknown:
                                    spool = await create_spool_from_tray(db, tray)
                                else:
                                    # Auto-add disabled: surface the slot so the
                                    # user can add it manually via the UI.
                                    await _broadcast_unknown_tag(
                                        printer_id=printer_id,
                                        ams_id=ams_id,
                                        tray_id=tray_id,
                                        tag_uid=tag_uid,
                                        tray_uuid=tray_uuid,
                                        tray_type=tray.get("tray_type"),
                                        tray_color=tray.get("tray_color"),
                                        tray_sub_brands=tray.get("tray_sub_brands"),
                                        tray_count=len(ams_unit.get("tray", [])),
                                    )
                                    continue
                            # Slot matched (existing tag, untagged inventory
                            # match, or freshly auto-created spool) — drop any
                            # stale dedup so a future tag swap re-prompts.
                            _clear_unknown_tag_dedup(printer_id, ams_id, tray_id)
                            await auto_assign_spool(
                                printer_id,
                                ams_id,
                                tray_id,
                                spool,
                                printer_manager,
                                db,
                                tray_info_idx=tray_info_idx,
                            )
                            await db.commit()
                            await ws_manager.broadcast(
                                {
                                    "type": "spool_auto_assigned",
                                    "printer_id": printer_id,
                                    "ams_id": ams_id,
                                    "tray_id": tray_id,
                                    "spool_id": spool.id,
                                }
                            )
                            logger.info(
                                "RFID auto-assigned spool %d to printer %d AMS%d-T%d",
                                spool.id,
                                printer_id,
                                ams_id,
                                tray_id,
                            )
                        elif is_valid_tag(tag_uid, tray_uuid):
                            if not await get_spool_by_tag(db, tag_uid, tray_uuid) and await get_archived_spool_by_tag(
                                db, tag_uid, tray_uuid
                            ):
                                continue  # an archived spool's reel still in the slot — see above
                            # Non-BL spool with some tag - let user choose
                            await _broadcast_unknown_tag(
                                printer_id=printer_id,
                                ams_id=ams_id,
                                tray_id=tray_id,
                                tag_uid=tag_uid,
                                tray_uuid=tray_uuid,
                                tray_type=tray.get("tray_type"),
                                tray_color=tray.get("tray_color"),
                                tray_sub_brands=tray.get("tray_sub_brands"),
                                tray_count=len(ams_unit.get("tray", [])),
                            )
                        # No-tag slots (generic non-RFID filament) are left alone:
                        # nothing to identify, prompting "+ Add" would just create
                        # ghost spools with empty tags on every confirm.
    except Exception as e:
        logger.warning("RFID spool auto-assign failed: %s", e, exc_info=True)

    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.printer import Printer

            # Check if Spoolman is enabled
            spoolman_enabled = await get_setting(db, "spoolman_enabled")
            if not spoolman_enabled or spoolman_enabled.lower() != "true":
                return

            # Check sync mode
            sync_mode = await get_setting(db, "spoolman_sync_mode")
            if sync_mode and sync_mode != "auto":
                return  # Only sync on auto mode

            _auto_add_raw_sm = await get_setting(db, "auto_add_unknown_rfid")
            auto_add_unknown_rfid = _auto_add_raw_sm is None or _auto_add_raw_sm.lower() == "true"

            # ``spoolman_disable_weight_sync`` is deprecated (#1119) — weight
            # is now always owned by per-print 3MF tracking (mirrors the
            # internal Filament Inventory). The AMS auto-sync path below still
            # refreshes spool metadata + slot assignments, but never writes
            # ``remaining_weight``. The setting is still readable in the
            # settings UI for backwards compat, but has no runtime effect.

            # Get Spoolman URL
            spoolman_url = await get_setting(db, "spoolman_url")
            if not spoolman_url:
                return

            # Get or create Spoolman client
            client = await get_spoolman_client()
            if not client:
                client = await init_spoolman_client(spoolman_url)

            # Check if Spoolman is reachable
            if not await client.health_check():
                logger.warning("Spoolman not reachable at %s", spoolman_url)
                return

            # Get printer name for location
            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            printer_name = printer.name if printer else f"Printer {printer_id}"

            # OPTIMIZATION: Fetch all spools once before processing trays
            # This eliminates redundant API calls (one per tray) when syncing multiple trays
            logger.debug("[Printer %s] Fetching spools cache for AMS sync...", printer_id)
            try:
                cached_spools = await client.get_spools()
                logger.debug("[Printer %s] Cached %d spools for batch sync", printer_id, len(cached_spools))
                archived_reels = ArchivedTagIndex(client)
            except Exception as e:
                logger.error(
                    "[Printer %s] Failed to fetch spools cache after retries, aborting AMS sync: %s",
                    printer_id,
                    e,
                )
                return

            # Load inventory weights as fallback (when AMS MQTT data lacks remain values)
            from sqlalchemy.orm import selectinload

            from backend.app.models.spool_assignment import SpoolAssignment

            inventory_weights: dict[tuple[int, int], float] = {}
            try:
                assign_result = await db.execute(
                    select(SpoolAssignment)
                    .options(selectinload(SpoolAssignment.spool))
                    .where(SpoolAssignment.printer_id == printer_id)
                )
                for assignment in assign_result.scalars().all():
                    spool = assignment.spool
                    if spool and spool.label_weight > 0:
                        remaining = max(0.0, spool.label_weight - (spool.weight_used or 0))
                        inventory_weights[(assignment.ams_id, assignment.tray_id)] = remaining
            except Exception as e:
                logger.debug("Could not load inventory weights for printer %s: %s", printer_id, e)

            # Sync each AMS tray, tracking UUIDs and spool IDs for stale-location cleanup (upstream #921)
            synced = 0
            current_tray_uuids: set[str] = set()
            synced_spool_ids: set[int] = set()
            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                trays = ams_unit.get("tray", [])

                for tray_data in trays:
                    tray = client.parse_ams_tray(ams_id, tray_data)
                    if not tray:
                        # Empty tray slot — drop any cached unknown-tag broadcast
                        # so a reinserted spool re-prompts.
                        _clear_unknown_tag_dedup(printer_id, ams_id, int(tray_data.get("id", 0)))
                        continue  # Empty tray

                    # Track this spool's UUID as currently present in the AMS so that
                    # clear_location_for_removed_spools doesn't clear it below.
                    spool_tag = (
                        tray.tray_uuid
                        if tray.tray_uuid and tray.tray_uuid != "00000000000000000000000000000000"
                        else tray.tag_uid
                    )
                    if spool_tag:
                        current_tray_uuids.add(spool_tag.upper())
                    if spool_tag and await archived_reels.holds_archived_reel(
                        spool_tag, cached_spools, tray.tag_uid or ""
                    ):
                        # An archived spool's reel still in the slot — typically the
                        # one the runout close-out just retired after an auto-switch.
                        # It is not unknown: no auto-create, no "+ Add" prompt.
                        logger.debug(
                            "[Printer %s] AMS %s tray %s holds an archived spool's reel — not syncing",
                            printer_id,
                            ams_id,
                            tray.tray_id,
                        )
                        continue

                    try:
                        inv_remaining = inventory_weights.get((ams_id, tray.tray_id))
                        # Per-print tracking is the only weight writer (#1119).
                        # AMS auto-sync still maintains spool metadata / slot
                        # assignments but no longer touches remaining_weight.
                        result = await client.sync_ams_tray(
                            tray,
                            printer_name,
                            disable_weight_sync=True,
                            cached_spools=cached_spools,
                            inventory_remaining=inv_remaining,
                            auto_add_unknown_rfid=auto_add_unknown_rfid,
                            db=db,
                        )
                        if result is None and spool_tag and not auto_add_unknown_rfid:
                            # Spoolman skipped auto-create per user setting — surface
                            # the slot so the UI can offer "+ Add to inventory".
                            await _broadcast_unknown_tag(
                                printer_id=printer_id,
                                ams_id=ams_id,
                                tray_id=tray.tray_id,
                                tag_uid=tray.tag_uid or "",
                                tray_uuid=tray.tray_uuid or "",
                                tray_type=tray.tray_type,
                                tray_color=tray.tray_color,
                                tray_sub_brands=tray.tray_sub_brands,
                                tray_count=len(trays),
                            )
                        elif result:
                            _clear_unknown_tag_dedup(printer_id, ams_id, tray.tray_id)
                        if result:
                            synced += 1
                            # (slot_preset_mappings retired — slot display names
                            # come from the identity resolver now.)
                            if result.get("id"):
                                synced_spool_ids.add(result["id"])
                                # If a new spool was created, add it to the cache
                                # so subsequent trays can find it if they reference the same tag
                                spool_exists = any(s.get("id") == result["id"] for s in cached_spools)
                                if not spool_exists:
                                    cached_spools.append(result)
                                    logger.debug(
                                        "[Printer %s] Added newly created spool %s to cache",
                                        printer_id,
                                        result["id"],
                                    )
                        # Bidirectional weight leg for Bambu-tagged spools —
                        # never while a print runs (the per-print tracker owns
                        # weight then; #1119's no-weight rule stays for
                        # everything untagged).
                        if result and result.get("id") and not _weight_sync_held:
                            try:
                                await _spoolman_bidirectional_weight_sync(
                                    client, db, printer_id, ams_id, tray, tray_data, result
                                )
                            except Exception as e:
                                logger.debug(
                                    "Bidirectional weight sync failed for AMS %s tray %s: %s",
                                    ams_id,
                                    tray.tray_id,
                                    e,
                                )
                    except SpoolBeingDeleted as refused:
                        # C047 §C3: the background sync skips a spool being deleted; the next report retries.
                        logger.info(
                            "[Printer %s] AMS %s tray %s: Spoolman spool %s is being deleted; skipped",
                            printer_id,
                            ams_id,
                            tray.tray_id,
                            refused.number,
                        )
                    except Exception as e:
                        logger.error("Error syncing AMS %s tray %s: %s", ams_id, tray.tray_id, e)

            if synced > 0:
                logger.info("Auto-synced %s AMS trays to Spoolman for printer %s", synced, printer_id)

            # Clear location for spools no longer in this printer's AMS (upstream #921).
            # Without this, removing a spool from the AMS leaves its Spoolman location pointing
            # at the printer - causing double-booked slots if the spool is later inserted elsewhere.
            try:
                cleared = await client.clear_location_for_removed_spools(
                    printer_name,
                    current_tray_uuids,
                    cached_spools=cached_spools,
                    synced_spool_ids=synced_spool_ids,
                )
                if cleared > 0:
                    logger.info(
                        "Auto-cleared location for %s spools removed from printer %s",
                        cleared,
                        printer_id,
                    )
            except Exception as e:
                logger.error("Error clearing locations for removed spools on printer %s: %s", printer_id, e)

    except Exception as e:
        logging.getLogger(__name__).warning(f"Spoolman AMS sync failed: {e}")


async def _spoolman_bidirectional_weight_sync(client, db, printer_id: int, ams_id: int, tray, tray_data, spool):
    """AMS-driven weight for a Bambu-tagged Spoolman spool, both directions.

    #1119 removed the AMS weight sync wholesale because non-RFID spools have
    no usable ``remain`` and were silently dropped. A valid Bambu tag's remain
    IS the firmware's own estimate — the same number BS mirrors into its
    Filament Manager — so it comes back narrowly, tagged spools only, idle
    only (the caller gates on printing): remaining dropped → ``use_spool`` the
    delta immediately (≥1 g); remaining rose → a downward correction of our
    books, applied only after the two-push debounce, via a
    ``remaining_weight`` PATCH. Zeros never reach here — ``grams_remaining``
    refuses them (inv-zero-remain-is-not-an-empty-spool).
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.services.ams_sync_debounce import debounce as _ams_decrease_debounce
    from backend.app.utils.filament_remaining import grams_remaining

    logger = logging.getLogger(__name__)

    tray_uuid = str(getattr(tray, "tray_uuid", "") or "")
    if not tray_uuid or set(tray_uuid) == {"0"}:
        return  # untagged — #1119 territory, weight stays per-print-owned

    # The spool's own net weight before its filament's: a 250 g refill of a
    # 1000 g filament is 250 g at 100 % (upstream #3194).
    from backend.app.services.spoolman_weights import spool_net_weight

    ref_weight = spool_net_weight(spool)
    if ref_weight is None:
        return

    remain_raw = tray_data.get("remain")
    try:
        remain_val = int(remain_raw) if remain_raw is not None else -1
    except (TypeError, ValueError):
        remain_val = -1
    new_remaining = grams_remaining(tray_data.get("remain_g"), remain_val, int(ref_weight))
    if new_remaining is None:
        return

    current_remaining = spool.get("remaining_weight")
    if current_remaining is None:
        return

    delta_used = float(current_remaining) - float(new_remaining)
    if delta_used > 1:
        await client.use_spool(spool["id"], round(delta_used, 1))
        logger.info(
            "Spoolman weight sync: spool %s consumed %.1fg per AMS reading (AMS%d-T%d)",
            spool["id"],
            delta_used,
            ams_id,
            tray.tray_id,
        )
    elif delta_used < -1:
        _bidir_raw = await get_setting(db, "ams_sync_bidirectional")
        if _bidir_raw is not None and _bidir_raw.lower() == "false":
            return
        if _ams_decrease_debounce.offer((printer_id, ams_id, tray.tray_id), float(new_remaining), tray_uuid):
            await client.update_spool(spool["id"], remaining_weight=round(float(new_remaining), 1))
            logger.info(
                "Spoolman weight sync (down): spool %s remaining -> %.1fg (debounced, tagged, AMS%d-T%d)",
                spool["id"],
                new_remaining,
                ams_id,
                tray.tray_id,
            )


async def _capture_snapshot_for_notification(
    printer_id: int, printer, logger, *, purpose: str = "snapshot"
) -> bytes | None:
    """Capture a camera snapshot for notification image attachment.

    Returns JPEG bytes (max 2.5MB) or None if capture fails or is unavailable.
    Uses: external camera > buffered frame > fresh capture.

    ``purpose`` is what the chamber-light lease reads (services/camera_light):
    a notification photo is a ``snapshot``; the in-print frame banked for the
    finish photo passes ``finish_bank``, which never takes the light.
    """
    if not printer:
        return None

    try:
        from backend.app.api.routes.settings import get_setting

        async with async_session() as db:
            capture_enabled = await get_setting(db, "capture_finish_photo")

        if capture_enabled is None or capture_enabled.lower() != "true":
            return None

        # Try external camera first
        if printer.external_camera_enabled and printer.external_camera_url:
            logger.info("[SNAPSHOT] Capturing from external camera for printer %s", printer_id)
            from backend.app.services.camera_runtime import CameraCaptureRequest, capture

            external_result = await capture(
                CameraCaptureRequest.external(
                    url=printer.external_camera_url,
                    camera_type=printer.external_camera_type or "mjpeg",
                    snapshot_url=printer.external_camera_snapshot_url,
                    purpose=purpose,
                    printer_id=printer_id,
                )
            )
            frame_data = external_result.frame
            if frame_data and len(frame_data) <= 2_500_000:
                logger.info("[SNAPSHOT] External camera frame: %s bytes", len(frame_data))
                return _apply_camera_rotation(frame_data, printer, logger)
            if external_result.source == "live_view":
                # The camera is being watched and its view had no usable frame (camera_runtime.capture stood
                # down). That is not a broken camera: no built-in stand-in from another angle either.
                logger.info("[SNAPSHOT] External camera of printer %s is busy with its live view", printer_id)
                return None

        # Try buffered frame from active stream
        from backend.app.api.routes.camera import get_buffered_frame, is_stream_active

        buffered_frame = get_buffered_frame(printer_id)

        if is_stream_active(printer_id) and buffered_frame:
            logger.info("[SNAPSHOT] Using buffered frame for printer %s: %s bytes", printer_id, len(buffered_frame))
            if len(buffered_frame) <= 2_500_000:
                return _apply_camera_rotation(buffered_frame, printer, logger)

        # Fresh capture from printer camera
        logger.info("[SNAPSHOT] Capturing fresh frame for printer %s", printer_id)
        from backend.app.services.camera_runtime import CameraCaptureRequest, capture

        frame_data = (
            await capture(
                CameraCaptureRequest.builtin(
                    ip_address=printer.ip_address,
                    access_code=printer.access_code,
                    model=printer.model,
                    timeout=15,
                    purpose=purpose,
                    printer_id=printer_id,
                )
            )
        ).frame
        if frame_data and len(frame_data) <= 2_500_000:
            logger.info("[SNAPSHOT] Fresh camera frame: %s bytes", len(frame_data))
            return _apply_camera_rotation(frame_data, printer, logger)

    except Exception as e:
        logger.warning("[SNAPSHOT] Failed to capture snapshot for printer %s: %s", printer_id, e)

    return None


def _finish_photo_key(printer_id: int):
    from backend.app.services.print_run_binding import current_print_effect_token

    token = current_print_effect_token(printer_manager, printer_id)
    return (printer_id, token.generation) if token is not None else None


def _clear_old_finish_frames(printer_id: int) -> None:
    key = _finish_photo_key(printer_id)
    for cache in (_stage22_finish_frames, _stage22_finish_in_flight, _inprint_frame_bank, _inprint_frame_bank_ts):
        for old_key in tuple(cache):
            if old_key[0] == printer_id and old_key != key:
                cache.pop(old_key, None)


async def _maybe_bank_inprint_frame(printer_id: int, layer_num: int) -> None:
    """#1867 follow-on: bank a recent in-print camera frame for the finish photo.

    Called on every layer change. Grabs one frame (throttled) into
    ``_inprint_frame_bank`` so the FINISH-state finish-photo path has a pre-swap
    image on firmware that never emits ``stg_cur=22``. Because it is driven by
    layer_num increases, banking stops the instant printing ends and the End
    G-code (e.g. a plate swap) runs — no further layer changes arrive — so the
    last banked frame is the finished print, not the swapped plate.
    Best-effort: any failure just leaves the previous banked frame.
    """
    from backend.app.services.print_run_binding import current_print_effect_token, print_effect_is_current

    token = current_print_effect_token(printer_manager, printer_id)
    if token is None:
        return
    photo_key = (printer_id, token.generation)
    logger = logging.getLogger(__name__)
    client = printer_manager.get_client(printer_id)
    state = client.state if client else None
    if not state or state.state != "RUNNING":
        return
    # Only during actual extrusion — firmware ticks layer_num during the
    # pre-print calibration sequence, whose sub-stages are non-zero (#1837).
    if getattr(state, "mc_print_sub_stage", 0) not in (None, 0):
        return

    # Once a finish photo has been taken for this print, the FINISH-state
    # fallback is a no-op (``_finish_photo_captured`` makes it one), so nothing
    # will ever read the bank again. Keep banking and we would open a camera
    # socket concurrently with that very grab: the same layer_num packet fires
    # ``on_layer_change`` and the last-layer ``on_finish_photo_moment`` as two
    # independent tasks, and the camera service has no capture lock. Bailing here
    # protects the path that already works. (BamDude addition — upstream's helper
    # has no such guard, and upstream would collide on every caught last-layer
    # edge because the final layer deliberately bypasses the throttle below.)
    if getattr(client, "_finish_photo_captured", False) or photo_key in _stage22_finish_in_flight:
        return

    total = state.total_layers or 0
    is_last_layer = total > 0 and layer_num >= total
    now = time.monotonic()
    last = _inprint_frame_bank_ts.get(photo_key, 0.0)
    if not is_last_layer and (now - last) < _INPRINT_BANK_MIN_INTERVAL:
        return
    if photo_key in _inprint_bank_in_flight:
        return
    # Stamp the ATTEMPT, not the success (divergence from upstream): with
    # ``capture_finish_photo`` off, or a camera that times out, upstream would
    # re-run two DB round-trips plus a 15 s RTSP timeout on EVERY layer change.
    _inprint_frame_bank_ts[photo_key] = now
    _inprint_bank_in_flight.add(photo_key)

    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
        if not printer or not print_effect_is_current(printer_manager, token):
            return
        # Never open a competing camera socket while a viewer is attached but the
        # fan-out buffer is momentarily empty — some firmwares (X2D 01.01.00.00,
        # and the single-connection port-6000 chamber stream) drop the live viewer
        # when a second socket opens. Same discipline as
        # ``obico_detection._capture_frame``. An external camera competes just the same: a USB device has
        # one handle (#2707), so the question is asked for both kinds. Only a camera with its own snapshot
        # URL is let through: that is an HTTP GET beside the stream (camera_runtime.capture).
        from backend.app.api.routes.camera import live_frame_for_picture

        defer_live, buffered_live = live_frame_for_picture(printer_id, printer)
        if defer_live and buffered_live is None:
            return
        # Reuses the notification snapshot path, which honours the
        # ``capture_finish_photo`` setting (returns None when disabled) so we
        # don't bank frames the user never asked for. Under its own purpose:
        # this runs every few layers all print long, and lighting it flashed
        # the chamber light on every other layer — ``finish_bank`` never takes
        # the light; the finish photo that actually shoots still does.
        frame = await _capture_snapshot_for_notification(printer_id, printer, logger, purpose="finish_bank")
        if frame and print_effect_is_current(printer_manager, token):
            _inprint_frame_bank[photo_key] = frame
            logger.debug(
                "[FINISH-PHOTO-BANK] banked in-print frame for printer %s at layer %s/%s (%d bytes)",
                printer_id,
                layer_num,
                total,
                len(frame),
            )
    except Exception as e:
        logger.debug("[FINISH-PHOTO-BANK] bank failed for printer %s: %s", printer_id, e)
    finally:
        _inprint_bank_in_flight.discard(photo_key)


def _apply_camera_rotation(image_data: bytes, printer, logger) -> bytes:
    """Apply the printer's configured camera rotation to a captured still.

    Thin wrapper over ``camera.apply_camera_rotation`` — the implementation is
    shared with the layer-timelapse and finish-photo paths, which used to leave
    their images unrotated while the snapshot of the very same print came out
    the right way up (#2708).
    """
    from backend.app.services.camera import apply_camera_rotation

    return apply_camera_rotation(image_data, getattr(printer, "camera_rotation", 0), logger)


async def _send_print_start_notification(
    printer_id: int,
    data: dict,
    archive_data: dict | None = None,
    logger=None,
):
    """Helper to send print start notification with optional archive data."""
    if logger is None:
        logger = logging.getLogger(__name__)

    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            printer_name = printer.name if printer else f"Printer {printer_id}"

            # Capture camera snapshot for notification image attachment
            image_data = await _capture_snapshot_for_notification(printer_id, printer, logger)
            if image_data:
                if archive_data is None:
                    archive_data = {}
                archive_data["image_data"] = image_data

            await notification_service.on_print_start(printer_id, printer_name, data, db, archive_data=archive_data)

            # Send user-specific email notification for print start
            if archive_data and archive_data.get("created_by_id"):
                await notification_service.send_user_print_email(
                    event_type="user_print_start",
                    created_by_id=archive_data["created_by_id"],
                    printer_name=printer_name,
                    filename=data.get("subtask_name") or data.get("filename", "Unknown"),
                    db=db,
                )
    except Exception as e:
        logger.warning("Notification on_print_start failed: %s", e)


async def _dispatch_user_print_email(
    status: str,
    created_by_id: int | None,
    printer_name: str,
    filename: str,
    db,
    image_data: bytes | None = None,
    finish_photo_url: str | None = None,
) -> None:
    """Send a user-specific print-completion email based on print status.

    Maps the normalised print status to the correct event type and delegates
    to :meth:`NotificationService.send_user_print_email`.  A single helper
    avoids duplicating the ``if status == "completed" / elif "failed" / elif
    "stopped"`` dispatch block at every call site.

    Does nothing if *created_by_id* is ``None``.
    """
    if created_by_id is None:
        return
    if status == "completed":
        event_type = "user_print_complete"
    elif status == "failed":
        event_type = "user_print_failed"
    elif status in ("stopped", "aborted", "cancelled"):
        event_type = "user_print_stopped"
    else:
        return
    await notification_service.send_user_print_email(
        event_type=event_type,
        created_by_id=created_by_id,
        printer_name=printer_name,
        filename=filename,
        db=db,
        image_data=image_data,
        finish_photo_url=finish_photo_url,
    )


async def _notify_completion_without_archive(printer_id: int, data: dict) -> None:
    """The print-complete / failed / stopped notice of a completion that found no archive. Never raises.

    ⚠️ **It guesses no owner** (owner, G03 E-4). In BamDude every print has an archive — every
    dispatch creates its execution archive before the upload, every observed start gets one — so a
    completion that finds none is recovery residue (an unproven start, a start missed while the
    server was down, an archive another closer already ended), never one of our jobs with a known
    owner. It used to take the owner of whatever queue row this printer finished in the last five
    minutes and mail them "your print is done / failed / was stopped" about a print that was not
    theirs; its estimate came from a library column that does not exist. Now only the providers
    subscribed to the event are told, with no personal recipient and no archive figures.
    """
    logger = logging.getLogger(__name__)
    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            printer_name = await db.scalar(select(Printer.name).where(Printer.id == printer_id))
            status = data.get("status", "completed")
            logger.info("[NOTIFY-BG] Sending notification without archive: printer=%s, status=%s", printer_id, status)
            await notification_service.on_print_complete(
                printer_id, printer_name or f"Printer {printer_id}", status, data, db, archive_data=None
            )
            logger.info("[NOTIFY-BG] Completed (no-archive path)")
    except Exception as e:
        logger.warning("[NOTIFY-BG] Failed to send notification without archive: %s", e, exc_info=True)


def _load_objects_from_archive(archive, printer_id: int, logger, *, is_retrigger: bool = False) -> None:
    """Thin wrapper around services.archive.load_objects_from_archive_into_state.

    Kept as a backward-compatible shim — call sites in this file pass a
    ``logger`` arg that the shared helper doesn't take (it uses its own
    module logger). Logging behaviour is equivalent.
    """
    _ = logger  # noqa: F841 — intentional discard, see docstring
    from backend.app.services.archive import load_objects_from_archive_into_state

    load_objects_from_archive_into_state(archive, printer_id, is_retrigger=is_retrigger)


def _bind_print_file_analysis_context(
    printer_id: int, archive, *, expected_generation: int | None = None, origin: str = "observed", observation=None
) -> bool:
    """Bind one manager-owned analysis context to the print archive. False when the start is no longer current.

    This is intentionally only lifecycle wiring.  Nothing is parsed here: once
    the archive's file is known (now, or when a later 3MF is attached) the one
    shared analysis is STARTED as a bounded background read, so what the status
    shows from the file (the next slicer pause) does not wait for a printer card
    or for final accounting to ask.

    ``origin`` is what the CALLER proved about the run: ``dispatch`` (our own
    expected print), ``external`` (a new start observed live) or
    ``exact_subtask`` (the device's unique id); everything found by a name, an
    alias or a hash stays ``observed`` (``print_run_binding.PROVEN_ORIGINS``).
    """
    from backend.app.services.print_file_analysis import bind_print_file_analysis, start_print_file_analysis
    from backend.app.services.print_run_binding import (
        PrintEffectToken,
        bind_print_run,
        print_effect_is_current,
        take_pending_terminal,
    )

    if expected_generation is not None and not print_effect_is_current(
        printer_manager, PrintEffectToken(printer_id, archive.id, expected_generation)
    ):
        return False

    # This is the one lifecycle choke point shared by own, external, adopted
    # and recovered starts.  The archive is already authoritative here; retain
    # that direct address alongside the analysis child context instead of
    # asking a later terminal event to rediscover it from a filename.
    bound = bind_print_run(
        printer_manager,
        printer_id=printer_id,
        archive_id=archive.id,
        observed_subtask_id=getattr(archive, "subtask_id", None),
        archive_identity=getattr(archive, "recovery_identity", None),
        # Lightweight manager doubles used by integrations before client setup
        # do not have a generation registry.  ``None`` is the same unknown
        # evidence a real not-yet-connected printer supplies.
        client_generation=getattr(printer_manager, "current_client_generation", lambda _printer_id: None)(printer_id),
        origin=origin,
    )
    _forget_unbound_user_stop(printer_id)
    if bound.archive_id == archive.id:
        _milestones_follow_run(printer_id, archive.id)
    # The run's first layer was announced before this process bound it (a restart): the cache follows the run.
    from backend.app.services import first_layer_announcement

    if bound.archive_id == archive.id and first_layer_announcement.announced(archive):
        _first_layer_notified[printer_id] = True
    from backend.app.services.layer_projection import bind_layer_grant

    bind_layer_grant(
        printer_manager,
        printer_id,
        archive,
        expected_generation=expected_generation,
        origin=origin,
        observation=observation,
    )
    # The usage journal's runout-tail facts of the print the client is on go to this
    # archive (post-rebuild F-R2) — accounting only, it gates nothing.
    from backend.app.services.print_usage_journal import bind_tail_owner_to_client

    bind_tail_owner_to_client(printer_id, archive.id)

    pending = take_pending_terminal(
        printer_manager,
        printer_id,
        subtask_id=getattr(archive, "subtask_id", None),
    )
    if pending is not None:
        logging.getLogger(__name__).info(
            "Replaying terminal buffered during start for printer %s archive %s sequence %s",
            printer_id,
            bound.archive_id,
            pending.sequence,
        )
        spawn_background_task(
            on_print_complete(printer_id, pending.data),
            name=f"replay-pending-terminal-{printer_id}-{bound.archive_id}",
        )

    relative_path = getattr(archive, "file_path", None)
    source = app_settings.base_dir / relative_path if relative_path else None
    bind_print_file_analysis(
        printer_manager,
        printer_id,
        archive.id,
        source,
        getattr(archive, "plate_index", None),
        expected_identity=getattr(archive, "recovery_identity", None),
    )
    start_print_file_analysis(printer_manager, printer_id, archive.id)
    return True


async def _claim_usage_session(printer_id: int, started_at, archive, bound: bool) -> None:
    """Stamp the usage session this start created with the archive it just bound (GH #115).

    Called by the three ``_on_print_start_impl`` branches that bind an archive, right after the bind. Each
    caller has committed its own session there, so this short session of its own waits on no writer, and a
    failure here never touches the caller's objects. Only a bind that happened (``bound``) claims, and
    ``usage_tracker.claim_session`` stamps only the session with this ``started_at``: a newer start's session
    is never given an older archive. A failure leaves the session unstamped, which a reconcile never lends
    or deletes.
    """
    identity = getattr(archive, "recovery_identity", None)
    if not bound or started_at is None or not identity:
        return
    archive_id = archive.id
    try:
        from backend.app.services.usage_tracker import claim_session

        async with async_session() as db:
            await claim_session(db, printer_id, started_at=started_at, archive_identity=identity)
    except Exception:  # noqa: BLE001 -- attribution bookkeeping must never break a print start
        logging.getLogger(__name__).exception(
            "[UsageTracker] Could not stamp the usage session of printer %s with archive %s", printer_id, archive_id
        )


_PENDING_TERMINAL_TIMEOUT_SECONDS = 45


async def _replay_expired_pending_terminal(printer_id: int, sequence: int) -> None:
    """Bounded escape from a start that never reaches an archive binding."""

    await asyncio.sleep(_PENDING_TERMINAL_TIMEOUT_SECONDS)
    from backend.app.services.print_run_binding import take_pending_terminal

    pending = take_pending_terminal(printer_manager, printer_id, sequence)
    if pending is None:
        return
    logging.getLogger(__name__).warning(
        "Pending terminal for printer %s start sequence %s expired before archive binding; using legacy resolution",
        printer_id,
        sequence,
    )
    await on_print_complete(printer_id, {**pending.data, "_bamdude_pending_terminal_timeout": True})


def _notify_print_file_analysis_source_ready(printer_id: int, archive) -> None:
    """Tell the bound live print that an attached archive file is available."""
    from backend.app.services.layer_projection import notify_current_print_source_ready

    notify_current_print_source_ready(printer_manager, printer_id, archive)


def _name_after_plate_reject(subtask_name: str, corrected_subtask: str | None, filename: str | None) -> str:
    """The title a row keeps after the wrong-plate guard (#1204) refused its 3MF.

    ``corrected_subtask`` is the name re-pointed at the running plate. Without
    one, the original name still titles the row when it carries no plate
    suffix — ``swap_plate_suffix`` returns None for that too, and such a name
    holds no stale plate number to be wrong about (upstream ebc72e1d, #3126);
    blanking it dropped the project name and titled the row ``plate_1`` from the
    gcode path. Only with no name at all is the gcode file's stem what is left.
    Title only: the stale name stays disowned for lookups.
    """
    name = corrected_subtask or subtask_name
    if name:
        return name
    if not filename:
        return ""
    return filename.split("/")[-1].replace(".gcode.3mf", "").replace(".gcode", "").replace(".3mf", "")


async def _check_plate_at_print_start(printer, db, *, may_run=None):
    """Consume the detector result for an already-started print, including unavailable checks."""
    from backend.app.services.plate_detection import PlateDetectionResult, check_plate_empty

    logger = logging.getLogger(__name__)
    roi = printer.plate_detection_roi
    try:
        result = await check_plate_empty(
            printer_id=printer.id,
            ip_address=printer.ip_address,
            access_code=printer.access_code,
            model=printer.model,
            include_debug_image=False,
            external_camera_url=printer.external_camera_url,
            external_camera_type=printer.external_camera_type,
            use_external=printer.external_camera_enabled,
            roi=tuple(roi[k] for k in ("x", "y", "w", "h")) if roi else None,
            polygon=printer.plate_detection_polygon,
            external_camera_snapshot_url=printer.external_camera_snapshot_url,
        )
    except Exception:
        logger.exception("[PLATE CHECK] Capture/check unavailable for printer %s", printer.id)
        result = PlateDetectionResult(False, 0, 0, "Plate check failed", status="unavailable")

    if may_run is not None and not may_run():
        return

    if result.status == "clear":
        logger.info("[PLATE CHECK] Plate is empty for printer %s, proceeding with print", printer.id)
        return
    if result.status == "unavailable" and printer.plate_detection_polygon is None:
        logger.warning(
            "[PLATE CHECK] Check unavailable for printer %s; legacy rectangle policy permits print", printer.id
        )
        return

    unavailable = result.status == "unavailable"
    reason = "plate_check_unavailable" if unavailable else "plate_objects"
    message = (
        "Plate check unavailable. Print paused; inspect the plate and camera calibration."
        if unavailable
        else f"Objects detected on build plate! Print paused. (Diff: {result.difference_percent:.1f}%)"
    )
    logger.warning("[PLATE CHECK] %s for printer %s", reason, printer.id)
    client = printer_manager.get_client(printer.id)
    if client:
        set_expected_pause_reason(printer.id, reason)
        client.pause_print()
    await ws_manager.broadcast(
        {
            "type": "plate_not_empty",
            "printer_id": printer.id,
            "printer_name": printer.name,
            "reason": reason,
            "message": message,
        }
    )
    # An unavailable check must never send an objects-detected push notification.
    if not unavailable:
        try:
            await notification_service.on_plate_not_empty(
                printer_id=printer.id,
                printer_name=printer.name,
                db=db,
                difference_percent=result.difference_percent,
                image_data=getattr(result, "frame", None),
            )
        except Exception as exc:
            logger.warning("[PLATE CHECK] Failed to send notification: %s", exc)


async def _record_unproven_start(printer_id, data, reason):
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import queue_sources
    from backend.app.services.print_observation import observation_is_current
    from backend.app.services.queue_ops import claim_lanes

    epoch = queue_sources.current_epoch()

    def current():
        return (
            queue_sources.current_epoch() == epoch
            and printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life"))
            and (
                data.get("_print_observation") is not None
                or printer_manager.accepts_client_callback_generation(
                    printer_id, data.get("_bamdude_client_generation")
                )
            )
            and observation_is_current(printer_manager, printer_id, data.get("_print_observation"))
        )

    if not current():
        return
    if not hasattr(printer_manager, "_unproven_starts"):
        printer_manager._unproven_starts = {}
    printer_manager._unproven_starts[printer_id] = {
        "reason": reason,
        "epoch": queue_sources.current_epoch(),
        "life": data.get("_bamdude_printer_life", getattr(printer_manager, "_printer_lives", {}).get(printer_id, 0)),
    }
    async with queue_sources.storage_mutation(), async_session() as db, claim_lanes(db, [printer_id]):
        if not current():
            return
        queue = await db.get(PrinterQueue, printer_id, populate_existing=True)
        if queue is not None:
            queue.is_paused = True
            await db.commit()
    logging.getLogger(__name__).warning("Unresolved print observation on printer %s: %s", printer_id, reason)


async def on_print_start(printer_id: int, data: dict):
    """Admit a live start once, independently of its prepared dispatch."""
    from backend.app.services.print_observation import observation_is_current
    from backend.app.services.print_run_binding import (
        claim_print_start,
        current_print_run,
        finish_print_start,
        start_admission_is_current,
    )

    if not (
        observation_is_current(printer_manager, printer_id, data.get("_print_observation"))
        if data.get("_print_observation") is not None
        else printer_manager.accepts_client_callback_generation(printer_id, data.get("_bamdude_client_generation"))
    ):
        logging.getLogger(__name__).warning("Ignoring print_start from stale MQTT client on printer %s", printer_id)
        return
    if not printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life")):
        # A session of a deleted printer record; the id may name the next one (spec gh109-o1 r2 R6).
        logging.getLogger(__name__).warning("Ignoring print_start of a deleted printer record %s", printer_id)
        return
    # A new print: OctoEverywhere's context and what was done for the last one end here (upstream Bambuddy).
    from backend.app.services.octoeverywhere_detection import octoeverywhere_detection_service

    octoeverywhere_detection_service.reset_printer(printer_id)
    # Notify! follows internal jobs too, as upstream does: before the internal-job return.
    try:
        notify_live_activities.print_started(printer_id, printer_manager.get_status(printer_id), data)
    except Exception:
        logging.getLogger(__name__).exception("Notify Live Activity start hook failed for printer %s", printer_id)
    if is_internal_printer_job(data.get("filename", ""), data.get("subtask_name", "")):
        logging.getLogger(__name__).info(
            "[CALLBACK] Skipping archive - internal printer job detected: filename=%s, subtask=%s",
            data.get("filename", ""),
            data.get("subtask_name", ""),
        )
        return
    from backend.app.services.print_execution import ExecutionChanged, decide_start, recheck_start

    if not observation_is_current(printer_manager, printer_id, data.get("_print_observation")):
        return
    try:
        async with async_session() as proof_db:
            prepared = current_print_run(printer_manager, printer_id)
            prepared_id = (
                prepared.archive_id
                if prepared
                and prepared.origin == "dispatch"
                and prepared.matches_device_subtask(data.get("subtask_id"))
                else None
            )
            decision = await decide_start(
                proof_db,
                printer_id,
                data,
                prepared_archive_id=prepared_id,
                prepared_archive_identity=prepared.archive_identity if prepared_id else None,
            )
            await recheck_start(
                proof_db,
                printer_id,
                decision,
                still_current=lambda: (
                    printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life"))
                    and observation_is_current(printer_manager, printer_id, data.get("_print_observation"))
                ),
            )
    except ExecutionChanged as exc:
        await _record_unproven_start(printer_id, data, str(exc))
        return
    if decision.current_id is not None and decision.old:
        from backend.app.models.archive import PrintArchive
        from backend.app.services.print_execution import publish_start

        try:
            async with async_session() as adoption_db:
                adopted = await adoption_db.get(PrintArchive, decision.current_id)
                if adopted is None:
                    raise ExecutionChanged("adopted execution disappeared")
                publication = await publish_start(
                    adoption_db,
                    adopted,
                    decision,
                    still_current=lambda: (
                        printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life"))
                        and (
                            data.get("_print_observation") is not None
                            or printer_manager.accepts_client_callback_generation(
                                printer_id, data.get("_bamdude_client_generation")
                            )
                        )
                        and observation_is_current(printer_manager, printer_id, data.get("_print_observation"))
                    ),
                )
                if not publication.current:
                    _request_published_run_recovery(printer_id, data, decision.epoch)
                    return
        except ExecutionChanged as exc:
            await _record_unproven_start(printer_id, data, str(exc))
            return
    data = {**data, "_execution_decision": decision}
    getattr(printer_manager, "_unproven_starts", {}).pop(printer_id, None)
    admission = claim_print_start(printer_manager, printer_id, data)
    bound = current_print_run(printer_manager, printer_id)
    if admission is None:
        if bound is not None and bound.matches_device_subtask(data.get("subtask_id")):
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                archive = await db.get(PrintArchive, bound.archive_id)
                if (
                    archive is not None
                    and current_print_run(printer_manager, printer_id) == bound
                    and (bound.archive_identity is None or archive.recovery_identity == bound.archive_identity)
                ):
                    _load_objects_from_archive(archive, printer_id, logging.getLogger(__name__), is_retrigger=True)
        logging.getLogger(__name__).info("[CALLBACK] Duplicate print_start for printer %s", printer_id)
        return
    effects_started = False
    try:
        actual_id = str(data.get("subtask_id") or "").strip()
        has_expected = any(key[0] == printer_id for key in _expected_prints)
        if bound is None and actual_id not in ("", "0") and not has_expected:
            # Positive device identity is recovery evidence; a printable name
            # alone is not enough to suppress a new live start.
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                candidates = list(
                    (
                        await db.execute(
                            select(PrintArchive)
                            .where(
                                PrintArchive.printer_id == printer_id,
                                PrintArchive.status == "printing",
                                PrintArchive.subtask_id == actual_id,
                            )
                            .limit(2)
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(candidates) == 1 and start_admission_is_current(printer_manager, printer_id, admission):
                    archive = candidates[0]
                    if not await mark_queue_printing_for_printer(
                        printer_id, archive_id=archive.id, expected_identity=archive.recovery_identity
                    ):
                        return
                    _bind_print_file_analysis_context(
                        printer_id,
                        archive,
                        expected_generation=admission.generation,
                        origin="exact_subtask",
                        observation=data.get("_print_observation"),
                    )
                    _load_objects_from_archive(archive, printer_id, logging.getLogger(__name__), is_retrigger=True)
                    admission.phase = "processed"
                    return
        # A binding restored from a durable run is recovery, not a new live
        # start. Prepared dispatch bindings must take the first-start path.
        if (
            bound is not None
            and bound.origin != "dispatch"
            and bound.observed_subtask_id
            and decision.current_id == bound.archive_id
            and (bound.archive_identity is None or decision.current_identity == bound.archive_identity)
        ):
            if bound.matches_device_subtask(data.get("subtask_id")):
                async with async_session() as db:
                    from backend.app.models.archive import PrintArchive

                    archive = await db.get(PrintArchive, bound.archive_id)
                    if archive is not None and start_admission_is_current(printer_manager, printer_id, admission):
                        _load_objects_from_archive(archive, printer_id, logging.getLogger(__name__), is_retrigger=True)
                admission.phase = "processed"
                return
        if start_admission_is_current(printer_manager, printer_id, admission):
            effects_started = True
            await _on_print_start_impl(
                printer_id,
                {
                    **data,
                    "_bamdude_start_admitted": True,
                    "_bamdude_start_generation": admission.generation,
                },
            )
    except ExecutionChanged as exc:
        await _record_unproven_start(printer_id, data, str(exc))
    except BaseException:
        if not effects_started and admission.phase == "processing":
            from backend.app.services.print_run_binding import retire_print_start

            retire_print_start(printer_manager, printer_id, admission.archive_id, expected=admission)
        raise
    finally:
        finish_print_start(printer_manager, printer_id, admission)


async def _on_print_start_impl(printer_id: int, data: dict):
    """Handle print start - archive the 3MF file immediately."""
    logger = logging.getLogger(__name__)
    from backend.app.services.print_run_binding import PrintEffectToken, print_effect_is_current

    start_generation = data.get("_bamdude_start_generation")

    def owns_start():
        from backend.app.services.print_observation import observation_is_current

        return (
            printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life"))
            and observation_is_current(printer_manager, printer_id, data.get("_print_observation"))
            and (
                start_generation is None
                or print_effect_is_current(printer_manager, PrintEffectToken(printer_id, None, start_generation))
            )
        )

    logger.info("[CALLBACK] on_print_start called for printer %s, data keys: %s", printer_id, list(data.keys()))

    # Clear any stale user-stopped flag from previous print cycles
    _user_stopped_printers.pop(printer_id, None)

    # A print starting on this bed answers the previous print's question: the
    # part is off, or the operator decided it is. Left standing, the gate from
    # the previous FINISH put Clear and Repeat on screen the instant this one
    # ends — while the completion handler is still fetching the 3MF and tidying
    # the card, minutes on a P1S — and Repeat, finding no completed row yet,
    # answered 409 to the card's own button (2026-09-04). The held row goes the
    # way Clear plate sends it, ``answer_by_clearing``: loading the next job is
    # how the operator cleared the plate.
    if printer_manager.is_awaiting_plate_clear(printer_id):
        try:
            from backend.app.services.plate_hold import clear_held_row

            # Its own session, under the printer's lanes and the held print's guard (G03 B, A21).
            await clear_held_row(printer_id)
        except Exception as e:  # noqa: BLE001 — never block a print start on tidying
            logger.warning("[PLATE] Could not let go of the held row for printer %s: %s", printer_id, e)
        if not owns_start():
            return
        printer_manager.set_awaiting_plate_clear(printer_id, False)
        logger.info("[PLATE] Released the plate gate for printer %s — a print is starting on it", printer_id)

    # #1721: drop any leftover pre-captured finish frame from a prior print
    # so a never-consumed cache entry can't bleed into the new print's photo.
    _clear_old_finish_frames(printer_id)
    # #1867 follow-on: same for the in-print frame bank — a queued print must not
    # reuse the previous job's banked frame.

    # Cancel any active bed cooldown task for this printer
    existing_task = _bed_cooldown_tasks.pop(printer_id, None)
    if existing_task and not existing_task.done():
        existing_task.cancel()
        logger.info("[BED-COOL] Cancelled bed cooldown monitor for printer %s (new print started)", printer_id)

    # Clear cached cover images so the new print's thumbnail is fetched fresh
    from backend.app.api.routes.printers import clear_cover_cache

    clear_cover_cache(printer_id)

    if not owns_start():
        return
    try:
        await ws_manager.send_print_start(
            printer_id, {k: v for k, v in data.items() if k not in {"_print_observation", "_execution_decision"}}
        )
    except Exception as e:
        logger.warning("Print-start WebSocket notification failed: %s", e)

    # Fire any user-defined ``print_started`` macros (MQTT-action type today —
    # e.g. "turn chamber light off when print starts"). Fire-and-forget so
    # macro delay_seconds doesn't stall this handler.
    try:
        from backend.app.services.macro_trigger import fire_event_macros

        await fire_event_macros("print_started", printer_id, async_session, printer_manager, may_run=owns_start)
    except Exception as e:
        logger.warning("print_started macros failed to schedule: %s", e)

    if not owns_start():
        return
    # Notify when the print-start AMS mapping references tray slots without spool assignments.
    try:
        await notify_missing_spool_assignments_on_print_start(printer_id, data, logger)
    except Exception as e:
        logger.warning("Print-start spool-assignment notification failed: %s", e)

    # MQTT relay - publish print start
    try:
        printer_info = printer_manager.get_printer(printer_id)
        if printer_info:
            await mqtt_relay.on_print_start(
                printer_id,
                printer_info.name,
                printer_info.serial_number,
                data.get("filename", ""),
                data.get("subtask_name", ""),
            )
    except Exception:
        pass  # Don't fail print start callback if MQTT fails

    # Capture the AMS tray remain%, the assignment snapshot, the dispatched
    # mapping and the seeded tray-change log.
    #
    # Unconditional, for both inventory backends. This only *captures* — the
    # writing is still split, with the internal tracker skipped at completion
    # when Spoolman owns usage. Spoolman's own durable row (#1820) already
    # carries its plate-scoped 3MF figures and stored mapping, but not the
    # tray-change log, and that log is the only record of which spool fed which
    # layers when AMS filament backup swaps trays mid-print. Capturing it on one
    # side only would leave Spoolman users with the mid-print restart bug this
    # fixes for everyone else.
    #
    # The session is made before this start's archive is decided; each branch below that binds the archive
    # stamps it with that archive (``_claim_usage_session``), by the ``started_at`` returned here.
    usage_session_started = None
    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.services.usage_tracker import on_print_start as usage_on_print_start

            _spoolman_on = await get_setting(db, "spoolman_enabled")
            usage_session_started = await usage_on_print_start(
                printer_id,
                data,
                printer_manager,
                db=db,
                spoolman_owns_usage=bool(_spoolman_on) and _spoolman_on.lower() == "true",
            )
    except Exception as e:
        logger.warning("Usage tracker on_print_start failed: %s", e)

    # Track if notification was sent (to avoid sending twice)
    notification_sent = False

    if not owns_start():
        return
    # Smart plug automation: turn on plug when print starts
    try:
        async with async_session() as db:
            await smart_plug_manager.on_print_start(printer_id, db, may_run=owns_start)
    except Exception as e:
        logger.warning("Smart plug on_print_start failed: %s", e)

    async with async_session() as db:
        from backend.app.models.printer import Printer

        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()

        if not owns_start():
            return
        # Auto-light-off used to fire here; it's superseded by the generic
        # macro framework (configure a ``chamber_light`` mqtt-action macro
        # with the value ``off`` on the ``print_started`` event).
        # fire_event_macros() above already dispatched those — nothing to do
        # here.

        # Dispatch-owned prints were checked before publish. Firmware jobs are
        # not user prints; all other starts use the same camera result consumer.
        from backend.app.services.print_run_binding import current_print_run

        _plate_bound = current_print_run(printer_manager, printer_id)
        if (
            printer
            and printer.plate_detection_enabled
            and (not _plate_bound or _plate_bound.origin != "dispatch")
            and not is_internal_printer_job(data.get("filename", ""), data.get("subtask_name", ""))
        ):
            await db.commit()  # Release the connection during camera I/O.
            await _check_plate_at_print_start(printer, db, may_run=owns_start)
            if not owns_start():
                return

        # The archive is the print log: every physical print is one row. There is
        # no "auto-archive" switch — the ``auto_archive`` column of ``printers`` is an upstream
        # leftover column nothing reads (owner, 2026-11-19); a row that still
        # holds False is archived like any other.
        if not printer:
            # Send notification without archive data (no printer row to archive under)
            logger.info("[CALLBACK] Skipping archive - printer %s is not in the DB", printer_id)
            if not notification_sent and owns_start():
                # Even without an archive, try to recover created_by_id from
                # a registered expected print (e.g. a library-file queue item) so the
                # user start email can still be sent.
                _fn = data.get("filename", "")
                _sn = data.get("subtask_name", "")
                _no_archive_creator_keys: list[tuple[int, str]] = []
                if _sn:
                    _no_archive_creator_keys += [
                        (printer_id, _sn),
                        (printer_id, f"{_sn}.3mf"),
                        (printer_id, f"{_sn}.gcode.3mf"),
                    ]
                if _fn:
                    _base_fn = _fn.split("/")[-1] if "/" in _fn else _fn
                    _no_archive_creator_keys.append((printer_id, _base_fn))
                    _no_archive_base = _base_fn.replace(".gcode", "").replace(".3mf", "")
                    _no_archive_creator_keys += [
                        (printer_id, _no_archive_base),
                        (printer_id, f"{_no_archive_base}.3mf"),
                    ]
                _no_archive_creator: int | None = None
                for _key in _no_archive_creator_keys:
                    # Clean up all dicts for every key to avoid memory leaks
                    _expected_prints.pop(_key, None)
                    _expected_print_registered_at.pop(_key, None)
                    popped_creator = _expected_print_creators.pop(_key, None)
                    if _no_archive_creator is None:
                        _no_archive_creator = popped_creator
                _creator_data = {"created_by_id": _no_archive_creator} if _no_archive_creator else None
                await _send_print_start_notification(printer_id, data, _creator_data, logger)
            return

        # Get the filename and subtask_name
        filename = data.get("filename", "")
        subtask_name = data.get("subtask_name", "")
        # Printer-assigned subtask identifier. Normalize "" and "0" to None —
        # both appear as "no subtask" in MQTT pushes and must not match across
        # prints (every missing-id archive would otherwise collide).
        subtask_id = data.get("subtask_id") or None
        if subtask_id == "0":
            subtask_id = None

        logger.info(
            "[CALLBACK] Print start detected - filename: %s, subtask: %s, subtask_id: %s",
            filename,
            subtask_name,
            subtask_id,
        )

        # The printer's own jobs — a calibration run is not a user's print. See
        # utils/print_jobs: the pressure-advance line reports as a subtask name
        # with no /usr/ path, which a prefix test alone never saw (upstream
        # 9c938843 / a4a1f4c5 / 164382b3). No "Print started" either: the event
        # describes the printer calibrating itself, and its completion is
        # silenced in on_print_complete for the same reason.
        if is_internal_printer_job(filename, subtask_name):
            logger.info(
                "[CALLBACK] Skipping archive - internal printer job detected: filename=%s, subtask=%s",
                filename,
                subtask_name,
            )
            return

        if not filename and not subtask_name:
            # Send notification without archive data (no filename)
            logger.info("[CALLBACK] Skipping archive - no filename or subtask_name")
            if not notification_sent and owns_start():
                await _send_print_start_notification(printer_id, data, logger=logger)
            return

        # Check if this is an expected print from reprint/scheduled
        # Build list of possible keys to check
        expected_keys = []
        if subtask_name:
            expected_keys.append((printer_id, subtask_name))
            expected_keys.append((printer_id, f"{subtask_name}.3mf"))
            expected_keys.append((printer_id, f"{subtask_name}.gcode.3mf"))
        if filename:
            fname = filename.split("/")[-1] if "/" in filename else filename
            expected_keys.append((printer_id, fname))
            # Strip extensions to match
            base = fname.replace(".gcode", "").replace(".3mf", "")
            expected_keys.append((printer_id, base))
            expected_keys.append((printer_id, f"{base}.3mf"))

        # Execution identity was proved before start admission. Filename aliases
        # neither adopt a run nor close an older one; publish_start owns that
        # decision and its first transaction, before any download.
        from backend.app.services.print_execution import decide_start

        decision = data.get("_execution_decision") or await decide_start(db, printer_id, data)
        expected_archive_id, fallback_creator = _take_expected_print(expected_keys)
        if not owns_start():
            return

        if expected_archive_id != decision.current_id:
            expected_archive_id = None

        if expected_archive_id:
            # This is a reprint/scheduled print - use existing archive, don't create new one
            logger.info("Using expected archive %s for print (skipping duplicate)", expected_archive_id)
            from backend.app.models.archive import PrintArchive

            result = await db.execute(select(PrintArchive).where(PrintArchive.id == expected_archive_id))
            archive = result.scalar_one_or_none()

            if archive and archive.recovery_identity == decision.current_identity and owns_start():
                # Update archive status to printing
                archive.status = "printing"
                archive.started_at = datetime.now(timezone.utc)
                # The printer runs it: the attempt is accepted, also when its watchdog had
                # already failed a slow parse (post-rebuild review).
                from backend.app.services import dispatch_phase as _dispatch_phase

                _dispatch_phase.mark_seen_running(archive)
                # ...and the end that watchdog wrote is not this print's.
                _dispatch_phase.forget_earlier_outcome(archive)

                # Reprint of an archive reuses the source row. Without resetting
                # ``timelapse_path`` _scan_for_timelapse_with_retries early-returns
                # ("already has timelapse") and _capture_finish_photo_from_timelapse
                # extracts the *original* print's last frame, which then ships in
                # the completion notification (#1707). Clear the path so the
                # scanner runs fresh; also unlink the old video file so reprints
                # don't accumulate orphans in the archive directory. Photos list
                # is left alone — accumulating one finish photo per run is fine.
                stale_timelapse_relpath = archive.timelapse_path
                if stale_timelapse_relpath:
                    # The registry row goes with the pointer, in this transaction —
                    # the seam's writer, not a bare assignment (G10 C).
                    from backend.app.services import archive_media

                    await archive_media.detach_primary_in(db, archive)
                # The earlier run's list of recordings is not this run's boundary.
                from backend.app.services import timelapse_baseline as _timelapse_baseline

                _timelapse_baseline.forget_in(archive)
                # #1403 follow-up: our dispatcher normally creates the archive
                # with printer_id already set, but reprint / adopted /
                # externally-registered expected prints can reach this branch
                # with printer_id=None. Leaving it unset permanently disables
                # every path gated on archive.printer_id — the "Scan for
                # timelapse" action, the per-printer Archives filter, and
                # per-printer stats. Idempotent: a value matching the running
                # printer is preserved (no clobber).
                if archive.printer_id != printer_id:
                    archive.printer_id = printer_id
                await db.commit()
                if stale_timelapse_relpath:
                    # Only now, after the commit that took it off the row, and
                    # only when nobody else shows it (G10 C): a rollback used to
                    # leave the row pointing at a file already gone.
                    await archive_media.unlink_released(db, app_settings.base_dir, stale_timelapse_relpath, archive.id)
                    logger.info(
                        "Released stale timelapse %s on reprint of archive %s",
                        stale_timelapse_relpath,
                        expected_archive_id,
                    )

                # Our own dispatch, registered by the dispatcher itself: a proven run.
                bound = _bind_print_file_analysis_context(
                    printer_id,
                    archive,
                    expected_generation=start_generation,
                    origin="dispatch",
                    observation=data.get("_print_observation"),
                )
                await _claim_usage_session(printer_id, usage_session_started, archive, bound)

                # Track as active print
                _remember_active_print((printer_id, archive.filename), archive)
                if subtask_name:
                    _remember_active_print((printer_id, f"{subtask_name}.3mf"), archive)

                # Start timelapse session if external camera is enabled
                # (upstream Bambuddy #1353 / commit f2e3de0a). The two
                # new-archive paths below (fallback + auto-archive) also
                # call start_session, but queue / VP-dispatched prints land
                # here in the expected-archive branch and used to skip it
                # entirely — so the timelapse session never started, no
                # frames were captured, and the post-print stitch silently
                # returned None. Every queue / reprint silently lost its
                # timelapse.
                if printer.external_camera_enabled and printer.external_camera_url:
                    from backend.app.services.layer_timelapse import start_session

                    start_session(
                        printer_id,
                        archive.id,
                        printer.external_camera_url,
                        printer.external_camera_type or "mjpeg",
                        snapshot_url=printer.external_camera_snapshot_url,
                        rotation=getattr(printer, "camera_rotation", 0) or 0,
                    )
                    logger.info(
                        "Started layer timelapse for printer %s, expected archive %s",
                        printer_id,
                        archive.id,
                    )

                # Ensure queue reflects the busy state (queue-driven flow
                # already sets this from the scheduler, but repeat-safe).
                claim_result = await mark_queue_printing_for_printer(
                    printer_id, archive_id=archive.id, expected_identity=archive.recovery_identity
                )
                if not claim_result:
                    return

                # Set up energy tracking (#941: persist start on archive row)
                await _record_energy_start(archive, printer_id, db, context="expected-print")

                await ws_manager.send_archive_updated(
                    {
                        "id": archive.id,
                        "status": "printing",
                    }
                )

                # Send notification with archive data (reprint/scheduled)
                if not notification_sent and owns_start():
                    # Use archive's created_by_id; fall back to the creator registered via
                    # register_expected_print (handles library-file-based queue items where
                    # the freshly-created archive has no created_by_id yet). It was
                    # taken together with the expectation, by ``_take_expected_print``.
                    archive_data = {
                        "print_time_seconds": archive.print_time_seconds,
                        "created_by_id": archive.created_by_id or fallback_creator,
                    }
                    await _send_print_start_notification(printer_id, data, archive_data, logger)

                # Extract printable objects from the archived 3MF file
                _load_objects_from_archive(archive, printer_id, logger)

                # Store Spoolman tracking data for per-filament usage reporting
                try:
                    await _store_spoolman_print_data(
                        printer_id,
                        archive.id,
                        archive.file_path,
                        db,
                        printer_manager,
                        ams_mapping=_get_start_ams_mapping(data, archive.id, archive.recovery_identity),
                        plate_id=archive.plate_index,
                    )
                except Exception as e:
                    logger.warning("[SPOOLMAN] Failed to store tracking data: %s", e)

                # Capture timelapse file baseline for snapshot-diff on completion
                # (mirrors the new-archive branch). Queue / VP-dispatched prints
                # hit this branch — without the baseline the completion-time scan
                # falls into its "take baseline now" fallback, which snapshots
                # AFTER the new MP4 already exists and never matches a diff
                # (#1403 follow-up).
                await _capture_timelapse_baseline_at_start(
                    printer,
                    printer_id,
                    logger,
                    archive_id=archive.id,
                    archive_identity=archive.recovery_identity,
                    may_run=owns_start,
                )

            return  # Skip creating a new archive

        # Check if there's already a "printing" archive for this printer/file
        # This prevents duplicates when backend restarts during an active print
        from backend.app.models.archive import PrintArchive

        check_name = subtask_name or filename.split("/")[-1].replace(".gcode", "").replace(".3mf", "")

        # Live plate-index from MQTT (``Metadata/plate_N.gcode``). For a
        # multi-plate container the on-disk 3MF + its content_hash are
        # identical across plates — only the plate the printer is actually
        # running differs. Without this, the hash/name adoption blocks below
        # rely solely on ``status='printing' AND printer_id`` to disambiguate,
        # which assumes the cleanup pass left exactly one in-flight row per
        # printer. Adding a plate-index narrowing makes adoption survive the
        # rare case where two ``'printing'`` rows for the same container
        # remain (e.g. a previous plate's row got stuck after a hard crash
        # and the cleanup time-gate could not close it).
        # Legacy archives (pre-m038) carry ``plate_index = NULL``; treat them
        # as plate-agnostic so this filter never excludes a legitimate
        # adoption candidate from an older install.
        # ⚠️ Third source, and for a slicer-launched print the only one that
        # answers: both of the first two are empty there — the printer names
        # such a print by container alone and reports no ``gcode_file`` — while
        # the dispatch command's ``param`` carries ``Metadata/plate_N.gcode``.
        # Last, not first: the two above are read from this print's own live
        # state, whereas the capture is a remembered value.
        live_plate_id = (
            parse_plate_id(data.get("filename"))
            or parse_plate_id((data.get("raw_data") or {}).get("gcode_file"))
            or parse_plate_id(data.get("plate_param"))
        )
        logger.info(
            "[adopt] check_name=%r live_plate_id=%s (filename=%r gcode_file=%r)",
            check_name,
            live_plate_id,
            data.get("filename"),
            (data.get("raw_data") or {}).get("gcode_file"),
        )

        def _plate_filter():
            """Adoption filter for ``plate_index``. None = no filter."""
            if live_plate_id is None:
                return None
            return or_(
                PrintArchive.plate_index == live_plate_id,
                PrintArchive.plate_index.is_(None),
            )

        from backend.app.services.print_execution import ExecutionChanged, decide_start

        existing_archive = (
            await db.get(PrintArchive, decision.current_id, populate_existing=True) if decision.current_id else None
        )
        if existing_archive is not None and (
            existing_archive.recovery_identity != decision.current_identity or existing_archive.status != "printing"
        ):
            raise ExecutionChanged("accepted execution changed")
        if existing_archive:
            # The printer just fired on_print_start for ``check_name``, and we
            # have a matching "printing" archive on file — by definition it IS
            # the current print (a backend restart mid-print re-triggered the
            # event). Adopt it, no matter how old it is. The previous 4-hour
            # stale-cancel heuristic was wrong: on long prints it killed the
            # real row and forced a duplicate to be created below.
            #
            # Divergence from upstream Bambuddy v0.2.3 #972 "revive" path: they
            # KEEP stale-cancel AND add a subtask_id-match revive. BamDude keeps
            # neither — we skip stale-cancel entirely, so there's nothing to
            # revive. An orphan row from a print that finished while BamDude was
            # stopped is closed by the startup sweep in
            # services/print_reconciliation.py, which runs on the first MQTT
            # status after a fresh connect — not by a stale-cancel-by-age
            # heuristic here.
            logger.info(
                "Adopting existing printing archive %s for %s (re-trigger of live print)",
                existing_archive.id,
                check_name,
            )
            _remember_active_print((printer_id, existing_archive.filename), existing_archive)
            # The printer runs it: an attempt of ours left unaccepted is accepted now.
            from backend.app.services import dispatch_phase as _dispatch_phase

            if _dispatch_phase.mark_seen_running(existing_archive):
                await db.commit()
            # Ensure queue reflects the busy state. The options are consulted
            # only if no row exists yet — a re-trigger normally adopts the row
            # the first pass created, which knows more than we do here.
            claim_result = await mark_queue_printing_for_printer(
                printer_id,
                archive_id=existing_archive.id,
                expected_identity=existing_archive.recovery_identity,
                options=_printer_reported_options(
                    data, existing_archive.id, live_plate_id, existing_archive.recovery_identity
                ),
            )
            if not claim_result:
                return
            # Also set up energy tracking if not already tracked (#941: persisted column)
            if existing_archive.energy_start_kwh is None:
                await _record_energy_start(existing_archive, printer_id, db, context="existing-printing")
            bound = _bind_print_file_analysis_context(
                printer_id, existing_archive, expected_generation=start_generation
            )
            await _claim_usage_session(printer_id, usage_session_started, existing_archive, bound)
            # Send notification with archive data (existing archive)
            if not notification_sent and owns_start():
                archive_data = {
                    "print_time_seconds": existing_archive.print_time_seconds,
                    "created_by_id": existing_archive.created_by_id,
                }
                await _send_print_start_notification(printer_id, data, archive_data, logger)
            # Extract printable objects from the archived 3MF file
            _load_objects_from_archive(existing_archive, printer_id, logger, is_retrigger=True)
            return

        # ── The archive row is created HERE, before the 3MF is fetched ─────
        # Reaching this point means no archive exists for this print: the
        # duplicate, expected-print and existing-row branches above all return
        # on their own, and dispatcher-driven prints never come this far.
        #
        # The row used to be created after the download, and that download is
        # slower than it looks: 22 MB off a P1S measured 8m40s *while the
        # printer was printing*, because the file comes back over the same SD
        # card the print is reading from (43 KB/s; the identical fetch on an
        # idle printer took 96 s). For those eight minutes the print existed
        # nowhere in BamDude — no card in Archives, no start notification, no
        # busy queue, no energy baseline. The baseline is the one waiting
        # cannot repair: read late, it silently discards every watt-hour drawn
        # before it, always in the same direction.
        #
        # Everything this row needs already arrived in the MQTT event that
        # woke the handler. What the 3MF adds — geometry, per-plate metadata,
        # weights, objects — lands later via ``attach_3mf_to_archive``.
        #
        # ⚠️ ``file_path=""`` is the marker the download-retry service selects
        # on, so the row is recoverable before we have tried even once.
        # ``no_3mf_available`` is deliberately NOT set here: that flag means
        # "we tried and could not", and it raises a warning banner in
        # Archives. Setting it optimistically would light that banner on every
        # external print for the length of its download.
        print_name = subtask_name or filename
        if print_name:
            print_name = print_name.split("/")[-1]
            print_name = print_name.replace(".gcode.3mf", "").replace(".gcode", "").replace(".3mf", "")
        else:
            print_name = "Unknown Print"

        # Best-effort filament metadata from MQTT — see
        # _extract_filament_data_from_mqtt. The slicer's ams_mapping (when
        # present) narrows it to the slots this print actually uses (#1533).
        mqtt_filament_meta = _extract_filament_data_from_mqtt(data, _get_start_ams_mapping(data, None))

        archive = PrintArchive(
            printer_id=printer_id,
            filename=filename or f"{print_name}.3mf",
            file_path="",  # attach_3mf_to_archive fills this once the 3MF lands
            file_size=0,
            print_name=print_name,
            subtask_id=subtask_id,
            status="printing",
            started_at=datetime.now(timezone.utc),
            filament_type=mqtt_filament_meta.get("filament_type"),
            filament_color=mqtt_filament_meta.get("filament_color"),
            # ⚠️ From the live MQTT state, never from the 3MF: the attach
            # parser reads this column back to decide which plate to describe.
            # A row created without it gets plate 1's thumbnail, weights and
            # object list stamped onto plate N's print.
            plate_index=live_plate_id,
            # External / direct-dispatch falls back to the printer's default
            # queue so post-m019 archive-driven counters include it.
            queue_id=await _default_queue_id_for_printer(db, printer_id),
            extra_data={
                "original_subtask": subtask_name,
                "_print_data": {
                    k: v for k, v in data.items() if k not in {"_print_observation", "_execution_decision"}
                },
            },
        )
        # A NEW observed external start -- no dispatch of ours took it over above. Only the full file name the
        # printer reports can declare a baked swap sequence; anything else waits, as pending, for the name the
        # download proves (swap-source-lifecycle spec r4 §7). The subtask finds the file, it declares nothing.
        if swap_compatibility.classify_external_name(filename):
            swap_compatibility.write_archive_fact(archive, True)
        else:
            swap_compatibility.write_archive_fact(archive, None, pending_external=True)
        if not owns_start():
            # Revoked while the row was being built -- by a deletion of this
            # printer record among others: no archive for a start nobody owns.
            return
        from backend.app.services.print_execution import publish_start

        publication = await publish_start(
            db,
            archive,
            decision,
            options=_printer_reported_options(data, None, live_plate_id),
            still_current=owns_start,
        )
        if not publication.current:
            _request_published_run_recovery(printer_id, data, decision.epoch)
            return
        await db.refresh(archive)
        logger.info("Created archive %s for %s at print start (3MF to follow)", archive.id, print_name)
        bound = _bind_print_file_analysis_context(
            printer_id,
            archive,
            expected_generation=start_generation,
            origin="external",
            observation=data.get("_print_observation"),
        )
        await _claim_usage_session(printer_id, usage_session_started, archive, bound)

        # Prefer the colours of the built-in-inventory spools loaded on the
        # used slots over the raw MQTT tray colours. Applied again after the
        # 3MF lands, because attaching it overwrites filament_color with the
        # slicer's own — the same order the dispatch path uses.
        from backend.app.services.archive_colors import apply_loaded_spool_colors

        await apply_loaded_spool_colors(
            db, archive, printer_id, _get_start_ams_mapping(data, archive.id, archive.recovery_identity)
        )
        await db.commit()

        # Track as the active print. This is also the re-trigger guard at the
        # top of the handler, so an MQTT reconnect during the download now
        # finds the print already tracked instead of starting a second one.
        _remember_active_print((printer_id, archive.filename), archive)
        if filename:
            _remember_active_print((printer_id, filename), archive)
        if subtask_name:
            _remember_active_print((printer_id, f"{subtask_name}.3mf"), archive)
            _remember_active_print((printer_id, subtask_name), archive)

        # Ensure the queue reflects the busy state (external / direct print).
        # This is where an external print's row is born, so it is also the one
        # chance to record what the printer told us about it.
        claim_result = await mark_queue_printing_for_printer(
            printer_id,
            archive_id=archive.id,
            expected_identity=archive.recovery_identity,
            options=_printer_reported_options(data, archive.id, live_plate_id, archive.recovery_identity),
        )
        if not claim_result:
            return
        await maybe_register_external_stagger(printer_id)

        # #941: the plug's lifetime counter, read at the real start of the
        # print rather than whenever its 3MF happened to finish downloading.
        await _record_energy_start(archive, printer_id, db, context="print-start")

        if printer.external_camera_enabled and printer.external_camera_url:
            from backend.app.services.layer_timelapse import start_session

            start_session(
                printer_id,
                archive.id,
                printer.external_camera_url,
                printer.external_camera_type or "mjpeg",
                snapshot_url=printer.external_camera_snapshot_url,
                rotation=getattr(printer, "camera_rotation", 0) or 0,
            )
            logger.info("Started layer timelapse for printer %s, archive %s", printer_id, archive.id)

        # Snapshot the printer's existing timelapse files while "at print start"
        # is still true. Taken after the 3MF download instead, a print shorter
        # than its own download would already have closed its video, and the
        # completion-time diff would find nothing new to attach.
        await _capture_timelapse_baseline_at_start(
            printer,
            printer_id,
            logger,
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            may_run=owns_start,
        )

        await ws_manager.send_archive_created(
            {
                "id": archive.id,
                "printer_id": archive.printer_id,
                "filename": archive.filename,
                "print_name": archive.print_name,
                "status": archive.status,
            }
        )

        try:
            await mqtt_relay.on_archive_created(
                archive_id=archive.id,
                print_name=archive.print_name,
                printer_name=printer.name,
                status=archive.status,
            )
        except Exception:
            pass  # Don't fail if MQTT fails

        # Sent now, not after the download. No ``print_time_seconds`` yet, and
        # that is fine — the notification service falls back to the printer's
        # own live ETA, which at print start is the fresher number anyway.
        if not notification_sent and owns_start():
            await _send_print_start_notification(printer_id, data, logger=logger)
            notification_sent = True

        # Release the pooled DB connection before the 3MF FTP download:
        # try_download_3mf probes several remote paths with retry/backoff and
        # can run for minutes under FTP contention, and holding the session
        # across it pinned one pooled connection idle-in-transaction (#2572).
        # Everything above is committed; expire_on_commit=False keeps
        # ``printer.*`` and ``archive.*`` readable afterwards.
        await db.commit()

        # Shared download helper (same logic used by the retry service).
        from backend.app.services.archive_download import last_download_failure_reason, try_download_3mf
        from backend.app.services.archive_download_retry import archive_download_retry

        temp_dir = app_settings.archive_dir / "temp"
        temp_path = None
        downloaded_filename = None

        # Claim the archive for the whole download-and-attach span. The retry
        # service selects exactly the shape this row has right now, and a
        # printer reconnect mid-download is routine — the dispatcher triggers
        # reconnects itself. Without the claim that trigger opens a second FTP
        # session for a file already on its way and attaches on top of ours,
        # cutting a second archive directory and orphaning the first.
        archive_identity = archive.recovery_identity
        async with archive_download_retry.claim(archive.id) as claimed:
            try:
                # Another download of this archive is already on its way: it
                # attaches the file. Neither fetch nor mark it unavailable here.
                download_result = await try_download_3mf(printer, subtask_name, filename, temp_dir) if claimed else None
                if download_result:
                    temp_path, downloaded_filename = download_result
                pinned = await db.get(PrintArchive, archive.id, populate_existing=True)
                if pinned is None or pinned.recovery_identity != archive_identity:
                    return
                if download_result:
                    temp_path, downloaded_filename = download_result

                # Validate the downloaded 3MF actually matches the plate that's running
                # (#1204): subtask_name lags across consecutive plates of the same model,
                # so the first FTP candidate (built from subtask_name) can land on the
                # previous plate's still-resident upload. Cross-check the slice_info
                # plate index against the plate parsed from gcode_file (always fresh —
                # it's the field whose change triggered this callback). Only runs when
                # parse_plate_id() returns a value, so single-plate / cloud-named /
                # non-Bambu jobs are unaffected.
                if downloaded_filename and temp_path:
                    from backend.app.services.archive import (
                        peek_plate_index_in_3mf,
                        swap_plate_suffix,
                    )
                    from backend.app.services.bambu_ftp import (
                        FileNotOnPrinterError,
                        download_file_async,
                        get_ftp_retry_settings,
                        with_ftp_retry,
                    )

                    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()

                    expected_plate = parse_plate_id(filename)
                    actual_plate = peek_plate_index_in_3mf(temp_path) if expected_plate is not None else None
                    if expected_plate is not None and actual_plate is not None and actual_plate != expected_plate:
                        logger.warning(
                            "[CALLBACK] 3MF plate mismatch: downloaded %s reports plate %s but printer is "
                            "running plate %s — subtask_name=%r appears stale, retrying with corrected name",
                            downloaded_filename,
                            actual_plate,
                            expected_plate,
                            subtask_name,
                        )
                        corrected_subtask = swap_plate_suffix(subtask_name, expected_plate)
                        retry_succeeded = False
                        if corrected_subtask and corrected_subtask != subtask_name:
                            # Retry FTP with the swapped suffix. We use the same path
                            # candidates list our existing download flow probes — the
                            # printer caches uploads in a few directories depending on
                            # firmware (root, /cache, /model, /data, /data/Metadata).
                            for try_filename in (f"{corrected_subtask}.gcode.3mf", f"{corrected_subtask}.3mf"):
                                retry_temp_path = temp_dir / try_filename
                                retry_temp_path.parent.mkdir(parents=True, exist_ok=True)
                                for remote_path in (
                                    f"/{try_filename}",
                                    f"/cache/{try_filename}",
                                    f"/model/{try_filename}",
                                    f"/data/{try_filename}",
                                    f"/data/Metadata/{try_filename}",
                                ):
                                    try:
                                        if ftp_retry_enabled:
                                            downloaded = await with_ftp_retry(
                                                download_file_async,
                                                printer.ip_address,
                                                printer.access_code,
                                                remote_path,
                                                retry_temp_path,
                                                timeout=ftp_timeout,
                                                socket_timeout=ftp_timeout,
                                                printer_model=printer.model,
                                                max_retries=ftp_retry_count,
                                                retry_delay=ftp_retry_delay,
                                                operation_name=f"Re-download 3MF from {remote_path}",
                                                non_retry_exceptions=(FileNotOnPrinterError,),
                                            )
                                        else:
                                            downloaded = await download_file_async(
                                                printer.ip_address,
                                                printer.access_code,
                                                remote_path,
                                                retry_temp_path,
                                                timeout=ftp_timeout,
                                                socket_timeout=ftp_timeout,
                                                printer_model=printer.model,
                                            )
                                        if downloaded and peek_plate_index_in_3mf(retry_temp_path) == expected_plate:
                                            logger.info(
                                                "[CALLBACK] Re-download succeeded with corrected name %s "
                                                "(plate %s) — replacing wrong file",
                                                try_filename,
                                                expected_plate,
                                            )
                                            try:
                                                temp_path.unlink(missing_ok=True)
                                            except OSError:
                                                pass
                                            temp_path = retry_temp_path
                                            downloaded_filename = try_filename
                                            subtask_name = corrected_subtask
                                            retry_succeeded = True
                                            break
                                        elif downloaded:
                                            # Wrong plate again — discard and keep trying.
                                            try:
                                                retry_temp_path.unlink(missing_ok=True)
                                            except OSError:
                                                pass
                                    except FileNotOnPrinterError:
                                        continue
                                    except Exception as e:
                                        logger.debug("Re-download failed for %s: %s", remote_path, e)
                                if retry_succeeded:
                                    break
                        # If the retry didn't find a matching file, drop the wrong 3MF
                        # so the no-3MF fallback below creates an archive whose name
                        # at least reflects the right plate.
                        if not retry_succeeded:
                            logger.warning(
                                "[CALLBACK] Could not re-download correct plate %s — falling back to no-3MF archive",
                                expected_plate,
                            )
                            try:
                                temp_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                            temp_path = None
                            downloaded_filename = None
                            # The row was named from the lagging subtask_name
                            # before the download could contradict it, and this
                            # branch is where it gets contradicted with no file
                            # to re-derive the name from. A successful
                            # re-download needs no fix — the attach renames from
                            # the corrected file it actually got. A name without
                            # a plate suffix keeps titling the row (#3126).
                            corrected_name = _name_after_plate_reject(subtask_name, corrected_subtask, filename)
                            # Disown the stale name for LOOKUPS: it has just
                            # fetched another plate's 3MF. Only a name re-pointed
                            # at the running plate survives.
                            subtask_name = corrected_subtask or ""
                            if corrected_name and corrected_name != archive.print_name:
                                logger.info(
                                    "Renaming archive %s %r -> %r (stale plate name)",
                                    archive.id,
                                    archive.print_name,
                                    corrected_name,
                                )
                                from backend.app.services.archive_write_scope import (
                                    archive_write_scope,
                                    load_active_archive_for_write,
                                )

                                async with archive_write_scope(db, archive.id):
                                    current = await load_active_archive_for_write(db, archive.id)
                                    if current is None or current.recovery_identity != archive_identity:
                                        return
                                    current.print_name = corrected_name
                                    await db.commit()

                # File identity cannot change the execution decision made at start.
                if not claimed:
                    logger.info(
                        "3MF for archive %s is already being fetched by another trigger; it attaches the file",
                        archive.id,
                    )
                elif not downloaded_filename or not temp_path:
                    # The row already exists and is fully wired up — what is
                    # missing is only the file. Record that the attempt was
                    # made and failed, which is what Archives renders as "3MF
                    # unavailable" and what the four retry triggers key on.
                    logger.warning("Could not find 3MF file for print: %s", filename or subtask_name)
                    archive_id = archive.id
                    await ArchiveService(db).mark_3mf_unavailable(
                        archive_id, reason=last_download_failure_reason(printer.id), expected_identity=archive_identity
                    )
                    archive = await db.get(PrintArchive, archive_id)
                else:
                    service = ArchiveService(db)
                    archive_id = archive.id
                    attached = await service.attach_3mf_to_archive(
                        archive_id,
                        temp_path,
                        downloaded_filename,
                        received_name=downloaded_filename,
                        expected_identity=archive.recovery_identity,
                    )
                    if attached:
                        logger.info(
                            "Attached 3MF %s to archive %s (%s bytes)",
                            downloaded_filename,
                            archive.id,
                            archive.file_size,
                        )

                        # The attach has just overwritten filament_color with
                        # the slicer's own colours, and a loaded inventory
                        # spool outranks those — the same order the dispatch
                        # path applies them in.
                        await apply_loaded_spool_colors(
                            db, archive, printer_id, _get_start_ams_mapping(data, archive.id, archive.recovery_identity)
                        )
                        await db.commit()
                        _notify_print_file_analysis_source_ready(printer_id, archive)

                        await ws_manager.send_archive_updated({"id": archive.id, "recovered_3mf": True})

                        # Extract printable objects for skip object functionality
                        try:
                            from backend.app.services.archive import (
                                extract_printable_objects_from_3mf,
                                extract_skip_support_from_3mf,
                            )
                            from backend.app.services.printer_manager import resolve_plate_id

                            with open(temp_path, "rb") as f:
                                threemf_data = f.read()
                            # Resolved before the extract so the plate can scope it.
                            client = printer_manager.get_client(printer_id)
                            # Extract with positions for UI overlay, scoped to the plate that
                            # is actually printing (#2522). identify_ids in a multi-plate 3MF
                            # are per-plate, so plate 1's list over plate 3's job offers the
                            # wrong objects — and skipping by a stale id cancels whatever
                            # really holds that id on the running plate. resolve_plate_id is
                            # the same resolver the cover/thumbnail path uses, so the object
                            # list cannot disagree with the picture it is drawn over.
                            printable_objects, bbox_all, approximate = extract_printable_objects_from_3mf(
                                threemf_data,
                                plate_number=resolve_plate_id(client.state) if client else None,
                                include_positions=True,
                                with_confidence=True,
                            )
                            if printable_objects:
                                # Store objects in printer state
                                if client:
                                    client.state.printable_objects = printable_objects
                                    client.state.printable_objects_bbox_all = bbox_all
                                    client.state.printable_objects_approximate = approximate
                                    client.state.skipped_objects = []  # Reset skipped objects for new print
                                    # Gate the Skip-Objects UI button. Derived straight from the
                                    # 3MF (not archive.extra_data, which this slicer-start path
                                    # doesn't populate) — otherwise the button stayed disabled
                                    # even though the object list loaded fine.
                                    client.state.skip_objects_supported = extract_skip_support_from_3mf(threemf_data)
                                    logger.info(
                                        "Loaded %s printable objects for printer %s (skip_objects_supported=%s)",
                                        len(printable_objects),
                                        printer_id,
                                        client.state.skip_objects_supported,
                                    )
                        except Exception as e:
                            logger.debug("Failed to extract printable objects: %s", e)
                    else:
                        # The bytes arrived but could not be attached (corrupt
                        # copy, unreadable ZIP). Leave the row marked so the
                        # retry triggers try again — a failed attach must not
                        # read as a finished one.
                        #
                        # ⚠️ ``attach_3mf_to_archive`` rolls back on failure,
                        # and a rollback expires every object in the session
                        # whatever ``expire_on_commit`` says. Reading
                        # ``archive.extra_data`` without this refresh would be
                        # a lazy load with no greenlet to run it in.
                        # ``attach_3mf_to_archive`` rolls its session back on
                        # a pre-commit failure, expiring every ORM attribute.
                        # Keep the scalar ID, then reload before *any* archive
                        # access (including logging) to avoid MissingGreenlet.
                        logger.warning("Failed to attach 3MF %s to archive %s", downloaded_filename, archive_id)
                        await service.mark_3mf_unavailable(archive_id, expected_identity=archive_identity)
                        archive = await db.get(PrintArchive, archive_id)
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink()

        # Spoolman tracking runs once, with whatever ``file_path`` the row
        # ended up carrying. ⚠️ It has to come AFTER the attach: given an empty
        # file_path ``store_print_data`` falls through to its remain%-delta
        # path, so calling it back at creation time would put every external
        # print on the degraded route even though its 3MF lands moments later.
        try:
            await _store_spoolman_print_data(
                printer_id,
                archive.id,
                archive.file_path,
                db,
                printer_manager,
                ams_mapping=_get_start_ams_mapping(data, archive.id, archive.recovery_identity),
                plate_id=archive.plate_index,
            )
        except Exception as e:
            logger.warning("[SPOOLMAN] Failed to store tracking data: %s", e)


async def _capture_timelapse_baseline_at_start(
    printer,
    printer_id: int,
    logger: logging.Logger,
    *,
    archive_id: int | None = None,
    archive_identity: str | None = None,
    taken: str = "start",
    may_run: Callable[[], bool] | None = None,
) -> None:
    """Snapshot the printer's existing timelapse video filenames so the
    completion-time scan can diff against it and attach only the video this
    print produced. Shared by ``on_print_start`` and the restart-recovery hook
    ``on_print_running_observed`` (#1485 follow-up).

    Kept in ``_timelapse_baselines[printer_id]`` and — when the archive is known
    — with the archive itself (``services/timelapse_baseline``, G10 C3), so a
    restart during the print does not lose the boundary. Best-effort: a capture
    that fails leaves no baseline, and the scan then attaches nothing by itself.

    ⚠️ The list belongs to one execution, one printer and one storage epoch.
    The process epoch is captured before the listing and compared after it — a
    restore that won meanwhile gets no old observation written into its storage
    — and *may_run* (the start's own proof) is asked at the same two points and
    again under the archive's write scope. A **late** capture first reads what
    the archive already keeps: a trusted boundary addressed to this very
    execution is taken back into memory and the card is not read again — a
    reread that fails must not replace it.
    """
    from backend.app.services import queue_sources, timelapse_baseline

    try:
        epoch = queue_sources.current_epoch()
        if may_run is not None and not may_run():
            return
        if taken == timelapse_baseline.TAKEN_LATE and archive_id is not None:
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                kept_row = await db.get(PrintArchive, archive_id)
                kept = None
                if kept_row is not None and kept_row.recovery_identity == archive_identity:
                    address = await timelapse_baseline.address_of(db, kept_row, printer)
                    kept = timelapse_baseline.read(kept_row.extra_data, address=address) if address else None
            if kept is not None and kept[1]:
                if queue_sources.current_epoch() != epoch or (may_run is not None and not may_run()):
                    return  # overtaken while the archive was read: nothing of the old run reaches memory
                _timelapse_baselines[printer_id] = kept[0]
                _untrusted_timelapse_baselines.discard(printer_id)
                _timelapse_baseline_owners[printer_id] = (
                    archive_identity,
                    epoch,
                    timelapse_baseline.media_of(kept_row.extra_data),
                )
                logger.info(
                    "[TIMELAPSE] Baseline for printer %s restored from archive %s (%s recordings)",
                    printer_id,
                    archive_id,
                    len(kept[0]),
                )
                return
        baseline_files, _ = await _list_timelapse_videos(printer, automatic=True)
        if queue_sources.current_epoch() != epoch or (may_run is not None and not may_run()):
            # A restore, or a newer print, won while the printer was being read.
            logger.info("[TIMELAPSE] Baseline for printer %s discarded: it was overtaken while being read", printer_id)
            return
        names = {f.get("name", "") for f in baseline_files}
        keys = {_recording_key(f) for f in baseline_files}
        trusted = bool(_last_listing_answered(printer.id))
        media = _last_listing_media(printer.id)
        if archive_id is not None:
            # The archive first, memory after: a list the archive refused — the run
            # was reset, a restore won, a proven list is already kept — must not
            # sit in memory as this printer's boundary. The scan then reads what
            # the archive keeps.
            async with async_session() as db:
                address = (
                    timelapse_baseline.Address(
                        archive_identity, printer.connector_identity, await timelapse_baseline.storage_epoch(db)
                    )
                    if archive_identity and getattr(printer, "connector_identity", None)
                    else None
                )
                await db.rollback()
                kept_with_archive = await timelapse_baseline.save(
                    db,
                    archive_id,
                    archive_identity,
                    timelapse_baseline.record(
                        names, trusted=trusted, taken=taken, entries=baseline_files, media=media, address=address
                    ),
                    expected_epoch=epoch,
                    may_run=may_run,
                )
            if (
                not kept_with_archive
                or queue_sources.current_epoch() != epoch
                or (may_run is not None and not may_run())
            ):
                logger.info(
                    "[TIMELAPSE] Baseline for printer %s is not taken into memory: archive %s did not keep it",
                    printer_id,
                    archive_id,
                )
                return
        _timelapse_baselines[printer_id] = keys
        _timelapse_baseline_owners[printer_id] = (archive_identity, epoch, media)
        if trusted:
            _untrusted_timelapse_baselines.discard(printer_id)
        else:
            _untrusted_timelapse_baselines.add(printer_id)
            logger.warning(
                "[TIMELAPSE] Baseline for printer %s taken while its storage did not answer — nothing will be "
                "attached automatically at completion; Scan for timelapse stays available",
                printer_id,
            )
        logger.info(
            "[TIMELAPSE] Baseline at print start: %s video files for printer %s",
            len(names),
            printer_id,
        )
    except Exception as e:
        logger.warning("[TIMELAPSE] Failed to capture baseline at print start: %s", e)


def _pop_timelapse_baseline(printer_id: int, *, archive_identity: str | None = None) -> tuple[set[str] | None, bool]:
    """Hand a finished print's baseline to its scan: ``(recordings, trusted)``, both forgotten here.

    ``(None, True)`` when memory holds none FOR THIS PRINT — none was taken, or
    the one held was taken for another archive incarnation or in another
    storage epoch. The scan then reads the one kept with the archive.
    """

    recordings, trusted, _media = _take_timelapse_baseline(printer_id, archive_identity=archive_identity)
    return recordings, trusted


def _take_timelapse_baseline(
    printer_id: int, *, archive_identity: str | None = None
) -> tuple[set[str] | None, bool, dict | None]:
    """``(recordings, trusted, media)`` — :func:`_pop_timelapse_baseline` plus what each medium said at the capture."""
    from backend.app.services import queue_sources

    trusted = printer_id not in _untrusted_timelapse_baselines
    _untrusted_timelapse_baselines.discard(printer_id)
    recordings = _timelapse_baselines.pop(printer_id, None)
    owner = _timelapse_baseline_owners.pop(printer_id, None)
    media = None
    if recordings is not None and owner is not None:
        owner_identity, owner_epoch, *rest = owner
        media = rest[0] if rest else None
        if owner_epoch != queue_sources.current_epoch() or (
            archive_identity is not None and owner_identity is not None and owner_identity != archive_identity
        ):
            return None, True, None
    return recordings, trusted, media


async def _scan_for_timelapse_with_retries(
    archive_id: int,
    baseline_names: set[str] | None = None,
    *,
    baseline_trusted: bool = True,
    baseline_media: dict | None = None,
    expected_identity: str | None = None,
    expected_epoch: int | None = None,
    may_run: Callable[[], bool] | None = None,
):
    """
    Scan for timelapse with retries using a snapshot-diff approach.

    Instead of picking the "most recent by mtime" (unreliable when the printer
    clock is wrong in LAN-only mode), the names that existed BEFORE this print
    recorded anything are the boundary, and a NEW name after each delay is the
    print's recording.

    ⚠️ **The boundary is never taken here** (G10 C3). ``baseline_names`` is the
    one captured at print start; without it the one kept with the archive is
    read (a restart during the print). A list taken now, after the print, is
    not a boundary — it already holds the print's own video — so with neither,
    nothing is attached and **Scan for timelapse** is left to the operator.
    The same when the boundary was read off storage that did not answer
    (*baseline_trusted* False): not even a single new recording is taken, an
    unread card is not an empty one.

    ⚠️ **A name that resembles the print is no reason to attach.** The old
    name-match fallback is gone: it attached, and with the tidy-up on deleted
    from the printer, an earlier print's video of the same model.

    *may_run* — still the same physical run? A newer print on this printer
    stops the scan: before a listing, before a download, before the attach,
    inside the publication itself (the seam asks again immediately before the
    bytes take their name — staging and the wait for locks are long enough for
    a print to start) and before the recording is removed from the printer.

    *baseline_media* — what each medium said when the boundary was taken
    (``timelapse_files.last_listing_media``). A recording on a medium that was
    not asked then, or did not answer, is not known to be new: the listing stops
    at the card when the card has videos, so internal storage may never have
    been looked at. Such a recording is left to the operator.

    ⚠️ Each attempt captures the video the archive shows before it lists and
    downloads, and publishes only over that; a recording on a printer that has
    no immutable identity is claimable by nobody and is left to the operator.
    """

    def _superseded() -> bool:
        return may_run is not None and not may_run()

    from backend.app.services import queue_sources

    expected_epoch = queue_sources.current_epoch() if expected_epoch is None else expected_epoch
    if queue_sources.current_epoch() != expected_epoch:
        return
    logger = logging.getLogger(__name__)

    # --- Phase 1: Take baseline snapshot of existing timelapse files ---
    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            service = ArchiveService(db)
            archive = await service.get_archive(archive_id)

            if (
                queue_sources.current_epoch() != expected_epoch
                or not archive
                or (expected_identity is not None and archive.recovery_identity != expected_identity)
            ):
                logger.warning("[TIMELAPSE] Archive %s not found, aborting", archive_id)
                return
            expected_identity = archive.recovery_identity
            if archive.timelapse_path:
                logger.info("[TIMELAPSE] Archive %s already has timelapse attached", archive_id)
                return
            if not archive.printer_id:
                logger.warning("[TIMELAPSE] Archive %s has no printer, aborting", archive_id)
                return

            from backend.app.services import timelapse_baseline

            if baseline_names is None:
                # Memory has none (this process restarted during the print): the
                # one kept with the archive, if the start or a late capture left
                # one — and only when it is addressed to this very incarnation,
                # this printer and this storage epoch.
                baseline_printer = await db.get(Printer, archive.printer_id)
                address = await timelapse_baseline.address_of(db, archive, baseline_printer)
                kept = timelapse_baseline.read(archive.extra_data, address=address) if address else None
                if kept is not None:
                    baseline_names, baseline_trusted = kept
                    baseline_media = timelapse_baseline.media_of(archive.extra_data)
            refusal = timelapse_baseline.automatic_attach_refusal(baseline_names, baseline_trusted)
            if refusal is not None:
                logger.warning(
                    "[TIMELAPSE] Nothing is attached to archive %s automatically (%s): there is no trusted "
                    "list of what was on the printer before this print recorded. Scan for timelapse stays available",
                    archive_id,
                    refusal,
                )
                return
            logger.info(
                "[TIMELAPSE] Using the baseline of %s existing video files for archive %s",
                len(baseline_names),
                archive_id,
            )

    except Exception as e:
        logger.warning("[TIMELAPSE] Failed to take baseline snapshot for archive %s: %s", archive_id, e)
        return

    # --- Phase 2: Retry loop - look for NEW files that weren't in baseline ---
    retry_delays = [5, 10, 20, 30]

    for attempt, delay in enumerate(retry_delays, 1):
        logger.info(
            "[TIMELAPSE] Attempt %s/%s: waiting %ss before scanning for archive %s",
            attempt,
            len(retry_delays),
            delay,
            archive_id,
        )
        await asyncio.sleep(delay)
        if _superseded():
            logger.info("[TIMELAPSE] A newer print owns the printer — the scan for archive %s stops", archive_id)
            return

        try:
            from backend.app.models.printer import Printer

            # Read phase: fetch archive + printer in a short session and release the
            # pooled connection BEFORE the FTP list/download below — holding it
            # across the round-trips left one connection idle-in-transaction per
            # in-flight scan, ×4 retries, per completed print (#2572).
            async with async_session() as db:
                archive = await ArchiveService(db).get_archive(archive_id)

                if (
                    queue_sources.current_epoch() != expected_epoch
                    or not archive
                    or (expected_identity is not None and archive.recovery_identity != expected_identity)
                ):
                    logger.warning("[TIMELAPSE] Archive %s not found, stopping retries", archive_id)
                    return
                if archive.timelapse_path:
                    logger.info("[TIMELAPSE] Archive %s already has timelapse attached, stopping retries", archive_id)
                    return
                from backend.app.services import archive_media

                # Before the listing and the download: what this attempt starts from.
                started_from = await archive_media.primary_token(db, archive)

                result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
                printer = result.scalar_one_or_none()
                if not printer:
                    logger.warning("[TIMELAPSE] Printer not found for archive %s, stopping retries", archive_id)
                    return

            # I/O phase (no DB connection held): FTP list + download.
            video_files, found_path = await _list_timelapse_videos(printer, automatic=True)

            if not video_files:
                logger.info("[TIMELAPSE] Attempt %s: No video files found, will retry", attempt)
                continue

            logger.info("[TIMELAPSE] Attempt %s: Found %s video files in %s", attempt, len(video_files), found_path)
            for f in video_files[:5]:
                logger.info("[TIMELAPSE]   - %s", f.get("name"))

            # Which of them is ours: new since this print started, and — when
            # the printer named the file it just closed — that one specifically.
            if _superseded():
                logger.info("[TIMELAPSE] A newer print owns the printer — the scan for archive %s stops", archive_id)
                return
            if baseline_media is not None:
                from backend.app.services.timelapse_files import MEDIUM_ANSWERED, recording_storage

                observed = [
                    f
                    for f in video_files
                    if baseline_media.get(recording_storage(f.get("path") or "")) == MEDIUM_ANSWERED
                ]
                if len(observed) != len(video_files):
                    logger.info(
                        "[TIMELAPSE] %s recording(s) on a medium the baseline of archive %s never saw are not "
                        "candidates; Scan for timelapse stays available",
                        len(video_files) - len(observed),
                        archive_id,
                    )
                video_files = observed
            target = _pick_new_recording(video_files, baseline_names, _last_recording_path(printer))

            if target is not None:
                file_name = target.get("name")
                remote_path = target.get("path") or f"/timelapse/{file_name}"
                claimed_source = remote_source(printer, remote_path, target)
                if claimed_source is None:
                    logger.warning(
                        "[TIMELAPSE] Printer of archive %s has no identity to claim %s under — nothing is "
                        "attached automatically; Scan for timelapse stays available",
                        archive_id,
                        file_name,
                    )
                    return
                logger.info(
                    "[TIMELAPSE] Attempt %s: New file detected: %s (downloading for archive %s)",
                    attempt,
                    file_name,
                    archive_id,
                )

                timelapse_data = await read_timelapse_video(printer, remote_path)
                if timelapse_data and _superseded():
                    # Downloaded, and the printer has moved on: neither attached nor deleted from it.
                    logger.info("[TIMELAPSE] A newer print owns the printer — %s is not attached", file_name)
                    return
                if timelapse_data:
                    # Write phase: attach in a fresh short-lived session.
                    async with async_session() as db:
                        success = await ArchiveService(db).attach_timelapse(
                            archive_id,
                            timelapse_data,
                            file_name,
                            expected_identity=expected_identity,
                            expected_epoch=expected_epoch,
                            expected_token=started_from,
                            origin="printer",
                            # Claimed: a recording another archive already holds is refused.
                            remote=claimed_source,
                            # Asked again inside the publication, after staging and the locks.
                            may_run=may_run,
                        )
                    if success:
                        logger.info("[TIMELAPSE] Successfully attached timelapse to archive %s", archive_id)
                        # Opt-in tidy-up, and only now: "we have a copy" is the
                        # entire justification for removing somebody's video —
                        # these bytes, re-read and compared, while this run still owns the printer.
                        await remove_recording_after_attach(printer, remote_path, may_run=may_run)
                        await ws_manager.send_archive_updated({"id": archive_id, "timelapse_attached": True})
                        return
                    else:
                        logger.warning("[TIMELAPSE] Failed to attach timelapse to archive %s", archive_id)
                else:
                    logger.warning("[TIMELAPSE] Attempt %s: Failed to download new file, will retry", attempt)
            else:
                logger.info("[TIMELAPSE] Attempt %s: No new files since baseline, will retry", attempt)

        except Exception as e:
            logger.warning("[TIMELAPSE] Attempt %s failed with error: %s", attempt, e)

    logger.warning(
        "[TIMELAPSE] No new recording appeared for archive %s — nothing attached; Scan for timelapse stays available",
        archive_id,
    )


# Defaults for the finish-photo-from-timelapse polling loop (#1397). These are
# module-level so tests can monkeypatch them down to ~0 without timing out.
_FINISH_PHOTO_TIMELAPSE_POLL_INTERVAL_SECONDS: float = 3.0
_FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS: float = 60.0


async def _capture_finish_photo_from_timelapse(
    archive_id: int,
    archive_dir: Path | None = None,
    rotation: int = 0,
    expected_identity: str | None = None,
    may_run=None,
) -> str | None:
    """Wait for the per-print timelapse to land on the archive and extract its
    last frame as the finish photo (#1397).

    ``may_run`` is the terminal's proof that the printer has not gone on to
    another print (G10 G): asked on every poll and again after the frame was
    extracted, so a photo of a superseded terminal is neither waited for nor
    left on disk. The photo folder is read off the archive at the moment of the
    write (``finish_photo_store``); ``archive_dir`` is no longer used.

    Bambu firmware stops timelapse recording after the toolhead parks but
    before the bed-drop end-gcode runs, so the last frame frames the finished
    print correctly. A live camera grab at gcode_state=FINISH captures the
    bed already lowered.

    ``_scan_for_timelapse_with_retries`` runs in parallel and writes
    ``archive.timelapse_path`` when the file lands. This function polls for
    that field. Returns the saved photo filename on success, or None if the
    timelapse never arrives within the timeout / extraction fails / no
    timelapse path was set — in which case the caller falls back to the
    existing live-camera capture chain.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.services import archive_media, finish_photo_store
    from backend.app.services.camera import apply_camera_rotation_to_file, extract_video_last_frame

    logger = logging.getLogger(__name__)

    deadline = asyncio.get_event_loop().time() + _FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS
    poll_interval = _FINISH_PHOTO_TIMELAPSE_POLL_INTERVAL_SECONDS

    while True:
        if may_run is not None and not may_run():
            return None
        async with async_session() as db:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
            archive = result.scalar_one_or_none()
            if archive is None or (expected_identity is not None and archive.recovery_identity != expected_identity):
                return None
            expected_identity = archive.recovery_identity
            timelapse_relpath = archive.timelapse_path if archive else None

        # The stored pointer is joined only through ``contained``: one that leads out of the
        # data directory names no video to take a frame from.
        video_path = archive_media.contained(app_settings.base_dir, timelapse_relpath) if timelapse_relpath else None
        if video_path is not None:
            if video_path.exists() and video_path.stat().st_size > 0:
                photos_dir = await finish_photo_store.photos_dir_at_write(
                    archive_id, expected_identity, session=async_session
                )
                if photos_dir is None:
                    return None
                filename = finish_photo_store.new_name()
                output_path = photos_dir / filename  # SEC-PATH-OK: a name generated by finish_photo_store
                # The store knows the address before ffmpeg may create the file: whatever ends this
                # attempt — a refusal, a failed extraction, a cancel — removes exactly that file.
                finish_photo_store.note_written(filename, output_path)

                async def remove_still(name: str = filename) -> None:
                    await finish_photo_store.discard(archive_id, name, session=async_session)

                in_extractor = True
                try:
                    # Every outcome of the extraction but success is settled by the extractor — a cancel,
                    # an error, a False before or after ffmpeg started: it stops ffmpeg and removes the
                    # still only once ffmpeg can no longer write it (Codex G2-R2, G2-R3).
                    extracted = await extract_video_last_frame(video_path, output_path, on_abandon=remove_still)
                    in_extractor = False
                    wanted = False
                    if extracted:
                        # ffmpeg writes the still straight to disk, so there are no
                        # bytes to rotate on the way past — do it in place. The
                        # VIDEO is left alone deliberately: it is the printer's own
                        # file, and rotating it would mean re-encoding it (#2708).
                        await finish_photo_store.to_the_end(
                            apply_camera_rotation_to_file(output_path, rotation, logger)
                        )
                        async with async_session() as db:
                            current = await db.get(PrintArchive, archive_id)
                            same = current is not None and current.recovery_identity == expected_identity
                        wanted = same and (may_run is None or may_run())
                except BaseException:
                    if not in_extractor:  # ffmpeg has ended: the still is removed here
                        await finish_photo_store.to_the_end(remove_still())
                    raise
                if wanted:
                    # Its exact address stays with the store until the caller attaches or refuses it.
                    logger.info(
                        "[PHOTO-BG] Extracted finish photo from timelapse %s for archive %s",
                        video_path.name,
                        archive_id,
                    )
                    return filename
                if extracted:
                    # Extracted, and no longer wanted: ffmpeg has ended, the still is this helper's to remove.
                    await finish_photo_store.discard(archive_id, filename, session=async_session)
                else:
                    # Not extracted: the extractor settles the still (perhaps through an owner that waits for
                    # ffmpeg's exit) — it is not taken from that owner here; the fallback capture may go on.
                    logger.warning(
                        "[PHOTO-BG] Timelapse %s landed but last-frame extraction failed for archive %s; falling back",
                        video_path.name,
                        archive_id,
                    )
                return None

        if asyncio.get_event_loop().time() >= deadline:
            logger.info(
                "[PHOTO-BG] Timelapse for archive %s didn't land within %.0fs; falling back to live camera",
                archive_id,
                _FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS,
            )
            return None

        await asyncio.sleep(poll_interval)


async def _seed_runout_detector_from_journal(printer_id: int, logger, *, generation=None) -> None:
    """Re-arm a mid-print client's runout dedup from what the journal holds.

    ⚠️ The HMS runout status stays active for the WHOLE print — an X2D whose
    mapped slot is empty keeps reporting it to the end, and inserting a reel
    elsewhere never clears it. A client created mid-print starts with an empty
    dedup memory, so it replays that status as a brand-new runout at the
    CURRENT layer. ``record_runout`` folds such a repeat into the open episode,
    which is why this stayed invisible for months — but the moment the operator
    CLOSES the episode (assigning a spool to the slot that emptied journals a
    ``spool_loaded``), the next restart's replay is read as "a second short
    reel" and splits the print's books at a layer nothing happened on.
    Measured on printer 10, archive 838: five restarts, five replays, and the
    one that landed after the operator's assignment cut 23 of 53 layers loose.

    Seeding with no active archive still ARMS the detector — an unseeded client
    holds its fire forever, and a printer with no print of ours has nothing to
    replay anyway.
    """
    client = printer_manager.get_client(printer_id)
    if client is None:
        return

    def seed_is_current():
        return generation is None or (
            printer_manager.get_client(printer_id) is client and client.journal_generation() == generation
        )

    from backend.app.models.print_usage_event import EVENT_RUNOUT
    from backend.app.services.print_usage_journal import active_archive_id, load_events, stale_runouts_to_seed

    try:
        async with async_session() as db:
            archive_id = await active_archive_id(db, printer_id)
            events = await load_events(db, printer_id, archive_id) if archive_id is not None else []
    except Exception:
        # Fall towards ARMED, never towards silence: an unseeded detector holds
        # its fire for the whole print, which would lose a real runout — far
        # worse than the replay this guards against.
        logger.exception("[UsageJournal] Could not read the journal to re-arm printer %s; arming unseeded", printer_id)
        if seed_is_current():
            client.seed_runout_fired({})
        return

    # ⚠️ Only for slots that physically hold filament. A slot the AMS reports
    # EMPTY may be running out RIGHT NOW — unrecorded, because we were down when
    # it happened — and seeding it would silence that for the rest of the print.
    # See ``stale_runouts_to_seed``: the presence bit is the arbiter, never the
    # filament type (our own write), and an episode closed on paper only is
    # seeded anyway.
    seeded = stale_runouts_to_seed(events, printer_manager.get_status(printer_id))
    if seed_is_current():
        client.seed_runout_fired(seeded)
    held = {e.global_tray_id: e.kind for e in events if e.event == EVENT_RUNOUT and e.kind}
    if held:
        logger.info(
            "[UsageJournal] Re-armed runout detector for printer %s from archive %s: %s (journal held %s)",
            printer_id,
            archive_id,
            seeded or "nothing — every ran-out slot reads empty",
            held,
        )


#: Reads of the persisted usage session a restart restore decides on before it gives up (Codex USO-1): a row
#: replaced during the decision is decided again, never deleted or restored on the old evidence.
_SESSION_RESTORE_ATTEMPTS = 3


async def _restore_usage_tracking_session(
    printer_id: int, state, db, logger, *, generation: int | None = None, retry: bool = True
) -> bool:
    """Put the filament-attribution context back after a restart mid-print — under the journal
    lock, taken before ``db`` is first used (post-rebuild F3). No assignment that takes the lock
    can come between the binding snapshot, the replay it names and the replay's write: that write
    happens before the lock is released, or — when it fails — before anything else the lock lets
    through. See ``_restore_usage_tracking_session_locked``.
    """
    from backend.app.services.print_usage_journal import journal_lock

    async with journal_lock(printer_id):
        return await _restore_usage_tracking_session_locked(
            printer_id, state, db, logger, generation=generation, retry=retry
        )


async def _restore_usage_tracking_session_locked(
    printer_id: int, state, db, logger, *, generation: int | None = None, retry: bool = True
) -> bool:
    """Put the filament-attribution context back after a restart mid-print.

    ``usage_tracker._active_sessions`` and ``PrinterState.tray_change_log`` both
    die with the process. The print keeps running, so at completion the tracker
    would fall back to whatever the printer reports *now* — and AMS filament
    backup makes "now" the substitute tray, charging the whole print to the
    spool that only finished it.

    The persisted row is only trusted when its print name still matches what the
    printer says it is running: a row left behind by a completion we never saw
    must not attach itself to the next print.

    The client that joined the print holds its usage journal until this seed
    (post-rebuild F-R4): the chronology, the tray last fed from — the origin of a
    runout tail the journal still holds open, else the chronology's last tray —
    and that tail. It then replays, in order, the tray reports it held. The seed
    names the print it was computed for (``generation``), so a client that has
    moved on to another print refuses it. A failure here leaves the journal held —
    nothing is journaled against an invented clean state — and is retried from the
    same evidence (``_retry_usage_seed``); one that keeps failing ends the hold with
    the feed stated unknown (``abandon_journal_hold``). True when it ran through.
    """
    try:
        from backend.app.api.routes.settings import get_setting
        from backend.app.services.usage_tracker import (
            clear_persisted_snapshot,
            get_persisted_print_name,
            read_persisted_snapshot,
            restore_session,
        )

        persisted_name = await get_persisted_print_name(db, printer_id)
        current_name = (state.subtask_name or "").strip()
        # Spoolman users get the tray-change log back but no in-memory
        # session — see ``usage_tracker.on_print_start`` on why that dict is
        # load-bearing for the remain%-sync guard.
        _spoolman_on = await get_setting(db, "spoolman_enabled")
        register_active = not (bool(_spoolman_on) and _spoolman_on.lower() == "true")
        persisted_log = None
        # Every decision is taken on ONE read of the row and acts on that row only. Across the resolver's
        # await a newer start may write its own session there, and neither the discard nor the restore may
        # touch that row (Codex USO-1). A row that changed under a decision is read and decided again.
        # Only a pre-m221 row of another print is deleted, as before; a stamped or unclaimed row that is not
        # this print's is left to its owner -- the reconcile of a print that ended meanwhile books with it.
        for _attempt in range(_SESSION_RESTORE_ATTEMPTS if persisted_name is not None else 0):
            await db.commit()  # end the read snapshot: the read below sees the row as it is now
            snapshot = await read_persisted_snapshot(db, printer_id)
            if snapshot is None:
                break
            await db.commit()  # no read held across the resolver's await: the writes below see it fresh
            if snapshot.archive_identity is not None:
                # A stamped row is this print's only when the running print resolves to the very archive it
                # names -- by the run binding, the firmware id or the one printing row its name fits. A name
                # proves nothing alone: an A1 mini reports ``X.3mf`` for ``X`` after a power cycle, and
                # another print may share the name (GH #115, Codex USN-1).
                live = await _live_archive_for_running_print(
                    printer_id, {"subtask_name": state.subtask_name, "subtask_id": getattr(state, "subtask_id", None)}
                )
                verdict = "restore" if live.archive_identity == snapshot.archive_identity else "leave"
                why = (
                    f"session archive {snapshot.archive_identity}, running print {live.archive_identity}: {live.reason}"
                )
            elif snapshot.owner_tracked:
                # A start no archive has claimed -- maybe still being decided: proof of nothing.
                verdict, why = "leave", "no archive has claimed it"
            else:
                # A row an earlier version wrote: the name exactly as recorded.
                mismatch = bool(snapshot.print_name and current_name and snapshot.print_name.strip() != current_name)
                verdict = "discard" if mismatch else "restore"
                why = f"{snapshot.print_name!r} != running {current_name!r}"
            if verdict == "restore":
                persisted_log = await restore_session(db, printer_id, register_active=register_active, expect=snapshot)
                if persisted_log is not None:
                    break
                continue
            if verdict == "discard":
                logger.info("[RESTART] Discarding stale print session for printer %s (%s)", printer_id, why)
                if await clear_persisted_snapshot(db, printer_id, snapshot):
                    # Fall through to seeding: the print on the printer is real, it just
                    # isn't the one the row described.
                    break
                continue
            logger.info("[RESTART] Not restoring the print session of printer %s (%s)", printer_id, why)
            await db.commit()
            if await read_persisted_snapshot(db, printer_id) == snapshot:
                break
        else:
            if persisted_name is not None:
                logger.info("[RESTART] Print session of printer %s changed on every read; nothing restored", printer_id)
        log = list(state.tray_change_log or [])
        if persisted_log:
            restored = [tuple(entry) for entry in persisted_log if isinstance(entry, (list, tuple)) and len(entry) == 2]
            # Anything this process already observed goes after the persisted
            # history — the log is ordered by layer, and a fresh process can only
            # have seen changes from later in the print.
            for entry in log:
                if tuple(entry) not in restored:
                    restored.append(tuple(entry))
            log = restored

        tray_now = state.tray_now
        # A loaded source: 0-254, or 255 on a direct rack whose snow said PrintTool 1's input.
        from backend.app.utils.tray_ids import names_loaded_source

        tray_now_loaded = names_loaded_source(tray_now, state)
        if tray_now_loaded and not log:
            # No persisted history — a print that started before this build,
            # or before the row existed. Seed with the tray feeding right now
            # so the remainder of the print is at least attributable to the
            # right spool.
            log = [(tray_now, state.layer_num)]
            logger.info(
                "[RESTART] Seeded tray change log for printer %s: tray=%d at layer=%d",
                printer_id,
                tray_now,
                state.layer_num,
            )

        from backend.app.services.print_usage_journal import active_archive_id, load_events, open_tail_episode

        archive_id = await active_archive_id(db, printer_id)
        tail = open_tail_episode(await load_events(db, printer_id, archive_id)) if archive_id is not None else None
        # Where the next held report is read from: the tail's origin, else the tray the
        # chronology last fed from. Only ever a real tray: ``last_loaded_tray`` is the
        # "survives the end-of-print retract to 255" fallback.
        if tail is not None:
            last_loaded = tail.origin
        elif log:
            last_loaded = log[-1][0]
        elif tray_now_loaded:
            last_loaded = tray_now
        else:
            last_loaded = state.last_loaded_tray

        client = printer_manager.get_client(printer_id)
        if client is not None and generation is not None:
            from backend.app.services import spoolman_source
            from backend.app.services.print_usage_journal import (
                binding_snapshot,
                deliver_pending_replay,
                queue_replay,
            )

            # The bindings the replay names its facts with, read under the journal lock, and the
            # Spoolman database they belong to (C047 acceptance A01); then this session's own work
            # ends, so the replay's write never waits for its writer.
            bindings_stamp = spoolman_source.base_generation()
            bindings = await binding_snapshot(db, printer_id)
            await db.commit()
            # A reconnect during those awaits replaced the client and carried the hold to the new
            # one: seed the client registered now, never a retired one (a missing one is retried).
            client = printer_manager.get_client(printer_id)
            if client is None:
                raise RuntimeError("the printer's client went away while the seed read its bindings")
            if client.seed_usage_journal(
                generation,
                log=log,
                last_loaded_tray=last_loaded,
                tail_origin=tail.origin if tail is not None else None,
                tail_open_layer=tail.start.layer_num if tail is not None else 0,
                bindings=bindings,
                bindings_stamp=bindings_stamp,
            ):
                queue_replay(printer_id, client.take_replay_batches(), archive_id=archive_id)
                await deliver_pending_replay(printer_id)
                if tail is not None:
                    logger.info(
                        "[RESTART] Printer %s resumes the runout tail of tray %d (from layer %d)",
                        printer_id,
                        tail.origin,
                        tail.start.layer_num,
                    )
            else:
                logger.info("[RESTART] Usage-journal seed for printer %s not applied: another print now", printer_id)
        else:
            # No held client to seed (nothing joined): put the chronology back as before.
            state.tray_change_log = log
            if tray_now_loaded:
                state.last_loaded_tray = tray_now
        return True
    except Exception:
        # Never let attribution recovery cost the caller its timelapse baseline —
        # that capture has to happen before the printer uploads the in-flight MP4
        # and there is no second chance at it.
        logger.exception("[RESTART] Failed to restore usage-tracking session for printer %s", printer_id)
        if retry and generation is not None:
            spawn_background_task(
                _retry_usage_seed(printer_id, generation), name=f"usage-journal-seed-retry-{printer_id}"
            )
        return False


#: Waits before each retry of a joined print's usage-journal seed that failed (post-rebuild
#: F-R4). Each reads the same evidence again — the persisted journal and session and the
#: reports the client holds; after the last one the hold ends with the feed stated unknown.
_USAGE_SEED_RETRY_DELAYS = (5.0, 30.0, 120.0)


async def _retry_usage_seed(printer_id: int, generation: int) -> None:
    """Seed the held journal of print ``generation`` again; give up visibly, never silently."""
    logger = logging.getLogger(__name__)

    def still_held():
        client = printer_manager.get_client(printer_id)
        return client is not None and client.journal_generation() == generation and client.journal_held()

    for delay in _USAGE_SEED_RETRY_DELAYS:
        await asyncio.sleep(delay)
        if not still_held():
            return  # seeded meanwhile, or the printer is on another print
        state = printer_manager.get_status(printer_id)
        if state is None:
            continue
        async with async_session() as db:
            if await _restore_usage_tracking_session(printer_id, state, db, logger, generation=generation, retry=False):
                return
    from backend.app.services.print_usage_journal import journal_lock

    async with journal_lock(printer_id):
        abandoned = await _abandon_held_journal_locked(printer_id, generation)
    if abandoned:
        logger.error(
            "[RESTART] Printer %s: the usage journal could not be restored after %d retries; the feed before "
            "the first observed tray report is recorded as unknown",
            printer_id,
            len(_USAGE_SEED_RETRY_DELAYS),
        )


async def _abandon_held_journal_locked(printer_id: int, generation: int) -> bool:
    """End the hold of print ``generation`` whose seed never came, and write what it releases.

    Under the journal lock, as the seed itself (post-rebuild F3): the bindings the released
    reports are named with are read there — no snapshot, and the write reads them in the same
    hold of the lock — and the replay is written before the lock lets anything else through.
    The hold ended is the one of the client registered then: a reconnect while the caller
    waited for the lock carried it to a new client. The caller holds the journal lock.
    """
    from backend.app.core import database
    from backend.app.services import spoolman_source
    from backend.app.services.print_usage_journal import (
        active_archive_id,
        binding_snapshot,
        deliver_pending_replay,
        queue_replay,
    )

    bindings = archive_id = None
    # The Spoolman database the snapshot's numbers belong to (C047 acceptance A01).
    bindings_stamp = spoolman_source.base_generation()
    try:
        # The factory the journal writes with, looked up now (as ``deliver_pending_replay`` does).
        async with database.async_session() as db:
            bindings = await binding_snapshot(db, printer_id)
            archive_id = await active_archive_id(db, printer_id)
    except Exception:  # noqa: BLE001 - the write then reads them, still under this lock
        logging.getLogger(__name__).exception("Binding snapshot for printer %s failed", printer_id)
    client = printer_manager.get_client(printer_id)
    if client is None or not client.abandon_journal_hold(generation, bindings=bindings, bindings_stamp=bindings_stamp):
        return False
    queue_replay(printer_id, client.take_replay_batches(), archive_id=archive_id)
    await deliver_pending_replay(printer_id)
    return True


@dataclass(frozen=True, slots=True)
class _LiveArchiveResolution:
    """Recovery evidence for a print observed after the process started."""

    archive_id: int | None
    plate_index: int | None
    reason: str
    archive_identity: str | None = None


async def _live_archive_for_running_print(printer_id: int, data: dict) -> _LiveArchiveResolution:
    """Resolve a previously running archive without treating a name as identity.

    A live binding or a positive firmware subtask ID is proof.  Names remain a
    recovery aid only where precisely one printing row agrees; multiple
    same-name rows are deliberately unresolved rather than "newest wins".
    """
    from backend.app.models.archive import PrintArchive as _PA
    from backend.app.services.print_reconciliation import _name_matches_subtask
    from backend.app.services.print_run_binding import current_print_run

    subtask = (data.get("subtask_name") or "").strip()
    subtask_id = str(data.get("subtask_id") or "").strip()
    if subtask_id == "0":
        subtask_id = ""
    bound = current_print_run(printer_manager, printer_id)
    async with async_session() as db:
        if bound is not None and bound.matches_device_subtask(subtask_id):
            bound_archive = await db.get(_PA, bound.archive_id)
            if (
                bound_archive is not None
                and bound_archive.printer_id == printer_id
                and bound_archive.status == "printing"
                and (bound.archive_identity is None or bound_archive.recovery_identity == bound.archive_identity)
            ):
                return _LiveArchiveResolution(
                    bound_archive.id, bound_archive.plate_index, "runtime_binding", bound_archive.recovery_identity
                )

        candidates = (
            (
                await db.execute(
                    select(_PA).where(_PA.printer_id == printer_id, _PA.status == "printing").order_by(_PA.id.desc())
                )
            )
            .scalars()
            .all()
        )
    from backend.app.services.print_execution import ExecutionChanged, durable_id

    known = {}
    for archive in candidates:
        try:
            known[archive.id] = durable_id(archive)
        except ExecutionChanged:
            return _LiveArchiveResolution(None, None, "ambiguous_run_identity")
    exact = [a for a in candidates if subtask_id and known[a.id] == subtask_id]
    if len(exact) == 1:
        return _LiveArchiveResolution(exact[0].id, exact[0].plate_index, "subtask_id", exact[0].recovery_identity)
    if len(exact) > 1:
        return _LiveArchiveResolution(None, None, "ambiguous_subtask_id")
    matching = [
        a
        for a in candidates
        if not (subtask_id and known[a.id] and subtask_id != known[a.id])
        and (not subtask or _name_matches_subtask(a, subtask))
    ]
    if len(matching) == 1:
        return _LiveArchiveResolution(
            matching[0].id, matching[0].plate_index, "unambiguous_name", matching[0].recovery_identity
        )
    if len(matching) > 1:
        return _LiveArchiveResolution(None, None, "ambiguous_name")
    return _LiveArchiveResolution(None, None, "no_live_archive")


async def _adopt_running_print(printer_id: int, data: dict, logger, *, still_current=None) -> int | None:
    """The print running on this printer started while BamDude was down: give it
    the row on_print_start would have (spec 2026-09-12). Provisional (3MF to
    follow through the retry service), queue row claimed, energy banked and
    flagged partial, usage session created — and SILENT: no start notification,
    macro, plate check or plug action; those are for a start that just happened.
    Never raises: the hook that calls this is on the connect path.

    Not a call into ``on_print_start``: that handler's first act is to decide
    whether a print is *starting*, and the #1304 guard exists precisely because
    this one is not. What is reproduced here is its fallback row and the side
    effects that row owns, with the same helpers — two differences:

    * ``started_at = None``. The start is unknown, not now;
      ``attach_3mf_to_archive`` reconstructs it from the slicer estimate and
      the remaining time recorded below (§3.3), and a 3MF that never arrives
      leaves it unknown rather than fictitious.
    * ``extra_data['recovered_start']`` carries the two numbers that
      reconstruction needs, taken at the moment we joined. ⚠️
      ``remaining_time`` is SECONDS in this payload (``bambu_mqtt`` builds it as
      minutes × 60) and is recorded verbatim — a conversion here would be
      invisible until it silently moved every adopted print's start.
    """
    try:
        from backend.app.api.routes.printers import clear_cover_cache
        from backend.app.models.archive import PrintArchive
        from backend.app.models.printer import Printer
        from backend.app.services.archive_colors import apply_loaded_spool_colors

        filename = data.get("filename") or ""
        subtask_name = data.get("subtask_name") or ""
        subtask_id = data.get("subtask_id") or None
        if subtask_id == "0":
            subtask_id = None

        # The same two refusals ``on_print_start`` makes before it would create
        # anything: the printer's own calibration run is not the user's print
        # (utils/print_jobs — by path OR by name), and a print we cannot even
        # name is not adoptable.
        if is_internal_printer_job(filename, subtask_name):
            logger.info(
                "[ADOPT] Printer %s is running an internal printer job (%s / %s) — not archived",
                printer_id,
                filename,
                subtask_name,
            )
            return None
        if not filename and not subtask_name:
            logger.info(
                "[ADOPT] Printer %s reports a running print with no filename or subtask — nothing to adopt", printer_id
            )
            return None

        # Named before the duplicate guard below, which needs the row's own
        # ``filename`` form as one of its keys.
        print_name = subtask_name or filename
        print_name = print_name.split("/")[-1]
        print_name = print_name.replace(".gcode.3mf", "").replace(".gcode", "").replace(".3mf", "")
        if not print_name:
            print_name = "Unknown Print"

        # Filename aliases are display/navigation aids, never an execution proof.
        # decide_start rechecks all live candidates before the first INSERT.

        # From the live state, exactly as the fallback row gets it — the attach
        # parser reads this column back to decide which plate to describe.
        live_plate_id = (
            parse_plate_id(data.get("filename"))
            or parse_plate_id((data.get("raw_data") or {}).get("gcode_file"))
            or parse_plate_id(data.get("plate_param"))
        )
        mqtt_filament_meta = _extract_filament_data_from_mqtt(data, _get_start_ams_mapping(data, None))

        async with async_session() as db:
            printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
            if printer is None:
                logger.warning("[ADOPT] Printer %s is not in the DB — nothing to adopt the print onto", printer_id)
                return None

            archive = PrintArchive(
                printer_id=printer_id,
                filename=filename or f"{print_name}.3mf",
                file_path="",  # the retry marker; attach_3mf_to_archive fills it
                file_size=0,
                print_name=print_name,
                subtask_id=subtask_id,
                status="printing",
                started_at=None,
                filament_type=mqtt_filament_meta.get("filament_type"),
                filament_color=mqtt_filament_meta.get("filament_color"),
                plate_index=live_plate_id,
                queue_id=await _default_queue_id_for_printer(db, printer_id),
                extra_data={
                    "original_subtask": subtask_name,
                    "_print_data": {
                        k: v for k, v in data.items() if k not in {"_print_observation", "_execution_decision"}
                    },
                    "recovered_start": {
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "remaining_seconds": data.get("remaining_time") or None,
                    },
                    # The plug counter is read NOW — there is no other moment —
                    # so the figure covers only the remainder of the print.
                    # ``energy_is_approximate`` is the key the reconciliation
                    # sweep already uses for a recovered *end*; the second says
                    # which end was recovered here.
                    "energy_is_approximate": True,
                    "energy_start_partial": True,
                },
            )
            # The run's origin is not proven -- it may be our own print under a renamed file -- so a marked name
            # is no authority, and the record is not pending: no later attach may finish it (D4, spec r4 §7).
            swap_compatibility.write_archive_fact(archive, None)
            if not printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life")):
                # Deleted during the awaits above: the row found by this id is the next record's.
                logger.info("[ADOPT] Printer %s was deleted while adopting -- nothing adopted", printer_id)
                return None
            from backend.app.services.print_execution import decide_start, publish_start

            decision = await decide_start(db, printer_id, data, catch_up=True)
            if decision.current_id is not None:
                return None
            publication = await publish_start(
                db,
                archive,
                decision,
                options=_printer_reported_options(data, None, live_plate_id),
                still_current=still_current
                or (lambda: printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life"))),
            )
            if not publication.current:
                _request_published_run_recovery(printer_id, data, decision.epoch)
                return None
            await db.refresh(archive)
            archive_id = archive.id
            _bind_print_file_analysis_context(printer_id, archive)
            logger.info(
                "[ADOPT] Adopted the print already running on printer %s as archive %s (%s) — start unknown, 3MF to follow",
                printer_id,
                archive_id,
                print_name,
            )

            await apply_loaded_spool_colors(
                db, archive, printer_id, _get_start_ams_mapping(data, archive_id, archive.recovery_identity)
            )
            await db.commit()

            # ``on_print_complete`` finds the row through these, and they are
            # also what stops a reconnect mid-download from adopting twice.
            _remember_active_print((printer_id, archive.filename), archive)
            if filename:
                _remember_active_print((printer_id, filename), archive)
            if subtask_name:
                _remember_active_print((printer_id, f"{subtask_name}.3mf"), archive)
                _remember_active_print((printer_id, subtask_name), archive)

            claim_result = await mark_queue_printing_for_printer(
                printer_id,
                archive_id=archive_id,
                expected_identity=archive.recovery_identity,
                options=_printer_reported_options(data, archive_id, live_plate_id, archive.recovery_identity),
            )
            if not claim_result:
                return
            await maybe_register_external_stagger(printer_id)
            await _record_energy_start(archive, printer_id, db, context="recovered-start")

            # The hook's own ``_restore_usage_tracking_session`` restores a
            # persisted row, and a print we never saw start has none — so the
            # session is CREATED here. Without it ``on_ams_change`` keeps running
            # the remain%-based weight sync until completion and the full
            # write-off at the end counts the first half twice.
            try:
                from backend.app.api.routes.settings import get_setting
                from backend.app.services.usage_tracker import on_print_start as usage_on_print_start

                _spoolman_on = await get_setting(db, "spoolman_enabled")
                await usage_on_print_start(
                    printer_id,
                    data,
                    printer_manager,
                    db=db,
                    spoolman_owns_usage=bool(_spoolman_on) and _spoolman_on.lower() == "true",
                    # The archive is already bound: the session belongs to it from birth (GH #115).
                    archive_identity=archive.recovery_identity,
                )
            except Exception as e:
                logger.warning("[ADOPT] Usage tracker on_print_start failed for archive %s: %s", archive_id, e)

            # A new archive changes the card's cover.
            clear_cover_cache(printer_id)

            await ws_manager.send_archive_created(
                {
                    "id": archive_id,
                    "printer_id": archive.printer_id,
                    "filename": archive.filename,
                    "print_name": archive.print_name,
                    "status": archive.status,
                }
            )
            try:
                await mqtt_relay.on_archive_created(
                    archive_id=archive_id,
                    print_name=archive.print_name,
                    printer_name=printer.name,
                    status=archive.status,
                )
            except Exception:
                pass  # Don't fail the adoption if MQTT fails

        # Never inline: a P1S serves a 22 MB file at 43 KB/s while printing, and
        # this is the connect path.
        spawn_background_task(
            _download_for_adopted_print(printer_id, archive_id, logger, expected_identity=archive.recovery_identity),
            name="adopt-running-print-download",
        )
        return archive_id
    except Exception:
        logger.exception("[ADOPT] Could not adopt the print already running on printer %s", printer_id)
        return None


async def _store_spoolman_for_adopted_print(printer_id: int, archive, db, ams_mapping, logger) -> None:
    """The adopted print's Spoolman tracking row, written once the download
    outcome is known — and whatever that outcome was.

    ``on_print_start`` stores it "with whatever ``file_path`` the row ended up
    carrying", and an adopted print is the same print: it is running, the
    operator's spools are being consumed now. The ``CLAUDE.md`` rule is about
    TIMING — given an empty ``file_path`` ``store_print_data`` falls through to
    its degraded remain%-delta path, which is why the call waits for the
    download instead of preceding it — never about omission. Hanging it off the
    attach instead left a Spoolman install with no row for the WHOLE print
    whenever the firmware locks the file mid-print (no 3MF to fetch) or the
    attach failed.

    The hash twin is the one path that does not come here: that row has had its
    own tracking since it was dispatched, and ours no longer exists.
    """
    try:
        await _store_spoolman_print_data(
            printer_id,
            archive.id,
            archive.file_path,
            db,
            printer_manager,
            ams_mapping=ams_mapping,
            plate_id=archive.plate_index,
        )
    except Exception as e:
        logger.warning("[SPOOLMAN] Failed to store tracking data for adopted archive %s: %s", archive.id, e)


async def _download_for_adopted_print(printer_id: int, archive_id: int, logger, *, expected_identity=None) -> None:
    """Attach recovered bytes to this incarnation; execution is decided before download."""
    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer
    from backend.app.services.archive_download import last_download_failure_reason, try_download_3mf
    from backend.app.services.archive_download_retry import archive_download_retry

    try:
        async with archive_download_retry.claim(archive_id) as claimed:
            if not claimed:
                # Another trigger is fetching this archive's 3MF right now (G10 A7).
                return
            async with async_session() as db:
                archive = await db.get(PrintArchive, archive_id)
                if (
                    archive is None
                    or archive.file_path
                    or (expected_identity is not None and archive.recovery_identity != expected_identity)
                ):
                    return
                printer = await db.get(Printer, printer_id)
                if printer is None:
                    return
                expected_identity = archive.recovery_identity
                meta = archive.extra_data if isinstance(archive.extra_data, dict) else {}
                print_data = meta.get("_print_data") or {}
                subtask_name = print_data.get("subtask_name") or meta.get("original_subtask")
                filename = print_data.get("filename") or archive.filename
                # Read here, not in the attach branch below: every outcome of the
                # download needs it for the tracking row, and it comes from the
                # row's own record of the print, never from what is loaded now.
                ams_mapping = _get_start_ams_mapping(print_data, archive_id, expected_identity)

            result = await try_download_3mf(printer, subtask_name, filename, app_settings.archive_dir / "temp")
            if result is None:
                # "we tried and failed" — the banner rule: set only after a
                # failed attempt. The four retry triggers take over from here.
                logger.warning("[ADOPT] No 3MF on printer %s for adopted archive %s yet", printer_id, archive_id)
                async with async_session() as db:
                    archive = await db.get(PrintArchive, archive_id)
                    if archive is not None and archive.recovery_identity == expected_identity:
                        if not archive.file_path:
                            extra = {**(archive.extra_data or {}), "no_3mf_available": True}
                            reason = last_download_failure_reason(printer.id)
                            if reason:
                                extra["no_3mf_reason"] = reason
                            archive.extra_data = extra
                            await db.commit()
                        # Locked-file firmware never hands the 3MF over, and the
                        # print still runs to the end — so the tracking row is
                        # written now, on the degraded path the empty
                        # ``file_path`` selects.
                        await _store_spoolman_for_adopted_print(printer_id, archive, db, ams_mapping, logger)
                return

            temp_path, downloaded_filename = result
            try:
                async with async_session() as db:
                    archive = await db.get(PrintArchive, archive_id)
                    if (
                        archive is None
                        or archive.file_path
                        or (expected_identity is not None and archive.recovery_identity != expected_identity)
                    ):
                        return
                    service = ArchiveService(db)
                    ok = await service.attach_3mf_to_archive(
                        archive_id,
                        temp_path,
                        downloaded_filename,
                        received_name=downloaded_filename,
                        expected_identity=expected_identity,
                    )
                    if not ok:
                        await service.mark_3mf_unavailable(archive_id, expected_identity=expected_identity)
                    refreshed = await db.get(PrintArchive, archive_id)
                    if ok:
                        logger.info("[ADOPT] Attached %s to adopted archive %s", downloaded_filename, archive_id)
                        # ⚠️ From here the tail is ``on_print_start``'s, not the
                        # retry service's: the retry service fills a row somebody
                        # else already wired up, while this task owns the adopted
                        # print end to end. Both calls below are silent losses
                        # when skipped — and they are the half of the tail that
                        # needs the file: there are no slicer colours to outrank
                        # and no objects to load when nothing was attached.
                        from backend.app.services.archive import load_objects_from_archive_into_state
                        from backend.app.services.archive_colors import apply_loaded_spool_colors

                        if refreshed is not None:
                            # The attach has just overwritten filament_color with
                            # the slicer's own colours, and a loaded inventory
                            # spool outranks those.
                            await apply_loaded_spool_colors(db, refreshed, printer_id, ams_mapping)
                            await db.commit()
                            _notify_print_file_analysis_source_ready(printer_id, refreshed)
                            load_objects_from_archive_into_state(refreshed, printer_id)
                        await ws_manager.send_archive_updated({"id": archive_id, "recovered_3mf": True})
                    else:
                        # The bytes arrived but could not be attached. The row
                        # keeps its empty ``file_path``, so the retry triggers
                        # come back to it — a failed attach must not read as a
                        # finished one.
                        logger.warning(
                            "[ADOPT] Could not attach %s to adopted archive %s", downloaded_filename, archive_id
                        )
                    # The other half of the tail follows the download's OUTCOME,
                    # not the attach's success: on a failed attach the row keeps
                    # its empty ``file_path`` and the store takes the degraded
                    # path, exactly as it does when there was no 3MF at all.
                    if refreshed is not None:
                        await _store_spoolman_for_adopted_print(printer_id, refreshed, db, ams_mapping, logger)
            finally:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass
    except Exception:
        logger.exception("[ADOPT] 3MF download for adopted archive %s failed", archive_id)


def _request_published_run_recovery(printer_id: int, data: dict, epoch: int) -> None:
    """Hand a committed run to a replacement connection, never bind from the stale callback."""
    from backend.app.services.print_observation import observation_is_current
    from backend.app.services.queue_sources import current_epoch

    client = printer_manager.get_client(printer_id)
    if (
        client is None
        or epoch != current_epoch()
        or not printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life"))
        or not observation_is_current(printer_manager, printer_id, data.get("_print_observation"))
    ):
        return
    generation = data.get("_journal_generation")
    if generation is None and data.get("_print_observation") is not None:
        generation = client.journal_generation()
    if isinstance(generation, int):
        client.request_inflight_recovery(generation)


async def on_print_running_observed(printer_id: int, data: dict):
    """Restart-recovery of a print already running, registered while it looks its run up.

    Registered before the first await: a layer edge scheduled after this callback finds the registration and
    waits for the run's binding before it decides the print's once-only first-layer announcement.
    """
    settled = asyncio.Event()
    pending = _running_print_adoptions.setdefault(printer_id, [])
    pending.append(settled)
    try:
        await _recover_running_print(printer_id, data)
    finally:
        settled.set()
        pending.remove(settled)
        if not pending and _running_print_adoptions.get(printer_id) is pending:
            del _running_print_adoptions[printer_id]


async def _recover_running_print(printer_id: int, data: dict):
    """Restart-recovery: capture a fresh timelapse baseline for a print that
    started before BamDude came up.

    bambu_mqtt.py suppresses ``on_print_start`` on the first RUNNING push
    after BamDude startup (#1304 guard, prevents duplicate archive creation).
    Without that path, ``_capture_timelapse_baseline_at_start`` never runs and
    ``_scan_for_timelapse_with_retries`` falls into its "take baseline now"
    fallback at completion time — but by then the printer has already uploaded
    the in-flight MP4, so the baseline includes it and no diff ever matches
    (#1485 follow-up).

    Fires once per session, in lieu of on_print_start when restart-recovery
    kicks in. The printer doesn't upload the timelapse until after PRINT
    COMPLETE, so a baseline captured any time during the print is still
    pre-upload.
    """
    if not printer_manager.accepts_client_callback_generation(printer_id, data.get("_bamdude_client_generation")):
        return
    logger = logging.getLogger(__name__)
    if not printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life")):
        logger.warning("Ignoring a running print observed by a deleted printer record %s", printer_id)
        return

    def recovery_current():
        client = printer_manager.get_client(printer_id)
        state = printer_manager.get_status(printer_id)
        generation = data.get("_journal_generation")
        return (
            printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life"))
            and state is not None
            and state.state in ("RUNNING", "PAUSE")
            and printer_manager.accepts_client_callback_generation(printer_id, data.get("_bamdude_client_generation"))
            and (generation is None or (client is not None and client.journal_generation() == generation))
        )

    if not recovery_current():
        return

    # Attribution recovery first, and deliberately BEFORE the baseline
    # early-return below: the two are independent, and a printer that already
    # has a baseline in this process must still get its tray-change log back.
    _state = printer_manager.get_status(printer_id)
    if _state is not None:
        async with async_session() as db:
            await _restore_usage_tracking_session(
                printer_id, _state, db, logger, generation=data.get("_journal_generation")
            )

    # ⚠️ Before the detector can fire: this client's runout dedup is empty and
    # the printer's HMS status is not. Unseeded, it holds its fire — so this
    # must run on every restart-recovery, not only when a session was restored.
    if not recovery_current():
        return
    await _seed_runout_detector_from_journal(printer_id, logger, generation=data.get("_journal_generation"))
    if not recovery_current():
        return

    # A print already under way claims its queue row like any other print, and
    # the gate armed before we joined — by the reconnect sweep, or left from the
    # previous FINISH — is moot: this bed is printing. Without the claim the
    # completion found no row to close and Repeat had nothing to re-arm
    # (2026-09-04). Only a live archive earns a row: one without an archive
    # could never be repeated and would sit in the queue for ever — so when
    # there is none, one is CREATED (spec 2026-09-12): the print started while
    # BamDude was down and had no row anywhere. ``_adopt_running_print`` claims
    # the queue row itself, so there is nothing left to claim here.
    claimed = False
    try:
        live = await _live_archive_for_running_print(printer_id, data)
        if not recovery_current():
            return
        if live.archive_id is None and live.reason.startswith("ambiguous_"):
            logger.warning(
                "[RECOVERY] Not adopting print on printer %s: %s leaves no unique archive address",
                printer_id,
                live.reason,
            )
        elif live.archive_id is None:
            # Nothing below reads the new id: the adoption claims the queue row
            # itself, and the plate-gate release keys off the printer.
            claimed = await _adopt_running_print(printer_id, data, logger, still_current=recovery_current) is not None
        else:
            from backend.app.services.print_execution import decide_start, publish_start

            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                archive = await db.get(PrintArchive, live.archive_id)
                if (
                    archive is None
                    or archive.status != "printing"
                    or (live.archive_identity is not None and archive.recovery_identity != live.archive_identity)
                ):
                    return
                decision = await decide_start(db, printer_id, data, catch_up=True)
                if decision.current_id != archive.id:
                    return
                if decision.old:
                    publication = await publish_start(db, archive, decision, still_current=recovery_current)
                    if not publication.current:
                        _request_published_run_recovery(printer_id, data, decision.epoch)
                        return
            claim_result = await mark_queue_printing_for_printer(
                printer_id,
                archive_id=live.archive_id,
                expected_identity=live.archive_identity,
                options=_printer_reported_options(data, live.archive_id, live.plate_index, live.archive_identity),
            )
            if not claim_result or not recovery_current():
                return
            # A typed unresolved claim never acquires an in-memory run binding.
            async with async_session() as db:
                archive = await db.get(PrintArchive, live.archive_id)
                if archive is None or archive.recovery_identity != claim_result.archive_identity:
                    return
                _bind_print_file_analysis_context(printer_id, archive)
            claimed = True
        if claimed and recovery_current() and printer_manager.is_awaiting_plate_clear(printer_id):
            from backend.app.services.plate_hold import clear_held_row

            # Under the printer's lanes and the held print's guard, as every plate answer (G03 B, A21).
            await clear_held_row(printer_id)
            printer_manager.set_awaiting_plate_clear(printer_id, False)
            logger.info("[PLATE] Released the plate gate for printer %s — it is printing", printer_id)
    except Exception:  # noqa: BLE001 — restart recovery must never break the connect path
        logger.exception("[QUEUE] Could not claim a row for the print already running on printer %s", printer_id)

    # Avoid double-capture: on_print_start may have run earlier in this process.
    if printer_id in _timelapse_baselines:
        logger.debug(
            "[TIMELAPSE] on_print_running_observed: baseline already present for printer %s, skipping",
            printer_id,
        )
        return

    async with async_session() as db:
        from backend.app.models.printer import Printer

        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
        if not printer:
            logger.warning(
                "[TIMELAPSE] on_print_running_observed: printer %s not found in DB, skipping baseline",
                printer_id,
            )
            return

    # A late baseline: the print is already running. What exists now is not new;
    # it is kept with the archive when the running print has one unique address.
    late_archive_id = late_identity = None
    try:
        late = await _live_archive_for_running_print(printer_id, data)
        late_archive_id, late_identity = late.archive_id, late.archive_identity
    except Exception as exc:  # noqa: BLE001 — recovery must never break the connect path
        logger.debug("[TIMELAPSE] No archive address for the late baseline of printer %s: %s", printer_id, exc)
    await _capture_timelapse_baseline_at_start(
        printer,
        printer_id,
        logger,
        archive_id=late_archive_id,
        archive_identity=late_identity,
        taken="late",
        # The recovery's own proof: a list read for a run that was reset or
        # replaced meanwhile reaches neither the archive nor memory.
        may_run=recovery_current,
    )


async def on_finish_photo_moment(printer_id: int, data: dict):
    """Pre-capture a finish photo when the printer enters stage 22 / FINISH (#1721).

    Fires either at the stage-22 ("Filament unloading") edge — toolhead
    parked, bed not yet dropped, optimal framing — or as a FINISH-state
    fallback for prints that skip stage 22 (cancel, external-spool-only,
    HMS halt, firmware variants). Grabs one frame via the same
    external-camera / RTSP path the post-completion fallback uses, stores
    the JPEG bytes in ``_stage22_finish_frames[photo_key]``, and lets
    ``_background_finish_photo`` consume the cached bytes when it runs.

    Replaces the #1397 "force timelapse on at dispatch" mechanism, which
    caused per-layer nozzle parking on slicer profiles with Timelapse Type
    set to Smooth (#1721). No force-on now means the user's explicit
    timelapse=off in the slicer send dialog is respected.
    """
    logger = logging.getLogger(__name__)
    trigger = data.get("trigger", "unknown")
    timelapse_was_active = bool(data.get("timelapse_was_active"))
    logger.info(
        "[FINISH-PHOTO-MOMENT] printer=%s trigger=%s timelapse_active=%s",
        printer_id,
        trigger,
        timelapse_was_active,
    )

    # A delayed end-of-print edge from A can otherwise bank/capture a picture
    # for B before the terminal callback's own admission gate gets a chance to
    # run. Old, identity-free firmware data keeps the established best-effort
    # behaviour; a named or ID-bearing event needs one active queue candidate.
    has_run_identity = bool(str(data.get("subtask_name") or "").strip()) or bool(
        str(data.get("subtask_id") or "").strip() not in ("", "0")
    )
    from backend.app.services.print_run_binding import (
        current_print_effect_token,
        current_print_run,
        print_effect_is_current,
    )

    token = current_print_effect_token(printer_manager, printer_id)
    run = current_print_run(printer_manager, printer_id)
    if token is None or (run is not None and not run.matches_device_subtask(data.get("subtask_id"))):
        return
    photo_key = (printer_id, token.generation)

    # If a timelapse is actively recording, skip the pre-capture — the
    # post-completion path will extract the last frame from the recorded
    # video, which still provides the best framing (toolhead parked,
    # before bed drop) without the per-layer parking side effects.
    if timelapse_was_active:
        logger.info(
            "[FINISH-PHOTO-MOMENT] timelapse active for printer %s — skipping pre-capture "
            "(last-frame extraction will run post-completion)",
            printer_id,
        )
        return

    # #1790: register producer-done BEFORE the first await so the back-to-back
    # consumer sees it immediately. The finally below guarantees set() on every exit
    # (captured / gave up / errored / disabled-setting early-return). Kept AFTER the
    # timelapse return so the timelapse branch stays event-free.
    producer_done = asyncio.Event()
    _stage22_finish_in_flight[photo_key] = producer_done

    try:
        if has_run_identity and await _completion_conflicts_with_active_queue(printer_id, data):
            return
        if not print_effect_is_current(printer_manager, token):
            return
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.printer import Printer

            capture_setting = await get_setting(db, "capture_finish_photo")
            if capture_setting is None or capture_setting.lower() != "true":
                logger.info("[FINISH-PHOTO-MOMENT] capture_finish_photo disabled — skipping pre-capture")
                return

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if printer is None:
                logger.warning("[FINISH-PHOTO-MOMENT] printer %s not found in DB", printer_id)
                return

        if not print_effect_is_current(printer_manager, token):
            return

        frame_bytes: bytes | None = None

        # #1867 follow-on: on the FINISH-state fallback the End G-code (e.g. a
        # SwapMod plate swap) has already run, so a live grab now captures the
        # swapped or empty plate. Prefer the banked in-print frame — the finished
        # print from the last object layer, before the swap. Only for
        # ``finish_state``: the ``stage_22`` and ``last_layer`` triggers fire
        # before the swap and give cleaner (parked-toolhead) framing live.
        # The banked frame arrives ALREADY rotated — it comes from
        # ``_capture_snapshot_for_notification``, which rotates before it
        # returns. Every other source below is a raw grab. Tracking which is
        # what lets ``_stage22_finish_frames`` hold exactly one rotation either
        # way, so consumers never have to guess (#2708).
        frame_already_rotated = False
        if trigger == "finish_state":
            banked = _inprint_frame_bank.get(photo_key)
            if banked:
                frame_bytes = banked
                frame_already_rotated = True
                logger.info(
                    "[FINISH-PHOTO-MOMENT] using banked in-print frame (%d bytes) — "
                    "avoids post-swap live grab on stage-22-less firmware",
                    len(banked),
                )

        # Reuse the live view before touching either camera (#2707). The
        # external branch never consulted the buffer at all, and the built-in
        # one below checked the buffer without asking whether a viewer was
        # attached — so an empty buffer during a live view fell through to a
        # competing handle on a single-reader device.
        if frame_bytes is None:
            from backend.app.api.routes.camera import live_frame_for_picture

            # A camera with its own snapshot URL is not deferred: the capture below fetches that still.
            defer, buffered_live = live_frame_for_picture(printer_id, printer)
            if defer:
                if buffered_live:
                    frame_bytes = buffered_live
                    logger.info("[FINISH-PHOTO-MOMENT] reused the live view's frame (%d bytes)", len(buffered_live))
                else:
                    logger.info(
                        "[FINISH-PHOTO-MOMENT] viewer attached for printer %s with an empty buffer — "
                        "not opening a competing handle; the post-completion fallback will retry",
                        printer_id,
                    )
                    frame_bytes = None

        if frame_bytes is None and printer.external_camera_enabled and printer.external_camera_url:
            from backend.app.services.camera_runtime import CameraCaptureRequest, capture

            frame_bytes = (
                await capture(
                    CameraCaptureRequest.external(
                        url=printer.external_camera_url,
                        camera_type=printer.external_camera_type or "mjpeg",
                        snapshot_url=printer.external_camera_snapshot_url,
                        purpose="finish_photo",
                        printer_id=printer_id,
                    )
                )
            ).frame
            if frame_bytes:
                logger.info("[FINISH-PHOTO-MOMENT] captured external-camera frame (%d bytes)", len(frame_bytes))
        elif frame_bytes is None:
            from backend.app.api.routes.camera import get_buffered_frame

            buffered = get_buffered_frame(printer_id)
            if buffered:
                frame_bytes = buffered
                logger.info("[FINISH-PHOTO-MOMENT] used buffered RTSP frame (%d bytes)", len(frame_bytes))
            else:
                from backend.app.services.camera_runtime import CameraCaptureRequest, capture

                frame_bytes = (
                    await capture(
                        CameraCaptureRequest.builtin(
                            ip_address=printer.ip_address,
                            access_code=printer.access_code,
                            model=printer.model,
                            timeout=15,
                            purpose="finish_photo",
                            printer_id=printer_id,
                        )
                    )
                ).frame
                if frame_bytes:
                    logger.info("[FINISH-PHOTO-MOMENT] captured RTSP frame (%d bytes)", len(frame_bytes))

        if (
            frame_bytes
            and print_effect_is_current(printer_manager, token)
            and _stage22_finish_in_flight.get(photo_key) is producer_done
        ):
            if not frame_already_rotated:
                frame_bytes = _apply_camera_rotation(frame_bytes, printer, logger)
            _stage22_finish_frames[photo_key] = frame_bytes
        else:
            logger.warning(
                "[FINISH-PHOTO-MOMENT] no frame captured for printer %s — post-completion fallback will retry",
                printer_id,
            )
    except Exception as e:
        logger.warning("[FINISH-PHOTO-MOMENT] pre-capture failed for printer %s: %s", printer_id, e)
    finally:
        # #1790: always unblock the consumer's bounded wait — captured / gave up / errored.
        producer_done.set()


async def _completion_belongs_to_item(db, queue_item, data: dict) -> bool:
    """Whether this completion event is the one that closes *queue_item*.

    Modern MQTT callbacks carry the printer's ``subtask_id`` snapshot as well
    as its name; old firmware and external starts can still be name-only.  The
    row used to be found by printer + ``status ='printing'`` alone, so *any*
    completion delivered for a printer closed whichever job was printing on it.
    The printer's own calibration runs and the tail of a previous job both
    arrive that way.

    The damage is not a wrong row in a list. A job closed like this is marked
    completed while the printer is still working, leaves the queue into history,
    and **strands the rest of its batch**: ``check_queue`` counts a printing row
    as a busy printer, and nothing else ever closes one, so the queue stops
    until somebody cancels by hand (upstream #2819 / #2829).

    ⚠️ **Only a positive disagreement refuses.** No archive, no recorded name or
    no subtask name in the payload is *unverifiable*, not *wrong* — and refusing
    there would strand the row just as thoroughly, only for a different reason.
    Comparison goes through :func:`_name_matches_subtask`, which already folds
    the space/underscore substitution the firmware applies and tolerates the
    printer truncating a long name; it learned that from our own X2D losing a
    two-hour print on restart, and it is the reason this guard can be added
    without repeating that lesson.
    """
    subtask = str(data.get("subtask_name") or "").strip()
    subtask_id = str(data.get("subtask_id") or "").strip()
    if subtask_id == "0":
        subtask_id = ""
    if not subtask or not queue_item.archive_id:
        # An echoed non-zero ID is sufficient by itself.  A name can be absent
        # on terminal firmware deltas, but an ID disagreement is still a real
        # disagreement and must not close the current row.
        if not (subtask_id and queue_item.archive_id):
            return True
    from backend.app.models.archive import PrintArchive as _PAMatch
    from backend.app.services.print_reconciliation import _name_matches_subtask

    archive = await db.get(_PAMatch, queue_item.archive_id)
    if archive is None:
        return True
    if subtask_id and archive.subtask_id:
        if archive.subtask_id == subtask_id:
            return True
        logging.getLogger(__name__).warning(
            "Completion subtask_id %r does not match queue item %s archive id %r — leaving the row alone",
            subtask_id,
            queue_item.id,
            archive.subtask_id,
        )
        return False
    if not subtask or not (archive.print_name or archive.filename):
        return True
    if _name_matches_subtask(archive, subtask):
        return True
    logging.getLogger(__name__).warning(
        "Completion for %r does not match queue item %s (dispatched as %r) — leaving the row alone",
        subtask,
        queue_item.id,
        archive.print_name or archive.filename,
    )
    return False


async def _printing_rows_for_printer(db, printer_id: int) -> list:
    """Rows in ``printing`` on this printer's queue, newest first.

    ⚠️ By the queue's ``printer_id``, never ``queue_id == printer_id``: the two
    are equal by construction and guaranteed by nothing (see
    ``services/printer_queues``), and a completion that looked in the wrong
    queue found no row to close — for ever. Newest first, so a stale row from
    an earlier session cannot shadow the live one when the guard below
    compares names.
    """
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue

    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.queue_id.in_(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id)))
        .where(PrintQueueItem.status == "printing")
        .order_by(PrintQueueItem.started_at.desc().nullslast(), PrintQueueItem.id.desc())
    )
    return list(result.scalars().all())


async def _bound_printing_item(db, printer_id: int, binding):
    """Return the one printing row addressed by an accepted run binding.

    The binding's archive is an execution archive, so this is a direct DB
    relation rather than another round of filename comparison.  A vanished or
    reused row is deliberately not replaced by a similarly named candidate.
    """

    rows = await _printing_rows_for_printer(db, printer_id)
    matches = [row for row in rows if row.archive_id == binding.archive_id]
    if binding.queue_item_id is not None:
        matches = [row for row in matches if row.id == binding.queue_item_id]
    return matches[0] if len(matches) == 1 else None, bool(rows and len(matches) != 1)


_TERMINAL_ACCEPTANCE_KEY = "bamdude_terminal_acceptance"
_TERMINAL_EFFECTS_KEY = "bamdude_terminal_effects"


def _terminal_acceptance_attempt_payload(queue_item) -> dict[str, object]:
    """A durable address for this queue attempt, not its printable name."""

    return {
        "queue_item_id": queue_item.id,
        # A repeated queue item is a new physical run once its claim timestamp
        # changes. ISO text stays portable through SQLite's timezone handling.
        "queue_started_at": queue_item.started_at.isoformat() if queue_item.started_at is not None else None,
    }


def _normalise_terminal_outcome(data: dict) -> str | None:
    status = str(data.get("status") or "completed").lower()
    if status == "aborted":
        status = "cancelled"
    return status if status in {"completed", "failed", "cancelled"} else None


def _terminal_acceptance_payload(queue_item, outcome: str | None = None) -> dict[str, object]:
    """Checkpoint payload, including only minimal recovery-safe terminal data."""
    return {
        "version": 2,
        **_terminal_acceptance_attempt_payload(queue_item),
        "outcome": outcome,
        "stage": "accepted",
    }


def _terminal_acceptance_matches(queue_item, archive) -> bool:
    """Whether this exact queue attempt already owns terminal processing."""

    extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
    marker = extra.get(_TERMINAL_ACCEPTANCE_KEY)
    return isinstance(marker, dict) and all(
        marker.get(key) == value for key, value in _terminal_acceptance_attempt_payload(queue_item).items()
    )


async def _record_terminal_effect_stage(
    archive_id: int | None,
    effect: str,
    stage: str,
    *,
    expected_identity: str | None = None,
) -> None:
    """Keep a narrow diagnostic record around a non-replay-safe terminal effect.

    Completion recovery deliberately does not replay printer I/O after a
    process crash.  These records distinguish an action that was never begun
    from one that was in flight, without turning archive metadata into a
    general-purpose outbox.  The accepting handler remains the only producer.
    """

    if archive_id is None or expected_identity is None:
        return

    from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

    try:
        async with async_session() as db, archive_write_scope(db, archive_id):
            archive = await load_active_archive_for_write(db, archive_id)
            if archive is None or archive.recovery_identity != expected_identity:
                return
            extra = dict(archive.extra_data or {})
            effects = dict(extra.get(_TERMINAL_EFFECTS_KEY) or {})
            effects[effect] = {
                "stage": stage,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            extra[_TERMINAL_EFFECTS_KEY] = effects
            archive.extra_data = extra
            await db.commit()
    except Exception:
        # The marker narrows post-crash diagnosis; it must never become the
        # reason an already-accepted terminal action is retried or abandoned.
        logging.getLogger(__name__).warning(
            "Could not record terminal effect %s=%s for archive %s",
            effect,
            stage,
            archive_id,
            exc_info=True,
        )


async def _accept_bound_terminal_run(printer_id: int, binding, data: dict | None = None) -> int | None:
    """Durably claim a known terminal run before any printer-wide effect.

    The archive guard serializes two terminal callbacks across processes.  The
    marker is scoped to the queue attempt's ``started_at`` rather than the
    filename (or archive alone), so re-arming the same queue row remains a new
    run while a retry/restart cannot replay a finishing run's effects.
    """

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

    async with async_session() as db, archive_write_scope(db, binding.archive_id):
        # Archive → queue is the established write order.  ``archive_write_scope``
        # acquires SQLite's writer before the read and PG's advisory+row locks.
        archive = await load_active_archive_for_write(db, binding.archive_id)
        queue_item, unresolved = await _bound_printing_item(db, printer_id, binding)
        if (
            archive is None
            or queue_item is None
            or archive.status != "printing"
            or (binding.archive_identity is not None and binding.archive_identity != archive.recovery_identity)
            or "noncurrent_execution" in (archive.extra_data or {})
        ):
            logging.getLogger(__name__).warning(
                "Bound terminal archive %s on printer %s is unresolved (archive=%s ambiguous=%s); preserving current run",
                binding.archive_id,
                printer_id,
                archive is not None,
                unresolved,
            )
            return None
        # Lock the addressed row after the archive; a reused row cannot
        # change identity while this short acceptance transaction commits.
        queue_item = await db.scalar(
            select(PrintQueueItem)
            .where(PrintQueueItem.id == queue_item.id, PrintQueueItem.status == "printing")
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if queue_item is None or queue_item.archive_id != binding.archive_id:
            logging.getLogger(__name__).warning(
                "Bound terminal archive %s on printer %s lost its addressed queue row; preserving current run",
                binding.archive_id,
                printer_id,
            )
            return None
        if _terminal_acceptance_matches(queue_item, archive):
            logging.getLogger(__name__).info(
                "Ignoring already accepted terminal for archive %s queue item %s",
                binding.archive_id,
                queue_item.id,
            )
            return None

        extra = dict(archive.extra_data or {})
        extra[_TERMINAL_ACCEPTANCE_KEY] = _terminal_acceptance_payload(
            queue_item, _normalise_terminal_outcome(data or {})
        )
        archive.extra_data = extra
        await db.commit()

    from backend.app.services.print_run_binding import begin_print_run_finishing, current_print_run

    # The DB read awaited. A newer B may have replaced A while it ran, so only
    # the still-current exact binding may enter finishing.
    if current_print_run(printer_manager, printer_id) != binding:
        return None
    if begin_print_run_finishing(printer_manager, printer_id, binding.archive_id) is None:
        return None
    return queue_item.id


def _completion_may_apply_printer_effect(
    printer_id: int,
    archive_id: int | None,
    *,
    is_addressed_run: bool,
) -> bool:
    """Keep an old finishing run away from the next run's printer resources."""
    if not is_addressed_run or archive_id is None:
        # A filename/adoption callback can resolve an archive row without ever
        # having acquired the new in-process address.  It remains the legacy
        # best-effort path; only a run that entered the addressed binding may
        # be vetoed for a newer physical run.
        return True
    from backend.app.services.print_run_binding import completion_effects_are_owned

    return completion_effects_are_owned(printer_manager, printer_id, archive_id)


async def _completion_conflicts_with_active_queue(printer_id: int, data: dict) -> bool:
    """Whether a terminal MQTT event positively belongs to no active queue row.

    This runs before printer-wide completion side effects.  Missing identity is
    still unverifiable (legacy external firmware) and retains the established
    fallback; a positive disagreement must not clear users, fire macros, or
    release a queue header for another run.
    """
    async with async_session() as db:
        rows = await _printing_rows_for_printer(db, printer_id)
        if not rows:
            from backend.app.models.archive import PrintArchive
            from backend.app.services.print_observation import run_id

            observed_id = run_id(data.get("subtask_id"))
            return bool(
                observed_id
                and await db.scalar(
                    select(PrintArchive.id)
                    .where(
                        PrintArchive.printer_id == printer_id,
                        PrintArchive.subtask_id == observed_id,
                        PrintArchive.extra_data["noncurrent_execution"].as_string().is_not(None),
                    )
                    .limit(1)
                )
            )
        matching = [row for row in rows if await _completion_belongs_to_item(db, row, data)]
        if len(matching) == 1:
            from backend.app.models.archive import PrintArchive

            archive = await db.get(PrintArchive, matching[0].archive_id) if matching[0].archive_id else None
            if archive is not None and _terminal_acceptance_matches(matching[0], archive):
                logging.getLogger(__name__).info(
                    "Ignoring terminal already accepted for archive %s queue item %s",
                    archive.id,
                    matching[0].id,
                )
                return True
            return False
        # Legacy terminal deltas can be identity-free.  Preserve their existing
        # fallback: we cannot prove them foreign.  Once a name or non-zero ID
        # is present, however, neither zero nor several candidates is safe to
        # attribute to a printer-wide completion event.
        subtask_id = str(data.get("subtask_id") or "").strip()
        has_identity = bool((data.get("subtask_name") or "").strip()) or bool(subtask_id and subtask_id != "0")
        if not has_identity:
            return False
        logging.getLogger(__name__).warning(
            "Ignoring unbound print completion on printer %s; matching=%s active queue items=%s",
            printer_id,
            [row.id for row in matching],
            [row.id for row in rows],
        )
        return True


async def _pause_after_swap_failure(
    printer_id: int, archive_id: int, execution_item_id: int | None, text: str, *, may_hold
) -> None:
    """The change_table macro failed: pause this printer's queue and say why on its next waiting job.

    ⚠️ A REAL pause, not just a waiting reason: the scheduler recomputes the reason every tick and clears it the
    moment the printer looks idle, so the reason alone never held anything -- the next job dispatched onto the
    un-swapped table one tick later. Under the printer's lane after the archive's guard, the queue read before the
    guards and again under them; the halt through its one writer (G03 B, A21, A9).
    """
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.queue_counters import halt_lane
    from backend.app.services.queue_ops import claim_lanes
    from backend.app.services.queue_wait_reason import set_wait_reason

    async with async_session() as db:
        # queue_id is NOT printer_id — resolve the row.
        planned = await db.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
        if planned is None:
            return
        await db.commit()
        async with claim_lanes(db, [planned], archive_id=archive_id):
            if (
                await db.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id)) != planned
                or not may_hold()
            ):
                return
            await halt_lane(db, planned, item_id=execution_item_id, kind="paused")
            waiting = await db.scalar(
                select(PrintQueueItem)
                .where(PrintQueueItem.queue_id == planned, PrintQueueItem.status == "pending")
                .order_by(PrintQueueItem.position)
                .limit(1)
                .with_for_update(key_share=True)
            )
            if waiting is not None and may_hold():
                set_wait_reason(waiting, "dispatch_failed", text)
            await db.commit()


async def _hold_for_owed_swap(
    printer_id: int,
    archive_id: int,
    execution_item_id: int | None,
    reason: str,
    cycle: str,
    *,
    owns_printer: bool,
    expected_identity: str | None = None,
    may_hold=None,
) -> str | None:
    """Hold this printer's queue because our owed table swap did not run (swap-source-lifecycle spec r4 §10).

    The archive records why (codes the archive card shows) whoever owns the printer now; the queue pause and the
    sentences -- on the next waiting job AND on the job that ran, so the reason is there without a next job --
    only while this run still owns the printer. ``set_queue_idle`` releases only a ``printing`` queue, so a later
    release of this completion cannot reopen it. Returns the sentence when the queue was held.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import swap_terminal
    from backend.app.services.archive_write_scope import archive_write_scope, locked_print

    try:
        async with async_session() as db, archive_write_scope(db, archive_id):
            archive = await locked_print(
                db, archive_id, select(PrintArchive).where(PrintArchive.id == archive_id).with_for_update()
            )
            if expected_identity is None or (archive is not None and archive.recovery_identity != expected_identity):
                return None
            if archive is not None and archive.deleted_at is None:
                extra = dict(archive.extra_data or {})
                extra[swap_terminal.HOLD_KEY] = swap_terminal.hold_record(reason, cycle)
                archive.extra_data = extra
            await db.commit()
    except Exception:
        logging.getLogger(__name__).warning("Could not record the swap hold of archive %s", archive_id, exc_info=True)
    if not owns_printer:
        return None
    text = swap_terminal.hold_text(reason, cycle)
    # The pause writes the header and job rows: under the printer's lane, after the archive (G03 B, A21) -- the
    # queue read before the guards and again under them.
    from backend.app.services.queue_counters import halt_lane
    from backend.app.services.queue_ops import claim_lanes

    async with async_session() as preflight:
        planned = await preflight.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
    if planned is None:
        return None
    async with async_session() as db, claim_lanes(db, [planned], archive_id=archive_id):
        archive = await locked_print(
            db, archive_id, select(PrintArchive).where(PrintArchive.id == archive_id).with_for_update()
        )
        if expected_identity is None or (archive is not None and archive.recovery_identity != expected_identity):
            return None
        queue_id = await db.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
        if queue_id != planned:
            logging.getLogger(__name__).info(
                "Swap hold of archive %s: printer %s's queue changed under the lane; preserving claims",
                archive_id,
                printer_id,
            )
            return None
        if may_hold is not None and not may_hold():
            return None
        # Without a proven execution row (an unbound finish) the printer's queue is held as a whole; a lane another
        # run holds is paused only, its claim left as it is (A9). A ``printing`` header no row of the queue prints
        # is nobody's claim: the addressed stale form halts it (a plain halt would read it as a live owner and leave
        # ``printing`` with a pause Resume does not clear).
        if execution_item_id is None:
            header = await db.scalar(
                select(PrinterQueue)
                .where(PrinterQueue.id == queue_id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
            printing = await db.scalar(
                select(PrintQueueItem.id)
                .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "printing")
                .limit(1)
            )
            stale = header is not None and header.status == "printing" and printing is None
            await halt_lane(
                db,
                queue_id,
                item_id=None,
                kind="paused",
                stale_only=stale,
                read_item_id=header.current_item_id if stale else None,
            )
        else:
            await halt_lane(db, queue_id, item_id=execution_item_id, kind="paused")
        if execution_item_id is not None:
            ran = await db.get(PrintQueueItem, execution_item_id)
            if ran is not None:
                ran.waiting_reason = text
        waiting = await db.scalar(
            select(PrintQueueItem)
            .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "pending")
            .order_by(PrintQueueItem.position)
            .limit(1)
        )
        if waiting is not None:
            waiting.waiting_reason = text
        await db.commit()
    return text


async def _auto_clean_completed_item(db, queue_item, *, queue_status: str, plate_auto_cleared: bool) -> bool:
    """Thin wrapper — the rule itself lives in ``services.plate_hold``.

    ⚠️ Shared rather than inlined here because ``print_reconciliation`` finishes
    rows too, and when it does this step has to happen there as well. It did not,
    which is how a swap printer ended up with a completed row nobody deleted.
    """
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.plate_hold import clean_up_finished_row
    from backend.app.services.queue_ops import claim_lanes

    if queue_status != "completed" or queue_item.archive_id is None:
        return False
    # G03 B, A21: after the terminal commit, in a fresh session under the row's lane and its archive's guard -- the
    # row read again by id and incarnation and deleted only while it is still this run's finished row.
    item_id, identity = queue_item.id, queue_item.recovery_identity
    queue_id, archive_id = queue_item.queue_id, queue_item.archive_id
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await db.commit()  # the completion's transaction ends: no writer of ours waits for itself
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with factory() as clean, claim_lanes(clean, [queue_id], archive_id=archive_id):
        row = await clean.scalar(
            select(PrintQueueItem)
            .where(PrintQueueItem.id == item_id, PrintQueueItem.queue_id == queue_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.recovery_identity != identity
            or row.status != "completed"
            or (row.queue_id, row.archive_id) != (queue_id, archive_id)
        ):
            return False
        return await clean_up_finished_row(clean, row, queue_status=queue_status, plate_auto_cleared=plate_auto_cleared)


async def _credit_free_stock(archive_id: int | None) -> None:
    """Put a just-finished order-less print onto the product's shelf.

    Module-level, like ``_record_print_energy``, so the completion hook can be
    tested without driving the whole of ``on_print_complete``.

    ⚠️ **This can never fail a print.** Everything the ledger might object to —
    a product whose parts changed under it, a balance that will not go where it
    is asked, a database that is briefly locked — is caught and logged, here for
    the session and inside ``part_stock.credit_if_unfiled`` for the ledger
    itself. The print happened; the bookkeeping is a consequence of it, never a
    condition on it. The credit is itself idempotent and silent for a print this
    does not apply to (filed under an order, not completed, a plate no product
    claims), so this is one call and no preconditions.

    ⚠️ It is NOT the only door any more: an external print reaches ``completed``
    before its 3MF arrives, so ``attach_3mf_to_archive`` runs the same credit
    once the part rows exist (Ruling 27). Both go through
    ``credit_if_unfiled``, and the per-part idempotency is what makes running it
    twice a no-op rather than a double credit.
    """
    logger = logging.getLogger(__name__)
    if not archive_id:
        return
    try:
        from backend.app.services import part_stock
        from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

        # The archive held before the parts (G03 B, A29): its write scope and row, in a session of its own after the
        # terminal commit -- the guard is not re-entrant, and this hook runs outside every archive scope of the
        # completion.
        async with async_session() as db, archive_write_scope(db, archive_id):
            archive = await load_active_archive_for_write(db, archive_id)
            if archive is None:
                return
            written = await part_stock.credit_if_unfiled(db, archive)
            if written:
                await db.commit()
                logger.info(
                    "[STOCK] archive %s had no order: credited %d part(s) to free stock", archive_id, len(written)
                )
    except Exception as e:
        logger.warning("[STOCK] free-stock credit failed for archive %s: %s", archive_id, e, exc_info=True)


async def _on_print_complete_impl(
    printer_id: int,
    data: dict,
    analysis_completion: dict,
) -> None:
    """Complete one print; the public wrapper owns final analysis release."""
    import time

    from backend.app.services import queue_sources

    completion_epoch = queue_sources.current_epoch()

    logger = logging.getLogger(__name__)
    start_time = time.time()

    def log_timing(section: str):
        elapsed = time.time() - start_time
        logger.info("[TIMING] %s: %.3fs elapsed", section, elapsed)

    logger.info("[CALLBACK] on_print_complete started for printer %s", printer_id)
    # The swap decision reads the terminal's status as the printer sent it (swap-source-lifecycle spec r4 §10): a
    # missing status is a completion, an empty or unknown one is not -- whatever run acceptance below makes of it.
    _raw_terminal_status = data.get("status", "completed")

    # A direct execution-archive binding wins over filename aliases.  This is
    # captured at the common authoritative archive lifecycle point, so an
    # ID-only terminal event can finish its own run.  A positive contradictory
    # device ID is still foreign even when an old binding happens to exist.
    from backend.app.services.print_run_binding import (
        current_print_run,
        defer_matching_terminal_during_start,
        finishing_print_runs,
    )

    bound_run = current_print_run(printer_manager, printer_id)
    from backend.app.services.print_run_binding import (
        accept_unbound_completion,
        print_effect_is_current,
        retire_print_start,
        snapshot_completion_token,
        unbound_terminal_matches_start,
    )

    effect_token = snapshot_completion_token(printer_manager, printer_id)
    selected_finish_macros = _active_macro_selection.get(printer_id)
    selected_finish_macros = tuple(selected_finish_macros) if selected_finish_macros is not None else None
    from backend.app.services.layer_timelapse import get_session, take_session

    observed_layer_session = get_session(printer_id)
    event_client_generation = data.get("_bamdude_client_generation")
    accepted_client_generation = printer_manager.current_client_generation(printer_id)
    if not printer_manager.accepts_client_callback_generation(printer_id, event_client_generation):
        logger.warning(
            "Ignoring terminal event from stale MQTT client on printer %s: event_generation=%s accepted_generation=%s",
            printer_id,
            event_client_generation,
            accepted_client_generation,
        )
        return
    if not printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life")):
        logger.warning("Ignoring terminal event of a deleted printer record %s", printer_id)
        return
    if bound_run is not None and not bound_run.matches_device_subtask(data.get("subtask_id")):
        logger.warning(
            "Ignoring foreign terminal event on printer %s: bound archive=%s sequence=%s device_subtask=%r",
            printer_id,
            bound_run.archive_id,
            bound_run.sequence,
            data.get("subtask_id"),
        )
        return
    if bound_run is None:
        if not unbound_terminal_matches_start(printer_manager, printer_id, data):
            logger.warning("Ignoring foreign terminal for unarchived live start on printer %s", printer_id)
            return
        finishing = tuple(
            run
            for run in finishing_print_runs(printer_manager, printer_id)
            if run.effect_generation == effect_token.generation
        )
        # Once A has been accepted it is removed from ``current`` before its
        # long effects. A duplicate terminal must not fall back into legacy
        # name matching and run those effects again. A new B would have its
        # own current binding, so an unbound event here is safely duplicate or
        # unresolved; both preserve the printer state.
        if finishing:
            if any(run.matches_device_subtask(data.get("subtask_id")) for run in finishing):
                logger.info(
                    "Ignoring duplicate terminal while archive(s) %s are finishing on printer %s",
                    [run.archive_id for run in finishing],
                    printer_id,
                )
            else:
                logger.warning(
                    "Ignoring terminal with conflicting identity while archive(s) %s are finishing on printer %s",
                    [run.archive_id for run in finishing],
                    printer_id,
                )
            return
        if not data.get("_bamdude_pending_terminal_timeout"):
            pending = defer_matching_terminal_during_start(printer_manager, printer_id, data)
            if pending is not None:
                logger.info(
                    "Buffering terminal for printer %s until matching start sequence %s binds an archive",
                    printer_id,
                    pending.sequence,
                )
                spawn_background_task(
                    _replay_expired_pending_terminal(printer_id, pending.sequence),
                    name=f"pending-terminal-timeout-{printer_id}-{pending.sequence}",
                )
                return
    # Legacy/adopted events that have not acquired a binding retain the
    # conservative old guard and its name-based recovery behavior.
    if bound_run is None and await _completion_conflicts_with_active_queue(printer_id, data):
        return

    legacy_archive_id = None
    terminal_archive_identity = bound_run.archive_identity if bound_run is not None else None
    if bound_run is None:
        # Resolve once, before effects. Numeric filename aliases cannot select a
        # reused archive or revive an execution already marked noncurrent.
        resolved = await _live_archive_for_running_print(printer_id, data)
        if resolved.reason.startswith("ambiguous"):
            return
        if resolved.archive_id is not None:
            from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

            async with async_session() as proof_db, archive_write_scope(proof_db, resolved.archive_id):
                proved = await load_active_archive_for_write(proof_db, resolved.archive_id)
                if (
                    proved is None
                    or proved.printer_id != printer_id
                    or proved.status != "printing"
                    or "noncurrent_execution" in (proved.extra_data or {})
                ):
                    return
                legacy_archive_id, terminal_archive_identity = proved.id, proved.recovery_identity
        if not accept_unbound_completion(printer_manager, effect_token):
            return

    accepted_queue_item_id: int | None = None
    archive_id = bound_run.archive_id if bound_run is not None else legacy_archive_id
    analysis_completion["archive_identity"] = terminal_archive_identity

    if bound_run is None:
        retire_print_start(printer_manager, printer_id, None)

    def _spawn_finish_task(coro, *, name):
        task = spawn_background_task(coro, name=name)
        scope = analysis_completion.get("tasks")
        if scope is not None:
            scope.add(task)
        return task

    def _owns_printer_effect() -> bool:
        """Re-check terminal ownership immediately before a printer-wide action."""

        return queue_sources.current_epoch() == completion_epoch and print_effect_is_current(
            printer_manager, effect_token
        )

    if archive_id is not None and bound_run is not None:
        # Resolve/freeze the exact durable queue row before WS, user cleanup,
        # relay or macros. A missing row never authorizes another row's finish.
        data = _normalise_user_stopped_terminal(printer_id, archive_id, data)
        terminal_outcome = _normalise_terminal_outcome(data)
        if terminal_outcome is None:
            logger.warning(
                "Ignoring bound terminal with unknown outcome for archive %s on printer %s: %r",
                archive_id,
                printer_id,
                data.get("status"),
            )
            return
        # The queue has never represented ``aborted``; all consumers of an
        # addressed run must see the same normalized terminal outcome as the
        # atomic archive/item commit below.  Keeping the raw MQTT spelling for
        # the later archive service would silently split that pair again.
        data = {**data, "status": terminal_outcome}
        accepted_queue_item_id = await _accept_bound_terminal_run(printer_id, bound_run, data)
        if accepted_queue_item_id is None:
            return
        # Keep terminal deduplication after the last consumer releases the
        # binding. A replay must not enter the now-unbound legacy path.
        accept_unbound_completion(printer_manager, effect_token)
        analysis_completion["archive_id"] = archive_id
        analysis_completion["run_sequence"] = bound_run.sequence
        from backend.app.services.print_file_analysis import begin_print_file_analysis_finishing

        begin_print_file_analysis_finishing(printer_manager, printer_id, archive_id)

    layer_session = take_session(printer_id, observed_layer_session) if _owns_printer_effect() else None

    if _owns_printer_effect():
        try:
            ws_data = {
                "status": data.get("status"),
                "filename": data.get("filename"),
                "subtask_name": data.get("subtask_name"),
                "timelapse_was_active": data.get("timelapse_was_active"),
            }
            await ws_manager.send_print_complete(printer_id, ws_data)
            log_timing("WebSocket send_print_complete")
        except Exception as e:
            logger.warning("[CALLBACK] WebSocket send_print_complete failed: %s", e)
    else:
        logger.info(
            "Skipping printer-wide completion event for finishing archive %s; a newer run owns printer %s",
            archive_id,
            printer_id,
        )

    # Capture user info before clearing (needed for print log entry).  A known
    # bound A must never read/clear B's attribution after a fast next start.
    _print_user_info = printer_manager.get_current_print_user(printer_id, archive_id=archive_id)

    # Clear current print user tracking (Issue #206)
    if _owns_printer_effect():
        printer_manager.clear_current_print_user(printer_id, archive_id=archive_id)

    # MQTT relay - publish print complete
    if _owns_printer_effect():
        try:
            printer_info = printer_manager.get_printer(printer_id)
            if printer_info:
                await mqtt_relay.on_print_complete(
                    printer_id,
                    printer_info.name,
                    printer_info.serial_number,
                    data.get("filename", ""),
                    data.get("subtask_name", ""),
                    data.get("status", "completed"),
                )
        except Exception:
            pass  # Don't fail print complete callback if MQTT fails

    filename = data.get("filename", "")
    subtask_name = data.get("subtask_name", "")

    if archive_id is None and not filename and not subtask_name:
        logger.warning("Print complete without filename or subtask_name")
        return

    logger.info("Print complete - filename: %s, subtask: %s, status: %s", filename, subtask_name, data.get("status"))

    # Fire any user-defined ``print_finished`` macros (e.g. turn chamber
    # light back on after print). Fire-and-forget so macro delay_seconds
    # doesn't stall completion. Mirrors the ``print_started`` path.
    if _owns_printer_effect():
        try:
            from backend.app.services.macro_trigger import fire_event_macros

            await _record_terminal_effect_stage(
                archive_id, "finish_macros", "attempted", expected_identity=terminal_archive_identity
            )
            await fire_event_macros(
                "print_finished",
                printer_id,
                async_session,
                printer_manager,
                may_run=_owns_printer_effect,
                archive_id=archive_id,
                selected_macro_ids=selected_finish_macros,
            )
            await _record_terminal_effect_stage(
                archive_id, "finish_macros", "scheduled", expected_identity=terminal_archive_identity
            )
        except Exception as e:
            await _record_terminal_effect_stage(
                archive_id, "finish_macros", "failed", expected_identity=terminal_archive_identity
            )
            logger.warning("print_finished macros failed to schedule: %s", e)

    if archive_id is not None and _owns_printer_effect():
        _forget_active_print(archive_id, terminal_archive_identity)

    # Move the run before any of the lengthy completion side effects below.
    # Otherwise a rapid next start could replace its current context while this
    # callback is still changing tables, updating queue rows or sending notices.
    # A stale/duplicate completion deliberately gets no new context from this
    # printer-id-only path.
    if archive_id:
        from backend.app.services.print_file_analysis import begin_print_file_analysis_finishing

        # Publish the archive identity before any later awaited side effect.
        # The public wrapper releases this exact finishing context in ``finally``
        # if an unexpected exception aborts the remainder of this long callback.
        analysis_completion["archive_id"] = archive_id
        begin_print_file_analysis_finishing(printer_manager, printer_id, archive_id)

    # Local flag — set True by any swap path (swap_compatible archive or
    # runtime change_table macro) that physically clears the plate. Used at
    # the end of on_print_complete to decide whether to arm the
    # awaiting_plate_clear gate (#961 inversion).
    _plate_auto_cleared_by_swap = False

    # Both swap paths below are gated on a successful completion. A print
    # that ended with status=failed / aborted / cancelled has left material
    # on the bed (the operator cancelled at hour 11, the printer self-
    # aborted on a clog, the touchscreen-stop fired mid-print, etc.) — the
    # bed is fouled regardless of whether the 3MF was swap_compatible or
    # the queue-job had swap_mode_change_table queued. In that state we
    # MUST NOT (a) auto-clear the gate (the operator has to inspect and
    # clear manually) and MUST NOT (b) physically swap the table via the
    # change_table macro (it'll either jam on the still-attached part or
    # rotate the fouled plate into the next print's path, depending on
    # the swap rig). Falling into the regular arm-gate block at the end
    # of this function — and skipping the change_table execution — is the
    # safe outcome.
    _swap_status_ok = _raw_terminal_status == "completed" and data.get("status", "completed") == "completed"

    # Last-chance 3MF download: if this print has a fallback archive
    # (file_path="") and the print just finished, the file is still on
    # SD right now and the printer is no longer busy writing to it — a
    # good moment to try one more download before the cleanup step
    # deletes or relocates it.
    if archive_id:
        try:
            from backend.app.services.archive_download_retry import archive_download_retry

            lc_status = await archive_download_retry.retry_archive(archive_id)
            if lc_status == "recovered":
                logger.info("[LAST-CHANCE] Recovered 3MF for archive %s", archive_id)
            elif lc_status == "already_has_file":
                logger.info("[LAST-CHANCE] Archive %s already has file — skipping", archive_id)
            elif lc_status == "in_progress":
                logger.info(
                    "[LAST-CHANCE] Archive %s — another retry already in progress, letting it finish",
                    archive_id,
                )
            else:
                logger.info(
                    "[LAST-CHANCE] Archive %s still without 3MF (status=%s); SD cleanup will run next. "
                    "Manual retry available via POST /archives/%s/retry-download.",
                    archive_id,
                    lc_status,
                    archive_id,
                )
        except Exception as e:
            logger.warning("[LAST-CHANCE] Download attempt failed non-fatally: %s", e)

    # Post-print housekeeping on the printer — the file we sent, and the copies
    # the printer derived from it, on the card and in internal storage.
    #
    # ⚠️ **Scheduled, not awaited.** Nothing downstream needs it: the archive is
    # already written, the last-chance 3MF download has already run, and the
    # queue, the notifications and the timelapse scan care about none of it. Run
    # inline it once held a completed print for **222 seconds** on a machine
    # whose FTP daemon had wedged — 30 s per listing, three retries per
    # candidate — and the operator's "print finished" message arrived nearly
    # four minutes late. Housekeeping must never be what makes a print look
    # unfinished.
    #
    # ⚠️ Bounded as well as detached, so a printer that never answers cannot
    # leave this running into the NEXT print on the same machine, where the
    # names it deletes by would belong to that job instead.
    cleanup_deadline: list[float | None] = [None]

    def _cleanup_may_run() -> bool:
        """This run still owns the printer AND the cleanup's budget is not spent."""
        deadline = cleanup_deadline[0]
        return _owns_printer_effect() and (deadline is None or time.monotonic() < deadline)

    def _cleanup_gate(path: str):
        """``may_run`` of one destructive step: ``_cleanup_may_run`` plus the file guard.

        The guard is what keeps the file a NEWER job prints from — one this
        process did not recognise as a new run, started from the slicer or the
        printer's own screen under the same name with the same bytes (G10 B).
        """
        from backend.app.services import printer_file_guard

        return printer_file_guard.may_delete(printer_id, path, also=_cleanup_may_run)

    async def _post_print_printer_cleanup() -> None:
        if not _owns_printer_effect():
            logger.info(
                "Skipping post-print cleanup for archive %s: a newer run owns printer %s",
                archive_id,
                printer_id,
            )
            return
        await _record_terminal_effect_stage(
            archive_id, "printer_cleanup", "attempted", expected_identity=terminal_archive_identity
        )
        if not _owns_printer_effect():
            await _record_terminal_effect_stage(
                archive_id, "printer_cleanup", "skipped", expected_identity=terminal_archive_identity
            )
            logger.info(
                "Skipping post-print cleanup for archive %s: printer %s has a newer run", archive_id, printer_id
            )
            return
        try:
            # ⚠️ A cooperative deadline, not ``asyncio.wait_for`` (G10 B, OD-8). A
            # timeout that cancels the coroutine walks away from a transfer still
            # running on the printer's file channel and reports the cleanup over;
            # here the step in flight ends by itself, owned to its end, and
            # ``_cleanup_may_run`` starts no further delete, patch or move.
            cleanup_deadline[0] = time.monotonic() + _POST_PRINT_CLEANUP_BUDGET
            cleanup_result = await _cleanup_printer_after_print()
            out_of_time = time.monotonic() >= cleanup_deadline[0]
            await _record_terminal_effect_stage(
                archive_id,
                "printer_cleanup",
                "uncertain" if out_of_time else "skipped" if cleanup_result is False else "finished",
                expected_identity=terminal_archive_identity,
            )
            if out_of_time:
                logger.warning(
                    "Post-print cleanup on printer %s ran out of its %ss — files may linger",
                    printer_id,
                    _POST_PRINT_CLEANUP_BUDGET,
                )
        except Exception:
            await _record_terminal_effect_stage(
                archive_id, "printer_cleanup", "failed", expected_identity=terminal_archive_identity
            )
            raise

    async def _cleanup_printer_after_print() -> None:
        try:
            if subtask_name:
                archive_filename: str | None = None
                uploaded_filename: str | None = None
                from types import SimpleNamespace

                from backend.app.services.printer_cleanup import archive_hashes
                from backend.app.utils.filename import recorded_upload_filename

                async with async_session() as db:
                    from backend.app.models.archive import PrintArchive
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()
                    # ⚠️ The digests come out in the SAME read as the filename.
                    # They are what proves the printer's own derived copies are
                    # ours before any of them is deleted — see services/
                    # printer_cleanup.py. Columns named individually rather than
                    # selecting the entity, so this keeps working through later
                    # migrations (the seed-column discipline).
                    expected_hashes: set[str] = set()
                    if archive_id:
                        archive_row = await db.execute(
                            select(
                                PrintArchive.filename,
                                PrintArchive.source_content_hash,
                                PrintArchive.content_hash,
                                PrintArchive.extra_data,
                            ).where(PrintArchive.id == archive_id)
                        )
                        row = archive_row.first()
                        if row is not None:
                            archive_filename = row[0]
                            uploaded_filename = recorded_upload_filename(row[3])
                            expected_hashes = archive_hashes(
                                SimpleNamespace(source_content_hash=row[1], content_hash=row[2])
                            )

                if printer:
                    # The DB read above may have yielded long enough for a
                    # direct/external B to begin.  Do not open FTP against a
                    # printer it now owns merely to clean A's old files.
                    if not _cleanup_may_run():
                        logger.info(
                            "Skipping post-print cleanup for archive %s after lookup: printer %s has a newer run",
                            archive_id,
                            printer_id,
                        )
                        return False
                    from backend.app.services.bambu_ftp import (
                        DeleteResult,
                        delete_file_async,
                        download_file_bytes_async,
                        list_files_async,
                        rename_file_async,
                        upload_bytes_async,
                    )
                    from backend.app.services.printer_cleanup import (
                        derived_copy_names,
                        remove_verified_copies,
                    )
                    from backend.app.utils.filename import derive_remote_filename

                    should_delete = getattr(printer, "cleanup_after_print", True)

                    # ⚠️ Where the file actually is decides how (and whether) to
                    # clean it up. Everything below this point — the /cache sweep,
                    # the .bbl patch, the delete, the move — is FTP on the card.
                    # A print that went to internal storage has none of that: no
                    # /cache directory, no rename in the protocol, and the file
                    # lives at a path only the tunnel's own listing knows.
                    from backend.app.services.printer_files.factory import transport_for
                    from backend.app.utils.printer_storage import INTERNAL, storage_capability_for

                    printed_to_internal = (
                        storage_capability_for(printer.model, printer_manager.get_status(printer_id))["print_target"]
                        == INTERNAL
                    )

                    # .3mf candidate paths, primary first: the exact path the
                    # dispatcher uploaded to (recorded in its intent). For a
                    # legacy archive, derive it from archive.filename. A row with
                    # with a doubled .gcode.3mf (#1542) leaves the real file behind
                    # because the subtask_name fallback doesn't match what's on the
                    # SD card. The subtask_name fallback stays for archive-less
                    # prints and older naming variants.
                    threemf_candidates: list[str] = []
                    if uploaded_filename:
                        threemf_candidates.append(f"/{uploaded_filename}")
                    elif archive_filename:
                        threemf_candidates.append(f"/{derive_remote_filename(archive_filename)}")
                    subtask_3mf = f"/{subtask_name}.3mf"
                    if subtask_3mf not in threemf_candidates:
                        threemf_candidates.append(subtask_3mf)

                    # What the printer makes for itself out of each of those — one
                    # upload, several files on the machine. Names only; whether any
                    # of them is actually ours is decided by their bytes.
                    derived_copy_names_wanted: set[str] = set()
                    for candidate in threemf_candidates:
                        derived_copy_names_wanted.update(derived_copy_names(candidate))

                    # ⚠️ **The card only.** /cache is a directory on the SD card:
                    # the printer's own working copy of the job plus the .bbl
                    # that records the print's parameters. A machine printing
                    # from internal storage has no such directory, so every line
                    # below is FTP work whose best outcome is finding nothing —
                    # and on a machine with no card in it, that is a 30-second
                    # timeout before the sweep can get to the medium that does
                    # hold something.
                    #
                    # The move-instead-of-delete branch further down is skipped
                    # for the same reason and already says so: keeping a file on
                    # internal storage means leaving it where it is, because
                    # nothing auto-starts from there.
                    if not printed_to_internal:
                        # Process /cache/ files: delete .gcode, patch .bbl (disable auto_recovery)
                        try:
                            if not _cleanup_may_run():
                                return False
                            cache_files = await list_files_async(
                                printer.ip_address,
                                printer.access_code,
                                "/cache",
                                printer_model=printer.model,
                            )
                            sanitized_base = subtask_name.replace(" ", "_")
                            for f in cache_files:
                                if not _cleanup_may_run():
                                    return False
                                fname = f.get("name", "")
                                # ⚠️ ``is_directory``, not ``is_dir`` — the key the FTP
                                # listing actually emits (bambu_ftp.list_files, pinned by
                                # test_bambu_ftp). This guard read the wrong name and so
                                # never fired: a directory in /cache went on to be passed
                                # to DELE, which cannot delete one.
                                if f.get("is_directory"):
                                    continue
                                fname_lower = fname.lower()

                                # Match .gcode and .bbl by base name
                                is_matching_gcode = fname_lower.endswith(".gcode")
                                is_matching_bbl = fname_lower.endswith(".bbl") and f"_{sanitized_base}" in fname

                                # cleanup_after_print=True: delete .gcode and .bbl from /cache/
                                if (
                                    should_delete
                                    and (is_matching_gcode or is_matching_bbl)
                                    or not should_delete
                                    and is_matching_gcode
                                ):
                                    try:
                                        cache_result = await delete_file_async(
                                            printer.ip_address,
                                            printer.access_code,
                                            f"/cache/{fname}",
                                            printer_model=printer.model,
                                            may_run=_cleanup_gate(f"/cache/{fname}"),
                                        )
                                        # The answer is read (G10 B): a kept or failed delete is not "Deleted".
                                        if cache_result == DeleteResult.DELETED:
                                            logger.info("Deleted /cache/%s from printer %s", fname, printer.name)
                                        else:
                                            logger.debug(
                                                "/cache/%s stays on printer %s (%s)",
                                                fname,
                                                printer.name,
                                                cache_result.value,
                                            )
                                    except Exception:
                                        pass

                                # Patch .bbl: set auto_recovery=false, fix file path if 3MF moved to /cache/
                                # .bbl name pattern: {plate_id}_{base_name}.bbl or {plate_id}_{base_name}_{model}.bbl
                                elif not should_delete and is_matching_bbl:
                                    try:
                                        bbl_path = f"/cache/{fname}"
                                        bbl_data = await download_file_bytes_async(
                                            printer.ip_address,
                                            printer.access_code,
                                            bbl_path,
                                            printer_model=printer.model,
                                        )
                                        if bbl_data:
                                            import json as _json

                                            bbl_text = bbl_data.decode("utf-8", errors="replace")
                                            bbl_json = _json.loads(bbl_text)
                                            modified = False

                                            # Disable auto_recovery
                                            if bbl_json.get("auto_recovery") is not False:
                                                bbl_json["auto_recovery"] = False
                                                modified = True

                                            # Fix file path if 3MF was moved to /cache/
                                            file_path_val = bbl_json.get("file path", "")
                                            if not should_delete and "/cache/" not in file_path_val and file_path_val:
                                                # e.g. "/sdcard/Voron.3mf" → "/sdcard/cache/Voron.3mf"
                                                bbl_json["file path"] = file_path_val.replace(
                                                    "/sdcard/", "/sdcard/cache/", 1
                                                )
                                                modified = True

                                            if modified:
                                                # Preserve original formatting (4-space indent)
                                                patched = _json.dumps(bbl_json, indent=4).encode("utf-8")
                                                bbl_written = await upload_bytes_async(
                                                    printer.ip_address,
                                                    printer.access_code,
                                                    patched,
                                                    bbl_path,
                                                    printer_model=printer.model,
                                                    may_run=_cleanup_gate(bbl_path),
                                                )
                                                if not bbl_written:
                                                    # Kept by the gate or refused by the printer: not "Patched".
                                                    logger.debug(
                                                        "/cache/%s on printer %s was not patched", fname, printer.name
                                                    )
                                                    continue
                                                logger.info(
                                                    "Patched .bbl /cache/%s on printer %s (auto_recovery=false%s)",
                                                    fname,
                                                    printer.name,
                                                    ", file path updated"
                                                    if "/cache/" in bbl_json.get("file path", "")
                                                    else "",
                                                )
                                    except Exception as e:
                                        logger.debug("Failed to patch .bbl /cache/%s: %s", fname, e)

                            # The copy the PRINTER made. We upload ``Cube.3mf`` and it
                            # writes ``Cube.gcode.3mf`` into /cache — a `.3mf`, so the
                            # `.gcode` sweep above never matched it and cleanup honestly
                            # reported "nothing to delete" while the file sat there.
                            #
                            # ⚠️ Proven by content wherever there is a digest to prove
                            # it with — the same name is what a print sent from
                            # BambuStudio leaves behind. The exception is a print we
                            # only picked up (slicer or printer screen) whose 3MF was
                            # never recovered: no digest exists, and the name is bound
                            # to the job that has just finished on this machine.
                            if should_delete and derived_copy_names_wanted:
                                await remove_verified_copies(
                                    entries=[
                                        {
                                            "name": f.get("name", ""),
                                            "path": f"/cache/{f.get('name', '')}",
                                            "is_directory": f.get("is_directory"),
                                        }
                                        for f in cache_files
                                    ],
                                    # Filtered at call time, not list-build time: a name the
                                    # NEXT dispatch has since registered as its expected print
                                    # is that job's working copy, not this job's leftover.
                                    wanted={
                                        n
                                        for n in derived_copy_names_wanted
                                        if not is_expected_print_file(printer_id, n)
                                    },
                                    expected_hashes=expected_hashes,
                                    read_bytes=lambda p: download_file_bytes_async(
                                        printer.ip_address, printer.access_code, p, printer_model=printer.model
                                    ),
                                    delete=lambda p: delete_file_async(
                                        printer.ip_address,
                                        printer.access_code,
                                        p,
                                        printer_model=printer.model,
                                        may_run=_cleanup_gate(p),
                                    ),
                                    label=f"{printer.name} /cache",
                                    allow_unverified=archive_id is not None,
                                    may_run=_cleanup_may_run,
                                )
                        except Exception:
                            pass  # best-effort

                    # Handle .3mf - delete or move to /cache/. Try each candidate
                    # (derived-first, subtask fallback) until one hits a real file.
                    if printed_to_internal:
                        # ⚠️ Only the delete half exists here, and that is not an
                        # omission. "Keep the file" on the card means moving it OUT
                        # of the root, because printers auto-start whatever sits
                        # there on a power cycle (#374, ghost prints). Internal
                        # storage is the printer's own history folder — nothing
                        # auto-starts from it — so keeping the file means leaving it
                        # exactly where it is. There is no /cache to move it to and
                        # no rename in the tunnel protocol at all.
                        if should_delete:
                            names = [p.split("/")[-1] for p in threemf_candidates]
                            transport = transport_for(printer, INTERNAL)
                            removed = await delete_internal_by_name(
                                transport, *names, may_run=_cleanup_may_run, gate=_cleanup_gate
                            )
                            logger.info(
                                "Internal-storage cleanup on %s: %s",
                                printer.name,
                                "deleted" if removed else "nothing matched",
                            )
                        else:
                            logger.debug(
                                "Internal-storage print on %s: file left in place (nothing auto-starts from it)",
                                printer.name,
                            )
                    elif should_delete:
                        # Track outcomes across all candidates so the final log line
                        # reflects what actually happened. A1 / A1 Mini firmware
                        # auto-cleans the SD card before our cleanup runs, so every
                        # candidate FTP-DELE returns 550 (NOT_FOUND) — the old code
                        # burned 3 retries × 2 s per candidate and then WARNed on a
                        # perfectly successful print (#1721). NOT_FOUND advances to
                        # the next candidate immediately without a warning; only a
                        # real network/auth/transient FAILED still warrants WARNING.
                        any_deleted = False
                        any_real_failure = False
                        any_not_found = False
                        for remote_3mf in threemf_candidates:
                            if is_expected_print_file(printer_id, remote_3mf):
                                logger.info(
                                    "SD cleanup: leaving %s in place — a dispatch in flight has "
                                    "registered it as the next print's file",
                                    remote_3mf,
                                )
                                continue
                            if not _cleanup_gate(remote_3mf)():
                                # Kept on purpose (a newer job reads it, or the printer's state is
                                # not known) — not a failed delete to retry and warn about.
                                continue
                            deleted = False
                            for attempt in range(1, 4):
                                if not _cleanup_may_run():
                                    return False
                                try:
                                    r = await delete_file_async(
                                        printer.ip_address,
                                        printer.access_code,
                                        remote_3mf,
                                        printer_model=printer.model,
                                        may_run=_cleanup_gate(remote_3mf),
                                    )
                                except Exception as e:
                                    r = DeleteResult.FAILED
                                    logger.warning("SD cleanup attempt %d/3 for %s: %s", attempt, remote_3mf, e)

                                if r == DeleteResult.DELETED:
                                    any_deleted = True
                                    logger.info("Deleted %s from printer %s SD card", remote_3mf, printer.name)
                                    deleted = True
                                    break
                                if r == DeleteResult.NOT_FOUND:
                                    any_not_found = True
                                    break  # 550 will not recover; try next candidate
                                # FAILED: real error — retry with backoff, then give up
                                if attempt < 3:
                                    await asyncio.sleep(2)
                                else:
                                    any_real_failure = True
                                    logger.warning(
                                        "SD cleanup failed after 3 attempts for %s "
                                        "(network/auth/transient error — file may linger on SD card)",
                                        remote_3mf,
                                    )
                            if deleted:
                                break  # real file found + removed; don't probe fallbacks

                        if not any_deleted and not any_real_failure and any_not_found:
                            # Every candidate said "not here." The printer firmware
                            # swept the SD card itself (common on A1) or nothing was
                            # written — nothing to clean up, no warning.
                            logger.debug(
                                "SD card cleanup: nothing to delete on %s — every candidate returned 550 "
                                "(printer likely self-cleaned)",
                                printer.name,
                            )
                    else:
                        # Move .3mf to /cache/
                        for remote_3mf in threemf_candidates:
                            if is_expected_print_file(printer_id, remote_3mf):
                                logger.info(
                                    "SD cleanup: not moving %s — a dispatch in flight has "
                                    "registered it as the next print's file",
                                    remote_3mf,
                                )
                                continue
                            if not _cleanup_gate(remote_3mf)():
                                continue
                            cache_3mf = f"/cache/{remote_3mf.lstrip('/')}"
                            moved = False
                            for attempt in range(1, 4):
                                if not _cleanup_may_run():
                                    return False
                                try:
                                    r = await rename_file_async(
                                        printer.ip_address,
                                        printer.access_code,
                                        remote_3mf,
                                        cache_3mf,
                                        printer_model=printer.model,
                                        may_run=_cleanup_gate(remote_3mf),
                                    )
                                    if r:
                                        logger.info("Moved %s to %s on printer %s", remote_3mf, cache_3mf, printer.name)
                                        moved = True
                                        break
                                    if r is None:
                                        break  # file not found, try next candidate
                                except Exception as e:
                                    logger.debug("SD move attempt %d/3 for %s: %s", attempt, remote_3mf, e)
                                if attempt < 3:
                                    await asyncio.sleep(2)
                            if moved:
                                break

                    # The printer's own copy in INTERNAL storage — and this runs
                    # whichever medium the print was read from.
                    #
                    # ⚠️ **Not part of the branch above, deliberately.** That one
                    # asks "where did this print come from"; this one asks "where
                    # did the printer keep a copy". With "store sent files to
                    # storage" on (``cfg`` bit 19 — the operator's setting, not our
                    # doing) a job read off the CARD still leaves ``X.gcode.3mf``
                    # in internal storage. Cleaning only the medium we printed from
                    # left the other one filling up with no sign of it anywhere.
                    #
                    # Not an attempt to stop the copies appearing: that is what the
                    # operator asked the printer to do. This is tidying up after
                    # ourselves once the print is over.
                    if should_delete and derived_copy_names_wanted:
                        capability = storage_capability_for(printer.model, printer_manager.get_status(printer_id))
                        if capability["can_browse_internal"] and _cleanup_may_run():
                            try:
                                internal = transport_for(printer, INTERNAL)
                                await remove_verified_copies(
                                    entries=[e.as_dict() for e in await internal.list_files("/")],
                                    # Filtered at call time, not list-build time: a name the
                                    # NEXT dispatch has since registered as its expected print
                                    # is that job's working copy, not this job's leftover.
                                    wanted={
                                        n
                                        for n in derived_copy_names_wanted
                                        if not is_expected_print_file(printer_id, n)
                                    },
                                    expected_hashes=expected_hashes,
                                    read_bytes=internal.read_bytes,
                                    delete=lambda p: internal.delete(p, may_run=_cleanup_gate(p)),
                                    label=f"{printer.name} internal",
                                    allow_unverified=archive_id is not None,
                                    may_run=_cleanup_may_run,
                                )
                            except Exception as exc:  # noqa: BLE001 — best-effort, like every branch here
                                logger.debug("Internal-storage copy cleanup on %s failed: %s", printer.name, exc)

        except Exception as e:
            logger.warning("SD card file cleanup failed for printer %s: %s", printer_id, e)

    _spawn_finish_task(_post_print_printer_cleanup(), name=f"post-print-cleanup-{printer_id}")

    log_timing("SD card cleanup")

    # Swap-mode change-table macro — runs after print completes (before queue
    # picks up the next item). If this fails the queue pauses so a half-swapped
    # table doesn't cause the next print to crash.
    #
    # Source of intent (in order):
    # 1. ``_active_swap_config[printer_id]`` (fast path, in-memory)
    # 2. ``archive.extra_data["swap_macro_events_pending"]`` — restart-recovery.
    #    Persisted by ``register_swap_config`` at dispatch time; surviving
    #    even when the backend was restarted between print-start and
    #    print-complete.
    #
    # After the macro fires (success OR fail) we clear
    # ``swap_macro_events_pending`` from extra_data so a re-trigger of this
    # ``on_print_complete`` (MQTT replay, reconnect flap) doesn't re-fire the
    # macro a second time. The in-memory ``_active_swap_config.pop`` already
    # provided this idempotency for the fast path; the persisted marker
    # needs the same protection.
    from backend.app.services.macro_trigger import clear_fired_layer_macros

    if _owns_printer_effect():
        clear_fired_layer_macros(printer_id)
    # Only here, and only this late. The layer-fired guard above also resets on
    # ``progress < 5``, but the selection must not: that point is the START of a
    # print, and clearing there would throw away what dispatch just registered.
    # This also has to sit below ``fire_event_macros("print_finished")`` — clear
    # first and the finish macros would have nothing to match against.
    if _owns_printer_effect():
        clear_macro_selection(printer_id)

    swap_config = _active_swap_config.pop(printer_id, None) if _owns_printer_effect() else None
    swap_events: list[str] = list(swap_config.get("swap_macro_events", [])) if swap_config else []

    # --- The swap decision (swap-source-lifecycle spec r4 §10, D3 + D5) -----------------------------------------
    # ONE fresh read of the archive, here: after the awaited last-chance attach above (which may have just written
    # the declaration) and before any terminal swap action or the plate decision below; a later attach changes
    # nothing this run decided. Only a bound terminal this callback accepted, of a proven origin, is a run
    # BamDude can stand behind -- a name, an alias, an ILIKE or an adoption archives the print and nothing more.
    from backend.app.services import swap_terminal
    from backend.app.services.print_run_binding import origin_is_proven

    _swap_run_proven = bound_run is not None and accepted_queue_item_id is not None and origin_is_proven(bound_run)
    # Baked-file clearance still needs its origin proof. An explicit
    # owed macro instead belongs to the durable queue execution, even when a
    # restart lost the dispatch binding or recovered it as ``observed``.
    from types import SimpleNamespace as _SwapExecution

    from backend.app.services.print_reconciliation import _run_holds_its_printer, _swap_run

    _swap_execution = None
    _swap_queue_id = None
    _swap_execution_owned = False
    _swap_hold: str | None = None
    _swap_hold_text: str | None = None
    _swap_fact = None
    _swap_pending_fact = None
    _swap_auto_eject = False
    _swap_cycle = swap_terminal.CYCLE_POSSIBLY_STARTED
    _swap_mode = False
    if archive_id:
        try:
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive as _SwArchive
                from backend.app.models.printer import Printer as _SwPrinter
                from backend.app.models.printer_queue import PrinterQueue as _SwQueue
                from backend.app.services.order_auto_eject import archive_mode as _sw_archive_mode

                _sw_archive = await db.get(_SwArchive, archive_id)
                if _sw_archive is not None:
                    _sw_extra = _sw_archive.extra_data if isinstance(_sw_archive.extra_data, dict) else {}
                    if not swap_events:
                        # Restart recovery: dispatch persisted what completion still owes.
                        _sw_pending = _sw_extra.get("swap_macro_events_pending")
                        if isinstance(_sw_pending, list) and _sw_pending:
                            swap_events = [str(e) for e in _sw_pending]
                            logger.info(
                                "[SWAP] Recovered swap config for printer %s from archive %s extra_data (events=%s)",
                                printer_id,
                                archive_id,
                                swap_events,
                            )
                    _swap_fact = swap_compatibility.archive_fact(_sw_archive).value
                    _swap_auto_eject = _sw_archive_mode(_sw_archive)
                    _swap_cycle = swap_terminal.cycle_state(_sw_extra)
                    if "swap_mode_change_table" in swap_events:
                        _swap_queue_id = await db.scalar(select(_SwQueue.id).where(_SwQueue.printer_id == printer_id))
                        _sw_item = await _swap_run(db, archive_id, _swap_queue_id, live=True)
                        if _sw_item is not None:
                            # Freeze before awaits/populate_existing can refresh the ORM row.
                            _swap_execution = _SwapExecution(id=_sw_item.id, started_at=_sw_item.started_at)
                            _swap_execution_owned = await _run_holds_its_printer(
                                db, printer_id, archive_id, _swap_queue_id, _swap_execution, live=True
                            )
                        _swap_pending_fact = await swap_terminal.pending_fact(db, _sw_archive, _sw_item)
                _sw_printer = await db.get(_SwPrinter, printer_id)
                _swap_mode = bool(_sw_printer is not None and _sw_printer.swap_mode_enabled)
        except Exception as e:
            # No answer is no permission: the run holds below instead of guessing.
            logger.warning("[SWAP] swap decision read failed for archive %s: %s", archive_id, e)

    if "swap_mode_change_table" in swap_events:
        _swap_hold = swap_terminal.change_table_hold(
            _swap_pending_fact,
            completed=_swap_status_ok,
            execution_owned=_swap_execution_owned,
            swap_mode=_swap_mode,
        )
    if "swap_mode_change_table" in swap_events and _swap_hold is None:
        _sw_attempted = False
        _sw_swapped = False
        try:
            async with async_session() as db:
                from backend.app.models.print_queue import PrintQueueItem
                from backend.app.models.printer import Printer as _SwapPrinter
                from backend.app.services.macro_executor import find_swap_macro

                _sw_printer = await db.get(_SwapPrinter, printer_id)
                macro = await find_swap_macro(db, "swap_mode_change_table", _sw_printer) if _sw_printer else None
                if not (macro and macro.gcode):
                    _swap_hold = swap_terminal.HOLD_SWAP_OFF
                elif _owns_printer_effect():

                    async def _swap_still_ours(session):
                        return _owns_printer_effect() and await _run_holds_its_printer(
                            session, printer_id, archive_id, _swap_queue_id, _swap_execution, live=True
                        )

                    # One claim decides whether the command may leave, committed before it -- the sweep takes the
                    # same one, so a reconnect can never send this run's table swap a second time (Codex review r1).
                    _sw_claim = await swap_terminal.claim_change_table(
                        async_session,
                        archive_id,
                        queue_id=_swap_queue_id,
                        owner="live",
                        allow_absent=True,
                        still_ours=_swap_still_ours,
                    )
                    _sw_attempted = _sw_claim == swap_terminal.CLAIMED
                    if not _sw_attempted:
                        logger.warning(
                            "[SWAP] change_table of archive %s is not this completion's to send (%s) — held",
                            archive_id,
                            _sw_claim,
                        )
                        _swap_hold = swap_terminal.HOLD_ATTEMPT_UNKNOWN
                if _sw_attempted:
                    logger.info("[SWAP] Running change_table macro '%s' for printer %s", macro.name, printer_id)
                    _sw_ok, _sw_msg = await printer_manager.execute_macro_and_wait(
                        printer_id, macro.gcode, macro.name, may_run=_owns_printer_effect
                    )
                    _sw_swapped = bool(_sw_ok)
                    # The answer, the event off the checklist and (on success) an earlier hold cleared -- one write.
                    await swap_terminal.finish_change_table(async_session, archive_id, ok=bool(_sw_ok))
                    if _sw_ok and _owns_printer_effect():
                        # Table swapped successfully — skip arming the
                        # plate-clear gate so the scheduler doesn't wait
                        # for manual confirmation.
                        _plate_auto_cleared_by_swap = True
                        logger.info("[SWAP] change_table done — plate auto-cleared for printer %s", printer_id)
                    if not _sw_ok and _owns_printer_effect():
                        logger.error("[SWAP] change_table macro failed: %s — pausing queue", _sw_msg)
                        # ⚠️ A REAL pause, not just waiting_reason: the
                        # scheduler recomputes waiting_reason every tick
                        # and clears it the moment the printer looks idle,
                        # so the reason alone never held anything — the
                        # next item dispatched onto the un-swapped table
                        # one tick later. set_queue_paused is what the
                        # scheduler actually skips on.
                        await db.commit()
                        await _pause_after_swap_failure(
                            printer_id,
                            archive_id,
                            accepted_queue_item_id,
                            f"Swap macro failed: {_sw_msg}",
                            may_hold=_owns_printer_effect,
                        )
        except Exception as e:
            logger.error("[SWAP] change_table macro error: %s", e)
            # No answer is no permission: before the claim nothing went out; after it the command may have -- it
            # stays claimed and is never sent again. Either way the plate is not taken as swapped, and the queue
            # waits with a reason.
            if not _sw_swapped:
                _swap_hold = swap_terminal.HOLD_ATTEMPT_UNKNOWN if _sw_attempted else swap_terminal.HOLD_MACRO_ERROR
        finally:
            if _sw_attempted:
                swap_terminal.release(archive_id)
    elif archive_id and "swap_mode_change_table" not in swap_events:
        # The source's own sequence clears the plate only for this exact, completed run (D5).
        if swap_terminal.baked_clears(
            _swap_fact, completed=_swap_status_ok, proven=_swap_run_proven, auto_eject=_swap_auto_eject
        ):
            _plate_auto_cleared_by_swap = True
            logger.info(
                "[SWAP] baked swap sequence of archive %s — plate auto-cleared for printer %s", archive_id, printer_id
            )
            # Nobody takes this print off the plate, so nobody is asked to grade it -- as after our change_table.
            try:
                await swap_terminal.note_baked_clear(async_session, archive_id)
            except Exception:  # noqa: BLE001 -- a missed mark leaves «Not checked»; the completion goes on
                logger.exception("[SWAP] could not count archive %s as checked after its baked swap", archive_id)
        elif _swap_fact is True:
            logger.info(
                "[SWAP] archive %s declares a baked swap sequence, but this finish does not prove the run "
                "(status=%s proven=%s auto_eject=%s) — the plate keeps its gate",
                archive_id,
                data.get("status"),
                _swap_run_proven,
                _swap_auto_eject,
            )

    if _swap_hold is not None and archive_id:
        # Nothing runs, the event stays owed, the plate is not cleared -- and this printer's queue waits with a
        # reason the operator can read even when no job is waiting (spec r4 §10, R2-6).
        logger.info("[SWAP] change_table held for archive %s on printer %s: %s", archive_id, printer_id, _swap_hold)
        _swap_hold_text = await _hold_for_owed_swap(
            printer_id,
            archive_id,
            accepted_queue_item_id,
            _swap_hold,
            _swap_cycle,
            owns_printer=_owns_printer_effect(),
            expected_identity=terminal_archive_identity,
            may_hold=_owns_printer_effect,
        )

    # Arm the plate-clear gate if the printer is configured to require it and
    # no swap path already cleared the plate. Persisted to DB so an Auto Off
    # power cycle can't let the queue bypass the confirmation (#961).
    #
    # ⚠️ MUST run BEFORE the queue-item update below releases the printer's
    # queue claim. This block used to sit at the very end of
    # ``on_print_complete``, ~70 ms after ``set_queue_idle`` — and a
    # ``check_queue`` tick threading that window saw "queue idle, state
    # FINISH, gate unarmed" and dispatched the next item over an uncleared
    # plate (live incident 2026-08-28: the premature dispatch then lost its
    # uploaded 3MF to this print's own cleanup and died on 0500_4002). Both
    # inputs are final here: ``archive_id`` is resolved at the top, and both
    # ``_plate_auto_cleared_by_swap`` writers (swap_compatible check,
    # change_table macro) run above this line.
    if archive_id and not _plate_auto_cleared_by_swap and _owns_printer_effect():
        try:
            _armed = printer_manager.arm_awaiting_plate_clear(printer_id, archive_id)
            # Old third-party manager adapters (and focused lifecycle tests)
            # expose only the former synchronous setter. Production returns a
            # coroutine and persists owner+generation before the queue moves.
            if inspect.isawaitable(_armed):
                _armed = await _armed
            else:
                printer_manager.set_awaiting_plate_clear(printer_id, True)
            if _armed:
                logger.info("[PLATE] Armed awaiting_plate_clear gate for printer %s", printer_id)
        except Exception as e:
            logger.warning("[PLATE] Failed to arm awaiting_plate_clear: %s", e)
            analysis_completion["return_critical_failed"] = True
    elif archive_id and _owns_printer_effect():
        # The swap path cleared the plate for real — so RELEASE the gate, don't
        # merely skip arming it. Skipping was the whole bug: the flag is
        # persisted to ``printers.awaiting_plate_clear`` and restored at
        # startup, so once anything armed it, a swap printer stayed gated
        # forever. Every subsequent print logged "plate auto-cleared", left the
        # flag standing, and the queue quietly refused to dispatch to that
        # printer — quietly because the auto-queue distributor logs its
        # per-tick decisions at DEBUG, so an INFO-level support bundle shows
        # nothing wrong at all. Reported from a farm of three swap A1 Minis
        # where the queue "just stopped moving" and clicking Clear Plate — the
        # only other caller of this setter — started the prints.
        #
        # Two ways in: a print started from the printer's own screen (no
        # dispatch, so no swap config is registered, so no auto-clear and the
        # arm block above fires), or any cancelled / failed print.
        #
        # Safe to release, and provably: ``_plate_auto_cleared_by_swap`` is set
        # in exactly two places and both require a successful print — the
        # swap_compatible branch is guarded by ``_swap_status_ok``, and the
        # change_table branch sets it only under ``if _sw_ok``, after the macro
        # reports success. A failed, aborted or cancelled print reaches neither,
        # so "the part is still on the bed" keeps its manual gate.
        if printer_manager.is_awaiting_plate_clear(printer_id):
            printer_manager.set_awaiting_plate_clear(printer_id, False)
            logger.info(
                "[PLATE] Released a stale awaiting_plate_clear gate for printer %s — the swap path cleared the plate",
                printer_id,
            )

    # Update queue item status early - must run before the archive_id early-return
    # so queue items don't get stuck in "printing" when archive lookup fails.
    try:
        async with async_session() as db, AsyncExitStack() as terminal_lane:
            from backend.app.models.print_queue import PrintQueueItem
            from backend.app.models.printer_queue import PrinterQueue as _PQForCount
            from backend.app.services.queue_ops import claim_lanes

            # G03 B, A18: the terminal SQL phase is a lane writer. The printer's queues and the execution archive
            # this completion closes are read first; the archive's guard and the lanes are taken before the
            # authoritative reads below and released with the terminal commit -- swap macros, notifications and the
            # clean-up stay outside them.
            terminal_queues = sorted(
                (await db.execute(select(_PQForCount.id).where(_PQForCount.printer_id == printer_id))).scalars().all()
            )
            if bound_run is not None:
                terminal_archive = bound_run.archive_id
            else:
                previewed = [
                    item
                    for item in await _printing_rows_for_printer(db, printer_id)
                    if await _completion_belongs_to_item(db, item, data)
                ]
                terminal_archive = previewed[0].archive_id if len(previewed) == 1 else None
            await db.commit()
            await terminal_lane.enter_async_context(claim_lanes(db, terminal_queues, archive_id=terminal_archive))

            printing_items = await _printing_rows_for_printer(db, printer_id)
            if len(printing_items) > 1:
                logger.warning(
                    "BUG: Multiple queue items in 'printing' status for printer %s: %s",
                    printer_id,
                    [(i.id, i.archive_id, i.library_file_id) for i in printing_items],
                )
            if bound_run is not None:
                queue_item = await db.get(PrintQueueItem, accepted_queue_item_id)
                queue_identity_unresolved = (
                    queue_item is None
                    or queue_item.status != "printing"
                    or queue_item.archive_id != bound_run.archive_id
                    or (bound_run.queue_item_id is not None and queue_item.id != bound_run.queue_item_id)
                )
                if queue_identity_unresolved:
                    logger.warning(
                        "Accepted completion archive %s on printer %s lost its addressed queue row; preserving claims",
                        bound_run.archive_id,
                        printer_id,
                    )
                    queue_item = None
            else:
                matching_items = [item for item in printing_items if await _completion_belongs_to_item(db, item, data)]
                queue_identity_unresolved = False
                if len(matching_items) == 1:
                    queue_item = matching_items[0]
                elif len(matching_items) > 1:
                    queue_item = None
                    queue_identity_unresolved = True
                    logger.warning(
                        "Completion on printer %s matches multiple printing queue items %s — preserving all claims",
                        printer_id,
                        [item.id for item in matching_items],
                    )
                else:
                    queue_item = None
                    queue_identity_unresolved = bool(printing_items)
            bound_archive = None
            if queue_item is not None and bound_run is not None:
                from backend.app.models.archive import PrintArchive as _BoundTerminalArchive

                bound_archive = await db.scalar(
                    select(_BoundTerminalArchive)
                    .where(
                        _BoundTerminalArchive.id == bound_run.archive_id,
                        _BoundTerminalArchive.status == "printing",
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if bound_archive is None:
                    logger.warning(
                        "Accepted completion archive %s on printer %s is no longer printing; preserving claims",
                        bound_run.archive_id,
                        printer_id,
                    )
                    queue_item = None
                    queue_identity_unresolved = True
            if queue_item is not None and (
                queue_item.queue_id not in terminal_queues
                or (bound_run is None and queue_item.archive_id != terminal_archive)
            ):
                # Found under the guards in a lane, or of an archive, this phase did not take: a refusal, never a
                # lock taken late (A18).
                logger.warning(
                    "Completion on printer %s found row %s outside the lanes it holds; preserving claims",
                    printer_id,
                    queue_item.id,
                )
                queue_item = None
                queue_identity_unresolved = True
            if queue_item:
                queue_status = data.get("status", "completed")
                # MQTT sends "aborted" for cancelled prints; normalise to
                # "cancelled" so it matches the queue schema Literal.
                if queue_status == "aborted":
                    queue_status = "cancelled"

                # Carry verbose error text from the queue item onto the
                # archive. Once the queue item is auto-deleted below, this is
                # the only surviving diagnostic for the failure — the
                # IssuesSection reads it off the archive on hover.
                if queue_item.error_message and queue_item.archive_id:
                    from backend.app.models.archive import PrintArchive as _ArchiveForErr

                    _err_archive = await db.get(_ArchiveForErr, queue_item.archive_id)
                    if _err_archive is not None and not _err_archive.error_message:
                        _err_archive.error_message = queue_item.error_message

                queue_item.status = queue_status
                queue_item.completed_at = datetime.now(timezone.utc)
                if queue_status == "failed" and not queue_item.error_code:
                    # A physical failure keeps the printer's own cause, ``other`` when it
                    # gave none — never a dispatch class (post-rebuild D-R6).
                    queue_item.error_code = (
                        derive_failure_reason(
                            queue_status,
                            data.get("hms_errors", []),
                            cancelled_on_printer=bool(data.get("cancelled_on_printer")),
                        )
                        or FAILURE_OTHER
                    )
                if _swap_hold_text:
                    # After the status: changing it clears an older reason (the model's validator).
                    queue_item.waiting_reason = _swap_hold_text
                # #1111: pre-RUNNING failures (FAILED from PREPARE/SLICING)
                # arrive without a queue_item.error_message because the print
                # never reached the runtime layer that normally populates it.
                # Synthesise from the live HMS list so the user sees e.g.
                # "[0500_4038] The nozzle diameter in sliced file is not
                # consistent with the current nozzle setting" instead of a
                # blank reason.
                if queue_status == "failed" and not queue_item.error_message:
                    queue_item.error_message = _format_hms_error_summary(
                        data.get("hms_errors") or [], hms_device_of(printer_id)
                    )

                # Bump usage counters on the source library file so admins can
                # sort by "last printed" and (eventually) auto-purge stale
                # files — #1008.
                from backend.app.services import library_usage

                if queue_status == "completed":
                    await library_usage.stage(db, bound_archive, queue_item.library_file_id)

                # Update queue status and counters
                from backend.app.services.queue_counters import halt_lane, set_queue_idle, update_queue_counters

                if queue_status == "failed":
                    # The one writer of a halt (A9): this run's header, or a pause beside another run's claim.
                    await halt_lane(db, queue_item.queue_id, item_id=queue_item.id, kind="error")
                elif queue_status == "completed":
                    await set_queue_idle(db, queue_item.queue_id, expected_item_id=queue_item.id)
                    # A success releases "only after a successful previous print" by
                    # itself, and must keep doing so after this row is cleaned up
                    # below or at Clear plate: the older failures stop gating in
                    # this same commit (G03 D1(a)).
                    from backend.app.services.previous_print_gate import acknowledge_failures_before_success

                    await acknowledge_failures_before_success(db, queue_item.queue_id, success_item_id=queue_item.id)
                else:
                    # Cancelled — pause the queue so the operator inspects
                    # the printer before the next pending item dispatches.
                    # Covers two cases the runtime can't distinguish
                    # locally: (a) user-initiated cancel (UI button or
                    # printer-screen Stop) and (b) printer-initiated
                    # abort on a mid-print error (auto-bed-leveling
                    # failure, runaway thermistor, AMS jam — printer
                    # transitions RUNNING → IDLE without ever flipping
                    # gcode_state to FAILED, so MQTT surfaces it as
                    # ``aborted`` → normalised to ``cancelled`` above).
                    # In both cases the safe default is to stop further
                    # dispatch and let the human decide whether the
                    # underlying cause is cleared. Mirrors
                    # ``_cancel_item`` in the scheduler (the scheduler-
                    # internal cancel path always paused the queue;
                    # this aligns the MQTT-runtime path with that).
                    await halt_lane(db, queue_item.queue_id, item_id=queue_item.id, kind="paused")
                await update_queue_counters(db, queue_item.queue_id)
                if bound_archive is not None:
                    # This is the durable live terminal boundary: the exact
                    # archive and exact queue attempt become terminal in the
                    # SAME commit.  The earlier acceptance marker only owns
                    # the in-flight window; it must not be mistaken for an
                    # already-final archive if this handler crashes.
                    terminal_at = queue_item.completed_at
                    bound_archive.status = queue_status
                    bound_archive.completed_at = terminal_at
                    # Wear: this end is measured (the printer's own report), and this is the
                    # transition out of ``printing`` -- the later status update finds the row
                    # terminal and stamps nothing.
                    from backend.app.services import wear_cost as _wear_cost

                    await _wear_cost.stamp_measured_end(
                        db,
                        bound_archive,
                        printer_id=printer_id,
                        ended_at=terminal_at,
                        record_holds=lambda: printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life")),
                    )
                    if queue_status == "completed":
                        # A print that completed has no failure reason, whatever an
                        # earlier end of this row left on it.
                        bound_archive.failure_reason = None
                    elif queue_status == "failed" and not bound_archive.failure_reason:
                        bound_archive.failure_reason = (
                            derive_failure_reason(
                                queue_status,
                                data.get("hms_errors", []),
                                cancelled_on_printer=bool(data.get("cancelled_on_printer")),
                            )
                            or FAILURE_OTHER
                        )
                    if queue_item.error_message and not bound_archive.error_message:
                        bound_archive.error_message = queue_item.error_message
                    print_user_id = _print_user_info.get("user_id") if _print_user_info else None
                    if print_user_id is not None and bound_archive.created_by_id is None:
                        bound_archive.created_by_id = print_user_id
                    extra = dict(bound_archive.extra_data or {})
                    checkpoint = extra.get(_TERMINAL_ACCEPTANCE_KEY)
                    if isinstance(checkpoint, dict):
                        checkpoint = dict(checkpoint)
                        checkpoint["stage"] = "db_finalized"
                        extra[_TERMINAL_ACCEPTANCE_KEY] = checkpoint
                        bound_archive.extra_data = extra
                await db.commit()
                await terminal_lane.aclose()  # the terminal phase ends with its commit (A18)
                logger.info("Updated queue item %s status to %s", queue_item.id, queue_status)
                await library_usage.finish(db)
                if queue_status == "failed" and bound_archive is not None:
                    from backend.app.services.auto_queue_return import capture_candidate

                    # No candidate escapes a failed terminal commit. A normal
                    # callback/cleanup alone cannot certify this transaction.
                    analysis_completion["return_candidate"] = await capture_candidate(
                        db, queue_item, bound_archive, epoch=completion_epoch, effect_token=effect_token
                    )

                # Filament-calibration sessions: flip the linked
                # ``calibration_session`` row to its post-print state
                # (``awaiting_user_input`` for save-prompt modes,
                # ``saved`` for tower modes, ``failed`` on print
                # failure). Without this the wizard's polling sees a
                # stale ``running`` status until the next manual
                # GET /sessions/{id} kicks ``reconcile_session_status``,
                # which can leave the modal showing "Calibration in
                # progress…" long after the printer is done. The link
                # lives on the session row (``session.print_queue_item_id``),
                # not on the queue item — look up by that side. Done
                # best-effort: failures here never block the
                # ``on_print_complete`` happy path.
                if queue_item.is_calibration:
                    try:
                        from backend.app.models.calibration_session import (
                            CalibrationSession as _CaliSession,
                        )
                        from backend.app.services.calibration_service import (
                            reconcile_session_status as _reconcile_cali,
                        )

                        _cali_session = (
                            await db.execute(
                                select(_CaliSession).where(
                                    _CaliSession.print_queue_item_id == queue_item.id,
                                    _CaliSession.status == "running",
                                )
                            )
                        ).scalar_one_or_none()
                        if _cali_session is not None:
                            await _reconcile_cali(db, _cali_session)
                    except Exception:
                        logger.exception(
                            "Failed to reconcile calibration session for queue_item %s",
                            queue_item.id,
                        )

                # Calibration LibraryFile cleanup: every calibration
                # ``start`` writes a synthetic ``calibration_<mode>_<ts>.gcode.3mf``
                # into ``library-files/`` so the dispatcher has a
                # ``library_file_id`` to thread through (mirrors the
                # regular library print flow). Once the print itself
                # finishes — regardless of outcome (completed / failed /
                # cancelled) — that file has served its purpose and
                # should disappear: the archive carries its own copy of
                # the sliced gcode + thumbnails under ``file_path``
                # (chain-of-custody invariant from m009/m039), and the
                # operator never needs to re-slice the same calibration
                # sweep. Without this cleanup every test print left an
                # orphan sliced 3MF in the library, indistinguishable
                # from a user upload. Detach archive back-references
                # to the same NULL the route-side trash paths produce,
                # then unlink the disk file + delete the row.
                if queue_item.is_calibration and queue_item.library_file_id is not None:
                    try:
                        from backend.app.models.library import LibraryFile as _CaliLibFile
                        from backend.app.services.library_trash import library_trash_service

                        _cali_lib_file = await db.get(_CaliLibFile, queue_item.library_file_id)
                        if _cali_lib_file is not None:
                            from sqlalchemy.ext.asyncio import async_sessionmaker

                            from backend.app.services.library_delete_cleanup import finish
                            from backend.app.services.library_delete_scope import library_delete_scope
                            from backend.app.services.library_identity import LibraryIdentity

                            cali_source = LibraryIdentity.capture(_cali_lib_file)
                            factory = async_sessionmaker(db.bind, expire_on_commit=False)
                            await db.commit()  # calibration reconciliation commits before cleanup
                            await db.close()  # no old SQLite reader/writer may wait for itself
                            cali_removed = False
                            async with factory() as cleanup_db:
                                current = await cleanup_db.get(_CaliLibFile, cali_source.id)
                                if cali_source.matches(current):
                                    async with library_delete_scope(cleanup_db, [cali_source]):
                                        current = await cleanup_db.get(
                                            _CaliLibFile, cali_source.id, populate_existing=True
                                        )
                                        # The terminal session may still cache the old
                                        # numeric link after a concurrent purge. Read
                                        # navigation afresh only after the library/job
                                        # guards; a reused source id grants no right.
                                        linked_job_id = await cleanup_db.scalar(
                                            select(PrintQueueItem.id).where(
                                                PrintQueueItem.id == queue_item.id,
                                                PrintQueueItem.recovery_identity == queue_item.recovery_identity,
                                                PrintQueueItem.library_file_id == cali_source.id,
                                            )
                                        )
                                        if linked_job_id is not None:
                                            await library_trash_service.purge_rows(cleanup_db, current)
                                            await cleanup_db.commit()
                                            cali_removed = True
                                    await finish(cleanup_db)
                            if cali_removed:
                                logger.info(
                                    "Calibration cleanup: removed library file %s (%s status=%s)",
                                    queue_item.library_file_id,
                                    queue_item.id,
                                    queue_status,
                                )
                    except Exception:
                        logger.exception(
                            "Failed to clean up calibration LibraryFile %s for queue_item %s",
                            queue_item.library_file_id,
                            queue_item.id,
                        )

                # MQTT relay - publish queue job completed.
                # Guarded by the `if queue_item:` scope because queue_item / queue_status
                # are only defined there; on the external-print path below there's no
                # job to report to the relay.
                try:
                    printer_info = printer_manager.get_printer(printer_id)
                    await mqtt_relay.on_queue_job_completed(
                        job_id=queue_item.id,
                        filename=filename or subtask_name,
                        printer_id=printer_id,
                        printer_name=printer_info.name if printer_info else "Unknown",
                        status=queue_status,
                    )
                except Exception:
                    pass  # Don't fail if MQTT fails

                # Power off the printer when the operator ticked "power off
                # when done" on this queue item. Must precede the
                # completed-item auto-cleanup below — that delete expires
                # ``queue_item``, so ``auto_off_after`` has to be read first.
                #
                # Delegates to the smart-plug manager so the off honours each
                # plug's configured strategy (time delay or temperature
                # threshold), is cancelled if the printer starts printing again,
                # and never cuts power on a loaded print (#1890). Previously an
                # inline block here hardcoded a 50°C / 600s cooldown wait and
                # powered off on the timeout regardless of print state — cutting
                # a touchscreen reprint mid-print.
                if queue_item.auto_off_after and _owns_printer_effect():
                    try:
                        await smart_plug_manager.schedule_off_after_queue_job(
                            printer_id, db, may_run=_owns_printer_effect
                        )
                    except Exception as e:
                        logger.warning("Failed to schedule queue auto-off for printer %s: %s", printer_id, e)

                # Auto-cleanup: completed queue items live on via their linked
                # archive (counters re-computed from print_archives post-m019).
                # ⚠️ Unless the printer confirms its plate — then the row waits
                # for Clear or Repeat instead. See ``_auto_clean_completed_item``.
                # ⚠️ Not while our owed table swap is held: the row is the hold's record.
                if _swap_hold is None:
                    await _auto_clean_completed_item(
                        db,
                        queue_item,
                        queue_status=queue_status,
                        plate_auto_cleared=_plate_auto_cleared_by_swap,
                    )

                # Queue may now be empty — fire the queue-completed
                # notifications.
                #
                # ⚠️ **Only for a row somebody actually queued.** This block
                # used to be justified by "only a finished *queue* item can
                # empty the queue (an external print never consumes a queue
                # item)". That was true when written and is not any more: since
                # 0.5.4 every print holds a row while it runs — a direct print
                # claims one at dispatch, an external one gets one made at
                # print start — so reaching this branch stopped meaning
                # anything about the queue, and both notifications began
                # arriving after prints nobody scheduled. ``origin`` is what
                # the old branch test was really asking.
                #
                # A repeat needs no special case: ``answer_by_repeating``
                # re-arms the same row, so it keeps the origin it was born
                # with — a repeated external print is still silent here, a
                # repeated queue item still counts.
                if queue_item.origin == "queue":
                    try:
                        from sqlalchemy import func as sa_func

                        count_result = await db.execute(
                            select(sa_func.count(PrintQueueItem.id)).where(PrintQueueItem.status == "pending")
                        )
                        pending_count = count_result.scalar() or 0

                        if pending_count == 0:
                            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                            completed_result = await db.execute(
                                select(sa_func.count(PrintQueueItem.id)).where(
                                    # Same filter as the gate above, for the
                                    # same reason: "All N queued jobs have
                                    # finished" must not count claim rows, or
                                    # the number in the message is a tally of
                                    # prints nobody queued.
                                    PrintQueueItem.origin == "queue",
                                    PrintQueueItem.status.in_(["completed", "failed", "skipped"]),
                                    PrintQueueItem.completed_at >= today_start,
                                )
                            )
                            completed_count = completed_result.scalar() or 1

                            await notification_service.on_queue_completed(
                                completed_count=completed_count,
                                db=db,
                            )
                    except Exception:
                        pass  # Don't fail if notification fails

                    # Per-printer: this printer's own queue just drained → fire
                    # the printer-scoped notification. Unlike the global event
                    # above, a paused / manual-start queue on another printer
                    # can't suppress it (the global pending_count never hits 0).
                    try:
                        from sqlalchemy import func as sa_func

                        printer_pending = await db.execute(
                            select(sa_func.count(PrintQueueItem.id)).where(
                                # By the queue's printer_id — ``queue_id ==
                                # printer_id`` is a preference, not a guarantee
                                # (services/printer_queues).
                                PrintQueueItem.queue_id.in_(
                                    select(_PQForCount.id).where(_PQForCount.printer_id == printer_id)
                                ),
                                PrintQueueItem.status == "pending",
                            )
                        )
                        if (printer_pending.scalar() or 0) == 0:
                            _pi = printer_manager.get_printer(printer_id)
                            await notification_service.on_printer_queue_completed(
                                printer_id=printer_id,
                                printer_name=_pi.name if _pi else f"Printer #{printer_id}",
                                db=db,
                            )
                    except Exception:
                        pass  # Don't fail if notification fails
            elif not queue_identity_unresolved:
                # No queue_item was printing. Still flip queue.status back to
                # idle/error so the UI's current-print card goes away and
                # pending items unblock.
                #
                # ⚠️ This is now a FALLBACK, not the direct/external path it was
                # written as. Every print holds a row while it runs — the
                # scheduler's, the claim a direct print takes at dispatch, or the
                # one created here at start for a print BamDude did not send — so
                # reaching this branch means the row vanished, or
                # ``_completion_belongs_to_item`` rejected it as somebody else's.
                #
                # ⚠️ **Do not delete it as dead code.** Without it, exactly those
                # cases leave the printer claimed for ever, and ``check_queue``
                # reads a stale claim as a live one with no age check — the farm
                # quietly stops taking work.
                from backend.app.models.printer_queue import PrinterQueue
                from backend.app.services.queue_counters import halt_lane, set_queue_idle

                # Inside the terminal phase's lanes (A18, r4 R12): only a header no live claim owns is released --
                # ``printing`` with no row of the queue printing -- by a compare-and-set on the header as read.
                _pq = (
                    await db.execute(
                        select(PrinterQueue)
                        .where(PrinterQueue.printer_id == printer_id)
                        .with_for_update(key_share=True)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if _pq is not None and _pq.id not in terminal_queues:
                    logger.warning(
                        "Completion on printer %s found queue %s outside its lanes; preserving claims",
                        printer_id,
                        _pq.id,
                    )
                    _pq = None
                if _pq and _pq.status == "printing":
                    status_raw = data.get("status", "completed")
                    if status_raw == "aborted":
                        status_raw = "cancelled"
                    read_item = _pq.current_item_id
                    live = await db.scalar(
                        select(PrintQueueItem.id)
                        .where(PrintQueueItem.queue_id == _pq.id, PrintQueueItem.status == "printing")
                        .limit(1)
                    )
                    if status_raw == "failed":
                        # A halting failure of a print this printer ran: the stale header halted, or a pause beside
                        # a live claim that took the header since (Q9(2)).
                        outcome = await halt_lane(
                            db, _pq.id, item_id=None, kind="error", stale_only=live is None, read_item_id=read_item
                        )
                    elif live is None:
                        outcome = (
                            "released"
                            if await set_queue_idle(db, _pq.id, stale_only=True, read_item_id=read_item)
                            else "unchanged"
                        )
                    else:
                        outcome = "live claim kept"
                    await db.commit()
                    logger.info(
                        "External/direct print finished on printer %s → queue %s (header item %s, %s): %s",
                        printer_id,
                        _pq.id,
                        read_item,
                        status_raw,
                        outcome,
                    )
                await terminal_lane.aclose()

                # Direct-dispatch / external prints also count toward LibraryFile
                # usage when we can trace the archive back to a library file.
                direct_status = data.get("status", "completed")
                if direct_status == "aborted":
                    direct_status = "cancelled"
                if direct_status == "completed" and archive_id:
                    from backend.app.models.archive import PrintArchive as _PaLib

                    lib_id = await db.scalar(select(_PaLib.library_file_id).where(_PaLib.id == archive_id))
                    if lib_id is not None:
                        await _bump_library_file_usage(db, lib_id)
                        await db.commit()

            else:
                await terminal_lane.aclose()
                logger.warning(
                    "Completion on printer %s did not identify its active queue item; preserving queue state",
                    printer_id,
                )
    except Exception as e:
        logging.getLogger(__name__).warning(f"Queue item update failed: {e}")

    log_timing("Queue item update")

    # Start bed cooldown monitor (polls bed temp until it drops below threshold)
    # Must run before archive_id early-return so it fires for all prints (including
    # prints started from BambuStudio/touchscreen that have no archive).
    async def _background_bed_cooldown():
        """Monitor bed temperature after print and notify when cooled."""
        try:
            from backend.app.api.routes.settings import get_setting
            from backend.app.services import notification_inbox

            # Check threshold setting
            async with async_session() as db:
                threshold_str = await get_setting(db, "bed_cooled_threshold")
            threshold = float(threshold_str) if threshold_str else 35.0

            # Check whether anybody receives bed_cooled at all (early exit if none).
            # ⚠️ ``not providers`` is no longer the question: the in-app inbox rides
            # every provider lookup as a channel, so the list is never empty. This
            # guard decides whether the WORK is worth doing — 120 polls over 30
            # minutes, with an MQTT status request every fourth — and bed_cooled is
            # an ``info`` event that no default subscription receives. Ask for a real
            # provider row, or a user who asked for this event in their inbox.
            async with async_session() as db:
                providers = await notification_service._get_providers_for_event(db, "on_bed_cooled", printer_id)
                has_network_provider = any(p.provider_type != "inbox" for p in providers)
                if not has_network_provider and not await notification_inbox.has_subscriber(db, "bed_cooled"):
                    logger.debug("[BED-COOL] No providers enabled for bed_cooled on printer %s", printer_id)
                    return

            logger.info("[BED-COOL] Monitoring bed temp for printer %s (threshold: %.0f°C)", printer_id, threshold)

            if not _owns_printer_effect():
                return
            # Request a fresh full status so we get current bed_temper
            printer_manager.request_status_update(printer_id)

            max_polls = 120  # 120 * 15s = 30 min timeout
            for poll_num in range(max_polls):
                await asyncio.sleep(15)
                if not _owns_printer_effect():
                    return

                # Request fresh temperature data every 60s - after print completion,
                # the printer may send partial MQTT updates without bed_temper,
                # leaving the cached value stale at the end-of-print temperature.
                if poll_num % 4 == 0:
                    printer_manager.request_status_update(printer_id)

                # Check if printer is still connected
                status = printer_manager.get_status(printer_id)
                if status is None:
                    logger.info("[BED-COOL] Printer %s disconnected, stopping monitor", printer_id)
                    return

                # Check if a new print started (state == RUNNING)
                if hasattr(status, "state") and status.state == "RUNNING":
                    logger.info("[BED-COOL] New print started on printer %s, stopping monitor", printer_id)
                    return

                # Get bed temperature
                bed_temp = None
                if hasattr(status, "temperatures") and isinstance(status.temperatures, dict):
                    bed_temp = status.temperatures.get("bed")

                if bed_temp is None:
                    logger.debug(
                        "[BED-COOL] Printer %s: bed temp is None (keys: %s, state: %s)",
                        printer_id,
                        list(status.temperatures.keys()) if isinstance(status.temperatures, dict) else "N/A",
                        status.state if hasattr(status, "state") else "N/A",
                    )
                    continue

                logger.debug("[BED-COOL] Printer %s: bed=%.1f°C, threshold=%.0f°C", printer_id, bed_temp, threshold)

                if bed_temp <= threshold:
                    logger.info(
                        "[BED-COOL] Bed cooled to %.1f°C on printer %s (threshold: %.0f°C)",
                        bed_temp,
                        printer_id,
                        threshold,
                    )
                    printer_info = printer_manager.get_printer(printer_id)
                    p_name = printer_info.name if printer_info else "Unknown"
                    async with async_session() as db:
                        if not _owns_printer_effect():
                            return
                        await notification_service.on_bed_cooled(
                            printer_id=printer_id,
                            printer_name=p_name,
                            bed_temp=bed_temp,
                            threshold=threshold,
                            filename=filename or subtask_name or "",
                            db=db,
                        )
                    return

            logger.info("[BED-COOL] Timeout waiting for bed to cool on printer %s", printer_id)
        except asyncio.CancelledError:
            logger.info("[BED-COOL] Bed cooldown monitor cancelled for printer %s", printer_id)
        except Exception as e:
            logger.warning("[BED-COOL] Failed: %s", e)
        finally:
            if _bed_cooldown_tasks.get(printer_id) is asyncio.current_task():
                _bed_cooldown_tasks.pop(printer_id, None)

    # Only start bed cooldown for completed prints
    if data.get("status") == "completed" and _owns_printer_effect():
        # Cancel any existing task for this printer
        existing_task = _bed_cooldown_tasks.pop(printer_id, None)
        if existing_task and not existing_task.done():
            existing_task.cancel()
        task = asyncio.create_task(_background_bed_cooldown())
        _bed_cooldown_tasks[printer_id] = task

    if not archive_id:
        # The printer's own calibration run has no archive by design and is no
        # print anybody asked to hear about: no no-archive notice for it
        # (upstream 9c938843). Everything above has already run.
        if is_internal_printer_job(filename, subtask_name):
            logger.info(
                "[CALLBACK] Internal printer job completed, no notification: filename=%s, subtask=%s",
                filename,
                subtask_name,
            )
            return
        logger.warning("Could not find archive for print complete: filename=%s, subtask=%s", filename, subtask_name)

        task = asyncio.create_task(_notify_completion_without_archive(printer_id, data))
        task.add_done_callback(lambda _t: None)
        return

    log_timing("Archive lookup")

    # Update archive status
    logger.info("[ARCHIVE] Updating archive %s status...", archive_id)
    try:
        async with async_session() as db:
            service = ArchiveService(db)
            status = data.get("status", "completed")

            # Back-fill created_by_id on reprint when NULL (#730 follow-up,
            # audit A.21). Reprint reuses the source archive row instead of
            # creating a new one, so an archive that was auto-created from a
            # printer-initiated print (created_by_id=NULL) would otherwise
            # stay unattributed forever — Print Log credited the reprinter
            # via _print_user_info but the Statistics per-user filter reads
            # archive.created_by_id and stayed unassigned. When we have a
            # print-session user AND the archive has no attribution yet,
            # credit the current user. Never overwrite existing attribution.
            _print_user_id = _print_user_info.get("user_id") if _print_user_info else None
            # Auto-detect failure reason via curated short-code map (see
            # _HMS_FAILURE_REASONS). Module-based heuristics mislabel H2D
            # user-cancels (module 0x0C cancel echo) as "Layer shift".
            hms_errors = data.get("hms_errors", []) if status == "failed" else None
            if hms_errors:
                logger.info("[ARCHIVE] HMS errors at failure: %s", hms_errors)
            failure_reason = derive_failure_reason(
                status, hms_errors, cancelled_on_printer=bool(data.get("cancelled_on_printer"))
            )
            if failure_reason:
                logger.info("[ARCHIVE] failure_reason=%r (status=%s)", failure_reason, status)
            elif status == "failed" and hms_errors:
                logger.info("[ARCHIVE] HMS errors present but none matched a known failure-reason short code")

            await db.commit()  # Release attribution reads before the archive writer.
            updated = await service.update_archive_status(
                archive_id,
                status=status,
                # ``cancelled`` joins the terminal-status list (#1198) so
                # queue-UI cancellations get a ``completed_at`` timestamp the
                # notification path can use to compute actual elapsed. Audited
                # every ``completed_at`` consumer first: the two
                # ``completed_at IS NULL`` queries in this file (lines 1707 +
                # 2388) are both paired with ``status == 'printing'`` so a
                # cancelled row can't slip in regardless; ``archives.py``'s
                # actual-elapsed read at :83 gates on ``status == 'completed'``
                # so this only adds rows to the stats-totals aggregation —
                # cancelled prints with their real elapsed are MORE accurate
                # than excluding them, which was the side-effect upstream
                # called out as a win.
                completed_at=(
                    analysis_completion["return_candidate"].completed_at
                    if analysis_completion.get("return_candidate") is not None
                    else datetime.now(timezone.utc)
                    if status in ("completed", "failed", "aborted", "cancelled")
                    else None
                ),
                failure_reason=failure_reason,
                expected_identity=terminal_archive_identity,
                expected_epoch=completion_epoch,
                created_by_id=_print_user_id,
                wear_printer_id=printer_id,
                wear_record_holds=lambda: printer_manager.life_holds(printer_id, data.get("_bamdude_printer_life")),
            )
            if not updated:
                return
            analysis_completion["return_archive_finalized"] = True
            logger.info(
                "[ARCHIVE] Archive %s status updated to %s, failure_reason=%s", archive_id, status, failure_reason
            )

            await ws_manager.send_archive_updated(
                {
                    "id": archive_id,
                    "status": status,
                }
            )
            logger.info("[ARCHIVE] WebSocket notification sent for archive %s", archive_id)

            # MQTT relay - publish archive updated
            try:
                await mqtt_relay.on_archive_updated(
                    archive_id=archive_id,
                    print_name=filename or subtask_name,
                    status=status,
                )
            except Exception:
                pass  # Don't fail if MQTT fails
    except Exception as e:
        logger.error("[ARCHIVE] Failed to update archive %s status: %s", archive_id, e, exc_info=True)
        # Continue with other operations even if archive update fails

    log_timing("Archive status update")

    # A print that belongs to no order puts its parts on the product's shelf
    # (pass 8, Decision 3). Here and not earlier: the archive is ``completed``
    # only after the block above, and its ``print_archive_parts`` rows are final
    # by now — seeded when the 3MF was attached, their ``defective`` counts
    # written by the skip-object callbacks during the print.
    await _credit_free_stock(archive_id)

    log_timing("Free stock credit")

    # Track filament consumption from AMS remain% deltas (skip if Spoolman handles usage)
    usage_results: list[dict] = []
    # Prefer ams_mapping captured from MQTT request topic (works for all print sources)
    stored_ams_mapping = _take_completion_ams_mapping(data, archive_id, terminal_archive_identity)
    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting

            _spoolman_on = await get_setting(db, "spoolman_enabled")
        if not _spoolman_on or _spoolman_on.lower() != "true":
            from backend.app.services.usage_tracker import on_print_complete as usage_on_print_complete

            async with async_session() as db:
                usage_results = await usage_on_print_complete(
                    printer_id,
                    data,
                    printer_manager,
                    db,
                    archive_id=archive_id,
                    ams_mapping=stored_ams_mapping,
                    expected_identity=terminal_archive_identity,
                    expected_epoch=completion_epoch,
                )
                if usage_results:
                    await ws_manager.broadcast(
                        {
                            "type": "spool_usage_logged",
                            "printer_id": printer_id,
                            "usage": usage_results,
                        }
                    )
                    log_timing("Usage tracker")

    except Exception as e:
        logger.warning("Usage tracker on_print_complete failed: %s", e)

    # Drop the print-start context unconditionally — the Spoolman branch above
    # skips the internal tracker entirely, so nothing else would clear what print
    # start captured, and a row surviving its print would be restored onto the
    # next one after a restart.
    try:
        from backend.app.services.usage_tracker import discard_session

        async with async_session() as db:
            await discard_session(db, printer_id)
    except Exception as e:
        logger.warning("Failed to clear persisted print session for printer %s: %s", printer_id, e)

    # Report filament usage to Spoolman if print completed successfully. Both writers divide by
    # the event's own figures (layer, layer count, progress), never the printer's state now.
    from backend.app.utils.tray_split import CompletionSnapshot

    completion = CompletionSnapshot.from_event(data)
    if data.get("status") == "completed":
        try:
            await _report_spoolman_usage(printer_id, archive_id, completion=completion)
            log_timing("Spoolman usage report")
        except Exception as e:
            logger.warning("Spoolman usage reporting failed: %s", e)
    else:
        # Report partial usage if tracking data exists (only stored when weight sync is disabled)
        try:
            async with async_session() as db:
                await _cleanup_spoolman_tracking(
                    printer_id,
                    archive_id,
                    db,
                    last_layer_num=data.get("last_layer_num"),
                    last_progress=data.get("last_progress"),
                    completion=completion,
                    status=data.get("status") or "failed",
                )
        except Exception as e:
            logger.debug("[SPOOLMAN] Cleanup failed: %s", e)

    # Run slow operations as background tasks to avoid blocking the event loop
    # These operations can take 5-10+ seconds and would freeze the UI if awaited

    # The moment this completion was handled: the energy window's end when the archive
    # carries none (spec plug-meter-watchdog W10) -- never a later "now" of the writer.
    energy_ended_at = datetime.now(timezone.utc)

    async def _background_energy_calculation():
        if not _owns_printer_effect():
            logger.info("[ENERGY-BG] skipping stale completion for archive %s", archive_id)
            return
        await _record_print_energy(
            archive_id,
            printer_id,
            may_read=_owns_printer_effect,
            ended_at=energy_ended_at,
            expected_identity=terminal_archive_identity,
            expected_epoch=completion_epoch,
        )

    async def _background_finish_photo() -> str | None:
        """Capture finish photo in background. Returns photo filename if captured."""
        try:
            if not _owns_printer_effect():
                logger.info("[PHOTO-BG] skipping stale completion for archive %s", archive_id)
                return None
            await _record_terminal_effect_stage(
                archive_id, "finish_photo", "attempted", expected_identity=terminal_archive_identity
            )
            logger.info("[PHOTO-BG] Starting finish photo capture for archive %s", archive_id)

            from backend.app.api.routes.camera import get_buffered_frame, is_stream_active

            # Read phase: settings + printer + archive in a short session, released
            # BEFORE the capture pipeline below (timelapse frame, stage-22 wait up to
            # 20s, external grab, or a fresh RTSP shot) which can take tens of seconds.
            # Holding the session across it pinned one pooled connection idle-in-
            # transaction per finishing print (#2572). expire_on_commit=False keeps
            # the loaded scalar columns readable; the write-back reopens a session.
            async with async_session() as db:
                from backend.app.api.routes.settings import get_setting
                from backend.app.models.archive import PrintArchive
                from backend.app.models.printer import Printer

                capture_enabled = await get_setting(db, "capture_finish_photo")
                if capture_enabled is None or capture_enabled.lower() != "true":
                    await _record_terminal_effect_stage(
                        archive_id, "finish_photo", "skipped", expected_identity=terminal_archive_identity
                    )
                    return None
                if not archive_id:
                    return None

                printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
                archive = (
                    await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                ).scalar_one_or_none()

            if not printer or not archive or archive.recovery_identity != terminal_archive_identity:
                await _record_terminal_effect_stage(
                    archive_id, "finish_photo", "skipped", expected_identity=terminal_archive_identity
                )
                return None

            if not _owns_printer_effect():
                logger.info("[PHOTO-BG] printer %s changed run during lookup", printer_id)
                await _record_terminal_effect_stage(
                    archive_id, "finish_photo", "skipped", expected_identity=terminal_archive_identity
                )
                return None

            from backend.app.services import finish_photo_store

            if not archive.file_path:
                logger.warning("[PHOTO-BG] Archive %s has no file_path, using its no_source folder", archive_id)

            async def _save_photo(frame: bytes) -> str | None:
                """One frame as this terminal's finish photo: the folder is read at the write (G10 G)."""
                return await finish_photo_store.save_frame(
                    archive_id, terminal_archive_identity, frame, session=async_session, may_run=_owns_printer_effect
                )

            photo_filename = None
            defer_live = False
            photo_key = (printer_id, effect_token.generation)

            # Prefer the timelapse last-frame source when a timelapse was recording —
            # it captures the moment after the toolhead parks but before the bed
            # drops, which the live-camera grab below would miss (#1397). Only runs
            # when the USER explicitly enabled timelapse for this print — #1721 removed
            # BamDude's force-on at dispatch because it caused per-layer nozzle parking
            # on Smooth-mode slicer profiles. Skipped for external cameras (their own
            # framing; they don't see a Bambu timelapse).
            prefer_timelapse_source = bool(data.get("timelapse_was_active")) and not (
                printer.external_camera_enabled and printer.external_camera_url
            )
            if prefer_timelapse_source:
                photo_filename = await _capture_finish_photo_from_timelapse(
                    archive_id=archive_id,
                    expected_identity=terminal_archive_identity,
                    rotation=getattr(printer, "camera_rotation", 0) or 0,
                    may_run=_owns_printer_effect,
                )

            # #1721: replacement framing path — on_finish_photo_moment pre-captured a
            # frame at the stage-22 / FINISH edge (toolhead parked, bed not yet
            # dropped) and cached the JPEG bytes in _stage22_finish_frames. Consume
            # them now so the saved photo has the better framing instead of the
            # post-bed-drop angle the live-camera fallback below would give.
            if not photo_filename:
                # #1790: the producer (on_finish_photo_moment) is dispatched
                # back-to-back with this consumer on the FINISH fallback path; wait for
                # it to store its frame (or give up) before touching the cache, so the
                # pop can't lose the race and the RTSP fallback below can't collide
                # with the producer's in-flight grab (single-client RTSP on Bambu). The
                # `is not None` guard skips timelapse / external-cam branches where the
                # producer never registered, so those can't hang.
                in_flight = _stage22_finish_in_flight.get(photo_key)
                if in_flight is not None:
                    try:
                        await asyncio.wait_for(in_flight.wait(), timeout=20.0)
                    except TimeoutError:
                        logger.warning(
                            "[PHOTO-BG] timed out waiting for stage-22 producer for printer %s — proceeding to fallback",
                            printer_id,
                        )
                if _stage22_finish_in_flight.get(photo_key) is in_flight:
                    _stage22_finish_in_flight.pop(photo_key, None)
                cached_frame = _stage22_finish_frames.pop(photo_key, None)
                if cached_frame:
                    photo_filename = await _save_photo(cached_frame)
                    if photo_filename:
                        logger.info(
                            "[PHOTO-BG] Saved stage-22 pre-captured frame: %s (%d bytes)",
                            photo_filename,
                            len(cached_frame),
                        )

            # Fallback chain: external camera → buffered live frame → fresh RTSP
            # capture. Only runs if neither the timelapse source nor the pre-captured
            # frame above produced a photo.
            if not photo_filename:
                if not _owns_printer_effect():
                    logger.info("[PHOTO-BG] printer %s changed run before camera capture", printer_id)
                    await _record_terminal_effect_stage(
                        archive_id, "finish_photo", "skipped", expected_identity=terminal_archive_identity
                    )
                    return None
                # One rule, asked once, before either camera kind (#2707). The
                # built-in branch below hand-rolls this same check by scanning
                # the stream registries; the external branch had none at all, so
                # a watched print with a USB camera opened a competing handle
                # and got nothing. Kept above both so neither can drift again.
                # A camera with its own snapshot URL is not deferred: its capture below fetches that still.
                from backend.app.api.routes.camera import live_frame_for_picture

                defer_live, buffered_live = live_frame_for_picture(printer_id, printer)
                if defer_live and buffered_live:
                    photo_filename = await _save_photo(_apply_camera_rotation(buffered_live, printer, logger))
                    if photo_filename:
                        logger.info("[PHOTO-BG] Reused the live view's frame: %s", photo_filename)
                elif defer_live:
                    logger.info(
                        "[PHOTO-BG] Viewer attached for printer %s with an empty buffer — leaving the "
                        "finish photo unset rather than opening a competing camera handle",
                        printer_id,
                    )

            if not photo_filename and not defer_live:
                # Check for external camera first
                if printer.external_camera_enabled and printer.external_camera_url:
                    logger.info("[PHOTO-BG] Using external camera")
                    from backend.app.services.camera_runtime import CameraCaptureRequest, capture

                    try:
                        camera_result = await capture(
                            CameraCaptureRequest.external(
                                url=printer.external_camera_url,
                                camera_type=printer.external_camera_type or "mjpeg",
                                snapshot_url=printer.external_camera_snapshot_url,
                                timeout=30,
                                purpose="finish_photo",
                                printer_id=printer_id,
                            )
                        )
                        frame_data = camera_result.frame
                    except Exception as exc:
                        logger.warning("[PHOTO-BG] Camera worker unavailable: %s", type(exc).__name__)
                        frame_data = None
                    if frame_data and _owns_printer_effect():
                        # Raw grab — the cached stage-22 frame above is already
                        # rotated, this one is not (#2708).
                        photo_filename = await _save_photo(_apply_camera_rotation(frame_data, printer, logger))
                        if photo_filename:
                            logger.info("[PHOTO-BG] Saved external camera frame: %s", photo_filename)
                else:
                    # Check if camera stream is active - use buffered frame to avoid freeze
                    # Check both RTSP streams (_active_streams) and chamber image streams (_active_chamber_streams)
                    buffered_frame = get_buffered_frame(printer_id)

                    if is_stream_active(printer_id) and buffered_frame:
                        # Use frame from active stream
                        logger.info("[PHOTO-BG] Using buffered frame from active stream")
                        photo_filename = await _save_photo(_apply_camera_rotation(buffered_frame, printer, logger))
                        if photo_filename:
                            logger.info("[PHOTO-BG] Saved buffered frame: %s", photo_filename)
                    else:
                        # No active stream - capture new frame
                        from backend.app.services.camera import capture_finish_frame

                        # The frame first, its folder after: the capture can take half a minute, and a
                        # 3MF attached meanwhile moves the archive's folder (Codex G1). ``_save_photo``
                        # reads the folder at the write and asks the print binding again.
                        fresh_frame = await capture_finish_frame(
                            printer_id=printer_id,
                            ip_address=printer.ip_address,
                            access_code=printer.access_code,
                            model=printer.model,
                            rotation=getattr(printer, "camera_rotation", 0) or 0,
                        )
                        if fresh_frame:
                            photo_filename = await _save_photo(fresh_frame)
                            if photo_filename:
                                logger.info("[PHOTO-BG] Saved a fresh frame: %s", photo_filename)

            # Write phase: attach the photo in a fresh short-lived session (the read
            # session was released before the capture pipeline). Re-fetch the archive
            # by id — the one loaded above is detached after its session closed.
            if photo_filename:
                attached_photo = False
                try:
                    async with async_session() as db:
                        from backend.app.models.archive import PrintArchive
                        from backend.app.services.archive_write_scope import (
                            archive_write_scope,
                            load_active_archive_for_write,
                        )

                        async with archive_write_scope(db, archive_id):
                            arch = await load_active_archive_for_write(db, archive_id)
                            if (
                                arch is not None
                                and arch.recovery_identity == terminal_archive_identity
                                and _owns_printer_effect()
                            ):
                                arch.photos = [*(arch.photos or []), photo_filename]
                                await db.commit()
                                attached_photo = True
                except BaseException as attach_error:
                    # The attach failed or was cancelled, perhaps inside its commit: whether the archive
                    # lists the photo is read afresh, and the file goes only when it does not (Codex G2).
                    # A cancel waits for that reading to end and is then passed on (Codex G2-R).
                    settled = await finish_photo_store.to_the_end(
                        finish_photo_store.settle_uncertain(
                            archive_id, terminal_archive_identity, photo_filename, session=async_session
                        )
                    )
                    if not isinstance(attach_error, Exception) or not settled:
                        raise
                    attached_photo = True
                if attached_photo:
                    finish_photo_store.confirm(photo_filename)
                if not attached_photo:
                    # Written and then refused — the archive is another incarnation, or the printer
                    # went on to another print. The file is this task's own: it does not stay as an
                    # orphan beside somebody's photos (G10 G).
                    await finish_photo_store.discard(archive_id, photo_filename, session=async_session)
                    await _record_terminal_effect_stage(
                        archive_id, "finish_photo", "skipped", expected_identity=terminal_archive_identity
                    )
                    return None
                logger.info("[PHOTO-BG] Saved: %s", photo_filename)
                await _record_terminal_effect_stage(
                    archive_id, "finish_photo", "finished", expected_identity=terminal_archive_identity
                )
                return photo_filename
            await _record_terminal_effect_stage(
                archive_id, "finish_photo", "skipped", expected_identity=terminal_archive_identity
            )
            return None
        except Exception as e:
            await _record_terminal_effect_stage(
                archive_id, "finish_photo", "failed", expected_identity=terminal_archive_identity
            )
            logger.warning("[PHOTO-BG] Failed: %s", e)
            return None

    _spawn_finish_task(_background_energy_calculation(), name="finish-energy-calc")
    # Photo capture task - result will be used by notifications
    photo_task = _spawn_finish_task(_background_finish_photo(), name="finish-photo")
    log_timing("Background tasks scheduled (energy, photo)")

    # Also run smart plug, notifications, and maintenance as background tasks
    print_status = data.get("status", "completed")

    async def _background_smart_plug():
        """Handle smart plug automation in background."""
        try:
            if not _owns_printer_effect():
                logger.info("[AUTO-OFF-BG] skipping stale completion for archive %s", archive_id)
                return
            await _record_terminal_effect_stage(
                archive_id, "smart_plug", "attempted", expected_identity=terminal_archive_identity
            )
            logger.info("[AUTO-OFF-BG] Starting smart plug automation for printer %s", printer_id)
            async with async_session() as db:
                if not _owns_printer_effect():
                    logger.info("[AUTO-OFF-BG] printer %s changed run during lookup", printer_id)
                    completed = False
                else:
                    await smart_plug_manager.on_print_complete(
                        printer_id, print_status, db, may_run=_owns_printer_effect
                    )
                    completed = True
            await _record_terminal_effect_stage(
                archive_id,
                "smart_plug",
                "finished" if completed else "skipped",
                expected_identity=terminal_archive_identity,
            )
            if completed:
                logger.info("[AUTO-OFF-BG] Completed")
        except Exception as e:
            await _record_terminal_effect_stage(
                archive_id, "smart_plug", "failed", expected_identity=terminal_archive_identity
            )
            logger.warning("[AUTO-OFF-BG] Failed: %s", e)

    async def _background_notifications(finish_photo_filename: str | None = None):
        """Send print complete notifications in background."""
        try:
            logger.info(
                "[NOTIFY-BG] Starting notifications for printer %s, photo=%s", printer_id, finish_photo_filename
            )
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive
                from backend.app.models.printer import Printer

                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                printer_name = printer.name if printer else f"Printer {printer_id}"

                archive_data = None
                if archive_id:
                    archive_result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                    archive = archive_result.scalar_one_or_none()
                    if archive and archive.recovery_identity == terminal_archive_identity:
                        # Actual elapsed from started_at/completed_at when both are
                        # populated — every terminal status sets completed_at after
                        # #1198 (cancelled joined completed/failed/aborted). When the
                        # row is missing either timestamp (rare — partially-recovered
                        # archive) we leave actual_time_seconds=None so the
                        # notification template falls back to the slicer estimate.
                        actual_time_seconds = None
                        if archive.started_at and archive.completed_at:
                            elapsed = (archive.completed_at - archive.started_at).total_seconds()
                            if elapsed > 0:
                                actual_time_seconds = int(elapsed)

                        archive_data = {
                            # ⚠️ The print the message is ABOUT. Telegram's «Брак…»
                            # button is built from this id and from nothing else —
                            # it used to be resolved by "the printer's newest
                            # completed archive", which on the no-archive path
                            # (``if not archive_id`` above) offered the operator
                            # the PREVIOUS plate's parts and wrote defects, and a
                            # ledger correction, against the wrong print.
                            "archive_id": archive.id,
                            "print_time_seconds": archive.print_time_seconds,
                            "actual_time_seconds": actual_time_seconds,
                            "actual_filament_grams": archive.filament_used_grams,
                            "failure_reason": archive.failure_reason,
                            "created_by_id": archive.created_by_id,
                        }

                        # Scale filament usage for partial prints
                        if print_status != "completed" and archive.filament_used_grams:
                            progress = data.get("progress") or 0
                            scale = max(0.0, min(progress / 100.0, 1.0))
                            archive_data["actual_filament_grams"] = round(archive.filament_used_grams * scale, 1)
                            archive_data["progress"] = progress

                        # Pass per-slot data from archive.extra_data
                        if archive.extra_data and archive.extra_data.get("filament_slots"):
                            slots = archive.extra_data["filament_slots"]
                            if print_status != "completed":
                                scale = max(0.0, min((data.get("progress") or 0) / 100.0, 1.0))
                                slots = [{**s, "used_g": round(s["used_g"] * scale, 1)} for s in slots]
                            archive_data["filament_slots"] = slots

                        # Pass usage tracker results for AMS slot info in notifications
                        if usage_results:
                            archive_data["usage_results"] = usage_results
                        # Add finish photo URL and image bytes if available
                        if finish_photo_filename:
                            # Read finish photo bytes for image attachment (e.g. Pushover)
                            # and for the link: {finish_photo_url} leads to a short-lived
                            # copy with an unguessable name, never to the archive's own
                            # photo, which is behind the media token like its other
                            # pictures (owner, 2026-11-17).
                            try:
                                from backend.app.utils.archive_paths import find_photo

                                # Wherever it was written: the 3MF may have been
                                # attached since the photo was taken, and that
                                # moves the archive's folder.
                                photo_path = find_photo(archive, finish_photo_filename)
                                if photo_path is not None:
                                    photo_bytes = await asyncio.to_thread(photo_path.read_bytes)
                                    photo_link = await notification_service.photo_link(
                                        db, photo_bytes, "print_complete", absolute_only=False
                                    )
                                    if photo_link:
                                        archive_data["finish_photo_url"] = photo_link
                                    if len(photo_bytes) <= 2_500_000:
                                        archive_data["image_data"] = photo_bytes
                                        logger.info("[NOTIFY-BG] Loaded finish photo bytes: %s bytes", len(photo_bytes))
                                    else:
                                        logger.warning(
                                            f"[NOTIFY-BG] Finish photo too large for attachment: "
                                            f"{len(photo_bytes)} bytes"
                                        )
                            except Exception as e:
                                logger.warning("[NOTIFY-BG] Failed to read finish photo bytes: %s", e)

                await notification_service.on_print_complete(
                    printer_id, printer_name, print_status, data, db, archive_data=archive_data
                )

                # Send user-specific email notification
                if archive_data:
                    created_by_id = archive_data.get("created_by_id")
                    raw_filename = data.get("subtask_name") or data.get("filename", "Unknown")
                    await _dispatch_user_print_email(
                        print_status,
                        created_by_id,
                        printer_name,
                        raw_filename,
                        db,
                        image_data=archive_data.get("image_data"),
                        finish_photo_url=archive_data.get("finish_photo_url"),
                    )

                logger.info("[NOTIFY-BG] Completed")
        except Exception as e:
            logger.error("[NOTIFY-BG] Failed: %s", e, exc_info=True)

    async def _background_maintenance_check():
        """Check for maintenance due in background."""
        if print_status != "completed":
            return
        try:
            logger.info("[MAINT-BG] Starting maintenance check for printer %s", printer_id)
            async with async_session() as db:
                from backend.app.models.printer import Printer

                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                printer_name = printer.name if printer else f"Printer {printer_id}"

                await ensure_default_types(db)
                overview = await _get_printer_maintenance_internal(printer_id, db, commit=True)

                items_needing_attention = [
                    {
                        "id": item.id,
                        "name": item.maintenance_type_name,
                        "is_due": item.is_due,
                        "is_warning": item.is_warning,
                    }
                    for item in overview.maintenance_items
                    if item.enabled and (item.is_due or item.is_warning)
                ]

                if items_needing_attention:
                    await notification_service.on_maintenance_due(printer_id, printer_name, items_needing_attention, db)
                    logger.info("[MAINT-BG] Sent notification: %s items need attention", len(items_needing_attention))

                    # MQTT relay - publish maintenance alerts
                    await mqtt_relay.on_maintenance_items_due(
                        printer_id,
                        printer_name,
                        [i for i in overview.maintenance_items if i.enabled and (i.is_due or i.is_warning)],
                    )
                else:
                    logger.info("[MAINT-BG] Completed (no items need attention)")
        except Exception as e:
            logger.warning("[MAINT-BG] Failed: %s", e)

    _spawn_finish_task(_background_smart_plug(), name="finish-smart-plug")
    _spawn_finish_task(_background_maintenance_check(), name="finish-maintenance-check")

    # Notification task waits for photo capture to complete first (with timeout).
    # When a timelapse was recording, photo sourcing polls the per-print
    # timelapse for up to 60s (#1397) — extend the budget so the notification
    # carries the correct bed-up photo instead of falling through to the
    # live-cam grab. Adds ~30s of notification latency at worst on slow links.
    photo_wait_timeout = 75 if data.get("timelapse_was_active") else 45

    async def _photo_then_notify():
        """Wait for photo capture, then send notification with photo URL."""
        finish_photo = None
        try:
            finish_photo = await asyncio.wait_for(photo_task, timeout=photo_wait_timeout)
            logger.info("[PHOTO-NOTIFY] Photo task returned: %s", finish_photo)
        except TimeoutError:
            logger.warning(
                "[PHOTO-NOTIFY] Photo capture timed out after %ss, sending notification without photo",
                photo_wait_timeout,
            )
        except Exception as e:
            logger.warning("[PHOTO-NOTIFY] Photo task failed: %s", e)
        try:
            await _background_notifications(finish_photo)
        except Exception as e:
            logger.error("[PHOTO-NOTIFY] Notification sending failed: %s", e, exc_info=True)

    _spawn_finish_task(_photo_then_notify(), name="finish-photo-notify")

    # Stitch external camera layer timelapse if session was active
    print_status = data.get("status", "completed")

    async def _background_layer_timelapse():
        """Stitch layer timelapse and attach to archive."""
        from backend.app.services.layer_timelapse import finish_session, retain_session

        try:
            if layer_session is None:
                return
            if print_status == "completed":
                logger.info("[LAYER-TL] Stitching layer timelapse for printer %s", printer_id)
                # Before the stitch: the video this result may replace (G10 C). A
                # recording attached while ffmpeg ran is not overwritten by it.
                layer_started_from = None
                if archive_id:
                    from backend.app.models.archive import PrintArchive
                    from backend.app.services import archive_media

                    async with async_session() as db:
                        layer_archive = await db.get(PrintArchive, archive_id)
                        if layer_archive is not None and (
                            terminal_archive_identity is None
                            or layer_archive.recovery_identity == terminal_archive_identity
                        ):
                            layer_started_from = await archive_media.primary_token(db, layer_archive)
                # The frames stay until the video is published (G10 F): a stitch that fails, or a
                # result the archive refuses, leaves them retained for a manual recovery.
                timelapse_path = await finish_session(layer_session, keep_frames=True)
                if timelapse_path and archive_id:
                    logger.info("[LAYER-TL] Attaching timelapse %s to archive %s", timelapse_path, archive_id)
                    async with async_session() as db:
                        service = ArchiveService(db)
                        # As a file: a long high-resolution video is never read into memory.
                        attached = await service.attach_timelapse_file(
                            archive_id,
                            timelapse_path,
                            "layer_timelapse.mp4",
                            expected_identity=terminal_archive_identity,
                            expected_epoch=completion_epoch,
                            expected_token=layer_started_from,
                            origin="layer",
                        )
                        if attached:
                            layer_session.cleanup()
                            logger.info("[LAYER-TL] Layer timelapse attached successfully")
                            # Open Archives pages learn of it, as they do for a printer's recording.
                            await ws_manager.send_archive_updated({"id": archive_id, "timelapse_attached": True})
                        else:
                            # Counted against the retention bounds at once; disk work, so off the loop and
                            # counted until its thread ends (a restore's drain sees it).
                            await retain_session(layer_session, f"not_published_{service.media_refusal or 'unknown'}")
                            logger.info(
                                "[LAYER-TL] Layer timelapse was not attached to archive %s (%s); its frames are kept",
                                archive_id,
                                service.media_refusal,
                            )
                elif timelapse_path:
                    # Timelapse created but no archive - just clean up
                    await asyncio.to_thread(timelapse_path.unlink, missing_ok=True)
                    layer_session.cleanup()
            else:
                # Print failed or cancelled - cancel timelapse session
                layer_session.cleanup()
                logger.info(
                    "[LAYER-TL] Cancelled layer timelapse for printer %s (status: %s)", printer_id, print_status
                )
        except Exception as e:
            logger.warning("[LAYER-TL] Failed: %s", e)
            # An error after the frames were captured keeps them for a recovery; a print that
            # did not complete has no video to recover.
            try:
                if print_status == "completed":
                    layer_session.retain("finish_error")
                else:
                    layer_session.cleanup()
            except Exception:
                pass  # Best-effort: the startup janitor settles what is left

    _spawn_finish_task(_background_layer_timelapse(), name="finish-layer-timelapse")

    log_timing("All background tasks scheduled")

    # The printer can finalize a partial recording after a failure or cancellation.
    # A superseded terminal must leave the next run's in-memory baseline alone.
    if (
        archive_id
        and terminal_archive_identity
        and data.get("timelapse_was_active")
        and data.get("status") in {"completed", "failed", "cancelled", "aborted"}
        and _owns_printer_effect()
    ):
        logger.info("[TIMELAPSE] Timelapse was active during print, scheduling auto-scan for archive %s", archive_id)
        # Schedule timelapse scan as background task with retries
        # The printer needs time to encode the video after print completion
        baseline, baseline_trusted, baseline_media = _take_timelapse_baseline(
            printer_id, archive_identity=terminal_archive_identity
        )
        spawn_background_task(
            _scan_for_timelapse_with_retries(
                archive_id,
                baseline,
                baseline_trusted=baseline_trusted,
                baseline_media=baseline_media,
                expected_identity=terminal_archive_identity,
                expected_epoch=completion_epoch,
                # A newer print on this printer ends the scan (G10 C3).
                may_run=_owns_printer_effect,
            ),
            name=f"timelapse-scan-{archive_id}",
        )
        log_timing("Timelapse scan scheduled")

    # (The plate-clear gate is armed/released ABOVE, before the queue-item
    # update releases the printer's queue claim — see the comment there.)
    logger.info("[CALLBACK] on_print_complete finished for printer %s, archive %s", printer_id, archive_id)
    analysis_completion["return_body_completed"] = True


# AMS sensor history recording
_ams_history_task: asyncio.Task | None = None
AMS_HISTORY_INTERVAL = 300  # Record every 5 minutes
AMS_HISTORY_RETENTION_DAYS = 30  # Keep data for 30 days


def _retention_days(raw, default: int) -> int:
    """A stored retention as whole days, never less than one.

    Zero or a negative number puts the cutoff at or after now, and the daily
    sweep then deletes the whole history; a value that is not a number used to
    stop the sweep with an error. Either reads as the default instead.
    """
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return days if days >= 1 else default


INBOX_RETENTION_DAYS = 30  # in-app inbox rows; spec: notification-center §4.4
_ams_cleanup_counter = 0  # Track recordings to trigger periodic cleanup
# (printer_id, ams_id) already reported as sending the drop index and no
# percentage. Logged once each so a printer that does this shows up in a
# support bundle rather than as a missing humidity reading (upstream #3140).
_ams_index_only_logged: set[tuple[int, int]] = set()
# Track alarm cooldowns (printer_id:ams_id:type -> last_alarm_time)
_ams_alarm_cooldown: dict[str, datetime] = {}
AMS_ALARM_COOLDOWN_MINUTES = 60  # Don't send same alarm more than once per hour

# Per-AMS "drying was live at" latch that holds back the high-temperature alarm
# through a cycle and the cool-down after it (upstream #1802). A settings row,
# not a dict beside ``_ams_alarm_cooldown``: a cycle plus its cool-down outlasts
# a restart, and a restart partway through would resume alarming about heat the
# user asked for.
AMS_DRYING_LATCH_KEY = "ams_drying_alarm_latch"
# Upper bound on that suppression. The latch normally clears as soon as the unit
# reads at or below the alarm threshold (see ``utils/ams_drying``); the cap only
# matters for a unit that never does.
AMS_DRYING_GRACE_MINUTES = 120


def _resolve_temp_alarm_threshold(fair_threshold: float, raw_alarm_value: str | None) -> float:
    """Temperature at which the AMS alarm fires, falling back to the display band.

    ``ams_temp_fair`` decides when the AMS card turns amber; it used to decide
    when a notification was sent as well, so a room above it made the alarm fire
    once an hour for as long as the weather lasted — and the only cure was to
    raise the display band and lose the colour that says the unit is warm
    (upstream #2905). ``ams_temp_alarm`` is its own threshold now.

    Unset resolves to the fair threshold, so an install that never sets one is
    unchanged. Settings storage stringifies ``None`` to ``"None"``, which lands in
    the same branch as any unparseable value. Zero, negatives, NaN and infinity
    are refused: zero would alarm permanently (more likely a cleared field than a
    choice), and nothing is ever greater than NaN, which would silence the alarm
    while looking configured.
    """
    import math

    if raw_alarm_value is None:
        return fair_threshold
    try:
        value = float(raw_alarm_value)
    except (TypeError, ValueError):
        return fair_threshold
    if not math.isfinite(value) or value <= 0:
        return fair_threshold
    return value


async def _load_ams_drying_latch(db) -> dict[str, datetime]:
    """Read the persisted per-AMS drying latch, dropping entries out of window.

    Anything older than the grace cap would expire on its next visit anyway, so
    dropping it here stops rows for deleted printers accumulating. Stamps ahead
    of now (a box whose clock jumped backwards — a Pi with no RTC before NTP)
    are discarded when wildly future and clamped to now otherwise: suppression is
    measured as now minus the stamp, so a future one would hold the alarm quiet
    for the skew on top of the cap. A corrupt row reads as no latch.
    """
    import json

    from backend.app.models.settings import Settings

    setting = (await db.execute(select(Settings).where(Settings.key == AMS_DRYING_LATCH_KEY))).scalar_one_or_none()
    if not setting or not setting.value:
        return {}
    try:
        raw = json.loads(setting.value)
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    now = datetime.now(timezone.utc)
    window = timedelta(minutes=AMS_DRYING_GRACE_MINUTES)
    latch: dict[str, datetime] = {}
    for key, value in raw.items():
        try:
            stamp = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if not (now - window <= stamp <= now + window):
            continue
        latch[str(key)] = min(stamp, now)
    return latch


async def _save_ams_drying_latch(db, latch: dict[str, datetime]) -> None:
    """Persist the latch, writing only when it changed.

    Adds to the session without committing — the caller's commit carries it, in
    the same transaction as the sensor rows that produced it. No row is created
    on an install that never dries anything.
    """
    import json

    from backend.app.models.settings import Settings

    payload = json.dumps({key: stamp.isoformat() for key, stamp in sorted(latch.items())})
    setting = (await db.execute(select(Settings).where(Settings.key == AMS_DRYING_LATCH_KEY))).scalar_one_or_none()
    if setting is None:
        if payload != "{}":
            db.add(Settings(key=AMS_DRYING_LATCH_KEY, value=payload))
    elif setting.value != payload:
        setting.value = payload


async def _release_unseeded_journal(printer_id: int) -> None:
    """A print that ends while its journal still waits for the restart seed (post-rebuild F-R4).

    The completion must not read a chronology the seed never restored — the empty or old one
    would charge the whole print to the tray it named last. The hold ends here, before anything
    reads the chronology: the feed is stated unknown until the first held report. Whatever the
    handoff left — the released reports, a replay whose write failed — is written here, under
    the journal lock, while the archive is still the printing one (post-rebuild F3). Never
    raises but for cancellation.
    """
    try:
        from backend.app.services.print_usage_journal import journal_lock, settle_pending_replay

        async with journal_lock(printer_id):  # entering it writes a replay still waiting
            client = printer_manager.get_client(printer_id)
            if client is not None and client.journal_held() is True:
                if await _abandon_held_journal_locked(printer_id, client.journal_generation()):
                    logging.getLogger(__name__).warning(
                        "Printer %s completed before its usage journal was seeded; its feed before the first "
                        "observed tray report is recorded as unknown",
                        printer_id,
                    )
            # What the completion reads never misses a replay: written, or stated unknown (F4).
            await settle_pending_replay(printer_id)
    except Exception:  # noqa: BLE001 - the completion goes on; its attribution stays as observed
        logging.getLogger(__name__).exception("Could not release the unseeded usage journal of printer %s", printer_id)


async def on_print_complete(printer_id: int, data: dict) -> None:
    """Register completion consumers, then release their exact resources.

    The wrapper closes registration in finally. Cleanup waits for consumers,
    independently of the token that fences actions against newer print runs.

    The Spoolman accounting window (C047 S15) opens here, before the first await,
    and closes after the last Spoolman writer: a delete of a Spoolman spool
    refuses while it is open and admits only if none came and went meanwhile.
    """
    from backend.app.services import spoolman_activity

    spoolman_activity.enter_window()
    try:
        await _on_print_complete_windowed(printer_id, data)
    finally:
        spoolman_activity.leave_window()


async def _on_print_complete_windowed(printer_id: int, data: dict) -> None:
    from backend.app.services.octoeverywhere_detection import octoeverywhere_detection_service
    from backend.app.services.print_completion_tasks import PrintCompletionTasks

    # The print is over: OctoEverywhere drops its context and any inspection in flight (upstream Bambuddy).
    octoeverywhere_detection_service.reset_printer(printer_id)
    try:
        notify_live_activities.print_finished(printer_id, data)
    except Exception:
        logging.getLogger(__name__).exception("Notify Live Activity completion hook failed for printer %s", printer_id)
    scope = PrintCompletionTasks(printer_id)
    analysis_completion = {"archive_id": None, "tasks": scope}
    from backend.app.services.print_reconciliation import defer_queue_repair_during_completion

    await _release_unseeded_journal(printer_id)

    # From here until the callback ends -- the swap macro and the Spoolman report included -- the finished
    # print's filament is not booked yet, so the remain% weight sync stays off for this printer.
    from backend.app.services.usage_tracker import completion_in_progress

    with defer_queue_repair_during_completion(printer_id), completion_in_progress(printer_id):
        try:
            await _on_print_complete_impl(printer_id, data, analysis_completion)
        finally:
            archive_id = analysis_completion["archive_id"]

            def release_completion():
                if archive_id is None:
                    return
                from backend.app.services.print_file_analysis import discard_print_file_analysis
                from backend.app.services.print_run_binding import discard_print_run

                discard_print_file_analysis(
                    printer_manager,
                    printer_id,
                    archive_id,
                    expected_identity=analysis_completion.get("archive_identity"),
                )
                discard_print_run(
                    printer_manager,
                    printer_id,
                    archive_id,
                    sequence=analysis_completion.get("run_sequence"),
                    expected_identity=analysis_completion.get("archive_identity"),
                )
                # Aliases are recovery aids, never a second lifecycle owner.
                # A failure after bound acceptance used to leave A's names in
                # RAM until a later callback happened to remove them; exact
                # archive cleanup cannot touch a same-named B.
                _forget_active_print(archive_id, analysis_completion.get("archive_identity"))

            scope.close(release_completion)

    # This print started after the printer's earlier finished rows ended, so a row a held swap kept (and its
    # router row) can never settle now. Retired after the completion, in its own sessions, awaited rather than
    # spawned so no task outlives the callback (queue-leftovers Q3). Never raises.
    from backend.app.services.queue_leftovers import retire_for_printer

    retirement = await retire_for_printer(printer_id)

    candidate = analysis_completion.get("return_candidate")
    if (
        candidate is not None
        and analysis_completion.get("return_archive_finalized") is True
        and analysis_completion.get("return_body_completed") is True
        and analysis_completion.get("return_critical_failed") is not True
        and getattr(retirement, "failed", False) is not True
    ):
        from backend.app.services.auto_queue_return_runtime import auto_queue_return_runtime

        # Normal-success tail only. The separate runtime retains ownership if
        # this callback is cancelled while source verification or SQL finishes.
        await auto_queue_return_runtime.publish(candidate, manager=printer_manager)


def _ams_has_filament(ams_data: dict) -> bool:
    """True if this AMS unit has at least one tray slot holding filament.

    Bambu firmware reports loaded slots via ``tray_exist_bits``, a per-AMS hex
    bitmap (one bit per tray slot — bit set = spool present). Empty AMS units
    still report sensor readings, but those readings are ambient and not
    actionable: no filament to dry, no humidity to push down. #1619 — gate
    humidity / temperature alarms on this check so empty units don't generate
    hourly noise. Sensor history still records regardless so the UI charts stay
    continuous.

    Fallback path inspects the ``tray`` array's ``tray_type`` fields for setups
    where ``tray_exist_bits`` is missing (some early-connection pushall shapes).
    """
    bits = ams_data.get("tray_exist_bits")
    if isinstance(bits, str) and bits.strip():
        try:
            return int(bits, 16) > 0
        except ValueError:
            pass
    trays = ams_data.get("tray")
    if isinstance(trays, list):
        return any(
            isinstance(t, dict) and isinstance(t.get("tray_type"), str) and t["tray_type"].strip() for t in trays
        )
    return False


async def record_ams_history():
    """Background task to record AMS humidity and temperature data."""
    logger = logging.getLogger(__name__)

    # Wait a short time for MQTT connections to establish on startup
    await asyncio.sleep(10)

    while True:
        try:
            from backend.app.models.ams_history import AMSSensorHistory
            from backend.app.models.printer import Printer
            from backend.app.models.settings import Settings

            async with async_session() as db:
                # Get all active printers
                result = await db.execute(
                    select(Printer).where(Printer.is_active.is_(True)).where(Printer.archived.is_(False))
                )
                printers = result.scalars().all()

                # Get alarm thresholds from settings
                humidity_threshold = 60.0  # Default: fair threshold
                temp_fair_threshold = 35.0  # Display band default (ams_temp_fair)
                result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_fair"))
                setting = result.scalar_one_or_none()
                if setting:
                    try:
                        humidity_threshold = float(setting.value)
                    except (ValueError, TypeError):
                        pass  # Keep default threshold if stored value is invalid

                # Per-filament humidity threshold overrides (#1605) — resolved
                # per-AMS below from the loaded tray types. Reuses the same
                # resolver as the auto-drying scheduler so behavior stays in
                # lockstep across both consumers.
                import json

                from backend.app.services.print_scheduler import PrintScheduler

                per_type_humidity_thresholds: dict[str, int] = {}
                result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_thresholds"))
                setting = result.scalar_one_or_none()
                if setting and setting.value:
                    try:
                        raw = json.loads(setting.value)
                        if isinstance(raw, dict):
                            for k, v in raw.items():
                                try:
                                    per_type_humidity_thresholds[str(k).upper() if k != "default" else "default"] = int(
                                        v
                                    )
                                except (TypeError, ValueError):
                                    continue
                    except (ValueError, TypeError):
                        pass  # Invalid JSON → no overrides, fall through to global threshold
                result = await db.execute(select(Settings).where(Settings.key == "ams_temp_fair"))
                setting = result.scalar_one_or_none()
                if setting:
                    try:
                        temp_fair_threshold = float(setting.value)
                    except (ValueError, TypeError):
                        pass  # Keep default threshold if stored value is invalid
                # The alarm has its own threshold (upstream #2905), seeded from
                # the display band so an install that never set one is unchanged.
                result = await db.execute(select(Settings).where(Settings.key == "ams_temp_alarm"))
                setting = result.scalar_one_or_none()
                temp_alarm_threshold = _resolve_temp_alarm_threshold(
                    temp_fair_threshold, setting.value if setting else None
                )

                # Per-AMS drying latch (upstream #1802), loaded once per pass and
                # written back below only if a unit changed it.
                from backend.app.utils.ams_drying import is_drying_active, temperature_alarm_suppressed

                drying_latch = await _load_ams_drying_latch(db)
                drying_latch_before = dict(drying_latch)

                recorded_count = 0
                for printer in printers:
                    # Get current state from printer manager
                    state = printer_manager.get_status(printer.id)
                    if not state or not state.connected or not state.raw_data:
                        continue  # Skip disconnected printers - don't use stale data

                    raw_data = state.raw_data
                    if "ams" not in raw_data or not isinstance(raw_data["ams"], list):
                        continue

                    # Record data for each AMS unit
                    for ams_data in raw_data["ams"]:
                        ams_id = int(ams_data.get("id", 0))

                        # Percentage only. The 1-5 index is inverted, so
                        # charting it as a percentage drew the wettest units as
                        # the driest (#3140); a unit that reports no percentage
                        # leaves a gap in the chart instead. See
                        # utils/ams_humidity.
                        humidity = ams_humidity_percent(ams_data)

                        # No supported printer is known to send the index
                        # alone -- the report came from unsupported firmware,
                        # and no install has been seen using the old fallback.
                        # "Known" is doing work there, so say so once per unit:
                        # the alternative is a silent blank card.
                        if humidity is None and ams_data.get("humidity") is not None:
                            unit_key = (printer.id, ams_id)
                            if unit_key not in _ams_index_only_logged:
                                _ams_index_only_logged.add(unit_key)
                                logger.info(
                                    "[%s] AMS %d reports the 1-5 humidity index but no usable humidity_raw "
                                    "percentage. The index is inverted and is not shown as a percentage "
                                    "(#3140), so this unit has no humidity reading, chart or alarm. "
                                    "Please report this with the printer and AMS firmware versions.",
                                    printer.name,
                                    ams_id,
                                )

                        # Get temperature
                        temperature = None
                        temp_str = ams_data.get("temp")
                        if temp_str is not None:
                            try:
                                temperature = float(temp_str)
                            except (ValueError, TypeError):
                                pass  # Skip unparseable temperature value

                        # Skip if no data
                        if humidity is None and temperature is None:
                            continue

                        # Record the data point
                        history = AMSSensorHistory(
                            printer_id=printer.id,
                            ams_id=ams_id,
                            humidity=humidity,
                            # Both columns hold the same reading now that the
                            # index can no longer reach ``humidity``. Writing it
                            # through the same value also stops a genuine 0%
                            # from being stored as NULL, which the old truthiness
                            # test did.
                            humidity_raw=humidity,
                            temperature=temperature,
                        )
                        db.add(history)
                        recorded_count += 1

                        # Skip alarm dispatch for empty AMS units — humidity /
                        # temperature readings are ambient with no filament to
                        # protect, and the hourly notification just becomes
                        # noise. Sensor history was already recorded above so the
                        # UI charts stay continuous (#1619). Per-AMS check so a
                        # multi-AMS setup with one loaded + one empty still alarms
                        # on the loaded unit.
                        if not _ams_has_filament(ams_data):
                            continue

                        # Generate AMS label and determine if it's AMS-HT (A, B, C, D or HT-A for AMS-Lite/Hub;
                        # AMS-3H / AMS-3P for a rack AMS) — utils/tray_ids.unit_name.
                        unit_pair = tray_ids.unit_type(ams_data)
                        unit_kind = unit_pair[1] if unit_pair else None
                        is_ams_ht = tray_ids.ams_type_of(ams_id, unit_kind) == tray_ids.AmsType.N3S
                        ams_label = tray_ids.unit_name(ams_id, unit_kind)

                        # Resolve per-filament humidity threshold for this AMS
                        # unit (#1605). Falls back to the global ams_humidity_fair
                        # when no per-type overrides are configured.
                        trays = ams_data.get("tray", []) or []
                        effective_humidity_threshold = float(
                            PrintScheduler.resolve_humidity_threshold(
                                trays, per_type_humidity_thresholds, int(humidity_threshold)
                            )
                        )

                        # Check humidity alarm (only if above threshold)
                        if humidity is not None and humidity > effective_humidity_threshold:
                            cooldown_key = f"{printer.id}:{ams_id}:humidity"
                            last_alarm = _ams_alarm_cooldown.get(cooldown_key)
                            now = datetime.now(timezone.utc)
                            if (
                                last_alarm is None
                                or (now - last_alarm).total_seconds() >= AMS_ALARM_COOLDOWN_MINUTES * 60
                            ):
                                _ams_alarm_cooldown[cooldown_key] = now
                                logger.info(
                                    f"Sending humidity alarm for {printer.name} {ams_label}: {humidity}% > {effective_humidity_threshold}%"
                                )
                                try:
                                    # Call different notification method based on AMS type
                                    if is_ams_ht:
                                        await notification_service.on_ams_ht_humidity_high(
                                            printer.id,
                                            printer.name,
                                            ams_label,
                                            humidity,
                                            effective_humidity_threshold,
                                            db,
                                        )
                                    else:
                                        await notification_service.on_ams_humidity_high(
                                            printer.id,
                                            printer.name,
                                            ams_label,
                                            humidity,
                                            effective_humidity_threshold,
                                            db,
                                        )
                                except Exception as e:
                                    logger.warning("Failed to send humidity alarm: %s", e)

                        # A drying cycle heats the unit far past the threshold on
                        # purpose (45 °C for PLA, 65 °C for PETG, up to 85 °C on an
                        # AMS-HT), so the alarm fired every hour of the cycle and on
                        # through the cool-down (upstream #1802). Latch on the
                        # firmware's own drying state and hold until the reading is
                        # back at the ALARM threshold — the display band would strand
                        # the latch on a unit that rests above it in a warm room.
                        # Humidity is left alone: it falls during drying, which is
                        # the whole point.
                        latch_key = f"{printer.id}:{ams_id}"
                        suppress_temp_alarm, new_latch = temperature_alarm_suppressed(
                            drying_active=is_drying_active(ams_data),
                            temperature=temperature,
                            threshold=temp_alarm_threshold,
                            latched_at=drying_latch.get(latch_key),
                            now=datetime.now(timezone.utc),
                            grace_minutes=AMS_DRYING_GRACE_MINUTES,
                        )
                        if new_latch is None:
                            drying_latch.pop(latch_key, None)
                        else:
                            drying_latch[latch_key] = new_latch

                        # Check temperature alarm (only if above threshold)
                        if temperature is not None and temperature > temp_alarm_threshold and not suppress_temp_alarm:
                            cooldown_key = f"{printer.id}:{ams_id}:temperature"
                            last_alarm = _ams_alarm_cooldown.get(cooldown_key)
                            now = datetime.now(timezone.utc)
                            if (
                                last_alarm is None
                                or (now - last_alarm).total_seconds() >= AMS_ALARM_COOLDOWN_MINUTES * 60
                            ):
                                _ams_alarm_cooldown[cooldown_key] = now
                                logger.info(
                                    f"Sending temperature alarm for {printer.name} {ams_label}: "
                                    f"{temperature}°C > {temp_alarm_threshold}°C"
                                )
                                try:
                                    # Call different notification method based on AMS type
                                    # The message quotes the threshold that fired, or it
                                    # would say "> 35 °C" while firing at 45.
                                    if is_ams_ht:
                                        await notification_service.on_ams_ht_temperature_high(
                                            printer.id, printer.name, ams_label, temperature, temp_alarm_threshold, db
                                        )
                                    else:
                                        await notification_service.on_ams_temperature_high(
                                            printer.id, printer.name, ams_label, temperature, temp_alarm_threshold, db
                                        )
                                except Exception as e:
                                    logger.warning("Failed to send temperature alarm: %s", e)

                if drying_latch != drying_latch_before:
                    await _save_ams_drying_latch(db, drying_latch)

                await db.commit()
                if recorded_count > 0:
                    logger.info("Recorded %s AMS sensor history entries", recorded_count)

                # Periodic cleanup of old data (every ~288 recordings = ~24 hours at 5min interval)
                global _ams_cleanup_counter
                _ams_cleanup_counter += 1
                if _ams_cleanup_counter >= 288:
                    _ams_cleanup_counter = 0
                    # Get retention days from settings
                    from backend.app.models.settings import Settings

                    result = await db.execute(select(Settings).where(Settings.key == "ams_history_retention_days"))
                    setting = result.scalar_one_or_none()
                    retention_days = _retention_days(setting.value if setting else None, AMS_HISTORY_RETENTION_DAYS)

                    cutoff = datetime.utcnow() - timedelta(days=retention_days)
                    result = await db.execute(delete(AMSSensorHistory).where(AMSSensorHistory.recorded_at < cutoff))
                    await db.commit()
                    if result.rowcount > 0:
                        logger.info(
                            f"Cleaned up {result.rowcount} old AMS sensor history entries (older than {retention_days} days)"
                        )

                    # Usage-journal retention rides the same daily tick. The
                    # sweep itself refuses rows of a still-printing archive.
                    try:
                        from backend.app.services.print_usage_journal import prune_finished

                        result = await db.execute(
                            select(Settings).where(Settings.key == "usage_events_retention_hours")
                        )
                        setting = result.scalar_one_or_none()
                        # A stored 0 (a restore, a row written past the settings door) would
                        # prune every finished print's journal; the door's own floor is 1.
                        try:
                            retention_hours = max(1, int(setting.value)) if setting else 72
                        except (TypeError, ValueError):
                            retention_hours = 72
                        await prune_finished(db, retention_hours)
                    except Exception as e:
                        logger.warning("Usage-journal retention sweep failed: %s", e)

                    # The inbox rides the same daily tick (spec: notification-center §4.4).
                    # Its own try/except, like the sweep above: one failing sweep
                    # must not take the other down.
                    try:
                        from backend.app.services.notification_inbox import prune_older_than

                        result = await db.execute(select(Settings).where(Settings.key == "inbox_retention_days"))
                        setting = result.scalar_one_or_none()
                        inbox_days = int(setting.value) if setting else INBOX_RETENTION_DAYS
                        pruned = await prune_older_than(db, inbox_days)
                        if pruned:
                            logger.info("Cleaned up %d inbox notifications older than %d days", pruned, inbox_days)
                    except Exception as e:
                        logger.warning("Inbox retention sweep failed: %s", e)

            # Wait until next recording interval
            await asyncio.sleep(AMS_HISTORY_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("AMS history recording failed: %s", e)
            await asyncio.sleep(60)  # Wait a bit before retrying


_filament_preset_sync_task = None
_family_backfill_task = None


def start_filament_preset_sync():
    """Start the cloud preset-mirror sync loop (spec A §3)."""
    global _filament_preset_sync_task
    if _filament_preset_sync_task is None:
        from backend.app.services.filament_preset_sync import filament_preset_sync_loop

        _filament_preset_sync_task = asyncio.create_task(filament_preset_sync_loop())
        logging.getLogger(__name__).info("Filament preset sync loop started")


def stop_filament_preset_sync():
    """Stop the cloud preset-mirror sync loop (and the one-shot backfill)."""
    global _filament_preset_sync_task, _family_backfill_task
    if _filament_preset_sync_task:
        _filament_preset_sync_task.cancel()
        _filament_preset_sync_task = None
    if _family_backfill_task:
        _family_backfill_task.cancel()
        _family_backfill_task = None


def start_ams_history_recording():
    """Start the AMS history recording background task."""
    global _ams_history_task
    if _ams_history_task is None:
        _ams_history_task = asyncio.create_task(record_ams_history())
        logging.getLogger(__name__).info("AMS history recording started")


def stop_ams_history_recording():
    """Stop the AMS history recording background task."""
    global _ams_history_task
    if _ams_history_task:
        _ams_history_task.cancel()
        _ams_history_task = None
        logging.getLogger(__name__).info("AMS history recording stopped")


# Printer heater (nozzle / bed / chamber) sensor history recording
_printer_sensor_history_task: asyncio.Task | None = None
PRINTER_SENSOR_HISTORY_INTERVAL = 60  # Record every minute — heaters move faster than AMS humidity
PRINTER_SENSOR_HISTORY_RETENTION_DAYS = 30
_printer_sensor_cleanup_counter = 0
# Sensor kinds tracked in state.temperatures — these are the normalised keys the
# MQTT parser writes, so we don't need to handle per-model field aliases here
# (nozzle_temper / left_nozzle_temper / right_nozzle_temper / chamber_temper
# are all collapsed by services/bambu_mqtt.py before they reach this loop).
_SENSOR_KINDS = ("nozzle", "nozzle_2", "bed", "chamber")
_SENSOR_TARGET_KEYS = {
    "nozzle": "nozzle_target",
    "nozzle_2": "nozzle_2_target",
    "bed": "bed_target",
    "chamber": "chamber_target",
}


async def record_printer_sensor_history():
    """Background task to record nozzle / bed / chamber readings.

    Pulls from ``state.temperatures`` (already normalised across all printer
    models by the MQTT parser) rather than re-parsing raw_data, so we get
    free coverage of dual-nozzle H2D, sensor-only X1C chamber, etc.
    """
    logger = logging.getLogger(__name__)

    await asyncio.sleep(10)

    while True:
        try:
            from backend.app.models.printer import Printer
            from backend.app.models.printer_sensor_history import PrinterSensorHistory
            from backend.app.models.settings import Settings

            async with async_session() as db:
                result = await db.execute(
                    select(Printer).where(Printer.is_active.is_(True)).where(Printer.archived.is_(False))
                )
                printers = result.scalars().all()

                recorded_count = 0
                for printer in printers:
                    state = printer_manager.get_status(printer.id)
                    if not state or not state.connected:
                        continue

                    temps = getattr(state, "temperatures", None) or {}
                    if not isinstance(temps, dict):
                        continue

                    for kind in _SENSOR_KINDS:
                        if kind not in temps:
                            continue
                        try:
                            value = float(temps[kind])
                        except (ValueError, TypeError):
                            continue

                        target_raw = temps.get(_SENSOR_TARGET_KEYS[kind])
                        target_val: float | None = None
                        if target_raw is not None:
                            try:
                                target_val = float(target_raw)
                            except (ValueError, TypeError):
                                target_val = None

                        db.add(
                            PrinterSensorHistory(
                                printer_id=printer.id,
                                sensor_kind=kind,
                                value=value,
                                target=target_val,
                            )
                        )
                        recorded_count += 1

                await db.commit()
                if recorded_count > 0:
                    logger.debug("Recorded %s printer sensor history entries", recorded_count)

                # Periodic cleanup — once every ~24h at this interval.
                global _printer_sensor_cleanup_counter
                _printer_sensor_cleanup_counter += 1
                cleanup_every = max(1, (24 * 60 * 60) // PRINTER_SENSOR_HISTORY_INTERVAL)
                if _printer_sensor_cleanup_counter >= cleanup_every:
                    _printer_sensor_cleanup_counter = 0
                    result = await db.execute(
                        select(Settings).where(Settings.key == "printer_sensor_history_retention_days")
                    )
                    setting = result.scalar_one_or_none()
                    retention_days = _retention_days(
                        setting.value if setting else None, PRINTER_SENSOR_HISTORY_RETENTION_DAYS
                    )

                    cutoff = datetime.utcnow() - timedelta(days=retention_days)
                    cleanup = await db.execute(
                        delete(PrinterSensorHistory).where(PrinterSensorHistory.recorded_at < cutoff)
                    )
                    await db.commit()
                    if cleanup.rowcount > 0:
                        logger.info(
                            "Cleaned up %s old printer sensor history entries (older than %s days)",
                            cleanup.rowcount,
                            retention_days,
                        )

            await asyncio.sleep(PRINTER_SENSOR_HISTORY_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Printer sensor history recording failed: %s", e)
            await asyncio.sleep(60)


def start_printer_sensor_history_recording():
    """Start the printer heater history recording background task."""
    global _printer_sensor_history_task
    if _printer_sensor_history_task is None:
        _printer_sensor_history_task = asyncio.create_task(record_printer_sensor_history())
        logging.getLogger(__name__).info("Printer sensor history recording started")


def stop_printer_sensor_history_recording():
    """Stop the printer heater history recording background task."""
    global _printer_sensor_history_task
    if _printer_sensor_history_task:
        _printer_sensor_history_task.cancel()
        _printer_sensor_history_task = None
        logging.getLogger(__name__).info("Printer sensor history recording stopped")


# Printer runtime tracking
_runtime_tracking_task: asyncio.Task | None = None
RUNTIME_TRACKING_INTERVAL = 30  # Update every 30 seconds

# Printers the runtime tracker has seen connected since this process started. One of them going offline clears
# its stored timestamp, so the time it was away is not added when it comes back printing. A printer not seen
# yet -- still connecting after a restart -- keeps the timestamp for a grace period: a print that ran on through
# a restart of BamDude is counted from the stored timestamp, as before.
_runtime_seen_connected: set[int] = set()
# A printer never seen connected this long after the tracker started is offline, not still connecting: its
# stored timestamp is from before a gap too.
RUNTIME_STARTUP_GRACE_SECONDS = 120


async def track_printer_runtime():
    """Background task to track printer active runtime (RUNNING state only).

    PAUSE is intentionally excluded — the runtime counter feeds hours-based
    maintenance intervals (rod lubrication, belt checks, nozzle cleaning)
    which track mechanical wear. Pause time has no motion and no wear, so
    counting it inflates maintenance warnings (#1521).

    Only the stretch between two consecutive RUNNING ticks of a connected
    printer is added. A tick that finds the printer in any other state, not
    connected or without a state clears ``last_runtime_update``, so the next
    RUNNING tick starts a fresh interval instead of adding the whole gap
    (upstream 175758d25): a printer that dropped off mid-print and came back
    hours later with another print used to be credited with every hour between.
    """
    logger = logging.getLogger(__name__)

    # Wait for MQTT connections to establish on startup
    await asyncio.sleep(15)
    started = time.monotonic()

    while True:
        try:
            from backend.app.models.printer import Printer

            async with async_session() as db:
                # Get all active printers
                result = await db.execute(
                    select(Printer).where(Printer.is_active.is_(True)).where(Printer.archived.is_(False))
                )
                printers = result.scalars().all()

                now = datetime.now(timezone.utc)
                updated_count = 0

                needs_commit = False
                # A printer in maintenance mode or retired is outside the loop below, so its stamp would
                # stand for as long as it is parked and the first RUNNING tick after its return would add
                # the whole stretch. Parked is not printing.
                parked = await db.execute(
                    update(Printer)
                    .where(Printer.last_runtime_update.is_not(None))
                    .where(or_(Printer.is_active.is_(False), Printer.archived.is_(True)))
                    .values(last_runtime_update=None)
                )
                if parked.rowcount:
                    needs_commit = True
                # Forgotten only once the cleared timestamps are saved: a commit that failed leaves the
                # printer marked, and the next offline tick clears it again.
                gone_offline: set[int] = set()

                for printer in printers:
                    # Get current state from printer manager
                    state = printer_manager.get_status(printer.id)
                    if not state or not state.connected:
                        logger.debug(
                            "[%s] Runtime tracking: %s",
                            printer.name,
                            "not connected" if state else "no state available",
                        )
                        offline = printer.id in _runtime_seen_connected or (
                            time.monotonic() - started > RUNTIME_STARTUP_GRACE_SECONDS
                        )
                        if offline:
                            if printer.last_runtime_update is not None:
                                printer.last_runtime_update = None
                                needs_commit = True
                            gone_offline.add(printer.id)
                        continue
                    _runtime_seen_connected.add(printer.id)

                    # Only RUNNING accrues runtime — PAUSE is excluded so paused
                    # prints don't inflate hours-based maintenance wear (#1521).
                    if state.state == "RUNNING":
                        # Calculate time since last update
                        if printer.last_runtime_update:
                            last_update = printer.last_runtime_update
                            if last_update.tzinfo is None:
                                last_update = last_update.replace(tzinfo=timezone.utc)
                            elapsed = (now - last_update).total_seconds()
                            if elapsed > 0:
                                printer.runtime_seconds += int(elapsed)
                                updated_count += 1
                                needs_commit = True
                                logger.debug(
                                    f"[{printer.name}] Runtime tracking: added {int(elapsed)}s, "
                                    f"total={printer.runtime_seconds}s ({printer.runtime_seconds / 3600:.2f}h)"
                                )
                        else:
                            # First time seeing printer active - need to commit to save timestamp
                            needs_commit = True
                            logger.debug("[%s] Runtime tracking: first active detection", printer.name)

                        printer.last_runtime_update = now
                    else:
                        # Printer is idle/offline/paused - clear last_runtime_update
                        # so the next RUNNING window starts a fresh interval (#1521).
                        if printer.last_runtime_update is not None:
                            logger.debug(
                                f"[{printer.name}] Runtime tracking: state={state.state}, clearing last_runtime_update"
                            )
                            printer.last_runtime_update = None
                            needs_commit = True

                if needs_commit:
                    await db.commit()
                    if updated_count > 0:
                        logger.debug("Updated runtime for %s printer(s)", updated_count)
                _runtime_seen_connected.difference_update(gone_offline)

        except asyncio.CancelledError:
            logger.info("Runtime tracking cancelled")
            break
        except Exception as e:
            logger.warning("Runtime tracking failed: %s", e)

        await asyncio.sleep(RUNTIME_TRACKING_INTERVAL)


def start_runtime_tracking():
    """Start the printer runtime tracking background task."""
    global _runtime_tracking_task
    if _runtime_tracking_task is None:
        _runtime_tracking_task = asyncio.create_task(track_printer_runtime())
        logging.getLogger(__name__).info("Printer runtime tracking started")


def stop_runtime_tracking():
    """Stop the printer runtime tracking background task."""
    global _runtime_tracking_task
    if _runtime_tracking_task:
        _runtime_tracking_task.cancel()
        _runtime_tracking_task = None
        logging.getLogger(__name__).info("Printer runtime tracking stopped")


# ---------------------------------------------------------------------------
# Expected-print TTL eviction
# ---------------------------------------------------------------------------


def _evict_stale_expected_prints() -> None:
    """Remove entries from _expected_prints / _expected_print_creators that are
    older than _EXPECTED_PRINT_TTL_SECONDS.

    This prevents unbounded growth when a print is registered (via
    register_expected_print) but on_print_start never fires - e.g. because the
    printer disconnects, the app restarts, or the print is started directly from
    the printer panel without going through the queue.
    """
    # Use monotonic time so the TTL is unaffected by system clock adjustments
    # (e.g. NTP sync, DST changes).
    cutoff = time.monotonic() - _EXPECTED_PRINT_TTL_SECONDS
    stale_keys = [k for k, t in _expected_print_registered_at.items() if t < cutoff]
    if not stale_keys:
        return

    evicted_archive_ids: set[int] = set()
    for key in stale_keys:
        archive_id = _expected_prints.pop(key, None)
        if archive_id is not None:
            evicted_archive_ids.add(archive_id)
        _expected_print_creators.pop(key, None)
        _expected_print_registered_at.pop(key, None)

    # Also clean up _print_ams_mappings for archive_ids that have no remaining
    # live keys in _expected_prints (i.e. all variants were just evicted).
    live_archive_ids = set(_expected_prints.values())
    for archive_id in evicted_archive_ids:
        if archive_id not in live_archive_ids:
            _print_ams_mappings.pop(archive_id, None)
            _print_ams_mapping_identities.pop(archive_id, None)

    logging.getLogger(__name__).info(
        "Evicted %d stale expected-print entries (TTL=%ds)", len(stale_keys), _EXPECTED_PRINT_TTL_SECONDS
    )


async def _expected_prints_cleanup_loop() -> None:
    """Background task: periodically evict stale expected-print entries."""
    while True:
        try:
            _evict_stale_expected_prints()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.getLogger(__name__).warning("Expected prints cleanup failed: %s", e)
        await asyncio.sleep(_EXPECTED_PRINT_CLEANUP_INTERVAL)


def start_expected_prints_cleanup() -> None:
    global _expected_prints_cleanup_task
    if _expected_prints_cleanup_task is None:
        _expected_prints_cleanup_task = asyncio.create_task(_expected_prints_cleanup_loop())
        logging.getLogger(__name__).info("Expected prints cleanup started")


def stop_expected_prints_cleanup() -> None:
    global _expected_prints_cleanup_task
    if _expected_prints_cleanup_task:
        _expected_prints_cleanup_task.cancel()
        _expected_prints_cleanup_task = None
        logging.getLogger(__name__).info("Expected prints cleanup stopped")


def start_label_reclaim() -> None:
    """Sweep label jobs whose device claimed them and then went quiet.

    ⚠️ Started unconditionally rather than behind ``device_labels_enabled``: the
    setting can be switched off while a job is out, and the sweep is what stops
    that job reading as "printing" forever. It is a no-op query against an empty
    table on every install that never uses this.
    """
    global _label_reclaim_task
    if _label_reclaim_task is None:
        from backend.app.services.label_reclaim import reclaim_loop

        _label_reclaim_task = asyncio.create_task(reclaim_loop())
        logging.getLogger(__name__).info("Label job reclaim sweep started")


def stop_label_reclaim() -> None:
    global _label_reclaim_task
    if _label_reclaim_task:
        _label_reclaim_task.cancel()
        _label_reclaim_task = None
        logging.getLogger(__name__).info("Label job reclaim sweep stopped")


_library_objects_backfill_task = None


async def _library_objects_backfill_once() -> None:
    """One pass of the library object-metadata sweep (spec §G).

    ``m158.seed()`` runs the same function at upgrade; this is the retry for
    everything it could not reach — a share that was down then and is up now.
    There is deliberately no manual script: the user who needs this is the user
    who cannot run one.

    ⚠️ **Nothing escapes this coroutine.** It is a fire-and-forget task, so an
    exception here would surface only as an "exception was never retrieved"
    line, and a ``CancelledError`` at shutdown is not a fault — the sweep is
    resumable by construction and simply runs again at the next start.
    """
    log = logging.getLogger(__name__)
    try:
        from backend.app.services.library_objects_backfill import backfill_library_objects

        summary = await backfill_library_objects(async_session)
        if summary.filled or summary.skipped_unreachable or summary.skipped_unparseable:
            log.info(
                "Library object metadata: filled %d of %d 3MF(s) (%d unreachable, %d unparseable), "
                "%d product(s) resynced",
                summary.filled,
                summary.scanned,
                summary.skipped_unreachable,
                summary.skipped_unparseable,
                summary.products_synced,
            )
    except asyncio.CancelledError:
        log.debug("Library object metadata backfill cancelled")
    except Exception:
        log.warning("Library object metadata backfill failed", exc_info=True)


def start_library_objects_backfill() -> None:
    """Fill in the objects of library 3MFs that have none — once, at boot.

    ⚠️ A background task rather than a step of the lifespan: on a healthy
    install it is one SELECT that returns nothing, but on an install whose NAS
    was unreachable it is a walk over every file the upgrade missed, and the app
    may not wait for that to answer its first request.
    """
    global _library_objects_backfill_task
    if _library_objects_backfill_task is None:
        _library_objects_backfill_task = asyncio.create_task(_library_objects_backfill_once())


async def stop_library_objects_backfill() -> None:
    """Cancel the sweep AND wait for it to notice.

    Awaited, unlike the loops beside it: this one holds a database session while
    it writes a chunk, and returning from the lifespan with that still in flight
    is how a shutdown ends with a half-written batch and a closed engine.
    """
    global _library_objects_backfill_task
    task = _library_objects_backfill_task
    _library_objects_backfill_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Queue-source spool collector — the boot sweep + the periodic GC (spec §9, §11)
# ---------------------------------------------------------------------------

_queue_source_sweep_task: asyncio.Task | None = None
_queue_source_gc_task: asyncio.Task | None = None


async def _queue_source_sweep_once() -> None:
    """Adopt what the previous process left in ``DATA_DIR/queue-sources``.

    ⚠️ **Nothing escapes this coroutine.** It is fire-and-forget, so an exception
    would surface only through the background-task logger, and a
    ``CancelledError`` at shutdown is not a fault — the sweep is idempotent by
    construction and simply runs again at the next start.
    """
    log = logging.getLogger(__name__)
    try:
        from backend.app.services.queue_sources import sweep_after_restart

        await sweep_after_restart()
    except asyncio.CancelledError:
        log.debug("Queue-source sweep cancelled")
    except Exception:
        log.warning("Queue-source sweep failed", exc_info=True)


async def _queue_source_gc_loop() -> None:
    """Release spool blobs whose last owner is gone, no more often than §6 allows.

    Sleeps FIRST: a pass at boot would race the sweep for the same guard and do
    its work twice. The interval is read from the service on every iteration so
    the one home for that number stays the one home.
    """
    log = logging.getLogger(__name__)
    from backend.app.services import queue_sources

    while True:
        try:
            await asyncio.sleep(queue_sources.GC_MIN_INTERVAL_SECONDS)
            report = await queue_sources.collect()
            if report.touched():
                log.info(
                    "Queue-source GC: released %d blob(s), %d orphan object(s), %d stray staging file(s), "
                    "%d marked, %d failed",
                    report.released,
                    report.orphan_objects,
                    report.orphan_parts,
                    report.marked,
                    report.failed,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            log.warning("Queue spool GC pass failed", exc_info=True)


def start_queue_source_collector() -> None:
    """Start the boot sweep and the periodic GC — both tracked, neither blocking.

    ⚠️ The sweep is a task, never a step of the lifespan: it walks the spool and
    can unlink files, and uvicorn must be answering requests before that finishes.
    """
    global _queue_source_sweep_task, _queue_source_gc_task
    if _queue_source_sweep_task is None:
        _queue_source_sweep_task = spawn_background_task(_queue_source_sweep_once(), name="queue-source-sweep")
    if _queue_source_gc_task is None:
        _queue_source_gc_task = spawn_background_task(_queue_source_gc_loop(), name="queue-source-gc")


async def stop_queue_source_collector() -> None:
    """Cancel both AND wait for them to notice.

    Awaited like ``stop_library_objects_backfill``: a pass holds a database
    session and may be mid-unlink, and returning from the lifespan with that in
    flight is how a shutdown ends against a closed engine.
    """
    global _queue_source_sweep_task, _queue_source_gc_task
    tasks = [_queue_source_sweep_task, _queue_source_gc_task]
    _queue_source_sweep_task = None
    _queue_source_gc_task = None
    for task in tasks:
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    # Startup
    printer_manager._print_lifecycle_stopping = False
    # Printers connect long before the analysis service starts (below): what asks for an analysis
    # in between waits for it instead of meeting "unavailable".
    from backend.app.services.analysis_runtime import expect_start as _expect_analysis_start

    _expect_analysis_start()
    # Install Windows-only asyncio Proactor cleanup-RST filter (#1113) before
    # anything else can spawn tasks that might trip it. The filter is a no-op
    # on non-Windows hosts.
    from backend.app.core.asyncio_handlers import install_proactor_reset_filter, warn_if_running_on_uvloop

    install_proactor_reset_filter()
    # Before init_db, so it sits near the top of the log rather than below a
    # migration run (upstream 0dfcff59).
    warn_if_running_on_uvloop()

    # Scratch goes on the data volume, not the system temp — before anything
    # stages its first file there. The backup copies the whole data tree into
    # scratch before zipping it, and on Docker the default is the container's
    # own layer, on some NAS hosts a tmpfs: not where a copy of the whole
    # library belongs, and not sized for it.
    from backend.app.core.paths import install_process_temp_dir

    install_process_temp_dir()

    # DATABASE_URL=embedded: the bundled PostgreSQL must accept connections
    # before init_db() opens the first one. The engine itself was created at
    # import with a fixed localhost URL, so nothing else has to wait.
    if app_settings.embedded_postgres:
        from backend.app.services import embedded_postgres as _embedded_pg

        await _embedded_pg.start()

    new_empty_store = await init_db()
    farm_startup_guard.admit_new_installation(new_empty_store=new_empty_store)

    # Camera ownership is worker-only. A failed worker degrades cameras but
    # must not prevent queueing, dispatch or health endpoints from starting.
    from backend.app.services.camera_runtime import configure_camera_runtime

    await configure_camera_runtime()

    # Warm the system language into process memory. Sync callers on hot paths
    # read it from there — notably the MQTT pause classifier, which cannot
    # await a settings read and would otherwise report every pause reason in
    # English regardless of the configured language. Kept fresh afterwards by
    # locale_updater on change and by every get_language() call.
    try:
        from backend.app.i18n import get_language as _warm_language

        await _warm_language()
    except Exception:  # noqa: BLE001 — nothing in startup may take the boot down
        logging.getLogger(__name__).debug("Language cache warm-up skipped", exc_info=True)

    # OIDC provider declared by BAMDUDE_OIDC_* (#2593). Re-applied on every boot,
    # which is what makes the environment the authority and the UI read-only —
    # and what lets removing the variables release the row rather than strand it.
    # Runs after init_db because it needs the is_env_managed column m130 adds.
    # Never raises by contract; a rejected config is logged and skipped.
    try:
        from backend.app.core.database import async_session as _oidc_session
        from backend.app.core.oidc_env import apply_env_oidc_provider

        async with _oidc_session() as _oidc_db:
            await apply_env_oidc_provider(_oidc_db)
    except Exception:  # noqa: BLE001 — nothing in startup may take the boot down
        logging.getLogger(__name__).exception("Env-managed OIDC provider could not be applied")

    # Zigbee coordinator. Best-effort by contract: start() never raises, so a
    # missing, busy or wrong-mode dongle leaves the app fully usable with the
    # reason available at GET /zigbee/status. Skipped entirely when disabled,
    # which is the default — an install that never wants Zigbee pays nothing.
    # The outer guard covers the settings read, not the coordinator: this is
    # startup, and nothing here may take the application down.
    try:
        # ``async_session`` is aliased, not used from module scope: a later block
        # in this same function imports that name locally, which makes Python
        # treat it as a local for the WHOLE function — so the module-level one
        # would raise UnboundLocalError here. Ruff (F823) catches it; the alias
        # sidesteps the shadowing entirely.
        from backend.app.api.routes.settings import get_setting as _zb_get_setting
        from backend.app.core.database import async_session as _zb_session
        from backend.app.services.zigbee.coordinator import zigbee_coordinator

        async with _zb_session() as _zb_db:
            _zb_settings = {
                _k: (await _zb_get_setting(_zb_db, _k) or "")
                for _k in ("zigbee_enabled", "zigbee_transport", "zigbee_path")
            }
        await zigbee_coordinator.start(_zb_settings)

        # Reporting for every Zigbee plug already bound to a printer. Without
        # this the driver looks configured and does half its job: commands go
        # straight to the cluster and work, so the plug switches — but nothing
        # feeds the cache, so status stays "unreachable" and energy never
        # arrives. Best-effort, like the start above.
        if zigbee_coordinator.app is not None:
            from sqlalchemy import select as _zb_select

            from backend.app.models.smart_plug import SmartPlug as _ZbPlug
            from backend.app.services.zigbee.driver import zigbee_smart_plug_service as _zb_driver
            from backend.app.services.zigbee.reporting import subscribe_all as _zb_subscribe

            async with _zb_session() as _zb_db2:
                _zb_rows = (
                    (await _zb_db2.execute(_zb_select(_ZbPlug).where(_ZbPlug.plug_type == "zigbee"))).scalars().all()
                )
            if _zb_rows:
                _zb_wired = await _zb_subscribe(_zb_driver, _zb_rows)
                logging.getLogger(__name__).info("Zigbee reporting set up for %s/%s plug(s)", _zb_wired, len(_zb_rows))

            # Sensor listeners, for every sensor already on the mesh. Purely
            # local — no bind, no configure_reporting — which is what lets it
            # run for a sleeping battery device too. Without it a sensor's
            # reports reach zigpy after a restart and are dropped: the device
            # looks perfectly paired and its readings stay blank for ever.
            from backend.app.services.zigbee.reporting import attach_all_sensors as _zb_attach_sensors

            _zb_sensors = _zb_attach_sensors(zigbee_coordinator.app)
            if _zb_sensors:
                logging.getLogger(__name__).info("Zigbee reporting attached for %s sensor(s)", _zb_sensors)

            # Devices paired before this table existed get their rows now. The
            # migration could not do it: it has no radio and cannot know what
            # is paired. Idempotent, so later boots cost one query per device.
            from backend.app.core.database import async_session as _zb_session
            from backend.app.services.zigbee.device_settings import reconcile_device_rows as _zb_reconcile
            from backend.app.services.zigbee.devices import describe_device as _zb_describe

            async with _zb_session() as _zb_db:
                _zb_added = await _zb_reconcile(
                    [_zb_describe(d) for d in list(zigbee_coordinator.app.devices.values())],
                    _zb_db,
                )
            if _zb_added:
                logging.getLogger(__name__).info("Zigbee: recorded %s device(s) paired before this version", _zb_added)

            # Polling, started whether or not any plug is configured yet: the
            # loop re-queries each cycle, so a plug added later joins without a
            # restart. Reporting alone does not keep readings current — ZHA
            # polls this hardware class by default for the same reason.
            from backend.app.services.zigbee.poller import zigbee_poller as _zb_poller

            _zb_poller.start(_zb_driver, _zb_session)

        # ⚠️ Outside the "did the radio come up" block on purpose. The
        # supervisor's whole job is the case where it did not: a dongle absent
        # at boot, a port another program is holding, or a radio that dies
        # later. Starting it only when the radio is already healthy would leave
        # every one of those needing a full application restart, which is the
        # bug it was written for.
        from backend.app.services.zigbee.supervisor import zigbee_supervisor as _zb_supervisor

        _zb_supervisor.start(_zb_session)
    except Exception as _zb_exc:
        logging.getLogger(__name__).warning("Zigbee coordinator not started: %s", _zb_exc)

    # Settle the timelapse frame directories earlier runs left. Sessions are
    # tracked in memory, so a restart mid-print leaves one nothing comes back
    # for: it becomes "retained" by its own manifest and the retention bounds
    # decide (G10 F). Filesystem-only and best-effort — a failure here must
    # never keep the app from starting.
    try:
        from backend.app.services import layer_timelapse as _layer_timelapse
        from backend.app.services.layer_timelapse import sweep_orphaned_frames

        _layer_timelapse.reopen()
        await asyncio.to_thread(sweep_orphaned_frames)
        # And again every hour while the server runs: frames past their time go without a restart.
        _layer_timelapse.start_janitor()
    except Exception as _tl_sweep_exc:
        logging.getLogger(__name__).debug("Timelapse frame sweep skipped: %s", _tl_sweep_exc)

    # Apply DB-backed log retention to the live rotating handler. The
    # handler was created at module-import time with a hardcoded 7-day
    # bootstrap; now that the DB is up, override with whatever the
    # operator configured under Settings -> Data Management. Best-effort
    # — silent fallback to the bootstrap default if the lookup fails.
    if app_settings.log_to_file:
        try:
            from backend.app.api.routes.settings import get_setting as _get_setting
            from backend.app.core.database import async_session
            from backend.app.core.logging_state import update_log_retention

            async with async_session() as _ls_db:
                _ret_str = await _get_setting(_ls_db, "log_retention_days")
            if _ret_str:
                update_log_retention(int(_ret_str))
        except Exception as _ret_exc:
            logging.getLogger(__name__).debug("Could not apply DB log_retention_days at startup: %s", _ret_exc)

    # The timing thresholds live in the database but the listeners were attached
    # at import time, so the stored values have to be pushed into module state
    # once at startup. Best-effort: a farm must boot even if this row is odd.
    try:
        from backend.app.api.routes.settings import get_setting as _get_timing_setting
        from backend.app.core.database import async_session as _timing_session

        async with _timing_session() as _tm_db:
            query_timing.set_query_threshold_ms(int(await _get_timing_setting(_tm_db, "slow_query_ms") or 0))
            query_timing.set_request_threshold_ms(int(await _get_timing_setting(_tm_db, "slow_request_ms") or 0))
    except Exception as _tm_exc:
        logging.getLogger(__name__).debug("Could not apply DB timing thresholds at startup: %s", _tm_exc)

    # Register an app-scoped httpx client for Bambu Cloud services so
    # per-request BambuCloudService instances reuse the same connection pool
    # (important for routes like /cloud/filament-info that chain many
    # get_setting_detail calls). The shared client stores no region/token
    # state, so the per-request ownership pattern that fixed the region-bleed
    # bug is preserved.
    import httpx as _httpx

    from backend.app.services.bambu_cloud import set_shared_http_client

    _shared_cloud_http_client = _httpx.AsyncClient(timeout=30.0)
    set_shared_http_client(_shared_cloud_http_client)

    # Slicer-API sidecar HTTP pool (Phase 1 of 0.5.x slicer cycle). Separate
    # pool because slice_with_profiles can block for several minutes on
    # complex H2D models — a 30 s timeout would chew through the cloud
    # client's per-request budget. The sidecar lives at localhost (or the
    # operator's configured URL) and is rarely cross-host, so connection
    # pooling savings are smaller than for cloud — but keeping it shared
    # avoids constructing fresh clients per slice request.
    from backend.app.services.slicer_api import (
        set_shared_http_client as _set_shared_slicer_http_client,
    )

    _shared_slicer_http_client = _httpx.AsyncClient(timeout=300.0)
    _set_shared_slicer_http_client(_shared_slicer_http_client)

    # MakerWorld API client pool (Phase 5 of 0.5.x cycle). 30 s budget
    # matches MakerWorld's actual API SLA; the 3MF download streams via a
    # dedicated 60 s read timeout inside ``download_3mf`` so a slow signed
    # CDN doesn't starve metadata calls. Reused across resolve / status /
    # import / recent so a single page load doesn't open four pools.
    from backend.app.services.model_providers.makerworld.service import (
        set_shared_http_client as _set_shared_makerworld_http_client,
    )

    _shared_makerworld_http_client = _httpx.AsyncClient(timeout=30.0)
    _set_shared_makerworld_http_client(_shared_makerworld_http_client)

    # Fix queue items stuck with invalid "aborted" status (should be "cancelled").
    # This can happen when a print was cancelled mid-print on versions before this fix.
    try:
        async with async_session() as db:
            from backend.app.models.print_queue import PrintQueueItem

            result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.status == "aborted"))
            aborted_items = result.scalars().all()
            if aborted_items:
                for item in aborted_items:
                    item.status = "cancelled"
                await db.commit()
                logging.info("Fixed %d queue item(s) with invalid 'aborted' status → 'cancelled'", len(aborted_items))
    except Exception as e:
        logging.warning("Failed to fix aborted queue items: %s", e)

    # Restore debug logging state from previous session
    await init_debug_logging()

    # Set up printer manager callbacks
    loop = asyncio.get_event_loop()
    printer_manager.set_event_loop(loop)
    printer_manager.set_status_change_callback(on_printer_status_change)
    printer_manager.set_print_start_callback(on_print_start)
    printer_manager.set_print_complete_callback(on_print_complete)
    printer_manager.set_print_running_observed_callback(on_print_running_observed)
    printer_manager.set_finish_photo_moment_callback(on_finish_photo_moment)
    printer_manager.set_ams_change_callback(on_ams_change)
    printer_manager.set_spool_inserted_callback(on_stock_spool_inserted)

    printer_manager.set_layer_change_callback(on_printer_layer_change)

    # #1349: smart-plug auto-off after AMS drying completes. The callback
    # fires once per AMS unit on the falling edge of ``dry_time``
    # (>0 → 0). We don't need the ``ams_id`` payload at the plug layer
    # because BamDude's plug model is plug→printer (not plug→AMS); the
    # plug-side handler walks every linked plug and respects per-plug
    # ``auto_off_after_drying`` toggles.
    async def on_drying_complete(printer_id: int, _ams_id: int):
        async with async_session() as db:
            try:
                await smart_plug_manager.on_drying_complete(printer_id, db)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Smart-plug drying auto-off failed for printer %s: %s", printer_id, e
                )

    printer_manager.set_drying_complete_callback(on_drying_complete)

    async def on_drying_cycle_end(printer_id: int, cycle):
        """Mark the spools of an AMS unit whose drying cycle just ended as dried (upstream 84f320b85)."""
        from backend.app.services.spool_drying import record_drying_cycle

        try:
            async with async_session() as db:
                await record_drying_cycle(db, printer_id, cycle)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to record drying for printer %s (AMS %s): %s", printer_id, cycle.ams_id, e
            )

    printer_manager.set_drying_cycle_end_callback(on_drying_cycle_end)

    async def on_assignment_verified(printer_id: int, ams_id: int, tray_id: int, verified: bool, detail: dict):
        """Surface the read-back result of a spool assignment to the UI (upstream #2582).

        The MQTT client confirms (or fails to confirm) that the tray telemetry
        echoed back the filament id we pushed. We relay that as a websocket event
        so the frontend can toast "loaded" / "assignment didn't take" instead of
        the historic silent fire-and-forget, which made the AMS→Studio hand-off
        feel random to users.
        """
        try:
            from backend.app.services.printer_manager import tray_layout_of
            from backend.app.services.spool_assignment_notifications import _slot_label_from_global_tray

            verified_layout = tray_layout_of(printer_id, printer_manager)
            global_id = verified_layout.global_tray_id(ams_id, tray_id)
            slot_label = _slot_label_from_global_tray(global_id, verified_layout)

            printer_info = printer_manager.get_printer(printer_id)
            printer_name = printer_info.name if printer_info else f"Printer {printer_id}"

            await ws_manager.broadcast(
                {
                    "type": "spool_assignment_verified",
                    "printer_id": printer_id,
                    "printer_name": printer_name,
                    "ams_id": ams_id,
                    "tray_id": tray_id,
                    "slot": slot_label,
                    "verified": verified,
                    # Present on success: False means the filament setting landed
                    # but the K-profile (cali_idx) did not.
                    "kprofile_applied": detail.get("kprofile_applied", True),
                    # Present on failure: whether any tray telemetry was seen in
                    # the window (distinguishes "printer silent" from "printer
                    # stored something else").
                    "saw_tray": detail.get("saw_tray", False),
                }
            )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to broadcast assignment verification for printer %d AMS%d-T%d: %s",
                printer_id,
                ams_id,
                tray_id,
                e,
            )

    printer_manager.set_assignment_verified_callback(on_assignment_verified)

    async def on_tray_change(printer_id: int, tray_global: int, layer_num: int):
        """Persist a mid-print tray change for completion-time attribution.

        AMS filament backup switches trays without telling the slicer, so the
        tray-change log is the only record of which spool fed which layers.
        Keeping it only in memory meant a restart mid-print charged everything to
        the tray that finished the job.
        """
        try:
            from backend.app.services.print_usage_journal import journal_lock
            from backend.app.services.usage_tracker import record_tray_change_event

            # The journal lock first: callbacks keep the order the printer reported in.
            async with journal_lock(printer_id), async_session() as db:
                await record_tray_change_event(db, printer_id, tray_global, layer_num)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to persist tray change for printer %d (tray=%d, layer=%d): %s",
                printer_id,
                tray_global,
                layer_num,
                e,
            )

    printer_manager.set_tray_change_callback(on_tray_change)

    async def on_usage_event(printer_id: int, event: str, kind: str | None, tray_global: int | None, layer_num: int):
        """Mirror detector events (runout / spool_loaded) into the usage journal.

        Spool ids are frozen HERE, at event time — the whole point of the
        journal: completion attributes segments without re-asking the DB after
        assignments have moved on. Events with no active archive have nothing
        to attach to and are dropped.
        """
        try:
            from backend.app.models.print_usage_event import EVENT_SPOOL_LOADED
            from backend.app.services.print_usage_journal import (
                active_archive_id,
                freeze_spool_ids,
                journal_lock,
                record_event,
                record_runout,
            )

            async with async_session() as db:
                # The journal lock is the first thing awaited (opening a session awaits
                # nothing), so journal writes keep the printer's order; the notification
                # below runs after it is released.
                async with journal_lock(printer_id):
                    archive_id = await active_archive_id(db, printer_id)
                    if archive_id is None:
                        logging.getLogger(__name__).debug(
                            "Usage event %s on printer %d has no active archive, dropped", event, printer_id
                        )
                        return
                    spool_id = spoolman_spool_id = None
                    if tray_global is not None:
                        spool_id, spoolman_spool_id = await freeze_spool_ids(db, printer_id, tray_global)
                    if event == EVENT_SPOOL_LOADED:
                        await record_event(
                            db,
                            printer_id=printer_id,
                            archive_id=archive_id,
                            layer_num=layer_num,
                            event=EVENT_SPOOL_LOADED,
                            global_tray_id=tray_global,
                            spool_id=spool_id,
                            spoolman_spool_id=spoolman_spool_id,
                        )
                        return
                    if event != "runout":
                        return
                    accepted = await record_runout(
                        db,
                        printer_id=printer_id,
                        archive_id=archive_id,
                        layer_num=layer_num,
                        kind=kind,
                        global_tray_id=tray_global,
                        spool_id=spool_id,
                        spoolman_spool_id=spoolman_spool_id,
                    )
                if not accepted.should_notify:
                    return
                # Notify — pause/external doubles as the human-in-the-loop
                # prompt to assign the replacement spool. Ambiguous stays
                # silent (could equally be a jam).
                if kind != "ambiguous":
                    try:
                        from backend.app.services.printer_manager import tray_layout_of
                        from backend.app.services.spool_assignment_notifications import (
                            _slot_label_from_global_tray,
                        )

                        printer_info = printer_manager.get_printer(printer_id)
                        await notification_service.on_filament_runout(
                            printer_id=printer_id,
                            printer_name=printer_info.name if printer_info else f"Printer {printer_id}",
                            slot=(
                                _slot_label_from_global_tray(tray_global, tray_layout_of(printer_id, printer_manager))
                                if tray_global is not None
                                else "?"
                            ),
                            kind=kind or "pause",
                            db=db,
                        )
                    except Exception as e:
                        logging.getLogger(__name__).warning(
                            "filament_runout notification failed for printer %d: %s", printer_id, e
                        )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to journal usage event %s for printer %d: %s", event, printer_id, e
            )

    printer_manager.set_usage_event_callback(on_usage_event)

    async def on_feed_event(
        printer_id: int,
        event: str,
        origin: int,
        layer_num: int,
        after_gap: bool,
        subtask_id: str | None,
        generation: int | None = None,
    ):
        """Journal a runout tail's start, the feed's return to its tray (post-rebuild F-R2), or an
        assignment the restart seed replays in its place among the held reports (F-R3)."""
        try:
            from backend.app.services.print_usage_journal import handle_feed_event, journal_lock

            async with journal_lock(printer_id), async_session() as db:
                await handle_feed_event(
                    db,
                    printer_id=printer_id,
                    event=event,
                    origin=origin,
                    layer_num=layer_num,
                    after_gap=after_gap,
                    subtask_id=subtask_id,
                    generation=generation,
                )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to journal %s for printer %d (tray=%d, layer=%d): %s", event, printer_id, origin, layer_num, e
            )

    printer_manager.set_feed_event_callback(on_feed_event)

    # Initialize MQTT relay from settings
    async with async_session() as db:
        from backend.app.api.routes.settings import get_setting

        mqtt_settings = {
            "mqtt_enabled": (await get_setting(db, "mqtt_enabled") or "false") == "true",
            "mqtt_broker": await get_setting(db, "mqtt_broker") or "",
            "mqtt_port": int(await get_setting(db, "mqtt_port") or "1883"),
            "mqtt_username": await get_setting(db, "mqtt_username") or "",
            "mqtt_password": await get_setting(db, "mqtt_password") or "",
            "mqtt_topic_prefix": await get_setting(db, "mqtt_topic_prefix") or "bamdude",
            "mqtt_use_tls": (await get_setting(db, "mqtt_use_tls") or "false") == "true",
            "mqtt_tls_verify": (await get_setting(db, "mqtt_tls_verify") or "false") == "true",
        }
        await mqtt_relay.configure(mqtt_settings)

        # Restore MQTT smart plug subscriptions
        if mqtt_settings.get("mqtt_enabled"):
            from backend.app.models.smart_plug import SmartPlug
            from backend.app.services.mqtt_smart_plug import subscribe_plug_to_mqtt

            result = await db.execute(select(SmartPlug).where(SmartPlug.plug_type == "mqtt"))
            mqtt_plugs = result.scalars().all()
            restored = 0
            for plug in mqtt_plugs:
                if subscribe_plug_to_mqtt(mqtt_relay.smart_plug_service, plug):
                    restored += 1
            if restored:
                logging.info("Restored %s MQTT smart plug subscriptions", restored)

    await printer_manager.load_awaiting_plate_clear_from_db()

    # Built-in slot rows written while Spoolman mode was on (a hole closed
    # since) — nothing else ever clears them. Best-effort, and BEFORE any
    # printer connects: nothing writes the slot ledger yet, so no assignment
    # lock is needed, and no first AMS push can rebuild the advertised overlay
    # from rows this is about to delete.
    async with async_session() as db:
        try:
            from backend.app.api.routes.settings import clear_stray_builtin_assignments

            _stray = await clear_stray_builtin_assignments(db)
            if _stray:
                logging.warning("Startup: removed %d built-in slot assignment(s) left in Spoolman mode", _stray)
        except Exception:  # noqa: BLE001 — never block startup on a clean-up
            await db.rollback()
            logging.exception("Startup: could not remove built-in slot assignments left in Spoolman mode")

    # WLED is optional (N02): its per-printer mappings are loaded before any printer connects.
    async with async_session() as db:
        await initialize_wled_printers(db)

    # Connect to all active printers
    async with async_session() as db:
        await init_printer_connections(db)

    # Auto-connect to Spoolman if enabled
    async with async_session() as db:
        from backend.app.api.routes.settings import get_setting

        spoolman_enabled = await get_setting(db, "spoolman_enabled")
        spoolman_url = await get_setting(db, "spoolman_url")

        if spoolman_enabled and spoolman_enabled.lower() == "true" and spoolman_url:
            try:
                client = await init_spoolman_client(spoolman_url)
                if await client.health_check():
                    logging.info("Auto-connected to Spoolman at %s", spoolman_url)
                    # Ensure the 'tag' extra field exists for RFID/UUID storage
                    await client.ensure_tag_extra_field()
                else:
                    logging.warning("Spoolman at %s is not reachable", spoolman_url)
            except Exception as e:
                logging.warning("Failed to auto-connect to Spoolman: %s", e)

    # Give back any printer claimed by a dispatch that died mid-flight. This has
    # to happen BEFORE the scheduler's first tick: check_queue seeds
    # busy_printers straight from PrinterQueue.status='printing', so a claim left
    # by the previous process would keep that printer out of dispatch for the
    # whole run. Best-effort — a farm must still start if this fails.
    try:
        from backend.app.services.print_reconciliation import release_interrupted_dispatch_claims

        async with async_session() as db:
            _released = await release_interrupted_dispatch_claims(db)
            if _released:
                await db.commit()
                logging.warning(
                    "Startup: released %d printer claim(s) left behind by an interrupted dispatch", _released
                )
    except Exception:  # noqa: BLE001 — never block startup on a recovery sweep
        logging.exception("Startup: could not release interrupted dispatch claims")
        farm_startup_guard.readiness_failed = True

    # Start the print scheduler
    app.state.print_scheduler_task = spawn_background_task(print_scheduler.run(), name="print-scheduler")

    # Start the auto-queue scheduler — routes pending auto items to idle
    # printers; assignment hands off to print_scheduler/background_dispatch.
    app.state.auto_queue_scheduler_task = spawn_background_task(auto_queue_scheduler.run(), name="auto-queue-scheduler")

    # Start the stock-break aggregator — the forecast that used to exist only in
    # the browser, so "this filament runs out before a replacement arrives"
    # reaches the operator when nobody has the Inventory page open (m118).
    spawn_background_task(stock_forecast_alerts.run(), name="stock-forecast-alerts")

    # Start background dispatch worker for send/start operations
    await background_dispatch.start()

    # The read-only print-state observer: one page of the diagnostics every
    # 15 s, round-robin, for System Health and the support bundle. It repairs
    # nothing; Info always reads fresh (spec printer-runtime-stall r3 R5).
    from backend.app.services.print_state_observer import print_state_observer

    print_state_observer.start()

    # Start the 3MF download retry service (fallback archives with file_path="").
    # The startup sweep does sequential FTP calls (up to 60s each) and MUST NOT
    # block lifespan — run it as a fire-and-forget task so uvicorn accepts
    # requests immediately.
    from backend.app.services.archive_download_retry import archive_download_retry

    spawn_background_task(archive_download_retry.start(), name="archive-download-retry")

    # Stagger-slot reconciliation: scan printers after MQTT reconnect and
    # register slots for any that are actively heating (PREPARE/RUNNING
    # with bed below target).  Runs as a background task so lifespan
    # doesn't wait on it.
    async def _reconcile_stagger_on_startup():
        # Give MQTT a moment to populate state for each reconnected client.
        await asyncio.sleep(5)
        try:
            for printer_id in list(printer_manager._clients.keys()):
                await maybe_register_external_stagger(printer_id)
        except Exception as e:
            logging.getLogger(__name__).warning("Stagger startup reconciliation failed: %s", e)

    spawn_background_task(_reconcile_stagger_on_startup(), name="reconcile-stagger-startup")

    # Locale reconciliation: if the system language setting has drifted from
    # what's seeded in `maintenance_types` / `notification_templates` (e.g.
    # user ran an older version that seeded EN defaults then switched the
    # system language to UK without re-triggering the settings-PATCH flow,
    # or a restore brought in EN rows under a UK config), realign once at
    # startup.  Idempotent — safe to run every boot.  Fire-and-forget.
    from backend.app.services.notification_service import (
        begin_internal_template_reconcile,
        complete_internal_template_reconcile,
        fail_internal_template_reconcile,
    )

    begin_internal_template_reconcile()

    async def _reconcile_locale_on_startup():
        try:
            from sqlalchemy import delete

            from backend.app.api.routes.settings import get_setting
            from backend.app.core.database import async_session
            from backend.app.models.settings import Settings as SettingsModel
            from backend.app.services.locale_updater import update_locale_data

            async with async_session() as locale_db:
                lang = await get_setting(locale_db, "language") or "en"
                result = await update_locale_data(locale_db, lang)

                # Drop dead `notification_language` row left over from
                # pre-0.3 versions. Feature was removed; notifications
                # now follow the `language` setting.
                stale = await locale_db.execute(
                    delete(SettingsModel).where(SettingsModel.key == "notification_language")
                )
                if stale.rowcount:
                    await locale_db.commit()

                logging.getLogger(__name__).info(
                    "Locale startup reconcile (%s): %d notification templates, %d maintenance types%s",
                    result["language"],
                    result["notification_templates_updated"],
                    result["maintenance_types_updated"],
                    " (cleaned up legacy notification_language row)" if stale.rowcount else "",
                )
            complete_internal_template_reconcile()
        except Exception as e:
            fail_internal_template_reconcile()
            logging.getLogger(__name__).warning("Locale startup reconciliation failed: %s", e)

    spawn_background_task(_reconcile_locale_on_startup(), name="reconcile-locale-startup")

    # Start the smart plug scheduler for time-based on/off
    smart_plug_manager.start_scheduler()
    ha_sensor_manager.start()
    location_ha_sensor_manager.start()

    # Resume any pending auto-offs that were interrupted by restart
    await smart_plug_manager.resume_pending_auto_offs()

    # Rehydrate plate-clear gates from DB so an Auto Off power cycle during a
    # pending confirmation can't let the queue auto-dispatch onto a dirty plate (#961).
    # Every printer has a queue. A guard for printers added by a path that never
    # created one (the Telegram bot did not) and for databases restored from
    # elsewhere — see services/printer_queues for what the gap cost.
    try:
        from backend.app.services.printer_queues import ensure_all_printer_queues

        async with async_session() as _db:
            await ensure_all_printer_queues(_db)
    except Exception:  # noqa: BLE001 — a guard, never a reason not to start
        logging.getLogger(__name__).exception("printer queue guard failed at startup")

    # Resume MQTT recordings the database says are running. Same reasoning as
    # the line above: the intent was persisted so it would survive a restart,
    # which is only a longer version of closing the window it already survives.
    # A printer still offline here is picked up by the connection watchdog's
    # sweep, which also re-attaches after it rebuilds a session.
    from backend.app.services.mqtt_recorder import resume_recordings

    await resume_recordings()

    # Start the notification digest scheduler
    notification_service.start_digest_scheduler()
    # Notify! Live Activities and Lock Screen widgets: dormant until a provider wants them.
    notify_live_activities.start()
    notify_widgets.start()

    # Start the Git backup scheduler
    await git_backup_service.start_scheduler()

    # Start the local backup scheduler (#884)
    await local_backup_service.start_scheduler()

    # Start AMS history recording
    start_ams_history_recording()

    # Start the filament preset-mirror sync loop (family catalog, spec A)
    start_filament_preset_sync()

    # One-shot: absorb pre-existing local presets + backfill spool family
    # links. Idempotent (fills NULLs only), so ordering vs the first mirror
    # sync is non-critical — an offline boot resolves what the catalog alone
    # can; the next boot picks up mirror-dependent ones.
    async def _family_catalog_startup_backfill():
        try:
            from backend.app.core import database
            from backend.app.services.filament_family_backfill import backfill_spool_families
            from backend.app.services.filament_preset_sync import (
                absorb_all_local_presets,
                request_sync_soon,
            )

            request_sync_soon()
            await asyncio.sleep(5)  # give a connected instance a chance to fill mirrors
            async with database.async_session() as db:
                absorbed = await absorb_all_local_presets(db)
                await db.commit()
                summary = await backfill_spool_families(db)
            logging.getLogger(__name__).info(
                "family catalog startup: %d local presets absorbed, backfill %s", absorbed, summary
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("family catalog startup backfill failed")

    global _family_backfill_task
    _family_backfill_task = asyncio.create_task(_family_catalog_startup_backfill())

    # Start printer heater (nozzle/bed/chamber) history recording
    start_printer_sensor_history_recording()

    # Rebuild MQTT sessions that stopped reconnecting on their own (#2732).
    # check_staleness() only ever handles a client that is still *connected* and
    # quiet, and we call it reactively from get_status() — so a printer nobody is
    # looking at has nothing watching it at all.
    from backend.app.services.connection_watchdog import start_connection_watchdog

    start_connection_watchdog()

    # Start printer runtime tracking
    start_runtime_tracking()

    # Start camera stream orphan cleanup

    # Start anonymized opt-out telemetry (daily snapshot)
    from backend.app.services.telemetry import start_telemetry

    start_telemetry()

    # Start expected-print TTL eviction (prevents memory leak when prints are
    # registered but on_print_start never fires)
    start_expected_prints_cleanup()
    start_label_reclaim()

    # ⚠️ A scan the previous process was running is gone with it, and the row it
    # left says `running` — which reads on screen as progress AND makes the
    # duplicate guard refuse every future scan of that folder. Swept before
    # anything can look at it.
    try:
        from backend.app.services.library_scan import sweep_interrupted_jobs

        swept = await sweep_interrupted_jobs()
        if swept:
            logging.getLogger(__name__).info("failed %d library scan(s) left running by a restart", swept)
    except Exception:
        logging.getLogger(__name__).warning("library scan sweep failed", exc_info=True)

    # ⚠️ queue-sources can hold three kinds of leftover from a process that
    # died: a `deleting` tombstone mid-unlink, a `.part` from a capture that
    # never finished, and a fully renamed object whose row was never committed.
    # Swept in the background, never in the lifespan — it walks a directory tree
    # and unlinks files. The periodic collector starts with it and releases a blob
    # only once every job row of both queues has let go (spec §9).
    start_queue_source_collector()

    # Library 3MFs that never got their object metadata (uploaded before the
    # extractor existed, or scanned while their mount was down) get it here.
    # m158 does the same at upgrade; this is the retry, and on an install where
    # nothing is missing it is one SELECT.
    start_library_objects_backfill()

    # Event-loop stall watchdog: dumps all thread stacks to stderr if the loop
    # freezes (#1486 — silent "container hangs after adding a printer" reports).
    from backend.app.services.loop_watchdog import start_loop_watchdog

    start_loop_watchdog()

    # Initialize virtual printer manager and sync from DB
    from backend.app.services.virtual_printer import virtual_printer_manager

    virtual_printer_manager.set_session_factory(async_session)
    virtual_printer_manager.set_printer_manager(printer_manager)
    from backend.app.services.camera_runtime import register_camera_recovery

    unregister_camera_recovery = register_camera_recovery(virtual_printer_manager.reconcile_camera_proxies)
    try:
        await virtual_printer_manager.sync_from_db()
        logging.info("Virtual printer manager synced from database")
    except Exception as e:
        logging.warning("Failed to sync virtual printers: %s", e)

    # Start Telegram bot polling
    try:
        print("[MAIN] Starting Telegram bot...")
        from backend.app.services.telegram_bot import start_telegram_bot

        await start_telegram_bot()
        print("[MAIN] Telegram bot startup complete")
    except Exception as e:
        print(f"[MAIN] Failed to start Telegram bot: {e}")
        import traceback

        traceback.print_exc()

    # Start Obico AI failure-detection loop. The loop itself is opt-in (gated by
    # the ``obico_enabled`` setting); we always launch it so it can pick up the
    # toggle without a restart.
    try:
        from backend.app.services.obico_detection import obico_detection_service
        from backend.app.services.obico_history import obico_history

        try:
            await obico_history.start()
        except Exception:
            logging.getLogger(__name__).exception("Failed to start Obico evidence recorder; detection will continue")
        # The ML API fetches frames from this server: no poll before it serves requests.
        await obico_detection_service.start(serving=lambda: farm_startup_guard.ready)
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to start Obico detection service: %s", e)
    # OctoEverywhere AI failure detection, opt-in by ``octoeverywhere_enabled`` like Obico's loop.
    try:
        from backend.app.services.octoeverywhere_detection import octoeverywhere_detection_service

        await octoeverywhere_detection_service.start()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to start OctoEverywhere detection service: %s", e)
    # A restore that turned OctoEverywhere off tells the administrators on this first start.
    from backend.app.services.failure_detection_restore import deliver_restore_notice

    spawn_background_task(deliver_restore_notice(), name="octoeverywhere-restore-notice")

    # Daily archive 3MF cleanup loop (gated internally by the
    # ``archive_3mf_retention_enabled`` setting; always launched so a
    # toggle takes effect without a restart).
    try:
        from backend.app.services.archive_cleanup_service import archive_cleanup_service

        archive_cleanup_service.start()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to start archive cleanup service: %s", e)

    # Library trash sweeper + auto-purge (#1008). Runs every 15 min: hard-deletes
    # rows whose deleted_at is older than the retention window AND, if enabled,
    # moves files older than the auto-purge threshold to trash (24h-throttled).
    try:
        from backend.app.services.library_trash import library_trash_service

        await library_trash_service.start_scheduler()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to start library trash sweeper: %s", e)

    # Archive auto-purge sweeper (#1008 follow-up). Same 15-min cadence,
    # 24h-throttled, opt-in via Settings.
    try:
        from backend.app.services.archive_purge import archive_purge_service

        await archive_purge_service.start_scheduler()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to start archive auto-purge sweeper: %s", e)

    # Cloud Link (opt-in, off by default). ``start()`` reads the pairing row
    # and returns having done nothing unless the link is enabled, paired and
    # not revoked — so an upgrade never connects a farm to anything.
    try:
        from backend.app.services.cloud_link.service import cloud_link_service

        await cloud_link_service.start()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to start Cloud Link: %s", e)

    from backend.app.services.preview_runtime import start_preview_runtime, stop_preview_runtime_within

    await start_preview_runtime(Path(app_settings.base_dir), Path(app_settings.app_dir))
    from backend.app.services.library_preview_jobs import start_library_preview_jobs, stop_library_preview_jobs_within

    start_library_preview_jobs()  # volatile: nothing from an earlier process is replayed
    from backend.app.services.analysis_runtime import start_analysis_runtime, stop_analysis_runtime
    from backend.app.services.local_worker_broker import stop_local_worker_broker

    await start_analysis_runtime(Path(app_settings.base_dir))
    from backend.app.services.assembly.runtime import start_assembly_runtime, stop_assembly_runtime

    try:
        await start_assembly_runtime(Path(app_settings.base_dir))
    except Exception:
        logging.getLogger(__name__).exception("Assembly service unavailable at startup")
    from backend.app.services.library_file_runtime import start_library_file_runtime, stop_library_file_runtime

    try:
        await start_library_file_runtime(Path(app_settings.base_dir))
    except Exception:
        logging.getLogger(__name__).exception("Library file service unavailable at startup")
    from backend.app.services.printer_file_lanes import printer_file_runtime

    # Only lifespan startup reopens printer file admission (a refused restore drain
    # left it closed on purpose).
    try:
        printer_file_runtime.reopen()
    except RuntimeError:
        logging.getLogger(__name__).exception("Printer file operations still running at startup")
    from backend.app.services.archive_media_runtime import archive_media_runtime

    # The same rule for archive video work (G10 C4).
    try:
        archive_media_runtime.reopen()
    except RuntimeError:
        logging.getLogger(__name__).exception("Archive video operations still running at startup")
    from backend.app.services import library_scan

    # And for folder scans, which a restore closes for the life of the process.
    if not library_scan.open_admission():
        logging.getLogger(__name__).error("Folder scans still running at startup; scans stay closed")
    # Download jobs live in memory (G10 D): what an earlier run left on disk goes, with every
    # grant it issued, before the first job of this one is admitted.
    from backend.app.core.auth import purge_media_download_grants
    from backend.app.services.media_downloads import clean_scratch, media_download_runtime

    try:
        media_download_runtime.reopen()
        removed, kept = await asyncio.to_thread(clean_scratch, media_download_runtime.scratch_root())
        if removed or kept:
            logging.getLogger(__name__).info(
                "Download scratch: %d leftover job directories removed, %d foreign entries left alone", removed, kept
            )
        await purge_media_download_grants()
    except Exception:
        logging.getLogger(__name__).exception("Download jobs could not be reset at startup")
    # Edit jobs live in memory too (G10 E): the scratch of an earlier run goes, with the staged
    # output each directory named.
    from backend.app.services import timelapse_editor
    from backend.app.services.timelapse_editor import timelapse_edit_runtime

    try:
        from backend.app.services.encoder_slot import encoder_slot

        encoder_slot.reset()  # a new process has no encoder running
        timelapse_edit_runtime.reopen()
        removed, kept = await asyncio.to_thread(
            timelapse_editor.clean_scratch, timelapse_edit_runtime.scratch_root(), app_settings.base_dir
        )
        if removed or kept:
            logging.getLogger(__name__).info(
                "Edit scratch: %d leftover job directories removed, %d foreign entries left alone", removed, kept
            )
    except Exception:
        logging.getLogger(__name__).exception("Timelapse edit jobs could not be reset at startup")
    from backend.app.services.part_render_scheduler import start_part_render, stop_part_render

    try:
        await start_part_render(Path(app_settings.base_dir), Path(app_settings.app_dir))
    except Exception:
        logging.getLogger(__name__).exception("Part render service unavailable at startup")
    # What a killed process left of a streamed model import (its own old ``.part`` files only).
    try:
        from backend.app.services import model_import

        swept = await asyncio.to_thread(model_import.sweep_stale_staging, create=False)
        if swept:
            logging.getLogger(__name__).info("Removed %d unfinished model import file(s)", swept)
    except Exception:
        logging.getLogger(__name__).exception("Model import staging sweep failed at startup")

    yield

    # Shutdown
    from backend.app.services.auto_queue_return_runtime import auto_queue_return_runtime

    auto_queue_return_runtime.close_admission()
    if not await auto_queue_return_runtime.quiesce_for_restore(10):
        logging.getLogger(__name__).warning("AutoQueue return shutdown work remains owned")
    # Spoolman deletes and print charges: closed and drained before network and DB resources go (C047 S35).
    from backend.app.services.spoolman_delete import delete_runtime as spoolman_delete_runtime

    if not await spoolman_delete_runtime.quiesce_for_restore(10):
        logging.getLogger(__name__).warning("Spoolman delete or charge shutdown work remains owned")
    from backend.app.services.print_run_binding import revoke_print_effects
    from backend.app.services.print_state_observer import print_state_observer

    # The observer reads what the services below own: it goes before they do.
    await print_state_observer.stop()
    # An in-memory repair whose client went away still owes its outcome row: let it finish while the run
    # registries and the database are still here (its compare-and-clear is fenced by the revocation below).
    from backend.app.services.print_state_recovery_store import drain_runtime_operations

    await drain_runtime_operations(timeout=10)
    revoke_print_effects(printer_manager)
    await stop_part_render()  # its attempt ends with its proof before any worker or the broker goes
    from backend.app.services import part_images

    await part_images.shutdown()  # events and photo removals already started finish, or are cancelled
    from backend.app.services import library_file_photos

    await library_file_photos.shutdown()  # photo files of purges that committed: started removals finish
    from backend.app.services import label_gate

    await label_gate.shutdown()  # the label workers: queued draws dropped, a running one ends by itself
    # jobs before the runtime their attempts run on; bounded, so a job stuck on a hung share cannot hold shutdown
    if not await stop_library_preview_jobs_within("shutdown", 10):
        logging.getLogger(__name__).warning("Library preview jobs did not prove their end at shutdown")
    if not await stop_preview_runtime_within(10):  # the same stop a refused restore left running, if any
        logging.getLogger(__name__).warning("Preview runtime did not prove its end at shutdown")
    await ws_manager.shutdown()
    # Cloud Link first: it holds a socket and describes this farm, so it should
    # let go before the services it describes start disappearing underneath it.
    # ``stop()`` cancels the client task rather than waiting on its stop event
    # — the wait can be ~35 s inside a handshake, longer than the grace period
    # Docker and systemd give us.
    try:
        from backend.app.services.cloud_link.service import cloud_link_service

        await cloud_link_service.stop()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to stop Cloud Link: %s", e)
    # Stop Telegram bot — bounded, because a poller sitting in a getUpdates
    # long poll would otherwise spend the whole shutdown grace period here.
    try:
        from backend.app.services.telegram_bot import stop_telegram_bot_bounded

        await stop_telegram_bot_bounded()
    except Exception:
        pass
    print_scheduler.stop()
    auto_queue_scheduler.stop()
    for task in (app.state.print_scheduler_task, app.state.auto_queue_scheduler_task):
        task.cancel()
    await asyncio.gather(app.state.print_scheduler_task, app.state.auto_queue_scheduler_task, return_exceptions=True)
    stock_forecast_alerts.stop()
    await background_dispatch.stop()
    from backend.app.services.macro_trigger import stop_event_macro_tasks
    from backend.app.services.print_completion_tasks import stop_print_completion_tasks

    await asyncio.gather(
        stop_print_completion_tasks(),
        stop_event_macro_tasks(),
        smart_plug_manager.stop_auto_off_tasks(),
        stop_bed_cooldown_tasks(),
    )
    await smart_plug_manager.stop_scheduler()
    await ha_sensor_manager.stop()
    await location_ha_sensor_manager.stop()
    try:
        from backend.app.services.zigbee.coordinator import zigbee_coordinator
        from backend.app.services.zigbee.poller import zigbee_poller
        from backend.app.services.zigbee.supervisor import zigbee_supervisor

        # Supervisor first, before anything it might restart: left running, it
        # would answer the coordinator's stop by bringing the radio back up
        # mid-teardown, and hold the lock while doing it.
        await zigbee_supervisor.stop()
        # Poller next: it reads through the radio the coordinator owns, so
        # stopping that out from under an in-flight read is how shutdown starts
        # logging exceptions nobody can act on.
        await zigbee_poller.stop()
        await zigbee_coordinator.stop()
    except Exception:
        pass
    try:
        from backend.app.services.obico_detection import obico_detection_service

        await obico_detection_service.quiesce(timeout=None)
    except Exception:
        pass
    try:
        from backend.app.services.octoeverywhere_detection import octoeverywhere_detection_service

        await octoeverywhere_detection_service.quiesce(timeout=None)
    except Exception:
        pass
    try:
        from backend.app.services.archive_cleanup_service import archive_cleanup_service

        await archive_cleanup_service.stop()
    except Exception:
        pass
    try:
        from backend.app.services.library_trash import library_trash_service

        library_trash_service.stop_scheduler()
    except Exception:
        pass
    try:
        from backend.app.services.firmware_batch import firmware_batch_service

        await firmware_batch_service.shutdown()
    except Exception:
        pass
    try:
        from backend.app.services.archive_purge import archive_purge_service

        archive_purge_service.stop_scheduler()
    except Exception:
        pass
    notification_service.stop_digest_scheduler()
    await notify_live_activities.close()
    await notify_widgets.close()
    if not await git_backup_service.quiesce_for_restore():
        logging.warning("Git backup shutdown work remains owned")
    local_backup_service.stop_scheduler()
    stop_ams_history_recording()
    stop_filament_preset_sync()
    stop_printer_sensor_history_recording()
    from backend.app.services.connection_watchdog import stop_connection_watchdog

    await stop_connection_watchdog()
    stop_runtime_tracking()
    # Live fan-out must unsubscribe while the worker control channel still
    # exists. Otherwise a normal shutdown looks like a crashed pump.
    try:
        from backend.app.services.camera_fanout import shutdown_all_broadcasters

        await shutdown_all_broadcasters()
    except Exception as e:
        logging.warning("Failed to shut down camera broadcasters: %s", e)
    try:
        from backend.app.services.camera_runtime import stop_configured_camera_runtime

        await stop_configured_camera_runtime()
    except Exception as e:
        logging.warning("Failed to stop camera worker runtime: %s", e)
    try:
        from backend.app.services import camera_light

        await camera_light.shutdown()
    except Exception as e:
        logging.warning("Failed to stop the camera-light lease: %s", e)
    # WLED sends and Finished timers are cancelled, its HTTP client closed (N02).
    try:
        await wled_manager.shutdown()
    except Exception as e:
        logging.warning("Failed to stop the WLED manager: %s", e)
    # Held slot unlinks ask again from a timer task (services/slot_unlink_hold).
    try:
        from backend.app.services import slot_unlink_hold

        await slot_unlink_hold.cancel_all()
    except Exception as e:
        logging.warning("Failed to stop the slot unlink re-checks: %s", e)
    # Cancel any pending offline-notification debounce tasks (#1752) so the 60s
    # sleep doesn't outlive the asyncio loop on shutdown.
    for _t in list(_printer_offline_notify_tasks.values()):
        if not _t.done():
            _t.cancel()
    _printer_offline_notify_tasks.clear()
    from backend.app.services.loop_watchdog import stop_loop_watchdog

    stop_loop_watchdog()
    from backend.app.services.telemetry import stop_telemetry

    stop_telemetry()
    stop_expected_prints_cleanup()
    stop_label_reclaim()
    await stop_queue_source_collector()
    await stop_library_objects_backfill()

    from backend.app.services.library_scan import cancel_running_scans

    cancel_running_scans()
    # Download jobs stop their transfers before the printer file lanes are drained (G10 D).
    if not await media_download_runtime.quiesce(10):
        logging.getLogger(__name__).warning("Download jobs did not end before shutdown; admission stays closed")
    # Printer file workers own sessions until their threads end; a drain that is not
    # proven keeps admission closed and is logged, never treated as done (G10 A).
    if not await printer_file_runtime.quiesce(10):
        logging.getLogger(__name__).warning(
            "Printer file operations did not end before shutdown; admission stays closed"
        )
    # Layer recording: no new frame is written and a running stitch is stopped (its frames
    # stay retained) before the seam is drained (G10 F).
    from backend.app.services import layer_timelapse as _layer_timelapse_stop

    await _layer_timelapse_stop.stop_janitor()
    if not await _layer_timelapse_stop.quiesce(15):
        logging.getLogger(__name__).warning("Layer timelapse work did not end before shutdown; recording stays closed")
    # Edit jobs stop their encoders before the seam they publish through is drained (G10 E).
    if not await timelapse_edit_runtime.quiesce(15):
        logging.getLogger(__name__).warning("Timelapse edits did not end before shutdown; admission stays closed")
    # Archive video work is waited for, bounded; an unproven end is logged, never treated as done.
    if not await archive_media_runtime.quiesce(10):
        logging.getLogger(__name__).warning(
            "Archive video operations did not end before shutdown; admission stays closed"
        )
    printer_manager.disconnect_all()
    await stop_library_file_runtime()
    await stop_assembly_runtime()
    await stop_analysis_runtime()
    await stop_local_worker_broker()
    await close_spoolman_client()

    # Stop all virtual printer services
    unregister_camera_recovery()
    await virtual_printer_manager.stop_all()

    await mqtt_smart_plug_service.disconnect(timeout=2)

    await mqtt_relay.disconnect(timeout=2)

    # Drop the shared Bambu Cloud HTTP client we registered at startup.
    set_shared_http_client(None)
    await _shared_cloud_http_client.aclose()

    # Cancel any in-flight slice jobs + drop the shared slicer HTTP client.
    from backend.app.services.slice_dispatch import slice_dispatch as _slice_dispatch
    from backend.app.services.slicer_api import (
        set_shared_http_client as _set_shared_slicer_http_client_off,
    )

    await _slice_dispatch.shutdown()
    _set_shared_slicer_http_client_off(None)
    await _shared_slicer_http_client.aclose()

    # Drop the shared MakerWorld HTTP client.
    from backend.app.services.model_providers.makerworld.service import (
        set_shared_http_client as _set_shared_makerworld_http_client_off,
    )

    _set_shared_makerworld_http_client_off(None)
    await _shared_makerworld_http_client.aclose()

    # Checkpoint WAL and close all database connections (SQLite only — the
    # PRAGMA is not PostgreSQL and used to log a spurious warning there)
    from backend.app.core.db_dialect import is_sqlite as _is_sqlite

    if _is_sqlite():
        try:
            async with engine.begin() as conn:
                await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            logging.info("WAL checkpoint completed")
        except Exception as e:
            logging.warning("WAL checkpoint failed: %s", e)
    await engine.dispose()

    # The bundled PostgreSQL stops last, after every connection is gone:
    # pg_ctl stop -m fast, a clean shutdown with a checkpoint.
    if app_settings.embedded_postgres:
        from backend.app.services import embedded_postgres as _embedded_pg

        await _embedded_pg.stop()


from backend.app.core.schema_repair_state import SchemaRepairPending
from backend.app.services.farm_startup_guard import FarmReadyMiddleware, FarmStartupRefused, farm_startup_guard


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ⚠️ First, before the farm guard, the database or any manager: the start-up heap is frozen
    # once per process, and a server that has started must never be (core/process_gc.py).
    process_gc.prepare_startup_heap()
    process_resources.raise_nofile_limit()
    _protect_uvicorn_log_handlers()
    farm_startup_guard.start(Path(app_settings.data_dir))
    from backend.app.core.auth_runtime import auth_runtime
    from backend.app.services.cloud_credentials import (
        close_admission as close_cloud_admission,
        open_admission as open_cloud_admission,
    )
    from backend.app.services.credential_rollout import credential_rollout

    auth_runtime.close_admission()
    close_cloud_admission()
    try:
        async with _application_lifespan(app):
            from backend.app.mcp_server.runtime import mcp_runtime
            from backend.app.services.connector_runtime import connector_runtime

            await mcp_runtime.reload()
            # A restore's fence is finished (a new storage epoch) before the slicer connector admits anything.
            await connector_runtime.startup()
            if connector_runtime.restore_fenced:
                raise RuntimeError("Authentication restore finalization is pending")
            open_cloud_admission()
            # The poke made inside the application lifespan landed behind the closed door.
            from backend.app.services.filament_preset_sync import request_sync_soon

            request_sync_soon()
            # This only schedules the loop. Technical admission is marked after
            # ASGI startup.complete; notification readiness never gates startup.
            credential_rollout.start(lambda: farm_startup_guard.ready)
            from backend.app.services.live_authority import live_authority

            live_authority.start()
            try:
                yield
            finally:
                auth_runtime.close_admission()
                close_cloud_admission()
                if not await live_authority.shutdown():
                    logging.getLogger(__name__).warning("Live authority cleanup is still owned at shutdown")
                if not await credential_rollout.shutdown():
                    logging.getLogger(__name__).warning("Credential rollout cleanup is still owned at shutdown")
                if not await auth_runtime.shutdown():
                    logging.getLogger(__name__).warning("Authentication cleanup is still owned at shutdown")
                await connector_runtime.shutdown()
                await mcp_runtime.shutdown()
                farm_startup_guard.close_admission()
    except SchemaRepairPending as exc:
        from backend.app.core import schema_repair_maintenance

        auth_runtime.close_admission()
        close_cloud_admission()
        farm_startup_guard.close_admission()
        schema_repair_maintenance.active = True
        logging.getLogger(__name__).warning("Schema repair maintenance: %s", exc.reason)
        try:
            yield
        finally:
            schema_repair_maintenance.active = False
            from backend.app.core.database import engine

            await engine.dispose()
            if app_settings.embedded_postgres:
                from backend.app.services import embedded_postgres as _embedded_pg

                await _embedded_pg.stop()
            farm_startup_guard.finish(clean=False)
    except BaseException:
        auth_runtime.close_admission()
        close_cloud_admission()
        farm_startup_guard.close_admission()
        raise
    else:
        from backend.app.services.farm_startup_guard import drain_commands

        await drain_commands()
        farm_startup_guard.finish(clean=True)


app = FastAPI(
    title=app_settings.app_name,
    description="Archive and manage Bambu Lab 3MF files",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(FarmReadyMiddleware)


from fastapi.exceptions import RequestValidationError  # noqa: E402


@app.exception_handler(RequestValidationError)
async def safe_validation_error_handler(request, exc):
    """Validation describes the field, never echoes credential-bearing input."""
    from fastapi.responses import JSONResponse

    details = []
    for error in exc.errors():
        kind = error["type"]
        location = error.get("loc", ())
        if kind == "extra_forbidden":
            location = (*location[:-1], "[unknown field]")
        details.append(
            {
                "type": kind,
                "loc": location,
                "msg": error["msg"],
            }
        )
    return JSONResponse(status_code=422, content={"detail": details})


@app.exception_handler(FarmStartupRefused)
async def farm_startup_refused_handler(request, exc):
    from fastapi.responses import JSONResponse

    from backend.app.services.filament_intake import routing_detail

    return JSONResponse(
        status_code=503 if exc.reason == "storage_unavailable" else 409, content={"detail": routing_detail(exc.reason)}
    )


@app.exception_handler(ForecastSourceUnavailable)
async def forecast_source_unavailable_handler(request, exc):
    # Spoolman is the inventory and did not answer: every forecast route says so
    # with one typed refusal, never an empty forecast (services/forecast_engine).
    from backend.app.i18n.api_errors import json_error

    return json_error(503, {"error": "spoolman_unreachable", "message": "Spoolman server is not reachable"})


from backend.app.services.spoolman_binding_fence import SpoolBeingDeleted  # noqa: E402


@app.exception_handler(SpoolBeingDeleted)
async def spool_being_deleted_handler(request, exc):
    # A binding or tag writer met a Spoolman spool being deleted (C047 §C3): nothing was
    # written locally; after a confirmed create the answer names the new spool (S37).
    from backend.app.i18n.api_errors import json_error

    if exc.created:
        return json_error(
            409,
            {
                "error": "spool_created_not_bound",
                "spool_id": exc.number,
                "message": "The spool was created in Spoolman but not bound because of a concurrent delete",
            },
        )
    return json_error(409, {"error": "spool_being_deleted", "message": "This spool is being deleted"})


# Every ``HTTPException`` answers in the system language: the English sentence
# raised at the site is looked up in ``data/api_errors_uk.json`` on the way out.
# See ``backend/app/i18n/api_errors.py`` for what is deliberately never translated.
install_api_error_translation(app)


# =============================================================================
# Authentication Middleware - Secures ALL API routes by default
# =============================================================================
# Public routes that don't require authentication even when auth is enabled
PUBLIC_API_ROUTES = {
    # Auth routes needed before/during login
    "/api/v1/auth/status",
    "/api/v1/auth/login",
    "/api/v1/auth/setup",  # Needed for initial setup and recovery
    # Sliding-session refresh (§18.14): gated by the HttpOnly refresh cookie,
    # not by bearer; /login→/refresh must not be bounced by the middleware
    # when the access token has already expired (that's the whole point).
    "/api/v1/auth/refresh",
    # Advanced auth status needed for login page
    "/api/v1/auth/advanced-auth/status",
    # Both halves of self-service recovery. ⚠️ The confirm half MUST be here:
    # it is reached by somebody who cannot sign in — that is the whole
    # premise — so leaving it behind the auth gate makes the reset link
    # answer 401 to the only person who would ever click it.
    "/api/v1/auth/forgot-password",
    "/api/v1/auth/forgot-password/confirm",
    # Version check for updates (no sensitive data)
    "/api/v1/updates/version",
    # Metrics endpoint handles its own prometheus_token authentication
    "/api/v1/metrics",
}

# Route prefixes that are public (for routes with dynamic segments)
PUBLIC_API_PREFIXES = [
    # WebSocket connections handle their own auth
    "/api/v1/ws",
]

# Route patterns that are public — ANCHORED regexes, one per intended route,
# matched with ``re.match`` against the RAW request path.
#
# Needed because a browser loads images and video via ``<img src>`` /
# ``<video src>``, which cannot carry an Authorization header: those routes
# take a token in the query string instead, and the middleware has to let the
# request REACH that gate. Matching here only skips the blanket JWT gate — the
# route's own ``RequirePermission`` / stream-token / overlay-token / nonce
# dependency still runs, and a route with none of those does not belong here…
# or the route is deliberately ANONYMOUS — an archive thumbnail, a MakerWorld
# cover, the OIDC button's icon — and then its entry says so, and the table in
# ``test_auth_public_patterns.py`` tags it ``anonymous`` so nobody has to guess
# later whether a missing gate was a decision or an oversight.
#
# ⚠️ These used to be plain substrings tested with ``in path``, which matched
# ANYWHERE in the path. Nothing was exposed (every route kept its own gate) but
# the middleware's defence in depth was silently absent on routes nobody had
# listed: ``"/timelapse"``, commented as the video, also opened the six
# timelapse write routes; ``"/camera/stream"`` opened
# ``POST /printers/camera/stream-token``; ``"/auth/oidc/providers"`` opened the
# provider CRUD; and ``"/thumbnail"`` was satisfied by a segment the CLIENT
# names (``…/card-download/thumbnail.txt``).
#
# Rules for adding one:
#   * anchor both ends — ``^/api/v1/...$``;
#   * ``\d+`` for an int path param, ``[^/]+`` for one string segment;
#   * ``.+`` ONLY where the ROUTE ITSELF declares ``{x:path}``, and always
#     behind that route's fixed segment, so a client tail cannot widen it;
#   * the middleware runs BEFORE routing — there is no matched route to read
#     here, only the raw path, which is why anchoring is the whole defence.
#
# ``backend/tests/test_auth_public_patterns.py`` holds the table of every route
# these are meant to open and fails on drift in either direction.
PUBLIC_API_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ⚠️ An entry lets a header-less request REACH its route; it opens nothing by
    # itself. Pictures and videos that are not the camera are gated by the
    # route's MEDIA-token dependency (``require_media_*`` — a signed-in user's
    # token in ``?token=``, or the ordinary headers, then the resource's own
    # permission and ownership rule; audit D9 a2). The camera routes keep the
    # stream token. Anonymous by decision: a notification's short-lived photo
    # copy, external-link icons and the OIDC button icon.
    # Thumbnails
    re.compile(r"^/api/v1/archives/\d+/thumbnail$"),
    re.compile(r"^/api/v1/library/files/\d+/thumbnail$"),
    re.compile(r"^/api/v1/archives/\d+/plate-thumbnail/\d+$"),
    re.compile(r"^/api/v1/library/files/\d+/plate-thumbnail/\d+$"),
    # <img> loaded like its siblings; its handler always claimed this
    re.compile(r"^/api/v1/archives/\d+/plate-preview$"),
    # Images and media
    re.compile(r"^/api/v1/archives/\d+/photos/[^/]+$"),
    # A notification's short-lived photo copy: anonymous, the unguessable file
    # name is the credential (utils/notification_photos).
    re.compile(r"^/api/v1/notifications/photos/[^/]+$"),
    # A browser's native download of a prepared job (G10 D): a navigation carries no
    # header. The first segment is a one-time grant, spent by the route's own dependency.
    re.compile(r"^/api/v1/media-downloads/[A-Za-z0-9_-]+/[^/]+$"),
    # ``{image_path:path}`` — the ONLY reason a ``.+`` appears here, and it sits
    # behind the route's own fixed segment.
    re.compile(r"^/api/v1/archives/\d+/project-image/.+$"),
    # /library/files/{id}/card-file/{zip_path} — the model card's pictures,
    # loaded by <img> from the card dialog. ``{zip_path:path}`` again; the
    # route's own media-token gate is what authenticates it, this entry only
    # lets the request reach that gate.
    re.compile(r"^/api/v1/library/files/\d+/card-file/.+$"),
    # /library/files/{id}/image — the full picture of an image file, loaded by
    # <img> from the File Manager's viewer. Serves picture types only; the
    # bearer /download beside it has no pattern and stays behind the header.
    re.compile(r"^/api/v1/library/files/\d+/image$"),
    # /library/files/{id}/photos/{photo}/image and /thumbnail — a photo attached
    # to a library file, loaded by <img> from the details dialog. Each checks
    # the parent file's visibility; the list and the writes have no pattern.
    re.compile(r"^/api/v1/library/files/\d+/photos/\d+/image$"),
    re.compile(r"^/api/v1/library/files/\d+/photos/\d+/thumbnail$"),
    # /products/{id}/attachment-image/{filename} — a product's gallery
    # pictures, loaded by <img> from the product page. Same reasoning: the
    # bearer-only attachment download deliberately lives under /attachments/
    # instead, so no pattern here can reach it.
    re.compile(r"^/api/v1/products/\d+/attachment-image/[^/]+$"),
    # The product and order covers, both media-token routes. The write methods
    # share these paths and ride in with them — the middleware sees a path, not
    # a method — and are stopped by their own products:update / orders:update permission.
    re.compile(r"^/api/v1/products/\d+/cover-image$"),
    re.compile(r"^/api/v1/product-parts/\d+/image$"),
    re.compile(r"^/api/v1/products/\d+/files/\d+/plates/\d+/objects/\d+/image$"),
    re.compile(r"^/api/v1/projects/\d+/cover-image$"),
    re.compile(r"^/api/v1/archives/\d+/qrcode$"),
    # The timelapse VIDEO only. The six timelapse routes beside it
    # (info/process/scan/select/upload/thumbnails) are ordinary bearer reads
    # and writes and are NOT public — a bare "/timelapse" substring opened
    # every one of them.
    re.compile(r"^/api/v1/archives/\d+/timelapse$"),
    # /printers/{id}/camera-cover — the running job's cover picture. The
    # segment is unique on purpose: it was ``/cover``, whitelisted as the bare
    # substring "/cover", which is unanchorable to one route.
    re.compile(r"^/api/v1/printers/\d+/camera-cover$"),
    re.compile(r"^/api/v1/external-links/\d+/icon$"),
    # The OIDC button's icon on the LOGIN page, so it must load with no session.
    # Narrow: the provider CRUD beside it is settings work behind SETTINGS_*.
    re.compile(r"^/api/v1/auth/oidc/providers/\d+/icon$"),
    # Streaming-overlay status feed (upstream #2613). OBS is a fresh browser
    # with no session, so the overlay carries a long-lived ``overlay``-scoped
    # token in the URL instead of a JWT. The route's own RequireOverlayToken
    # gate is what authenticates it; this entry only lets it be reached.
    re.compile(r"^/api/v1/printers/\d+/overlay-status$"),
    # The installation's logo that overlay draws, under the SAME overlay token (RequireOverlayToken) — the one
    # picture a long-lived token opens, by the owner's decision (2026-11-15): branding, not a user's resource.
    # This one file only: the upload / remove routes live under /settings/overlay-logo and are not public.
    re.compile(r"^/api/v1/overlay-branding/logo$"),
    # Cam Wall kiosk feed (upstream #2531). Same reasoning as the overlay feed
    # above: a TV or Pi has no login session, so the wall carries a long-lived
    # ``camwall``-scoped token in the URL, checked by RequireCamWallToken.
    re.compile(r"^/api/v1/camwall/printers$"),
    # ...and the standalone cameras beside them on the same wall. Same token,
    # same reasoning; the list carries no URL, because an RTSP camera's
    # credentials live inside one.
    re.compile(r"^/api/v1/camwall/cameras$"),
    # Only these anchored read feeds validate a monitor-scoped Bearer grant.
    re.compile(r"^/api/v1/monitor/kiosk/(?:snapshot|forecast)$"),
    # Camera (streams loaded via <img> tag). ⚠️ NOT the token MINTER beside
    # them — ``POST /printers/camera/stream-token`` needs CAMERA_VIEW and is
    # what a substring "/camera/stream" quietly opened.
    re.compile(r"^/api/v1/printers/\d+/camera/stream$"),
    re.compile(r"^/api/v1/printers/\d+/camera/snapshot$"),
    # A standalone camera's stream and snapshot, loaded by the same <img> tags
    # and gated by the same stream token. NOT its CRUD, which is settings work.
    re.compile(r"^/api/v1/cameras/\d+/stream$"),
    re.compile(r"^/api/v1/cameras/\d+/snapshot$"),
    # A plate-detection reference picture, shown by <img> in the same settings
    # panel and gated by the same stream token.
    re.compile(r"^/api/v1/printers/\d+/camera/plate-detection/references/\d+/thumbnail$"),
    # Slicer token-authenticated downloads — protocol handlers
    # (bambustudioopen://, orcaslicer://) cannot send auth headers. These
    # endpoints validate a short-lived download token in the URL path instead.
    re.compile(r"^/api/v1/archives/\d+/dl/[^/]+/[^/]+$"),
    # The source 3MF beside it has a segment of its own, and so needs a
    # pattern of its own: without it the slicer's header-less request was 401'd
    # here, before the route's token check ran — "Open source 3MF in slicer"
    # could not work on any install (upstream #3029).
    re.compile(r"^/api/v1/archives/\d+/source-dl/[^/]+/[^/]+$"),
    re.compile(r"^/api/v1/library/files/\d+/dl/[^/]+/[^/]+$"),
    # 2FA + OIDC endpoints consumed by the login page before the user has a
    # JWT. /2fa/verify trades a pre-auth token for a JWT and /2fa/email/send
    # mails the code for it; /oidc/providers lists enabled OIDC buttons;
    # /oidc/authorize/{id} starts the PKCE flow; /oidc/callback lands from the
    # identity provider; /oidc/exchange swaps the bridge token for a JWT. All
    # carry their own short-lived token binding, so the middleware can skip
    # them safely.
    # ⚠️ ``/2fa/email/send`` is listed here because the login page calls it with
    # no session. The substring list named ``/auth/2fa/send-code``, a route
    # that has never existed — so email OTP could not send its code at all.
    re.compile(r"^/api/v1/auth/2fa/verify$"),
    re.compile(r"^/api/v1/auth/2fa/email/send$"),
    re.compile(r"^/api/v1/auth/oidc/providers$"),
    re.compile(r"^/api/v1/auth/oidc/authorize/\d+$"),
    re.compile(r"^/api/v1/auth/oidc/callback$"),
    re.compile(r"^/api/v1/auth/oidc/exchange$"),
    # Obico ML API fetches JPEG frames by one-shot nonce (issue #172 follow-up).
    # The nonce itself is the credential: 32-byte random, single-use, ~30s TTL.
    re.compile(r"^/api/v1/obico/cached-frame/[^/]+$"),
    # MakerWorld covers and the thumbnail proxy (B.5 — 0.5.x cycle). <img> tags
    # can't send Authorization headers and would 401 every image here; the
    # routes' own media-token gates authenticate them (the covers under the
    # library file's ownership, the proxy under MakerWorld's view permission),
    # and the proxy's SSRF guard still restricts the upstream to the CDN
    # allowlist.
    re.compile(r"^/api/v1/makerworld/imports/\d+/cover$"),
    re.compile(r"^/api/v1/makerworld/imports/\d+/cover-variant$"),
    re.compile(r"^/api/v1/makerworld/thumbnail$"),
    # A Manyfold model's preview for the catalogue's <img> tags: the route's own
    # media-token gate (the source's view permission) authenticates it. The
    # picture is addressed by model id — the request names no address — and is
    # fetched from the configured instance only.
    re.compile(r"^/api/v1/model-sources/manyfold/models/[A-Za-z0-9_-]{1,64}/preview$"),
)


# NOTE: security_headers_middleware is registered *after* auth_middleware below
# so it becomes the outermost layer and its headers also apply to the early
# JSONResponse returns from auth_middleware (401 auth-required, 503 setup-required).
# Starlette middleware order: the LAST @app.middleware("http")-decorated function
# is the OUTERMOST, so its post-call_next response patch runs on every response.


# Setup-gate cache - True once we've confirmed at least one admin exists.
# Kept process-local because /auth/setup invalidates it via
# invalidate_setup_gate_cache() and restarts reset it automatically.
_has_admin_cache: bool | None = None


def invalidate_setup_gate_cache() -> None:
    """Drop the cached admin-presence flag.

    Called by /auth/setup after creating the first admin, and by any endpoint
    that deletes the last remaining admin, so the middleware re-checks the DB
    on the next request.
    """
    global _has_admin_cache
    _has_admin_cache = None


# Routes that remain reachable while the system is unconfigured (no admin yet).
# These MUST stay in lockstep with the frontend bootstrap so Setup can be
# completed without an admin context.
SETUP_WHITELIST_ROUTES = {
    "/api/v1/auth/status",
    "/api/v1/auth/setup",
    "/api/v1/system/health",
}


@app.middleware("http")
async def auth_middleware(request, call_next):
    """Enforce authentication at the API gateway.

    Two-stage gate:

    1. **Setup gate** - if no admin user exists, reject every API request with
       503 except those in ``SETUP_WHITELIST_ROUTES``. This forces the user
       through the initial admin creation flow.

    2. **Auth gate** - once at least one admin exists, every non-public API
       route requires either a valid JWT or an API key.
    """
    from starlette.responses import JSONResponse

    path = request.url.path

    # Only apply to API routes
    if not path.startswith("/api/"):
        return await call_next(request)

    # --- Setup gate --------------------------------------------------------
    # Runs ahead of the public-route allowlist so that even "/api/v1/auth/login"
    # is blocked until setup completes - a login attempt before setup is a bug,
    # not a legitimate request.
    global _has_admin_cache
    if _has_admin_cache is not True:
        try:
            async with async_session() as db:
                from backend.app.core.auth import has_any_admin

                has_admin = await has_any_admin(db)
            _has_admin_cache = has_admin
        except Exception:
            # If we can't determine admin presence (e.g. DB not yet ready),
            # fail closed only for the setup gate's sake by assuming "no admin"
            # - that routes the user to /setup where the error will surface
            # clearly rather than masquerading as a 401 elsewhere.
            has_admin = False
            _has_admin_cache = None  # don't poison the cache on transient errors

        if not has_admin:
            if path in SETUP_WHITELIST_ROUTES:
                return await call_next(request)
            return JSONResponse(
                status_code=503,
                content={"detail": "setup_required"},
            )

    # Allow public routes
    if path in PUBLIC_API_ROUTES:
        return await call_next(request)

    # Allow public prefixes
    for prefix in PUBLIC_API_PREFIXES:
        if path.startswith(prefix):
            return await call_next(request)

    # Allow public patterns (read-only display data like thumbnails).
    # ``fullmatch``, never ``search``: the patterns are anchored, and a search
    # would put the old substring hole back in a costlier form. ⚠️ Not ``match``
    # either — the trailing ``$`` every pattern carries also matches immediately
    # BEFORE a final newline, and the ASGI path is percent-DECODED, so a request
    # for ``…/thumbnail%0A`` arrived here as ``…/thumbnail\n`` and skipped the
    # gate. Such a path DOES route — Starlette's own route regex ends in ``$``
    # and is ``.match``ed, so ``…/thumbnail\n`` reached the thumbnail handler.
    # What made it harmless is narrower: the only paths ``match`` let in were a
    # whitelisted route plus a trailing newline, which route to the very handler
    # that entry was written for, so no route OUTSIDE the whitelist was ever
    # opened and each route's own gate applied either way. The whitelist's
    # promise is still "exactly these paths", and ``fullmatch`` is what makes it
    # exactly.
    if any(pattern.fullmatch(path) for pattern in PUBLIC_API_PATTERNS):
        return await call_next(request)

    # --- Auth gate ---------------------------------------------------------
    auth_header = request.headers.get("Authorization")

    from backend.app.core.auth import (
        APIKeyValidationFailure,
        finish_request_auth,
        resolve_api_key_authority,
        selected_api_key,
    )

    key = selected_api_key(request)
    if key is not None:
        try:
            try:
                await resolve_api_key_authority(request, key)
            except APIKeyValidationFailure as exc:
                overloaded = exc.reason == "overloaded"
                return JSONResponse(
                    status_code=503 if overloaded else 401,
                    content={"detail": "API key validation is busy" if overloaded else "Invalid API key"},
                    headers={"Retry-After": "5"} if overloaded else {"WWW-Authenticate": "Bearer"},
                )
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
            return await call_next(request)
        finally:
            await finish_request_auth(request)

    # Check for JWT auth
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # The same complete validation is reused by route dependencies below.
    from backend.app.core.auth import JWTValidationFailure, finish_request_auth, resolve_jwt_authority

    try:
        try:
            await resolve_jwt_authority(request, auth_header.removeprefix("Bearer "))
        except JWTValidationFailure as exc:
            if exc.reason == "expired":
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Token has expired"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if exc.reason in {"missing_user", "inactive"}:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "User not found or inactive"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # A revoked session, an older generation, a token that names no session. The
            # browser reads this text as "try the refresh cookie, and sign out when that is
            # refused too"; under "Invalid token" (which it does not know, and which the
            # Bambu Cloud routes use for THEIR credential) a tab whose session had been ended
            # stayed signed in on stale data until its access token ran out.
            return JSONResponse(
                status_code=401,
                content={"detail": "Could not validate credentials"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        except HTTPException as exc:
            # Infrastructure failure is retryable, not a false credential
            # refusal that causes clients to discard otherwise valid sessions.
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        return await call_next(request)
    finally:
        await finish_request_auth(request)


_security_headers_logger = logging.getLogger("backend.app.main.security_headers")


def _parse_trusted_frame_origins() -> tuple[str, ...]:
    """Parse ``TRUSTED_FRAME_ORIGINS`` env var into a validated allowlist (#1191).

    Format: comma-separated list of ``scheme://host[:port]`` origins.

    Used by ``security_headers_middleware`` to relax ``frame-ancestors`` for
    trusted same-LAN deployments (typically Home Assistant Webpage panel
    embedding BamDude on a different port). Defaults to empty — strict
    ``'none'``.

    Validation is strict by design — only ``http(s)``, no paths, no query/
    fragment, no wildcards. Invalid entries are dropped with a warning rather
    than failing startup, so a typo in one origin doesn't take the whole
    deployment down.
    """
    raw = os.environ.get("TRUSTED_FRAME_ORIGINS", "").strip()
    if not raw:
        return ()
    valid: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            parsed = urlparse(candidate)
        except ValueError as e:
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — %s", candidate, e)
            continue
        if parsed.scheme not in ("http", "https"):
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — must be http(s)", candidate)
            continue
        if not parsed.netloc:
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — missing host", candidate)
            continue
        if parsed.path and parsed.path != "/":
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — paths not allowed", candidate)
            continue
        if parsed.query or parsed.fragment:
            _security_headers_logger.warning(
                "TRUSTED_FRAME_ORIGINS: dropping %r — query/fragment not allowed", candidate
            )
            continue
        if "*" in parsed.netloc:
            _security_headers_logger.warning("TRUSTED_FRAME_ORIGINS: dropping %r — wildcards not allowed", candidate)
            continue
        valid.append(f"{parsed.scheme}://{parsed.netloc}")
    if valid:
        _security_headers_logger.info("TRUSTED_FRAME_ORIGINS: %s", ", ".join(valid))
    return tuple(valid)


_TRUSTED_FRAME_ORIGINS: tuple[str, ...] = _parse_trusted_frame_origins()


def _frame_ancestors(default_value: str) -> str:
    """Compose the ``frame-ancestors`` CSP directive (#1191).

    ``default_value`` is the strict directive used when the operator has not
    configured ``TRUSTED_FRAME_ORIGINS`` — typically ``'none'``. When trusted
    origins are configured, ``'self'`` is always included so same-origin
    embedding never breaks even if an operator forgets to add their own
    origin to the list.
    """
    if _TRUSTED_FRAME_ORIGINS:
        return "frame-ancestors 'self' " + " ".join(_TRUSTED_FRAME_ORIGINS) + ";"
    return f"frame-ancestors {default_value};"


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    """Add HTTP security headers + Content-Security-Policy to every response.

    Registered AFTER auth_middleware so it runs OUTERMOST — meaning it also
    patches the early JSONResponse returns auth_middleware uses for 401/503.

    CSP notes:
    - ``script-src 'self'``: hard XSS-exfiltration guard. Inline scripts are
      not allowed; the SW registration script lives at ``/sw-register.js``
      (see ``serve_sw_register`` below) so the strict directive holds.
    - ``style-src 'unsafe-inline'``: React + several UI libs inject inline
      styles at runtime; we cannot drop this without rewriting them.
    - ``connect-src 'self' ws: wss:``: API + the same-origin /api/v1/ws
      WebSocket. ``ws:``/``wss:`` is permissive on protocol but not host —
      Safari historically does not accept ``'self'`` for WebSockets, hence
      the explicit scheme allow.
    - ``img-src``/``media-src`` accept ``data:``/``blob:`` for base64
      thumbnails and Blob-URL timelapse previews.
    - ``frame-src 'self' http: https:``: BamDude embeds Spoolman via
      reverse-proxy (same origin) and arbitrary external links from the
      sidebar. ``http:`` is allowed because self-hosted Spoolman typically
      runs on plain HTTP on a LAN address (upstream #1054). ``frame-ancestors``
      below still blocks BamDude being framed cross-origin (default ``'none'``)
      — that's the clickjacking defense that actually matters.
    - ``frame-ancestors``: by default ``'none'`` — nobody may embed BamDude.
      This is the modern equivalent of ``X-Frame-Options: DENY``. Operators
      can opt into trusted-origin embedding (e.g. HA Webpage panel) via the
      ``TRUSTED_FRAME_ORIGINS`` env var (#1191); when set, the directive
      becomes ``'self' <list>`` and ``X-Frame-Options`` is dropped (legacy
      ``ALLOW-FROM`` syntax is deprecated and inconsistent across vendors —
      modern browsers honour ``frame-ancestors`` which takes precedence).
    """
    # Per-request nonce stamped into the SPA ``script-src`` (#1460). On its own
    # this changes nothing for our own pages — index.html has no inline scripts
    # since SW registration moved to /sw-register.js. It's here for Cloudflare:
    # a CF-fronted deployment has the bot-detection script injected into the
    # HTML on the edge with a fresh hash every load (so hashes can't be
    # allowlisted). When CF sees a nonce in our CSP it clones the same nonce
    # onto its injected <script>, so it passes without us needing 'unsafe-inline'.
    # https://developers.cloudflare.com/cloudflare-challenges/challenge-types/javascript-detections/#if-you-have-a-content-security-policy-csp
    csp_nonce = secrets.token_urlsafe(16)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not _TRUSTED_FRAME_ORIGINS:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # ⚠️ The streaming overlay is embedded SAME-ORIGIN by the URL builder's
    # preview in Settings. Every SPA route otherwise sends
    # ``frame-ancestors 'none'``, which is stricter than the SAMEORIGIN in
    # ``X-Frame-Options`` beside it and refuses even a same-origin frame — the
    # preview showed the browser's "another site has embedded it" page instead
    # of the overlay.
    #
    # ``'self'`` only permits a framer on THIS origin, which is BamDude's own
    # UI, so a clickjacking page on another host is blocked exactly as before.
    # (The overlay draws status over a camera feed and has no interactive
    # element to bait a click into, even from a same-origin framer.)
    # Cross-origin embedding — Home Assistant on another port — remains what
    # ``TRUSTED_FRAME_ORIGINS`` is for, and ``_frame_ancestors`` already folds
    # that allowlist in. Every other path keeps ``'none'``.
    embeddable_same_origin = request.url.path.startswith("/overlay/")

    if request.url.path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        # FastAPI's built-in Swagger UI / ReDoc pages load assets from
        # cdn.jsdelivr.net and bootstrap with an inline <script>, so the
        # default CSP would render a blank page (audit A.38).
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: blob: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "connect-src 'self'; "
            "font-src 'self' data:; "
            "worker-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; " + _frame_ancestors("'none'")
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{csp_nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-src 'self' http: https:; " + _frame_ancestors("'self'" if embeddable_same_origin else "'none'")
        )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def trace_id_middleware(request, call_next):
    """Stamp every HTTP request with a trace ID and echo it back (audit B.12).

    Decorated AFTER auth_middleware + security_headers_middleware on
    purpose: Starlette stacks @app.middleware decorators LIFO, so the
    last-decorated runs first inbound. Putting the trace stamp last
    makes it the OUTERMOST layer, which means every line emitted on the
    way down to the route handler — including the auth-middleware's
    early 401/503 returns and the security-headers stamping — all carry
    the same trace ID. If we put it before auth, those layers' logs
    would be stamped with the *previous* request's ID — useless for
    correlation.

    Honours an inbound ``X-Trace-Id`` header so callers running their
    own tracing can correlate their span IDs with our log lines, but
    only if the value passes the whitelist gate in
    ``backend.app.core.trace.normalise_inbound_trace_id`` — anything
    rejected (too long, contains control chars, etc.) silently triggers
    a freshly minted server-side ID rather than failing the request.

    The minted (or echoed) ID is set on a ContextVar so that every log
    record emitted during the request — application logs *and* uvicorn's
    access log — carries it via TraceIDFilter, and is also written to
    the ``X-Trace-Id`` response header so clients can pin a server-side
    log search to the exact request they made.
    """
    from backend.app.core.trace import (
        generate_trace_id,
        normalise_inbound_trace_id,
        trace_id_var,
    )

    inbound = normalise_inbound_trace_id(request.headers.get("X-Trace-Id"))
    trace_id = inbound if inbound is not None else generate_trace_id()

    token = trace_id_var.set(trace_id)
    try:
        response = await call_next(request)
    finally:
        # Reset the ContextVar so a record emitted in a totally
        # unrelated background task that just happens to inherit this
        # context doesn't keep referencing this request's ID forever.
        # In practice ContextVar.reset is best-effort under asyncio
        # task-spawn semantics, but the cost is one attribute write so
        # we may as well do it.
        trace_id_var.reset(token)

    response.headers["X-Trace-Id"] = trace_id
    return response


@app.middleware("http")
async def request_timing_middleware(request, call_next):
    """Time every request and say how much of it was the database.

    ⚠️ Decorated LAST, so it is the OUTERMOST layer — the only position that
    measures auth_middleware's early 401/503 returns, and a 503 setup_required
    storm is a real symptom. The cost of being outermost is that it runs outside
    trace_id_middleware, so this record's own ``trace_id`` field is "-"; the
    trace is read back from the response header instead.

    The accumulator is set BEFORE call_next because BaseHTTPMiddleware runs the
    inner app in a child task, which copies the context — see query_timing.

    Both halves are off unless slow_request_ms is set; the Server-Timing header
    is always sent, because it costs nothing and a browser's Network panel
    renders it.
    """
    timing, token = query_timing.begin_request()
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        query_timing.end_request(token)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"db;dur={timing.total_ms:.1f}, total;dur={elapsed_ms:.1f}"

    query_timing.log_slow_request(
        request.method,
        request.url.path,
        elapsed_ms,
        timing,
        response.headers.get("X-Trace-Id", "-"),
    )
    return response


# API routes
app.include_router(auth.router, prefix=app_settings.api_prefix)
app.include_router(mfa.router, prefix=app_settings.api_prefix)
app.include_router(users.router, prefix=app_settings.api_prefix)
app.include_router(groups.router, prefix=app_settings.api_prefix)
app.include_router(printers.router, prefix=app_settings.api_prefix)
app.include_router(scheduled_drying_routes.router, prefix=app_settings.api_prefix)
app.include_router(hms_routes.router, prefix=app_settings.api_prefix)
app.include_router(printer_locations.router, prefix=app_settings.api_prefix)
app.include_router(printer_tags.router, prefix=app_settings.api_prefix)
# No prefix of its own: its two paths belong to two existing namespaces.
app.include_router(measurement_history.router, prefix=app_settings.api_prefix)
# archive_purge must come BEFORE archives so its `/archives/trash/*` routes
# don't get swallowed by archives' `/archives/{archive_id}` catch-all.
app.include_router(archive_purge.router, prefix=app_settings.api_prefix)
app.include_router(archives.router, prefix=app_settings.api_prefix)
from backend.app.api.routes import media_downloads as media_downloads_routes

app.include_router(media_downloads_routes.router, prefix=app_settings.api_prefix)
from backend.app.api.routes import timelapse_edits as timelapse_edits_routes  # noqa: E402

app.include_router(timelapse_edits_routes.router, prefix=app_settings.api_prefix)
media_downloads_routes.install(app)
app.include_router(statistics.router, prefix=app_settings.api_prefix)
app.include_router(inventory.router, prefix=app_settings.api_prefix)
app.include_router(filament_families_routes.router, prefix=app_settings.api_prefix)
app.include_router(orca_cloud.router, prefix=app_settings.api_prefix)
app.include_router(labels.router, prefix=app_settings.api_prefix)
app.include_router(label_templates.router, prefix=app_settings.api_prefix)
app.include_router(label_devices.router, prefix=app_settings.api_prefix)
app.include_router(settings_routes.router, prefix=app_settings.api_prefix)
app.include_router(cloud.router, prefix=app_settings.api_prefix)
app.include_router(cloud_link.router, prefix=app_settings.api_prefix)
app.include_router(local_presets.router, prefix=app_settings.api_prefix)
app.include_router(slicer_presets.router, prefix=app_settings.api_prefix)
app.include_router(slicer_pipelines.router, prefix=app_settings.api_prefix)
app.include_router(slice_jobs.router, prefix=app_settings.api_prefix)
app.include_router(library_assembly.router, prefix=app_settings.api_prefix)
app.include_router(makerworld.router, prefix=app_settings.api_prefix)
app.include_router(model_sources.connection_router, prefix=app_settings.api_prefix)
app.include_router(model_sources.manyfold_router, prefix=app_settings.api_prefix)
app.include_router(smart_plugs.router, prefix=app_settings.api_prefix)
app.include_router(zigbee.router, prefix=app_settings.api_prefix)
app.include_router(ha_sensors.router, prefix=app_settings.api_prefix)
app.include_router(location_ha_sensors.router, prefix=app_settings.api_prefix)
app.include_router(print_queue.router, prefix=app_settings.api_prefix)
app.include_router(print_options_preferences.router, prefix=app_settings.api_prefix)
app.include_router(auto_queue.router, prefix=app_settings.api_prefix)
app.include_router(background_dispatch_routes.router, prefix=app_settings.api_prefix)
app.include_router(kprofiles.router, prefix=app_settings.api_prefix)
app.include_router(notifications.router, prefix=app_settings.api_prefix)
app.include_router(notification_templates.router, prefix=app_settings.api_prefix)
app.include_router(user_notifications.router, prefix=app_settings.api_prefix)
app.include_router(inbox.router, prefix=app_settings.api_prefix)
app.include_router(spoolman.router, prefix=app_settings.api_prefix)
app.include_router(spoolman_inventory.router, prefix=app_settings.api_prefix)
app.include_router(spoolman_usage.router, prefix=app_settings.api_prefix)
app.include_router(updates.router, prefix=app_settings.api_prefix)
app.include_router(macros.router, prefix=app_settings.api_prefix)
app.include_router(maintenance.router, prefix=app_settings.api_prefix)
app.include_router(camera.router, prefix=app_settings.api_prefix)
app.include_router(camwall.router, prefix=app_settings.api_prefix)
app.include_router(overlay_branding.router, prefix=app_settings.api_prefix)
app.include_router(cameras.router, prefix=app_settings.api_prefix)
app.include_router(monitor.router, prefix=app_settings.api_prefix)
app.include_router(external_links.router, prefix=app_settings.api_prefix)
app.include_router(projects.router, prefix=app_settings.api_prefix)
app.include_router(customers.router, prefix=app_settings.api_prefix)
app.include_router(delivery_methods.router, prefix=app_settings.api_prefix)
app.include_router(products.router, prefix=app_settings.api_prefix)
app.include_router(part_images_routes.router, prefix=app_settings.api_prefix)
app.include_router(part_images_routes.product_router, prefix=app_settings.api_prefix)
app.include_router(product_categories.router, prefix=app_settings.api_prefix)
app.include_router(stock.router, prefix=app_settings.api_prefix)
app.include_router(stock_issues.router, prefix=app_settings.api_prefix)
app.include_router(library.router, prefix=app_settings.api_prefix)
app.include_router(library_notes.router, prefix=app_settings.api_prefix)
app.include_router(library_photos.router, prefix=app_settings.api_prefix)
app.include_router(library_thumbnail_jobs.router, prefix=app_settings.api_prefix)
app.include_router(library_tags.router, prefix=app_settings.api_prefix)
app.include_router(library_trash.router, prefix=app_settings.api_prefix)
# archive_purge router is registered above before archives.router (route order).
from backend.app.api.routes import credential_rollout as credential_rollout_routes

app.include_router(credential_rollout_routes.router, prefix=app_settings.api_prefix)
app.include_router(api_keys.router, prefix=app_settings.api_prefix)
from backend.app.api.routes import mcp_integration

app.include_router(mcp_integration.router, prefix=app_settings.api_prefix)
from backend.app.api.routes import connector as connector_routes

app.include_router(connector_routes.router, prefix=app_settings.api_prefix)
app.include_router(webhook.router, prefix=app_settings.api_prefix)
app.include_router(ams_history.router, prefix=app_settings.api_prefix)
app.include_router(printer_sensor_history.router, prefix=app_settings.api_prefix)
app.include_router(ams_settings_routes.router, prefix=app_settings.api_prefix)
app.include_router(printer_settings_routes.router, prefix=app_settings.api_prefix)
app.include_router(filament_calibration_routes.router, prefix=app_settings.api_prefix)
from backend.app.api.routes import farm_startup

app.include_router(farm_startup.router, prefix=app_settings.api_prefix)
app.include_router(system.router, prefix=app_settings.api_prefix)
app.include_router(print_state_recovery.router, prefix=app_settings.api_prefix)
app.include_router(support.router, prefix=app_settings.api_prefix)
app.include_router(bug_report.router, prefix=app_settings.api_prefix)
app.include_router(websocket.router, prefix=app_settings.api_prefix)
app.include_router(discovery.router, prefix=app_settings.api_prefix)
app.include_router(firmware.router, prefix=app_settings.api_prefix)
app.include_router(git_backup.router, prefix=app_settings.api_prefix)
app.include_router(local_backup.router, prefix=app_settings.api_prefix)
app.include_router(metrics.router, prefix=app_settings.api_prefix)
app.include_router(obico.router, prefix=app_settings.api_prefix)
app.include_router(octoeverywhere.router, prefix=app_settings.api_prefix)
app.include_router(virtual_printers.router, prefix=app_settings.api_prefix)
app.include_router(printer_queues.router, prefix=app_settings.api_prefix)
app.include_router(telegram.router, prefix=app_settings.api_prefix)


# Serve static files (React build)
if app_settings.static_dir.exists() and any(app_settings.static_dir.iterdir()):
    # Compressed and cache-marked on THIS mount only (core/static_assets.py): an app-wide
    # compression middleware would also wrap the camera streams and sit inside the media gate.
    from backend.app.core.static_assets import AssetStaticFiles

    app.mount(
        "/assets",
        AssetStaticFiles(directory=app_settings.static_dir / "assets"),
        name="assets",
    )
    if (app_settings.static_dir / "img").exists():
        app.mount(
            "/img",
            StaticFiles(directory=app_settings.static_dir / "img"),
            name="img",
        )
    if (app_settings.static_dir / "icons").exists():
        app.mount(
            "/icons",
            StaticFiles(directory=app_settings.static_dir / "icons"),
            name="icons",
        )
    # Self-hosted Inter woff2 files (#1460). Without this mount /fonts/*.woff2
    # falls through to the SPA catch-all and returns index.html, which the
    # browser's font sanitizer rejects ("downloadable font: rejected by
    # sanitizer").
    if (app_settings.static_dir / "fonts").exists():
        app.mount(
            "/fonts",
            StaticFiles(directory=app_settings.static_dir / "fonts"),
            name="fonts",
        )


@app.get("/")
async def serve_frontend():
    """Serve the React frontend.

    ``Cache-Control: no-cache, must-revalidate`` so the browser always
    revalidates ``index.html`` against the server (returning 304 if
    unchanged). Without this, browsers cache the HTML aggressively and
    keep referencing rotated-out hashed asset filenames after a deploy,
    which surfaces as "the UI is broken until I hard-refresh". The
    hashed asset bundles under ``/assets/*`` keep their default long-
    lived caching — only the document that points at them needs to
    revalidate.
    """
    index_file = app_settings.static_dir / "index.html"
    if index_file.exists():
        return FileResponse(
            index_file,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )
    return {
        "message": "BamDude API",
        "docs": "/docs",
        "frontend": "Build and place React app in /static directory",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


# The four routes below answer GET and HEAD, each declared once per method
# rather than as a single `api_route(methods=["GET", "HEAD"])`: FastAPI derives
# one operationId per ROUTE, so a two-method route publishes the same id for
# both operations — and an unstable one, because the method that names it comes
# from `list(route.methods)[0]`, a set whose order follows the process's hash
# seed.
@app.get("/manifest.json")
@app.head("/manifest.json")
async def serve_manifest():
    """Serve PWA manifest."""
    manifest_file = app_settings.static_dir / "manifest.json"
    if manifest_file.exists():
        return FileResponse(manifest_file, media_type="application/manifest+json")
    return {"error": "Manifest not found"}


@app.get("/favicon.ico")
@app.head("/favicon.ico")
async def serve_favicon():
    """Root favicon for clients that never read ``<link rel="icon">``.

    Without this the SPA catch-all returned ``index.html`` for it. The file is
    the pack's multi-size ``.ico`` shipped under ``static/img/brand/``.
    """
    ico = app_settings.static_dir / "img" / "brand" / "favicon.ico"
    if ico.exists():
        return FileResponse(ico, media_type="image/x-icon")
    return Response(status_code=404)


@app.get("/sw.js")
@app.head("/sw.js")
async def serve_service_worker():
    """Serve service worker."""
    sw_file = app_settings.static_dir / "sw.js"
    if sw_file.exists():
        return FileResponse(
            sw_file,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"error": "Service worker not found"}


@app.get("/sw-register.js")
@app.head("/sw-register.js")
async def serve_sw_register():
    """Serve the service-worker registration bootstrap script.

    Served as a real JS file so the strict ``script-src 'self'`` CSP covers it
    without needing ``'unsafe-inline'`` or per-build hashes on the inline tag.
    """
    reg_file = app_settings.static_dir / "sw-register.js"
    if reg_file.exists():
        return FileResponse(reg_file, media_type="application/javascript")
    return {"error": "sw-register.js not found"}


# Catch-all route for React Router (must be last)
# The exact MCP dispatcher wraps REST, so neither the SPA nor ordinary
# REST auth/CORS can answer for this separate key-only path. The outer media
# boundary below passes this non-media path through unchanged.
from backend.app.mcp_server.runtime import McpMiddleware

app.add_middleware(McpMiddleware)

from backend.app.services.live_media import LiveMediaDeliveryMiddleware, install_live_media_routes

install_live_media_routes(app)
app.add_middleware(LiveMediaDeliveryMiddleware)

from backend.app.core.schema_repair_maintenance import SchemaRepairMaintenanceMiddleware

app.add_middleware(SchemaRepairMaintenanceMiddleware)


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve React app for client-side routing.

    Same ``Cache-Control: no-cache, must-revalidate`` as ``serve_frontend``
    so deep-links into client-side routes (``/printers``, ``/queue``, …)
    don't cache stale HTML pointing at rotated-out asset bundles.
    """
    # Don't intercept API routes - raise proper 404 so FastAPI can handle redirects
    if full_path.startswith("api/"):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not found")

    index_file = app_settings.static_dir / "index.html"
    if index_file.exists():
        return FileResponse(
            index_file,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    return {"error": "Frontend not built"}
