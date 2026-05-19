import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, or_, select, text

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
    cloud,
    discovery,
    external_links,
    filament_calibration as filament_calibration_routes,
    firmware,
    git_backup,
    groups,
    inventory,
    kprofiles,
    labels,
    library,
    library_notes,
    library_trash,
    local_backup,
    local_presets,
    macros,
    maintenance,
    makerworld,
    metrics,
    mfa,
    notification_templates,
    notifications,
    obico,
    print_options_preferences,
    print_queue,
    printer_queues,
    printer_settings as printer_settings_routes,
    printers,
    projects,
    settings as settings_routes,
    slice_jobs,
    slicer_presets,
    smart_plugs,
    spoolman,
    spoolman_inventory,
    support,
    system,
    telegram,
    updates,
    user_notifications,
    users,
    virtual_printers,
    webhook,
    websocket,
)
from backend.app.api.routes.maintenance import _get_printer_maintenance_internal, ensure_default_types
from backend.app.api.routes.support import init_debug_logging
from backend.app.core.config import APP_VERSION, settings as app_settings
from backend.app.core.database import async_session, engine, init_db
from backend.app.core.websocket import ws_manager
from backend.app.models.smart_plug import SmartPlug
from backend.app.services.archive import ArchiveService, resolve_display_stem
from backend.app.services.auto_queue_scheduler import auto_queue_scheduler
from backend.app.services.background_dispatch import background_dispatch
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.git_backup import git_backup_service
from backend.app.services.homeassistant import homeassistant_service
from backend.app.services.local_backup import local_backup_service
from backend.app.services.mqtt_relay import mqtt_relay
from backend.app.services.mqtt_smart_plug import mqtt_smart_plug_service
from backend.app.services.notification_service import notification_service
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
from backend.app.services.spoolman import close_spoolman_client, get_spoolman_client, init_spoolman_client
from backend.app.services.spoolman_tracking import (
    cleanup_tracking as _cleanup_spoolman_tracking,
    report_usage as _report_spoolman_usage,
    store_print_data as _store_spoolman_print_data,
)
from backend.app.services.tasmota import tasmota_service


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
from backend.app.core.trace import TraceIDFilter  # noqa: E402

_trace_id_filter = TraceIDFilter()

# Console handler - always enabled
console_handler = logging.StreamHandler()
console_handler.setLevel(log_level)
console_handler.setFormatter(logging.Formatter(log_format))
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
        app_log_filename_namer,
        set_app_log_handler,
    )

    log_file = app_settings.log_dir / "bamdude.log"
    file_handler = TimedRotatingFileHandler(
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
    file_handler.setFormatter(logging.Formatter(log_format))
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
    from backend.app.core.logging_filters import WriteRequestsOnlyFilter  # noqa: E402

    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addHandler(file_handler)
    uvicorn_access_logger.addFilter(WriteRequestsOnlyFilter())
    # Uvicorn's access logger has propagate=False (its own default), so
    # the root-attached TraceIDFilter never sees these records. Attach a
    # second filter instance directly to the access logger so HTTP access
    # lines carry the same trace ID column as the application logs they
    # correlate with.
    uvicorn_access_logger.addFilter(TraceIDFilter())

# Reduce noise from third-party libraries in production
if not app_settings.debug:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("paho.mqtt").setLevel(logging.WARNING)

# Drop SQLAlchemy pool noise driven by Starlette's BaseHTTPMiddleware
# cancellation propagation on client disconnect (#1112 follow-up).
from backend.app.core.logging_filters import CancelledPoolNoiseFilter  # noqa: E402

logging.getLogger("sqlalchemy.pool").addFilter(CancelledPoolNoiseFilter())

logging.info("BamDude starting - debug=%s, log_level=%s", app_settings.debug, log_level_str)


# Track active prints: {(printer_id, filename): archive_id}
_active_prints: dict[tuple[int, str], int] = {}

# Track expected prints from reprint/scheduled (skip auto-archiving for these)
# {(printer_id, filename): archive_id}
_expected_prints: dict[tuple[int, str], int] = {}

# Track AMS mapping for prints: {archive_id: [global_tray_id_per_slot]}
# Used by usage tracker to map 3MF slots to physical AMS trays
_print_ams_mappings: dict[int, list[int]] = {}

# Swap-macro configuration for active prints: {printer_id: {"swap_macro_events": [...]}}
# Populated at dispatch time; consumed + cleaned up in on_print_complete.
# Only one print is active per printer at a time, so printer_id is a safe key.
_active_swap_config: dict[int, dict] = {}

# Track progress milestones for notifications: {printer_id: last_milestone_notified}
# Milestones are 25, 50, 75. Value of 0 means no milestone notified yet for current print.
_last_progress_milestone: dict[int, int] = {}

# Track whether first layer complete notification has been sent for current print
_first_layer_notified: dict[int, bool] = {}

# Track HMS errors that have been notified: {printer_id: set of error codes}
# This prevents sending duplicate notifications for the same error
_notified_hms_errors: dict[int, set[str]] = {}
# Track when HMS errors were last seen: {printer_id: timestamp}
# Used to debounce clearing - prevents flapping errors from re-triggering notifications
_hms_last_seen: dict[int, float] = {}
_HMS_CLEAR_GRACE_SECONDS = 30.0

# Track timelapse file baselines at print start: {printer_id: set of video filenames}
# Used for snapshot-diff detection at print completion
_timelapse_baselines: dict[int, set[str]] = {}

# Track active bed cooldown monitoring tasks: {printer_id: asyncio.Task}
_bed_cooldown_tasks: dict[int, asyncio.Task] = {}

# Track printers where the user explicitly stopped the print from the queue UI.
# When on_print_complete fires with status "failed" for these printers we treat it
# as "cancelled" (stopped by user) so the correct notification email is sent.
_user_stopped_printers: set[int] = set()


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
_HMS_FAILURE_REASONS: dict[str, str] = {
    # Layer shift / step loss
    "0300_4057": "Layer shift",
    "0300_4068": "Layer shift",
    "0300_800C": "Layer shift",
    # Filament runout (printer-side & per-AMS-slot)
    "0300_8004": "Filament runout",
    "0700_8011": "Filament runout",
    "0701_8011": "Filament runout",
    "0702_8011": "Filament runout",
    "0703_8011": "Filament runout",
    "0704_8011": "Filament runout",
    "0705_8011": "Filament runout",
    "0706_8011": "Filament runout",
    "0707_8011": "Filament runout",
    "07FF_8011": "Filament runout",
    # Clogged nozzle / extruder
    "0300_4006": "Clogged nozzle",
    "0300_8016": "Clogged nozzle",
    "0300_801C": "Clogged nozzle",
    "0700_8003": "Clogged nozzle",
    "0700_8007": "Clogged nozzle",
    "0700_8013": "Clogged nozzle",
    "0701_8003": "Clogged nozzle",
    "0701_8007": "Clogged nozzle",
    "0701_8013": "Clogged nozzle",
    "0702_8003": "Clogged nozzle",
}


def _hms_short_code(attr: int, code: int | str) -> str:
    """Build the canonical "MMMM_CCCC" HMS short code from raw attr/code values."""
    if isinstance(code, str):
        code_int = int(code.replace("0x", ""), 16) if code else 0
    else:
        code_int = int(code or 0)
    attr_int = int(attr or 0)
    return f"{(attr_int >> 16) & 0xFFFF:04X}_{code_int & 0xFFFF:04X}"


def derive_failure_reason(status: str, hms_errors: list[dict] | None) -> str | None:
    """Derive a human-readable failure_reason for an archived print.

    Returns "User cancelled" for cancelled/aborted prints; for failed prints,
    returns the first matching reason from _HMS_FAILURE_REASONS, or None when
    no HMS code matches (don't guess — null is honest).
    """
    if status in ("aborted", "cancelled"):
        return "User cancelled"
    if status != "failed":
        return None
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


async def _get_plug_energy(plug, db) -> dict | None:
    """Get energy from plug regardless of type (Tasmota, Home Assistant, MQTT, or REST).

    For HA plugs, configures the service with current settings from DB.
    For MQTT plugs, returns data from the subscription service.
    For REST plugs, polls the status URL with JSON path extraction.
    """
    if plug.plug_type == "homeassistant":
        from backend.app.api.routes.settings import get_homeassistant_settings

        ha_settings = await get_homeassistant_settings(db)
        homeassistant_service.configure(ha_settings["ha_url"], ha_settings["ha_token"])
        return await homeassistant_service.get_energy(plug)
    elif plug.plug_type == "mqtt":
        # MQTT plugs report "today" energy, not lifetime total
        # For per-print tracking, we use "today" as the counter (resets at midnight)
        mqtt_data = mqtt_relay.smart_plug_service.get_plug_data(plug.id)
        if mqtt_data:
            return {
                "power": mqtt_data.power,
                "today": mqtt_data.energy,
                "total": mqtt_data.energy,  # Use today as total for per-print calculations
            }
        return None
    elif plug.plug_type == "rest":
        from backend.app.services.rest_smart_plug import rest_smart_plug_service

        return await rest_smart_plug_service.get_energy(plug)
    else:
        return await tasmota_service.get_energy(plug)


async def _default_queue_id_for_printer(db, printer_id: int) -> int | None:
    """Resolve a printer's single PrinterQueue row → its id, or None.

    External / direct-dispatch archives use this to fill ``archive.queue_id``
    so the post-m019 archive-driven counters in ``GET /printer-queues/``
    include them under the printer's default queue.
    """
    from backend.app.models.printer_queue import PrinterQueue

    return (await db.execute(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))).scalar_one_or_none()


async def _record_energy_start(archive, printer_id: int, db, *, context: str = "") -> bool:
    """Capture the smart plug lifetime counter on the archive at print start.

    Persists `energy_start_kwh` on the archive row (upstream #941) so per-print
    energy tracking survives a backend restart mid-print. The print-end handler
    reads this value back from the DB and computes the delta against the current
    plug counter.
    """
    _logger = logging.getLogger(__name__)
    try:
        plug_result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
        plug = plug_result.scalar_one_or_none()
        if not plug:
            _logger.info("[ENERGY] No smart plug for printer %s (archive %s)", printer_id, archive.id)
            return False
        energy = await _get_plug_energy(plug, db)
        if not energy or energy.get("total") is None:
            _logger.warning("[ENERGY] No 'total' in energy response for archive %s", archive.id)
            return False
        archive.energy_start_kwh = float(energy["total"])
        await db.commit()
        _logger.info(
            "[ENERGY] Recorded starting energy%s for archive %s: %s kWh",
            f" ({context})" if context else "",
            archive.id,
            energy["total"],
        )
        return True
    except Exception as e:
        _logger.warning("[ENERGY] Failed to record starting energy for archive %s: %s", archive.id, e)
        return False


def register_expected_print(
    printer_id: int,
    filename: str,
    archive_id: int,
    ams_mapping: list[int] | None = None,
    created_by_id: int | None = None,
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


async def mark_queue_printing_for_printer(printer_id: int, item_id: int | None = None) -> None:
    """Ensure the printer's queue reflects the real busy state.

    Call this once we know an active print exists on *printer_id* —
    regardless of source (queue-driven, BamDude direct, external).
    The scheduler itself doesn't rely on queue.status (uses MQTT state),
    but the UI does, and a stale idle status while a print runs is
    confusing.

    ``item_id`` is ``None`` for external / direct-dispatch prints that
    have no corresponding ``PrintQueueItem``.
    """
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.queue_counters import set_queue_printing

    async with async_session() as db:
        result = await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
        queue = result.scalar_one_or_none()
        if queue is None:
            return
        if queue.status == "printing" and queue.current_item_id == item_id:
            return  # already in correct state
        await set_queue_printing(db, queue.id, item_id)
        await db.commit()


def _get_start_ams_mapping(data: dict, archive_id: int | None) -> list[int] | None:
    """Resolve AMS mapping for print start without consuming stored queue/reprint state."""
    stored_ams_mapping = data.get("ams_mapping")
    if not stored_ams_mapping and archive_id:
        stored_ams_mapping = _print_ams_mappings.get(archive_id)
    return stored_ams_mapping


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


def _format_hms_error_summary(hms_errors: list[dict]) -> str | None:
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
    from backend.app.services.hms_errors import get_error_description

    parts: list[str] = []
    for err in hms_errors:
        try:
            code_str = str(err.get("code", "")).replace("0x", "")
            error_num = int(code_str, 16) if code_str else 0
            module_num = (int(err.get("attr", 0)) >> 16) & 0xFFFF
            short_code = f"{module_num:04X}_{error_num:04X}"
        except (TypeError, ValueError):
            continue
        description = get_error_description(short_code)
        parts.append(f"[{short_code}] {description}" if description else f"[{short_code}]")
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


def mark_printer_stopped_by_user(printer_id: int) -> None:
    """Mark that the active print on this printer was stopped by the user from the queue UI.

    When on_print_complete fires with status 'failed' for a printer in this set we
    reclassify it as 'cancelled' so the correct 'print stopped' notification is sent
    rather than a 'print failed' notification.
    """
    _user_stopped_printers.add(printer_id)
    logging.getLogger(__name__).info("Marked printer %s as user-stopped from queue", printer_id)


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
_pause_started_at: dict[int, float] = {}
_expected_pause_reasons: dict[int, str] = {}


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

        hms_codes = [e.get("code") for e in (state.hms_errors or []) if isinstance(e, dict) and e.get("code")]
        expected = _expected_pause_reasons.pop(printer_id, None)
        reason_code, reason_label, hms_code = classify_pause_reason(hms_codes, expected)

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

        filename = state.subtask_name or state.gcode_file
        ws_data = {
            "filename": filename,
            "reason": reason_label,
            "reason_code": reason_code,
            "hms_code": hms_code,
        }
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


async def _handle_resume_edge(printer_id: int, state: PrinterState):
    """Fire on_print_resume notification + WS push on PAUSE→RUNNING.

    Computes paused duration from ``_pause_started_at`` (planted by
    ``_handle_pause_edge``); falls back to ``None`` when the resume hits
    without a recorded start (e.g. BamDude restarted while the printer
    was paused).
    """
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

        filename = state.subtask_name or state.gcode_file
        ws_data = {
            "filename": filename,
            "paused_for_seconds": paused_for_seconds,
        }
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


async def on_printer_status_change(printer_id: int, state: PrinterState):
    """Handle printer status changes - broadcast via WebSocket."""
    # Only broadcast if something meaningful changed (reduce WebSocket spam)
    # Include rounded temperatures to detect meaningful temp changes (within 1 degree)
    temps = state.temperatures or {}
    nozzle_temp = round(temps.get("nozzle", 0))
    bed_temp = round(temps.get("bed", 0))
    nozzle_2_temp = round(temps.get("nozzle_2", 0)) if "nozzle_2" in temps else ""
    chamber_temp = round(temps.get("chamber", 0)) if "chamber" in temps else ""

    # Auto-detect dual-nozzle printers from MQTT temperature data
    if "nozzle_2" in temps and printer_id not in _nozzle_count_updated:
        _nozzle_count_updated.add(printer_id)
        # Update nozzle_count in database
        async with async_session() as db:
            from backend.app.models.printer import Printer

            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if printer and printer.nozzle_count != 2:
                printer.nozzle_count = 2
                await db.commit()
                logging.getLogger(__name__).info(
                    f"Auto-detected dual-nozzle printer {printer_id}, updated nozzle_count=2"
                )

    # Include target temps for heating phase detection
    bed_target = round(temps.get("bed_target", 0))
    nozzle_target = round(temps.get("nozzle_target", 0))

    # Include tray_now and vt_tray hash so external spool changes trigger broadcasts
    vt_tray_key = hash(str(state.raw_data.get("vt_tray", []))) if state.raw_data else 0
    # Include AMS dry_time and tray state values so drying/slot changes trigger broadcasts
    ams_dry_key = tuple(a.get("dry_time", 0) for a in (state.raw_data.get("ams") or [])) if state.raw_data else ()
    # Include tray states so load/unload transitions (state 11→10) trigger broadcasts (#784)
    ams_tray_key = (
        tuple(
            (t.get("id"), t.get("tray_type", ""), t.get("state"))
            for a in (state.raw_data.get("ams") or [])
            for t in a.get("tray", [])
        )
        if state.raw_data
        else ()
    )
    status_key = (
        f"{state.connected}:{state.state}:{state.progress}:{state.layer_num}:"
        f"{nozzle_temp}:{bed_temp}:{nozzle_2_temp}:{chamber_temp}:"
        f"{state.stg_cur}:{bed_target}:{nozzle_target}:"
        f"{state.cooling_fan_speed}:{state.big_fan1_speed}:{state.big_fan2_speed}:"
        f"{state.chamber_light}:{state.active_extruder}:{state.tray_now}:{vt_tray_key}:"
        f"{ams_dry_key}:{ams_tray_key}"
    )

    # MQTT relay - publish status (before dedup check - always publish to MQTT)
    try:
        printer_info = printer_manager.get_printer(printer_id)
        if printer_info:
            await mqtt_relay.on_printer_status(printer_id, state, printer_info.name, printer_info.serial_number)
    except Exception:
        pass  # Don't fail status callback if MQTT fails

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
            await _handle_resume_edge(printer_id, state)
    _last_printer_state[printer_id] = current_state

    if _last_status_broadcast.get(printer_id) == status_key:
        return  # No change, skip WebSocket broadcast

    _last_status_broadcast[printer_id] = status_key

    # Check for progress milestone notifications (25%, 50%, 75%)
    progress = state.progress or 0
    is_printing = state.state in ("RUNNING", "PRINTING")

    if is_printing and progress > 0:
        # Determine which milestone we've reached
        current_milestone = 0
        if progress >= 75:
            current_milestone = 75
        elif progress >= 50:
            current_milestone = 50
        elif progress >= 25:
            current_milestone = 25

        last_milestone = _last_progress_milestone.get(printer_id, 0)

        # If we've crossed a new milestone, send notification
        if current_milestone > last_milestone:
            _last_progress_milestone[printer_id] = current_milestone
            try:
                async with async_session() as db:
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()
                    printer_name = printer.name if printer else f"Printer {printer_id}"
                    filename = state.subtask_name or state.gcode_file or "Unknown"
                    # remaining_time is in minutes, convert to seconds for notification
                    remaining_time_seconds = state.remaining_time * 60 if state.remaining_time else None

                    # Capture camera snapshot for notification image attachment
                    image_data = await _capture_snapshot_for_notification(
                        printer_id, printer, logging.getLogger(__name__)
                    )

                    await notification_service.on_print_progress(
                        printer_id,
                        printer_name,
                        filename,
                        current_milestone,
                        db,
                        remaining_time_seconds,
                        image_data=image_data,
                    )
            except Exception as e:
                logging.getLogger(__name__).warning(f"Progress milestone notification failed: {e}")
    elif progress < 5:
        # Reset milestone tracking when print restarts or new print begins
        _last_progress_milestone[printer_id] = 0
        _first_layer_notified[printer_id] = False

    # HMS error codes that should not trigger notifications even though they
    # have known descriptions (e.g. user-initiated actions, not real errors).
    _HMS_NOTIFICATION_SUPPRESS = {
        "0500_400E",  # Printing was cancelled (user action, not an error)
    }

    # Check for new HMS errors and send notifications
    current_hms_errors = getattr(state, "hms_errors", []) or []
    if current_hms_errors:
        # Build set of current error codes (using attr for uniqueness)
        current_error_codes = {f"{e.attr:08x}" for e in current_hms_errors}
        previously_notified = _notified_hms_errors.get(printer_id, set())

        # Find new errors that haven't been notified yet
        new_error_codes = current_error_codes - previously_notified

        # Update tracking immediately to prevent duplicate notifications from concurrent callbacks
        _notified_hms_errors[printer_id] = current_error_codes
        _hms_last_seen[printer_id] = time.time()

        if new_error_codes:
            # Get the actual new errors for the notification
            # Filter to severity >= 2 (skip informational/status messages like H2D sends)
            new_errors = [e for e in current_hms_errors if f"{e.attr:08x}" in new_error_codes and e.severity >= 2]

            try:
                async with async_session() as db:
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()
                    printer_name = printer.name if printer else f"Printer {printer_id}"

                    # Format error details for notification
                    # Module 0x07 = AMS/Filament, 0x05 = Nozzle, 0x0C = Motion Controller, etc.
                    module_names = {
                        0x03: "Print/Task",
                        0x05: "Nozzle/Extruder",
                        0x07: "AMS/Filament",
                        0x0C: "Motion Controller",
                        0x12: "Chamber",
                    }

                    from backend.app.services.hms_errors import get_error_description

                    # Capture camera snapshot once for all error notifications
                    error_image_data = await _capture_snapshot_for_notification(
                        printer_id, printer, logging.getLogger(__name__)
                    )

                    sent_count = 0
                    for error in new_errors:
                        module_name = module_names.get(error.module, f"Module 0x{error.module:02X}")
                        # Build short code like "0700_8010"
                        # Mask to 16 bits to handle printers that send larger values
                        error_code_int = int(error.code.replace("0x", ""), 16) if error.code else 0
                        error_code_masked = error_code_int & 0xFFFF
                        short_code = f"{(error.attr >> 16) & 0xFFFF:04X}_{error_code_masked:04X}"

                        # Only notify for errors with known descriptions - printers
                        # send many undocumented/phantom codes that aren't real errors.
                        description = get_error_description(short_code)
                        if not description or short_code in _HMS_NOTIFICATION_SUPPRESS:
                            continue

                        error_type = f"{module_name} Error"
                        error_detail = description

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

    else:
        # No HMS errors - only clear tracking after a grace period to prevent
        # flapping errors (brief hms:[] gaps) from re-triggering notifications.
        # Some HMS codes (e.g. chamber temp regulation during PETG prints) toggle
        # on/off every few seconds as conditions fluctuate around thresholds.
        if printer_id in _notified_hms_errors:
            last_seen = _hms_last_seen.get(printer_id, 0)
            if time.time() - last_seen >= _HMS_CLEAR_GRACE_SECONDS:
                _notified_hms_errors.pop(printer_id, None)
                _hms_last_seen.pop(printer_id, None)

    await ws_manager.send_printer_status(
        printer_id,
        printer_state_to_dict(state, printer_id, printer_manager.get_model(printer_id)),
    )


def _is_bambu_uuid(tray_uuid: str) -> bool:
    """Check if a tray UUID looks like a valid Bambu Lab RFID UUID (non-empty, non-zero)."""
    return bool(tray_uuid) and tray_uuid not in ("", "0" * len(tray_uuid))


async def on_ams_change(printer_id: int, ams_data: list):
    """Handle AMS data changes - sync to Spoolman if enabled and auto mode."""
    logger = logging.getLogger(__name__)

    # Check if a print is actively running on this printer - if so, skip AMS
    # weight sync to avoid double-deducting spool weight (the usage tracker
    # handles weight deduction precisely during prints via 3MF/G-code data).
    from backend.app.services.usage_tracker import _active_sessions

    _print_active = printer_id in _active_sessions

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
                printer_state_to_dict(state, printer_id, printer_manager.get_model(printer_id)),
            )
    except Exception as e:
        logger.warning("Failed to broadcast AMS change for printer %s: %s", printer_id, e)

    from backend.app.utils.color_utils import colors_similar as _colors_similar

    # Auto-unlink spool assignments with stale fingerprints
    try:
        async with async_session() as db:
            from sqlalchemy.orm import selectinload

            from backend.app.api.routes.inventory import _find_tray_in_ams_data
            from backend.app.models.spool import Spool as _Spool
            from backend.app.models.spool_assignment import SpoolAssignment as SA

            result = await db.execute(
                select(SA)
                .where(SA.printer_id == printer_id)
                .options(selectinload(SA.spool).selectinload(_Spool.k_profiles))
            )
            stale = []
            for assignment in result.scalars().all():
                # External spool assignments (ams_id=255) live in vt_tray, not AMS data
                if assignment.ams_id == 255:
                    ps = printer_manager.get_status(printer_id)
                    vt_tray_raw = ps.raw_data.get("vt_tray", []) if ps else []
                    ext_id = assignment.tray_id + 254  # 0→254, 1→255
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
                        cur_color = current_tray.get("tray_color", "")
                        cur_type = current_tray.get("tray_type", "")
                        fp_color = assignment.fingerprint_color or ""
                        fp_type = assignment.fingerprint_type or ""
                        if cur_color.upper() != fp_color.upper() or cur_type.upper() != fp_type.upper():
                            assignment.fingerprint_color = cur_color
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
                    cur_color = current_tray.get("tray_color", "")
                    cur_type = current_tray.get("tray_type", "")
                    cur_state = current_tray.get("state")
                    fp_color = assignment.fingerprint_color or ""
                    fp_type = assignment.fingerprint_type or ""

                    # Pre-config replay: empty fingerprint_type means the slot
                    # was empty when the user pre-assigned (Bambu firmware
                    # drops ams_filament_setting on empty slots, so MQTT was
                    # deferred). The moment any filament gets inserted —
                    # Bambu RFID, 3rd-party tag, or even an existing-but-now-
                    # reconfigured spool — fire the deferred configuration.
                    # The "loaded" signal is `state == 11` (Bambu's "filament
                    # fed to extruder" code), NOT tray_type — 3rd-party spools
                    # without readable RFID report state=11 but tray_type=""
                    # because the AMS sensor can't read filament metadata.
                    # Requiring a non-empty tray_type would lock out exactly
                    # the users this feature targets. Upstream b42aaca5 #1247.
                    if not fp_type.strip() and cur_state == 11 and assignment.spool:
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
                        assignment.fingerprint_color = cur_color
                        assignment.fingerprint_type = cur_type
                        continue

                    if not _colors_similar(cur_color, fp_color) or cur_type.upper() != fp_type.upper():
                        # Fingerprint mismatch - but check if tray now matches the
                        # assigned spool (e.g. auto-configure changed the tray).
                        spool = assignment.spool
                        if spool:
                            spool_color = (spool.rgba or "FFFFFFFF").upper()
                            spool_type = (spool.material or "").upper()
                            if _colors_similar(cur_color, spool_color) and cur_type.upper() == spool_type:
                                # Tray was reconfigured to match the spool - update fingerprint
                                logger.info(
                                    "Auto-unlink: spool %d AMS%d-T%d - fingerprint mismatch but tray matches spool, updating fp",
                                    assignment.spool_id,
                                    assignment.ams_id,
                                    assignment.tray_id,
                                )
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
                        stale.append(assignment)  # Spool changed
            for a in stale:
                await db.delete(a)
            if stale:
                logger.info("Auto-unlinked %d stale spool assignments for printer %d", len(stale), printer_id)
            # Commit any changes (stale deletions and/or fingerprint updates)
            await db.commit()
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
        async with _get_ams_assignment_lock(printer_id), async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.spool import Spool as _Spool2
            from backend.app.models.spool_assignment import SpoolAssignment as SA
            from backend.app.services.spool_tag_matcher import (
                auto_assign_spool,
                create_spool_from_tray,
                find_matching_untagged_spool,
                get_spool_by_tag,
                is_bambu_tag,
                is_valid_tag,
                link_tag_to_inventory_spool,
            )

            _spoolman_on = await get_setting(db, "spoolman_enabled")
            if not _spoolman_on or _spoolman_on.lower() != "true":
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
                            remain_raw = tray.get("remain")
                            if (
                                remain_raw is not None
                                and existing_assignment.spool
                                and not existing_assignment.spool.weight_locked
                            ):
                                try:
                                    remain_val = int(remain_raw)
                                except (TypeError, ValueError):
                                    remain_val = -1
                                if 1 <= remain_val <= 100:
                                    lw = existing_assignment.spool.label_weight or 1000
                                    new_used = round(lw * (100 - remain_val) / 100.0, 1)
                                    current_used = existing_assignment.spool.weight_used or 0
                                    if new_used > current_used + 1:
                                        logger.info(
                                            "Weight sync: spool %d weight_used %s -> %s (remain=%d)",
                                            existing_assignment.spool_id,
                                            current_used,
                                            new_used,
                                            remain_val,
                                        )
                                        existing_assignment.spool.weight_used = new_used
                                        await db.commit()

                            # Re-apply stored K-profile when the live tray's
                            # cali_idx drifted from the spool's stored profile.
                            # Catches "reset slot → re-read" + any other path
                            # where the firmware loses the user's K-profile
                            # selection while the SpoolAssignment row persists.
                            # Per upstream maintainer's rule (b30a2831): any time
                            # a spool tag is identified and matches inventory,
                            # the slot must be configured with the spool's
                            # stored settings. Without this block the existing-
                            # assignment branch only ran weight-sync and let the
                            # firmware-default cali_idx win.
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

                                    state = printer_manager.get_status(printer_id)
                                    nozzle_diameter = "0.4"
                                    if state and state.nozzles:
                                        nd = state.nozzles[0].nozzle_diameter
                                        if nd:
                                            nozzle_diameter = nd
                                    try:
                                        nozzle_dia_float = float(nozzle_diameter)
                                    except (TypeError, ValueError):
                                        nozzle_dia_float = 0.4
                                    nozzle_vt = str(getattr(state, "nozzle_volume_type", "standard") or "standard")
                                    slot_extruder: int = 0
                                    if state and state.ams_extruder_map:
                                        if ams_id == 255:
                                            slot_extruder = 1 - tray_id
                                        else:
                                            slot_extruder = state.ams_extruder_map.get(str(ams_id)) or 0

                                    effective_filament_id = derive_effective_filament_id(
                                        spool=spool, slot_tray_info_idx=tray_info_idx or None
                                    )
                                    if effective_filament_id:
                                        # Helper fires only when the live cali_idx
                                        # disagrees with the cached row's stable
                                        # identity, so steady-state pushes are no-ops.
                                        fired, fc = await apply_active_calibration_to_slot(
                                            db=db,
                                            printer_id=printer_id,
                                            ams_id=ams_id,
                                            slot_id=tray_id,
                                            filament_id=effective_filament_id,
                                            nozzle_diameter=nozzle_dia_float,
                                            nozzle_volume_type=nozzle_vt,
                                            extruder_id=slot_extruder,
                                            spool_id=spool.id,
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
                            if not spool:
                                # Try matching an untagged inventory spool (same material/color)
                                spool = await find_matching_untagged_spool(db, tray)
                                if spool:
                                    await link_tag_to_inventory_spool(db, spool, tray)
                                else:
                                    spool = await create_spool_from_tray(db, tray)
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
                            # Non-BL spool with some tag - let user choose
                            await ws_manager.broadcast(
                                {
                                    "type": "unknown_tag",
                                    "printer_id": printer_id,
                                    "ams_id": ams_id,
                                    "tray_id": tray_id,
                                    "tag_uid": tag_uid,
                                    "tray_uuid": tray_uuid,
                                }
                            )
                        else:
                            # No tag at all - let user choose from inventory
                            await ws_manager.broadcast(
                                {
                                    "type": "unknown_tag",
                                    "printer_id": printer_id,
                                    "ams_id": ams_id,
                                    "tray_id": tray_id,
                                    "tag_uid": "",
                                    "tray_uuid": "",
                                }
                            )
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

            # Check if weight sync is disabled
            disable_weight_sync_str = await get_setting(db, "spoolman_disable_weight_sync")
            disable_weight_sync = disable_weight_sync_str and disable_weight_sync_str.lower() == "true"

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

                    try:
                        inv_remaining = inventory_weights.get((ams_id, tray.tray_id))
                        result = await client.sync_ams_tray(
                            tray,
                            printer_name,
                            disable_weight_sync=disable_weight_sync,
                            cached_spools=cached_spools,
                            inventory_remaining=inv_remaining,
                        )
                        if result:
                            synced += 1
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


async def _capture_snapshot_for_notification(printer_id: int, printer, logger) -> bytes | None:
    """Capture a camera snapshot for notification image attachment.

    Returns JPEG bytes (max 2.5MB) or None if capture fails or is unavailable.
    Uses: external camera > buffered frame > fresh capture.
    """
    if not printer:
        return None

    try:
        from backend.app.api.routes.settings import get_setting

        async with async_session() as db:
            capture_enabled = await get_setting(db, "capture_finish_photo")

        if capture_enabled is not None and capture_enabled.lower() != "true":
            return None

        # Try external camera first
        if printer.external_camera_enabled and printer.external_camera_url:
            logger.info("[SNAPSHOT] Capturing from external camera for printer %s", printer_id)
            from backend.app.services.external_camera import capture_frame

            frame_data = await capture_frame(
                printer.external_camera_url,
                printer.external_camera_type or "mjpeg",
                snapshot_url=printer.external_camera_snapshot_url,
            )
            if frame_data and len(frame_data) <= 2_500_000:
                logger.info("[SNAPSHOT] External camera frame: %s bytes", len(frame_data))
                return _apply_camera_rotation(frame_data, printer, logger)

        # Try buffered frame from active stream
        from backend.app.api.routes.camera import _active_chamber_streams, _active_streams, get_buffered_frame

        active_for_printer = [k for k in _active_streams if k.startswith(f"{printer_id}-")]
        active_chamber = [k for k in _active_chamber_streams if k.startswith(f"{printer_id}-")]
        buffered_frame = get_buffered_frame(printer_id)

        if (active_for_printer or active_chamber) and buffered_frame:
            logger.info("[SNAPSHOT] Using buffered frame for printer %s: %s bytes", printer_id, len(buffered_frame))
            if len(buffered_frame) <= 2_500_000:
                return _apply_camera_rotation(buffered_frame, printer, logger)

        # Fresh capture from printer camera
        logger.info("[SNAPSHOT] Capturing fresh frame for printer %s", printer_id)
        from backend.app.services.camera import capture_camera_frame_bytes

        frame_data = await capture_camera_frame_bytes(
            printer.ip_address, printer.access_code, printer.model, timeout=15
        )
        if frame_data and len(frame_data) <= 2_500_000:
            logger.info("[SNAPSHOT] Fresh camera frame: %s bytes", len(frame_data))
            return _apply_camera_rotation(frame_data, printer, logger)

    except Exception as e:
        logger.warning("[SNAPSHOT] Failed to capture snapshot for printer %s: %s", printer_id, e)

    return None


def _apply_camera_rotation(image_data: bytes, printer, logger) -> bytes:
    """Apply camera rotation to snapshot image if configured."""
    rotation = getattr(printer, "camera_rotation", 0)
    if not rotation or rotation == 0:
        return image_data

    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(image_data))
        # PIL rotate is counter-clockwise, so negate for clockwise rotation
        img = img.rotate(-rotation, expand=True)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        rotated = buf.getvalue()
        logger.info("[SNAPSHOT] Applied %d° rotation: %s → %s bytes", rotation, len(image_data), len(rotated))
        return rotated
    except Exception as e:
        logger.warning("[SNAPSHOT] Failed to apply rotation: %s", e)
        return image_data


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
    )


def _load_objects_from_archive(archive, printer_id: int, logger) -> None:
    """Thin wrapper around services.archive.load_objects_from_archive_into_state.

    Kept as a backward-compatible shim — call sites in this file pass a
    ``logger`` arg that the shared helper doesn't take (it uses its own
    module logger). Logging behaviour is equivalent.
    """
    _ = logger  # noqa: F841 — intentional discard, see docstring
    from backend.app.services.archive import load_objects_from_archive_into_state

    load_objects_from_archive_into_state(archive, printer_id)


def _archive_matches_check_name(row, check_name: str) -> bool:
    """Whether a ``PrintArchive`` row should be considered "the same print" as
    the on_print_start event identified by ``check_name``.

    Mirrors the matching rule used by the name-match adoption block — print_name
    OR filename in {check_name, check_name+".3mf", check_name+".gcode.3mf"} —
    and also tolerates rows whose ``filename`` is a full path like
    ``/data/Metadata/Plate_1.gcode.3mf`` (compares the basename too).

    Strict equality on ``filename`` is **not** enough: legacy rows can have
    been stored as ``Plate_1.3mf`` (from the fallback path's
    ``f"{print_name}.3mf"``) while a fresh on_print_start reports
    ``Plate_1.gcode.3mf`` — without this leniency cleanup would close the live
    row as "different file", and the name-match block would then create a
    duplicate fresh archive.
    """
    if not check_name:
        return False
    candidates = {
        check_name,
        f"{check_name}.3mf",
        f"{check_name}.gcode.3mf",
    }
    if row.print_name and row.print_name == check_name:
        return True
    if row.filename:
        if row.filename in candidates:
            return True
        if row.filename.split("/")[-1] in candidates:
            return True
    return False


async def _close_stale_printing_rows(
    printer_id: int,
    check_name: str,
    db,
    logger: logging.Logger,
) -> None:
    """Close stale ``status='printing'`` archive rows on this printer.

    Runs at the top of ``on_print_start`` (after the ``_active_prints`` /
    ``_expected_prints`` early returns) so the downstream name-match adoption
    block + FTP/hash flow only sees rows that could plausibly be the live
    print.

    Closure rule: any row with non-NULL ``started_at`` + ``print_time_seconds``
    is closed when its name doesn't match ``check_name`` (printer has moved on
    to a different file) or when there's a *newer* same-name sibling (older
    same-name rows are leftovers from a prior aborted dispatch — they can't
    be the live print since a printer runs one job at a time). The name match
    follows ``_archive_matches_check_name`` — same lenient rule the downstream
    name-match adoption block uses. Status:

      * ``predicted_end < now`` → ``'completed'`` with
        ``completed_at = started_at + print_time_seconds`` (slicer's predicted
        natural end; treat the row as if the print finished cleanly when its
        timer ran out — accuracy is approximate, slicer estimates can drift,
        but the row was clearly orphaned and the alternative is leaving it
        forever in ``'printing'``).
      * ``predicted_end > now`` → ``'cancelled'`` (printer is now on a
        different print — the old one never reached its predicted end).

    Each closed row gets ``extra_data['recovered_by_cleanup']=True`` for audit.

    The newest same-name row is **left alone** so the downstream name-match
    adoption block can decide based on its own time gate:

      * That block now also checks ``predicted_end > now``. If true → adopt
        the row directly (mid-print recovery shortcut, skips FTP + hash on
        a potentially big 3MF).
      * If ``predicted_end < now`` → defer to the FTP/hash path which will
        either confirm (slicer underestimated, hash matches) or fall through
        to a fresh fallback archive (different bytes despite same filename).

    Trade-off: a long BamDude downtime followed by the operator reprinting
    the same file from the printer's screen could see the new live print
    glued onto the old (still ``'printing'``) row downstream — the new
    on_print_start adopts the old archive instead of creating a fresh one.
    Documented as acceptable: the same risk exists in pre-cleanup-pass code
    via the unconditional name-match adoption block this feeds.
    """
    from backend.app.models.archive import PrintArchive

    now = datetime.now(timezone.utc)

    rows = (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.status == "printing")
                .where(PrintArchive.completed_at.is_(None))
                .order_by(PrintArchive.started_at.asc())
            )
        )
        .scalars()
        .all()
    )

    if not rows:
        return

    same_name = [r for r in rows if _archive_matches_check_name(r, check_name)]
    other_name = [r for r in rows if not _archive_matches_check_name(r, check_name)]

    logger.info(
        "[cleanup] printer=%s check_name=%r → %d 'printing' rows: %d same-name, %d other-name",
        printer_id,
        check_name,
        len(rows),
        len(same_name),
        len(other_name),
    )
    for r in rows:
        match_kind = "same" if _archive_matches_check_name(r, check_name) else "other"
        logger.info(
            "[cleanup]   row #%s filename=%r print_name=%r started_at=%s print_time=%ss → %s-name",
            r.id,
            r.filename,
            r.print_name,
            r.started_at.isoformat() if r.started_at else None,
            r.print_time_seconds,
            match_kind,
        )

    rows_to_close: list = list(other_name)
    if len(same_name) > 1:
        rows_to_close.extend(same_name[:-1])  # keep newest, close older siblings
        logger.info(
            "[cleanup] keeping newest same-name row #%s, closing %d older sibling(s)",
            same_name[-1].id,
            len(same_name) - 1,
        )
    elif len(same_name) == 1:
        logger.info(
            "[cleanup] keeping single same-name row #%s for downstream name-match adoption",
            same_name[0].id,
        )

    closed_count = 0
    for row in rows_to_close:
        if row.started_at is None or row.print_time_seconds is None:
            continue
        started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=timezone.utc)
        predicted_end = started + timedelta(seconds=row.print_time_seconds)
        if predicted_end < now:
            row.status = "completed"
            row.completed_at = predicted_end
        else:
            row.status = "cancelled"
        extra = dict(row.extra_data or {})
        extra["recovered_by_cleanup"] = True
        row.extra_data = extra
        logger.info(
            "[cleanup] Closed stale archive #%s (filename=%r) as %s (predicted_end=%s)",
            row.id,
            row.filename,
            row.status,
            predicted_end.isoformat(),
        )
        closed_count += 1

    if closed_count > 0:
        await db.commit()
        logger.info(
            "[cleanup] Closed %d stale 'printing' archive row(s) on printer %s",
            closed_count,
            printer_id,
        )


async def on_print_start(printer_id: int, data: dict):
    """Handle print start - archive the 3MF file immediately."""
    logger = logging.getLogger(__name__)

    logger.info("[CALLBACK] on_print_start called for printer %s, data keys: %s", printer_id, list(data.keys()))

    # Clear any stale user-stopped flag from previous print cycles
    _user_stopped_printers.discard(printer_id)

    # Cancel any active bed cooldown task for this printer
    existing_task = _bed_cooldown_tasks.pop(printer_id, None)
    if existing_task and not existing_task.done():
        existing_task.cancel()
        logger.info("[BED-COOL] Cancelled bed cooldown monitor for printer %s (new print started)", printer_id)

    # Clear cached cover images so the new print's thumbnail is fetched fresh
    from backend.app.api.routes.printers import clear_cover_cache

    clear_cover_cache(printer_id)

    await ws_manager.send_print_start(printer_id, data)

    # Fire any user-defined ``print_started`` macros (MQTT-action type today —
    # e.g. "turn chamber light off when print starts"). Fire-and-forget so
    # macro delay_seconds doesn't stall this handler.
    try:
        from backend.app.services.macro_trigger import fire_event_macros

        await fire_event_macros("print_started", printer_id, async_session, printer_manager)
    except Exception as e:
        logger.warning("print_started macros failed to schedule: %s", e)

    # Notify when the print-start AMS mapping references tray slots without spool assignments.
    await notify_missing_spool_assignments_on_print_start(printer_id, data, logger)

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

    # Capture AMS tray remain% for filament consumption tracking (skip if Spoolman handles usage)
    try:
        async with async_session() as db:
            from backend.app.api.routes.settings import get_setting

            _spoolman_on = await get_setting(db, "spoolman_enabled")
            if not _spoolman_on or _spoolman_on.lower() != "true":
                from backend.app.services.usage_tracker import on_print_start as usage_on_print_start

                await usage_on_print_start(printer_id, data, printer_manager, db=db)
    except Exception as e:
        logger.warning("Usage tracker on_print_start failed: %s", e)

    # Track if notification was sent (to avoid sending twice)
    notification_sent = False

    # Smart plug automation: turn on plug when print starts
    try:
        async with async_session() as db:
            await smart_plug_manager.on_print_start(printer_id, db)
    except Exception as e:
        logger.warning("Smart plug on_print_start failed: %s", e)

    async with async_session() as db:
        from backend.app.models.printer import Printer

        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()

        # Auto-light-off used to fire here; it's superseded by the generic
        # macro framework (configure a ``chamber_light_off`` mqtt-action
        # macro on the ``print_started`` event). fire_event_macros() above
        # already dispatched those — nothing to do here.

        # Plate detection check - pause if objects detected on build plate
        logger.info(
            f"[PLATE CHECK] printer_id={printer_id}, plate_detection_enabled={printer.plate_detection_enabled if printer else 'NO PRINTER'}"
        )
        if printer and printer.plate_detection_enabled:
            logger.info("[PLATE CHECK] ENTERING plate detection code for printer %s", printer_id)
            try:
                from backend.app.services.plate_detection import check_plate_empty

                # Build ROI tuple from printer settings if available
                roi = None
                if all(
                    [
                        printer.plate_detection_roi_x is not None,
                        printer.plate_detection_roi_y is not None,
                        printer.plate_detection_roi_w is not None,
                        printer.plate_detection_roi_h is not None,
                    ]
                ):
                    roi = (
                        printer.plate_detection_roi_x,
                        printer.plate_detection_roi_y,
                        printer.plate_detection_roi_w,
                        printer.plate_detection_roi_h,
                    )

                # Auto-turn on chamber light if it's off for better detection
                light_was_off = False
                client = printer_manager.get_client(printer_id)
                if client and client.state:
                    light_was_off = not client.state.chamber_light
                    if light_was_off:
                        logger.info("[PLATE CHECK] Turning on chamber light for printer %s", printer_id)
                        client.set_chamber_light(True)
                        # Wait for light to physically turn on and camera to adjust exposure
                        await asyncio.sleep(2.5)

                logger.info("[PLATE CHECK] Running plate detection for printer %s", printer_id)
                plate_result = await check_plate_empty(
                    printer_id=printer_id,
                    ip_address=printer.ip_address,
                    access_code=printer.access_code,
                    model=printer.model,
                    include_debug_image=False,
                    external_camera_url=printer.external_camera_url,
                    external_camera_type=printer.external_camera_type,
                    use_external=printer.external_camera_enabled,
                    roi=roi,
                    external_camera_snapshot_url=printer.external_camera_snapshot_url,
                )

                # Restore chamber light to original state
                if light_was_off and client:
                    logger.info("[PLATE CHECK] Restoring chamber light to off for printer %s", printer_id)
                    client.set_chamber_light(False)

                if not plate_result.needs_calibration and not plate_result.is_empty:
                    # Objects detected - pause the print!
                    logger.warning(
                        f"[PLATE CHECK] Objects detected on plate for printer {printer_id}! "
                        f"Confidence: {plate_result.confidence:.0%}, Diff: {plate_result.difference_percent:.1f}%"
                    )
                    client = printer_manager.get_client(printer_id)
                    if client:
                        # Plant the reason hint BEFORE issuing the pause
                        # command — Bambu firmware fires HMS 0300_8001
                        # ("paused by user") for any pause-command we send,
                        # so without this hint the resulting RUNNING→PAUSE
                        # edge would label this auto-pause as user-initiated.
                        set_expected_pause_reason(printer_id, "plate_objects")
                        client.pause_print()
                        logger.info("[PLATE CHECK] Print paused for printer %s", printer_id)

                    # Send notification about plate not empty
                    await ws_manager.broadcast(
                        {
                            "type": "plate_not_empty",
                            "printer_id": printer_id,
                            "printer_name": printer.name,
                            "message": f"Objects detected on build plate! Print paused. (Diff: {plate_result.difference_percent:.1f}%)",
                        }
                    )

                    # Also send push notification
                    try:
                        await notification_service.on_plate_not_empty(
                            printer_id=printer_id,
                            printer_name=printer.name,
                            db=db,
                            difference_percent=plate_result.difference_percent,
                        )
                    except Exception as notif_err:
                        logger.warning("[PLATE CHECK] Failed to send notification: %s", notif_err)
                else:
                    logger.info("[PLATE CHECK] Plate is empty for printer %s, proceeding with print", printer_id)
            except Exception as plate_err:
                # Don't block print on plate detection errors
                logger.warning("[PLATE CHECK] Plate detection failed for printer %s: %s", printer_id, plate_err)

        if not printer or not printer.auto_archive:
            # Send notification without archive data (auto-archive disabled)
            logger.info(
                f"[CALLBACK] Skipping archive - printer: {printer is not None}, auto_archive: {printer.auto_archive if printer else 'N/A'}"
            )
            if not notification_sent:
                # Even with auto-archive disabled, try to recover created_by_id from
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

        # Skip calibration prints - internal printer files should not be archived
        # Bambu calibration gcode lives under /usr/ (e.g. /usr/etc/print/auto_cali_for_user.gcode)
        if filename and filename.startswith("/usr/"):
            logger.info("[CALLBACK] Skipping archive - internal printer file detected: %s", filename)
            if not notification_sent:
                await _send_print_start_notification(printer_id, data, logger=logger)
            return

        if not filename and not subtask_name:
            # Send notification without archive data (no filename)
            logger.info("[CALLBACK] Skipping archive - no filename or subtask_name")
            if not notification_sent:
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

        # Re-trigger guard: MQTT reconnect / state flap / keep-alive can cause
        # on_print_start to fire multiple times for the same physical print.
        # Each repeat event, if it reached the fallback archive-create path,
        # would add a new archive row (same file on disk, dedup'd dir — but
        # still a new DB record). Reports in the wild: laptop sleeping →
        # printer reconnects → several archive duplicates spaced minutes
        # apart. If _active_prints already tracks ANY variation of this print
        # on this printer, treat the event as a duplicate and return — no new
        # archive, no re-notification.
        #
        # **Side effect we DO need to repeat**: re-load printable_objects +
        # skip_objects_supported into the freshly-created MQTT client state.
        # ``ensure_fresh_connection`` (default mqtt_connection_timeout=300s
        # for legacy printers) periodically swaps the BambuMQTTClient for a
        # new one with empty state, so without this re-load the skip-objects
        # button goes dark roughly every 5 minutes mid-print. Server-restart
        # papers over the symptom (it wipes _active_prints, so the next
        # on_print_start takes the full path and loads objects), but the
        # underlying state was being silently lost.
        for key in expected_keys:
            active_archive_id = _active_prints.get(key)
            if active_archive_id:
                logger.info(
                    "[CALLBACK] Duplicate print_start for printer %s (active archive %s via key %s) — skipping (re-loading objects into fresh client state)",
                    printer_id,
                    active_archive_id,
                    key,
                )
                try:
                    from backend.app.models.archive import PrintArchive

                    _arc = (
                        await db.execute(select(PrintArchive).where(PrintArchive.id == active_archive_id))
                    ).scalar_one_or_none()
                    if _arc is not None:
                        _load_objects_from_archive(_arc, printer_id, logger)
                except Exception as e:
                    logger.debug("[CALLBACK] re-load printable_objects failed: %s", e)
                return

        # Cleanup pass: close stale 'printing' rows that can't be the live print.
        # Runs before _expected_prints lookup so leftover rows from a previous
        # session don't pollute the downstream name-match adoption block. The
        # newest row that matches the same check_name as this event is preserved
        # — the name-match block + FTP path decide what to do with it. The same
        # lenient name-matching rule (print_name OR filename in {check_name,
        # check_name+".3mf", check_name+".gcode.3mf"} with basename tolerance)
        # is shared between cleanup and the adoption block, so legacy rows
        # stored as "Plate_1.3mf" don't get cleaned up when the new event
        # reports "Plate_1.gcode.3mf".
        cleanup_check_name = subtask_name or (
            filename.split("/")[-1].replace(".gcode", "").replace(".3mf", "") if filename else ""
        )
        logger.info(
            "[cleanup] inputs: subtask_name=%r filename=%r → check_name=%r",
            subtask_name,
            filename,
            cleanup_check_name,
        )
        if cleanup_check_name:
            try:
                await _close_stale_printing_rows(printer_id, cleanup_check_name, db, logger)
            except Exception as e:
                logger.warning("[cleanup] Stale-printing cleanup failed: %s", e)

        expected_archive_id = None
        for key in expected_keys:
            expected_archive_id = _expected_prints.pop(key, None)
            _expected_print_registered_at.pop(key, None)
            if expected_archive_id:
                # Clean up other possible keys for this print
                for other_key in expected_keys:
                    _expected_prints.pop(other_key, None)
                    _expected_print_registered_at.pop(other_key, None)
                break

        if expected_archive_id:
            # This is a reprint/scheduled print - use existing archive, don't create new one
            logger.info("Using expected archive %s for print (skipping duplicate)", expected_archive_id)
            from backend.app.models.archive import PrintArchive

            result = await db.execute(select(PrintArchive).where(PrintArchive.id == expected_archive_id))
            archive = result.scalar_one_or_none()

            if archive:
                # Update archive status to printing
                archive.status = "printing"
                archive.started_at = datetime.now(timezone.utc)
                await db.commit()

                # Track as active print
                _active_prints[(printer_id, archive.filename)] = archive.id
                if subtask_name:
                    _active_prints[(printer_id, f"{subtask_name}.3mf")] = archive.id

                # Ensure queue reflects the busy state (queue-driven flow
                # already sets this from the scheduler, but repeat-safe).
                await mark_queue_printing_for_printer(printer_id)

                # Set up energy tracking (#941: persist start on archive row)
                await _record_energy_start(archive, printer_id, db, context="expected-print")

                await ws_manager.send_archive_updated(
                    {
                        "id": archive.id,
                        "status": "printing",
                    }
                )

                # Send notification with archive data (reprint/scheduled)
                if not notification_sent:
                    # Use archive's created_by_id; fall back to the creator registered via
                    # register_expected_print (handles library-file-based queue items where
                    # the freshly-created archive has no created_by_id yet).
                    # Pop ALL matching keys so no stale entries remain in the dict.
                    fallback_creator = None
                    for key in expected_keys:
                        popped = _expected_print_creators.pop(key, None)
                        if fallback_creator is None:
                            fallback_creator = popped
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
                        ams_mapping=_get_start_ams_mapping(data, archive.id),
                    )
                except Exception as e:
                    logger.warning("[SPOOLMAN] Failed to store tracking data: %s", e)

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
        live_plate_id = parse_plate_id(data.get("filename")) or parse_plate_id(
            (data.get("raw_data") or {}).get("gcode_file")
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

        # Pre-check: if the printer told us a subtask_id, look for an archive
        # with the same id on this printer first. The printer-assigned id is
        # unique per submission (see start_print submission_id), so a match is
        # a stronger signal than name alone. Only consult status="printing" to
        # avoid reviving old cancelled rows — BamDude has no stale-cancel
        # heuristic, so a terminal status here is deliberate (#972 port).
        existing_archive: PrintArchive | None = None
        if subtask_id:
            subtask_match = await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.subtask_id == subtask_id)
                .where(PrintArchive.status == "printing")
                .order_by(PrintArchive.created_at.desc())
                .limit(1)
            )
            existing_archive = subtask_match.scalar_one_or_none()
            if existing_archive:
                logger.info(
                    "Resuming archive %s on subtask_id match (%s)",
                    existing_archive.id,
                    subtask_id,
                )

        if existing_archive is None:
            name_query = (
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.status == "printing")
                .where(
                    or_(
                        PrintArchive.print_name == check_name,
                        PrintArchive.filename.in_(
                            [
                                f"{check_name}.3mf",
                                f"{check_name}.gcode.3mf",
                            ]
                        ),
                    )
                )
            )
            _pf = _plate_filter()
            if _pf is not None:
                name_query = name_query.where(_pf)
            existing = await db.execute(name_query.order_by(PrintArchive.created_at.desc()))
            candidates = existing.scalars().all()
            # Time gate: only adopt rows where the slicer's predicted print end
            # is still in the future ("the print could plausibly still be
            # running"). Rows with NULL started_at / print_time_seconds keep
            # the legacy unconditional-adoption behaviour so this fix doesn't
            # regress legacy archives without timing data. Rows with
            # predicted_end < now are deliberately *not* adopted here — if
            # they're the live print under a slicer-underestimate scenario,
            # the FTP/hash path that follows will catch them; otherwise this
            # is a stuck row that the cleanup pass should have already closed.
            now_ts = datetime.now(timezone.utc)
            existing_archive = None
            for cand in candidates:
                if cand.started_at is None or cand.print_time_seconds is None:
                    logger.info(
                        "[name-match] adopt #%s (NULL started_at or print_time — legacy fallback path)",
                        cand.id,
                    )
                    existing_archive = cand
                    break
                started_ts = cand.started_at if cand.started_at.tzinfo else cand.started_at.replace(tzinfo=timezone.utc)
                predicted_end = started_ts + timedelta(seconds=cand.print_time_seconds)
                # Diagnostic: surface the exact comparison so a "this should
                # have been adopted but wasn't" report can be triaged from
                # logs without running a debugger.
                decision = "adopt" if predicted_end > now_ts else "defer-to-ftp"
                logger.info(
                    "[name-match] cand #%s started_at=%s (tz=%s) print_time=%ss predicted_end=%s now=%s → %s",
                    cand.id,
                    cand.started_at.isoformat() if cand.started_at else None,
                    cand.started_at.tzinfo if cand.started_at else None,
                    cand.print_time_seconds,
                    predicted_end.isoformat(),
                    now_ts.isoformat(),
                    decision,
                )
                if predicted_end > now_ts:
                    existing_archive = cand
                    break
            # Backfill subtask_id onto an archive that matched by name only —
            # next restart can then use the faster subtask_id pre-check path.
            if existing_archive and subtask_id and existing_archive.subtask_id is None:
                existing_archive.subtask_id = subtask_id
                await db.commit()
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
            _active_prints[(printer_id, existing_archive.filename)] = existing_archive.id
            # Ensure queue reflects the busy state.
            await mark_queue_printing_for_printer(printer_id)
            # Also set up energy tracking if not already tracked (#941: persisted column)
            if existing_archive.energy_start_kwh is None:
                await _record_energy_start(existing_archive, printer_id, db, context="existing-printing")
            # Send notification with archive data (existing archive)
            if not notification_sent:
                archive_data = {
                    "print_time_seconds": existing_archive.print_time_seconds,
                    "created_by_id": existing_archive.created_by_id,
                }
                await _send_print_start_notification(printer_id, data, archive_data, logger)
            # Extract printable objects from the archived 3MF file
            _load_objects_from_archive(existing_archive, printer_id, logger)
            return

        # Shared download helper (same logic used by the retry service).
        from backend.app.services.archive_download import try_download_3mf

        temp_dir = app_settings.archive_dir / "temp"
        download_result = await try_download_3mf(printer, subtask_name, filename, temp_dir)
        if download_result:
            temp_path, downloaded_filename = download_result
        else:
            temp_path = None
            downloaded_filename = None

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
                    # Override the stale subtask_name so the fallback archive's
                    # print_name reflects the correct plate. Prefer the swapped
                    # name when we have one; otherwise let filename win.
                    if corrected_subtask:
                        subtask_name = corrected_subtask
                    else:
                        subtask_name = ""

        # Post-download content-hash adoption: secondary safety net for the
        # mid-print recovery case (BamDude restarted while a print was active
        # → MQTT replay fires on_print_start, but pre-download name lookup
        # missed because of a name normalisation quirk). Only adopts archives
        # that are STILL in-flight on this printer (status="printing" with no
        # completed_at). Terminal-status rows (completed/failed/cancelled) are
        # NEVER touched: when a user reprints the same file from the printer
        # screen, the prior history must stay intact and the new run must get
        # its own archive row. The earlier "flip terminal back to printing"
        # behaviour ate users' history when one file was reprinted across
        # multiple printers from the screen.
        if temp_path:
            from backend.app.services.archive import ArchiveService as _ArchiveSvc

            temp_hash = _ArchiveSvc.compute_file_hash(temp_path)
            hash_query = (
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.status == "printing")
                .where(PrintArchive.completed_at.is_(None))
                .where(PrintArchive.file_path != "")
                .where(
                    or_(
                        PrintArchive.content_hash == temp_hash,
                        PrintArchive.source_content_hash == temp_hash,
                    )
                )
            )
            # Multi-plate disambiguation: ``temp_hash`` is identical across
            # plates of the same container (whole 3MF on disk + on SD), so
            # the live plate index is the only thing that distinguishes a
            # plate-5 row from a stuck plate-1 sibling on the same printer.
            _pf = _plate_filter()
            if _pf is not None:
                hash_query = hash_query.where(_pf)
            hash_match_result = await db.execute(hash_query.order_by(PrintArchive.created_at.desc()).limit(1))
            hash_match = hash_match_result.scalar_one_or_none()
            if hash_match is not None:
                logger.info(
                    "Adopting in-flight archive %s by content_hash match",
                    hash_match.id,
                )
                # Backfill subtask_id so the next restart skips the content_hash path.
                if subtask_id and hash_match.subtask_id is None:
                    hash_match.subtask_id = subtask_id
                    await db.commit()
                _active_prints[(printer_id, hash_match.filename)] = hash_match.id
                if subtask_name:
                    _active_prints[(printer_id, f"{subtask_name}.3mf")] = hash_match.id
                    _active_prints[(printer_id, subtask_name)] = hash_match.id
                await mark_queue_printing_for_printer(printer_id)
                if hash_match.energy_start_kwh is None:
                    await _record_energy_start(hash_match, printer_id, db, context="hash-adoption")
                if not notification_sent:
                    archive_data = {
                        "print_time_seconds": hash_match.print_time_seconds,
                        "created_by_id": hash_match.created_by_id,
                    }
                    await _send_print_start_notification(printer_id, data, archive_data, logger)
                _load_objects_from_archive(hash_match, printer_id, logger)
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                return

        if not downloaded_filename or not temp_path:
            logger.warning("Could not find 3MF file for print: %s", filename or subtask_name)
            # Create a fallback archive without 3MF data so the print is still tracked
            # This commonly happens with P1S/A1 printers where FTP has file size limitations
            try:
                from backend.app.models.archive import PrintArchive

                # Derive print name from subtask_name or filename
                print_name = subtask_name or filename
                if print_name:
                    # Clean up the name (remove extensions, path parts)
                    print_name = print_name.split("/")[-1]
                    print_name = print_name.replace(".gcode.3mf", "").replace(".gcode", "").replace(".3mf", "")
                else:
                    print_name = "Unknown Print"

                # Create minimal archive entry
                fallback_archive = PrintArchive(
                    printer_id=printer_id,
                    filename=filename or f"{print_name}.3mf",
                    file_path="",  # Empty - no 3MF file available
                    file_size=0,
                    print_name=print_name,
                    subtask_id=subtask_id,
                    status="printing",
                    started_at=datetime.now(timezone.utc),
                    # External / direct-dispatch falls back to the printer's
                    # default queue so post-m019 archive-driven counters
                    # include it.
                    queue_id=await _default_queue_id_for_printer(db, printer_id),
                    # Retry hooks recover from this state on:
                    # (1) BamDude startup sweep, (2) printer reconnect,
                    # (3) on_print_complete last-chance, (4) manual via API.
                    # Purely on-demand — no periodic loop.
                    extra_data={
                        "no_3mf_available": True,
                        "original_subtask": subtask_name,
                        "_print_data": data,
                    },
                )

                db.add(fallback_archive)
                await db.commit()
                await db.refresh(fallback_archive)

                logger.info("Created fallback archive %s for %s (no 3MF available)", fallback_archive.id, print_name)

                # Start timelapse session if external camera is enabled
                if printer.external_camera_enabled and printer.external_camera_url:
                    from backend.app.services.layer_timelapse import start_session

                    start_session(
                        printer_id,
                        fallback_archive.id,
                        printer.external_camera_url,
                        printer.external_camera_type or "mjpeg",
                        snapshot_url=printer.external_camera_snapshot_url,
                    )
                    logger.info("Started layer timelapse for printer %s, archive %s", printer_id, fallback_archive.id)

                # Track as active print
                _active_prints[(printer_id, fallback_archive.filename)] = fallback_archive.id
                if filename:
                    _active_prints[(printer_id, filename)] = fallback_archive.id
                if subtask_name:
                    _active_prints[(printer_id, f"{subtask_name}.3mf")] = fallback_archive.id
                    _active_prints[(printer_id, subtask_name)] = fallback_archive.id

                # Ensure queue reflects the busy state (external / direct print).
                await mark_queue_printing_for_printer(printer_id)
                await maybe_register_external_stagger(printer_id)

                # Record starting energy if smart plug available (#941: persisted column)
                await _record_energy_start(fallback_archive, printer_id, db, context="fallback")

                # Send WebSocket notification
                await ws_manager.send_archive_created(
                    {
                        "id": fallback_archive.id,
                        "printer_id": fallback_archive.printer_id,
                        "filename": fallback_archive.filename,
                        "print_name": fallback_archive.print_name,
                        "status": fallback_archive.status,
                    }
                )

                # MQTT relay - publish archive created
                try:
                    await mqtt_relay.on_archive_created(
                        archive_id=fallback_archive.id,
                        print_name=fallback_archive.print_name,
                        printer_name=printer.name,
                        status=fallback_archive.status,
                    )
                except Exception:
                    pass  # Don't fail if MQTT fails

                # Store Spoolman tracking data (may not work for fallback since no 3MF)
                try:
                    await _store_spoolman_print_data(
                        printer_id,
                        fallback_archive.id,
                        fallback_archive.file_path,
                        db,
                        printer_manager,
                        ams_mapping=_get_start_ams_mapping(data, fallback_archive.id),
                    )
                except Exception as e:
                    logger.debug("[SPOOLMAN] Could not store tracking for fallback archive: %s", e)

                # Send notification without archive data (file not found)
                if not notification_sent:
                    await _send_print_start_notification(printer_id, data, logger=logger)
                return
            except Exception as e:
                logger.error("Failed to create fallback archive: %s", e)
                # Send notification without archive data (file not found)
                if not notification_sent:
                    await _send_print_start_notification(printer_id, data, logger=logger)
                return

        try:
            # Archive the file with status "printing"
            service = ArchiveService(db)
            archive = await service.archive_print(
                printer_id=printer_id,
                source_file=temp_path,
                print_data={**data, "status": "printing"},
                subtask_id=subtask_id,
            )

            if archive:
                # External / direct-dispatch archive: attribute to the
                # printer's default queue so post-m019 archive-driven
                # counters include it. (Queue-driven prints already had
                # queue_id set at dispatch time — nothing to do there.)
                # Explicit commit because the success path below doesn't
                # always commit (``_record_energy_start`` only does so when
                # a smart plug is present).
                if archive.queue_id is None:
                    archive.queue_id = await _default_queue_id_for_printer(db, printer_id)
                    await db.commit()

                # Detect swap compatibility from filename. Covers both the
                # singular ".swap." suffix (older / custom tooling) and the
                # ".swaps." suffix that swaplist.app actually emits on export.
                fname_lower = (filename or downloaded_filename or "").lower()
                if (
                    fname_lower.endswith((".swap.3mf", ".swaps.3mf"))
                    or ".swap." in fname_lower
                    or ".swaps." in fname_lower
                ):
                    archive.swap_compatible = True
                    await db.flush()

                # Track this active print (use both original filename and downloaded filename)
                _active_prints[(printer_id, downloaded_filename)] = archive.id
                if filename and filename != downloaded_filename:
                    _active_prints[(printer_id, filename)] = archive.id
                if subtask_name:
                    _active_prints[(printer_id, f"{subtask_name}.3mf")] = archive.id

                # Ensure queue reflects the busy state (external / direct print).
                await mark_queue_printing_for_printer(printer_id)
                await maybe_register_external_stagger(printer_id)

                logger.info("Created archive %s for %s", archive.id, downloaded_filename)

                # Start timelapse session if external camera is enabled
                if printer.external_camera_enabled and printer.external_camera_url:
                    from backend.app.services.layer_timelapse import start_session

                    start_session(
                        printer_id,
                        archive.id,
                        printer.external_camera_url,
                        printer.external_camera_type or "mjpeg",
                        snapshot_url=printer.external_camera_snapshot_url,
                    )
                    logger.info("Started layer timelapse for printer %s, archive %s", printer_id, archive.id)

                # Record starting energy from smart plug if available (#941: persisted column)
                await _record_energy_start(archive, printer_id, db, context="auto-archive")

                await ws_manager.send_archive_created(
                    {
                        "id": archive.id,
                        "printer_id": archive.printer_id,
                        "filename": archive.filename,
                        "print_name": archive.print_name,
                        "status": archive.status,
                    }
                )

                # MQTT relay - publish archive created
                try:
                    await mqtt_relay.on_archive_created(
                        archive_id=archive.id,
                        print_name=archive.print_name,
                        printer_name=printer.name,
                        status=archive.status,
                    )
                except Exception:
                    pass  # Don't fail if MQTT fails

                # Send notification with archive data (new archive created)
                if not notification_sent:
                    archive_data = {
                        "print_time_seconds": archive.print_time_seconds,
                        "created_by_id": archive.created_by_id,
                    }
                    await _send_print_start_notification(printer_id, data, archive_data, logger)

                # Extract printable objects for skip object functionality
                try:
                    from backend.app.services.archive import extract_printable_objects_from_3mf

                    with open(temp_path, "rb") as f:
                        threemf_data = f.read()
                    # Extract with positions for UI overlay
                    printable_objects, bbox_all = extract_printable_objects_from_3mf(
                        threemf_data, include_positions=True
                    )
                    if printable_objects:
                        # Store objects in printer state
                        client = printer_manager.get_client(printer_id)
                        if client:
                            client.state.printable_objects = printable_objects
                            client.state.printable_objects_bbox_all = bbox_all
                            client.state.skipped_objects = []  # Reset skipped objects for new print
                            logger.info(
                                "Loaded %s printable objects for printer %s", len(printable_objects), printer_id
                            )
                except Exception as e:
                    logger.debug("Failed to extract printable objects: %s", e)

                # Store Spoolman tracking data for per-filament usage reporting
                try:
                    await _store_spoolman_print_data(
                        printer_id,
                        archive.id,
                        archive.file_path,
                        db,
                        printer_manager,
                        ams_mapping=_get_start_ams_mapping(data, archive.id),
                    )
                except Exception as e:
                    logger.warning("[SPOOLMAN] Failed to store tracking data: %s", e)

                # Capture timelapse file baseline for snapshot-diff on completion
                try:
                    baseline_files, _ = await _list_timelapse_videos(printer)
                    _timelapse_baselines[printer_id] = {f.get("name", "") for f in baseline_files}
                    logger.info(
                        "[TIMELAPSE] Baseline at print start: %s video files for printer %s",
                        len(_timelapse_baselines[printer_id]),
                        printer_id,
                    )
                except Exception as e:
                    logger.warning("[TIMELAPSE] Failed to capture baseline at print start: %s", e)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()


_TIMELAPSE_VIDEO_EXTENSIONS = (".mp4", ".avi")


async def _list_timelapse_videos(printer) -> tuple[list[dict], str | None]:
    """List video files from printer's timelapse directory.

    Finds MP4 (X1/A1 series) and AVI (P1 series) timelapse files.
    Returns (video_files, found_path) where video_files is a list of file dicts
    and found_path is the directory where they were found, or ([], None).
    """
    from backend.app.services.bambu_ftp import list_files_async

    logger = logging.getLogger(__name__)

    for timelapse_path in ["/timelapse", "/timelapse/video", "/record", "/recording"]:
        try:
            found_files = await list_files_async(
                printer.ip_address, printer.access_code, timelapse_path, printer_model=printer.model
            )
            if found_files:
                video_files = [
                    f
                    for f in found_files
                    if not f.get("is_directory") and f.get("name", "").lower().endswith(_TIMELAPSE_VIDEO_EXTENSIONS)
                ]
                if video_files:
                    return video_files, timelapse_path
        except Exception as e:
            logger.debug("[TIMELAPSE] Path %s failed: %s", timelapse_path, e)
            continue

    return [], None


async def _scan_for_timelapse_with_retries(archive_id: int, baseline_names: set[str] | None = None):
    """
    Scan for timelapse with retries using a snapshot-diff approach.

    Instead of picking the "most recent by mtime" (unreliable when the printer
    clock is wrong in LAN-only mode), we snapshot existing MP4 filenames BEFORE
    waiting, then look for any NEW filename that appears after each delay.

    If baseline_names is provided (captured at print start), it is used directly.
    Otherwise falls back to taking a baseline at completion time (best-effort
    for prints started before app restart).

    Falls back to name-matching (print name contained in MP4 filename) if no
    new file appears after all retries.
    """
    logger = logging.getLogger(__name__)

    # --- Phase 1: Take baseline snapshot of existing timelapse files ---
    try:
        async with async_session() as db:
            from backend.app.models.printer import Printer

            service = ArchiveService(db)
            archive = await service.get_archive(archive_id)

            if not archive:
                logger.warning("[TIMELAPSE] Archive %s not found, aborting", archive_id)
                return
            if archive.timelapse_path:
                logger.info("[TIMELAPSE] Archive %s already has timelapse attached", archive_id)
                return
            if not archive.printer_id:
                logger.warning("[TIMELAPSE] Archive %s has no printer, aborting", archive_id)
                return

            if baseline_names is not None:
                # Use pre-captured baseline from print start (no race condition)
                logger.info(
                    "[TIMELAPSE] Using print-start baseline: %s existing video files for archive %s",
                    len(baseline_names),
                    archive_id,
                )
            else:
                # Fallback: take baseline now (e.g. app restarted mid-print)
                result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
                printer = result.scalar_one_or_none()
                if not printer:
                    logger.warning("[TIMELAPSE] Printer not found for archive %s, aborting", archive_id)
                    return

                baseline_files, _ = await _list_timelapse_videos(printer)
                baseline_names = {f.get("name", "") for f in baseline_files}
                logger.info(
                    "[TIMELAPSE] Baseline snapshot (fallback): %s existing video files for archive %s",
                    len(baseline_names),
                    archive_id,
                )

            # Derive base_name for name-matching fallback. `resolve_display_stem`
            # (#1152) drops the full `.gcode.3mf` double-suffix Bambu Studio
            # writes by default — replaces the previous two-step strip-stem-
            # then-strip-`.gcode` sequence with one canonical helper.
            base_name = resolve_display_stem(archive.filename) if archive.filename else ""

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

        try:
            async with async_session() as db:
                from backend.app.models.printer import Printer
                from backend.app.services.bambu_ftp import download_file_bytes_async

                service = ArchiveService(db)
                archive = await service.get_archive(archive_id)

                if not archive:
                    logger.warning("[TIMELAPSE] Archive %s not found, stopping retries", archive_id)
                    return
                if archive.timelapse_path:
                    logger.info("[TIMELAPSE] Archive %s already has timelapse attached, stopping retries", archive_id)
                    return

                result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
                printer = result.scalar_one_or_none()
                if not printer:
                    logger.warning("[TIMELAPSE] Printer not found for archive %s, stopping retries", archive_id)
                    return

                video_files, found_path = await _list_timelapse_videos(printer)

                if not video_files:
                    logger.info("[TIMELAPSE] Attempt %s: No video files found, will retry", attempt)
                    continue

                logger.info("[TIMELAPSE] Attempt %s: Found %s video files in %s", attempt, len(video_files), found_path)
                for f in video_files[:5]:
                    logger.info("[TIMELAPSE]   - %s", f.get("name"))

                # Find files that are NEW (not in baseline snapshot)
                new_files = [f for f in video_files if f.get("name", "") not in baseline_names]

                if new_files:
                    # Pick the first new file (there should typically be exactly one)
                    target = new_files[0]
                    file_name = target.get("name")
                    remote_path = target.get("path") or f"/timelapse/{file_name}"
                    logger.info(
                        "[TIMELAPSE] Attempt %s: New file detected: %s (downloading for archive %s)",
                        attempt,
                        file_name,
                        archive_id,
                    )

                    timelapse_data = await download_file_bytes_async(
                        printer.ip_address, printer.access_code, remote_path, printer_model=printer.model
                    )
                    if timelapse_data:
                        success = await service.attach_timelapse(archive_id, timelapse_data, file_name)
                        if success:
                            logger.info("[TIMELAPSE] Successfully attached timelapse to archive %s", archive_id)
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

    # --- Phase 3: Fallback - try name matching against all files ---
    if base_name:
        logger.info("[TIMELAPSE] Retries exhausted, trying name-match fallback for '%s'", base_name)
        try:
            async with async_session() as db:
                from backend.app.models.printer import Printer
                from backend.app.services.bambu_ftp import download_file_bytes_async

                service = ArchiveService(db)
                archive = await service.get_archive(archive_id)
                if not archive or archive.timelapse_path:
                    return

                result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
                printer = result.scalar_one_or_none()
                if not printer:
                    return

                video_files, found_path = await _list_timelapse_videos(printer)
                for f in video_files:
                    fname = f.get("name", "")
                    if base_name.lower() in fname.lower():
                        remote_path = f.get("path") or f"/timelapse/{fname}"
                        logger.info("[TIMELAPSE] Name-match fallback: '%s' matches '%s'", base_name, fname)

                        timelapse_data = await download_file_bytes_async(
                            printer.ip_address, printer.access_code, remote_path, printer_model=printer.model
                        )
                        if timelapse_data:
                            success = await service.attach_timelapse(archive_id, timelapse_data, fname)
                            if success:
                                logger.info(
                                    "[TIMELAPSE] Name-match fallback attached timelapse to archive %s", archive_id
                                )
                                await ws_manager.send_archive_updated({"id": archive_id, "timelapse_attached": True})
                                return
                        break  # Only try the first name match

        except Exception as e:
            logger.warning("[TIMELAPSE] Name-match fallback failed: %s", e)

    logger.warning("[TIMELAPSE] All attempts exhausted for archive %s, giving up", archive_id)


async def on_print_complete(printer_id: int, data: dict):
    """Handle print completion - update the archive status."""
    import time

    logger = logging.getLogger(__name__)
    start_time = time.time()

    def log_timing(section: str):
        elapsed = time.time() - start_time
        logger.info("[TIMING] %s: %.3fs elapsed", section, elapsed)

    logger.info("[CALLBACK] on_print_complete started for printer %s", printer_id)

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

    # Capture user info before clearing (needed for print log entry)
    _print_user_info = printer_manager.get_current_print_user(printer_id)

    # Clear current print user tracking (Issue #206)
    printer_manager.clear_current_print_user(printer_id)

    # If the user explicitly stopped this print from the queue UI the printer will
    # report "failed" or "aborted" via MQTT.  Override that to "cancelled" so the
    # correct "print stopped" notification/email is sent instead of a failure alert.
    _raw_status = data.get("status", "completed")
    if printer_id in _user_stopped_printers and _raw_status in ("failed", "aborted"):
        logger.info(
            "[CALLBACK] Overriding status '%s' -> 'cancelled' for printer %s (print was stopped from queue by user)",
            _raw_status,
            printer_id,
        )
        data = {**data, "status": "cancelled"}
    _user_stopped_printers.discard(printer_id)

    # MQTT relay - publish print complete
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

    if not filename and not subtask_name:
        logger.warning("Print complete without filename or subtask_name")
        return

    logger.info("Print complete - filename: %s, subtask: %s, status: %s", filename, subtask_name, data.get("status"))

    # Fire any user-defined ``print_finished`` macros (e.g. turn chamber
    # light back on after print). Fire-and-forget so macro delay_seconds
    # doesn't stall completion. Mirrors the ``print_started`` path.
    try:
        from backend.app.services.macro_trigger import fire_event_macros

        await fire_event_macros("print_finished", printer_id, async_session, printer_manager)
    except Exception as e:
        logger.warning("print_finished macros failed to schedule: %s", e)

    # Build list of possible keys to try (matching how they were registered in on_print_start)
    possible_keys = []

    # Try subtask_name variations first (most reliable for matching)
    if subtask_name:
        possible_keys.append((printer_id, f"{subtask_name}.3mf"))
        possible_keys.append((printer_id, f"{subtask_name}.gcode.3mf"))
        possible_keys.append((printer_id, subtask_name))

    # Try filename variations
    if filename:
        # Extract just the filename if it's a path
        fname = filename.split("/")[-1] if "/" in filename else filename

        if fname.endswith(".3mf"):
            possible_keys.append((printer_id, fname))
        elif fname.endswith(".gcode"):
            base_name = fname.rsplit(".", 1)[0]
            possible_keys.append((printer_id, f"{base_name}.gcode.3mf"))
            possible_keys.append((printer_id, f"{base_name}.3mf"))
            possible_keys.append((printer_id, fname))
        else:
            possible_keys.append((printer_id, f"{fname}.gcode.3mf"))
            possible_keys.append((printer_id, f"{fname}.3mf"))
            possible_keys.append((printer_id, fname))

        # Also try full path versions
        if filename.endswith(".3mf"):
            possible_keys.append((printer_id, filename))
        elif filename.endswith(".gcode"):
            base_name = filename.rsplit(".", 1)[0]
            possible_keys.append((printer_id, f"{base_name}.3mf"))
            possible_keys.append((printer_id, filename))
        else:
            possible_keys.append((printer_id, f"{filename}.3mf"))
            possible_keys.append((printer_id, filename))

    # Find the archive for this print
    logger.info("Looking for archive in _active_prints, keys to try: %s...", possible_keys[:5])
    logger.info("Current _active_prints: %s", list(_active_prints.keys()))
    archive_id = None
    for key in possible_keys:
        archive_id = _active_prints.pop(key, None)
        if archive_id:
            logger.info("Found archive %s with key %s", archive_id, key)
            # Also clean up any other keys pointing to this archive
            keys_to_remove = [k for k, v in _active_prints.items() if v == archive_id]
            for k in keys_to_remove:
                _active_prints.pop(k, None)
            break

    if not archive_id:
        # Try to find by filename or subtask_name if not tracked (for prints started before app)
        async with async_session() as db:
            from backend.app.models.archive import PrintArchive

            # Try matching by subtask_name (stored as print_name) first
            if subtask_name:
                result = await db.execute(
                    select(PrintArchive)
                    .where(PrintArchive.printer_id == printer_id)
                    .where(PrintArchive.status == "printing")
                    .where(
                        or_(
                            PrintArchive.print_name.ilike(f"%{subtask_name}%"),
                            PrintArchive.filename.ilike(f"%{subtask_name}%"),
                        )
                    )
                    .order_by(PrintArchive.created_at.desc())
                    .limit(1)
                )
                archive = result.scalar_one_or_none()
                if archive:
                    archive_id = archive.id
                    logger.info("Found archive %s by subtask_name match: %s", archive_id, subtask_name)

            # Also try by filename
            if not archive_id and filename:
                result = await db.execute(
                    select(PrintArchive)
                    .where(PrintArchive.printer_id == printer_id)
                    .where(PrintArchive.filename == filename)
                    .where(PrintArchive.status == "printing")
                    .order_by(PrintArchive.created_at.desc())
                    .limit(1)
                )
                archive = result.scalar_one_or_none()
                if archive:
                    archive_id = archive.id

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
    _swap_status_ok = data.get("status", "completed") == "completed"

    # Swap-compatible files (macros baked in by third-party tooling like
    # swaplist.app) handle table changes internally — the plate is already
    # swapped and clean by the time the print finishes. Skip arming the
    # plate-clear gate so the queue scheduler doesn't block on manual
    # confirmation.
    if archive_id and _swap_status_ok:
        try:
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive as _ScArchive

                _sc_result = await db.execute(select(_ScArchive.swap_compatible).where(_ScArchive.id == archive_id))
                _sc_swap = _sc_result.scalar_one_or_none()
                if _sc_swap:
                    _plate_auto_cleared_by_swap = True
                    logger.info(
                        "[SWAP] swap_compatible archive %s — plate auto-cleared for printer %s", archive_id, printer_id
                    )
        except Exception as e:
            logger.debug("[SWAP] swap_compatible check failed (non-critical): %s", e)
    elif archive_id and not _swap_status_ok:
        logger.info(
            "[SWAP] Skipping swap_compatible auto-clear for printer %s archive %s: status=%s "
            "(failed/aborted/cancelled prints leave material on the bed; manual clear required)",
            printer_id,
            archive_id,
            data.get("status"),
        )

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

    # Post-print SD card cleanup (Issue #374)
    # Printers auto-start files from root on power cycle (ghost prints).
    # cleanup_after_print=True (default): delete 3MF from root
    # cleanup_after_print=False: move 3MF from root to /cache/ (keeps file, prevents ghost prints)
    # Always: delete .gcode from root and from /cache/
    try:
        if subtask_name:
            async with async_session() as db:
                from backend.app.models.printer import Printer

                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()

            if printer:
                from backend.app.services.bambu_ftp import (
                    delete_file_async,
                    download_file_bytes_async,
                    list_files_async,
                    rename_file_async,
                    upload_bytes_async,
                )

                should_delete = getattr(printer, "cleanup_after_print", True)

                # Process /cache/ files: delete .gcode, patch .bbl (disable auto_recovery)
                try:
                    cache_files = await list_files_async(
                        printer.ip_address,
                        printer.access_code,
                        "/cache",
                        printer_model=printer.model,
                    )
                    sanitized_base = subtask_name.replace(" ", "_")
                    for f in cache_files:
                        fname = f.get("name", "")
                        if f.get("is_dir"):
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
                                await delete_file_async(
                                    printer.ip_address,
                                    printer.access_code,
                                    f"/cache/{fname}",
                                    printer_model=printer.model,
                                )
                                logger.info("Deleted /cache/%s from printer %s", fname, printer.name)
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
                                        bbl_json["file path"] = file_path_val.replace("/sdcard/", "/sdcard/cache/", 1)
                                        modified = True

                                    if modified:
                                        # Preserve original formatting (4-space indent)
                                        patched = _json.dumps(bbl_json, indent=4).encode("utf-8")
                                        await upload_bytes_async(
                                            printer.ip_address,
                                            printer.access_code,
                                            patched,
                                            bbl_path,
                                            printer_model=printer.model,
                                        )
                                        logger.info(
                                            "Patched .bbl /cache/%s on printer %s (auto_recovery=false%s)",
                                            fname,
                                            printer.name,
                                            ", file path updated" if "/cache/" in bbl_json.get("file path", "") else "",
                                        )
                            except Exception as e:
                                logger.debug("Failed to patch .bbl /cache/%s: %s", fname, e)
                except Exception:
                    pass  # best-effort

                # Handle .3mf - delete or move to /cache/
                remote_3mf = f"/{subtask_name}.3mf"
                if should_delete:
                    for attempt in range(1, 4):
                        try:
                            r = await delete_file_async(
                                printer.ip_address,
                                printer.access_code,
                                remote_3mf,
                                printer_model=printer.model,
                            )
                            if r:
                                logger.info("Deleted %s from printer %s SD card", remote_3mf, printer.name)
                                break
                        except Exception as e:
                            r = False
                            logger.warning("SD cleanup attempt %d/3 for %s: %s", attempt, remote_3mf, e)
                        if not r and attempt < 3:
                            await asyncio.sleep(2)
                        elif not r:
                            logger.warning("SD cleanup failed after 3 attempts for %s", remote_3mf)
                else:
                    # Move .3mf to /cache/
                    cache_3mf = f"/cache/{subtask_name}.3mf"
                    for attempt in range(1, 4):
                        try:
                            r = await rename_file_async(
                                printer.ip_address,
                                printer.access_code,
                                remote_3mf,
                                cache_3mf,
                                printer_model=printer.model,
                            )
                            if r:
                                logger.info("Moved %s to %s on printer %s", remote_3mf, cache_3mf, printer.name)
                                break
                            if r is None:
                                break  # file not found, no retry
                        except Exception as e:
                            logger.debug("SD move attempt %d/3 for %s: %s", attempt, remote_3mf, e)
                        if attempt < 3:
                            await asyncio.sleep(2)

    except Exception as e:
        logger.warning("SD card file cleanup failed for printer %s: %s", printer_id, e)

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
    swap_config = _active_swap_config.pop(printer_id, None)
    swap_events: list[str] = list(swap_config.get("swap_macro_events", [])) if swap_config else []
    if not swap_events and archive_id:
        try:
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive as _PASwap

                _pa_extra = (
                    await db.execute(select(_PASwap.extra_data).where(_PASwap.id == archive_id))
                ).scalar_one_or_none()
                if isinstance(_pa_extra, dict):
                    pending = _pa_extra.get("swap_macro_events_pending")
                    if isinstance(pending, list) and pending:
                        swap_events = [str(e) for e in pending]
                        logger.info(
                            "[SWAP] Recovered swap config for printer %s from archive %s extra_data (events=%s)",
                            printer_id,
                            archive_id,
                            swap_events,
                        )
        except Exception as e:
            logger.debug("[SWAP] extra_data fallback lookup failed (non-critical): %s", e)

    if "swap_mode_change_table" in swap_events and not _swap_status_ok:
        # Print ended in a non-success state — the part is still attached
        # to the plate (or its remains are). Running change_table now would
        # either jam the swap rig on the stuck part or rotate the fouled
        # plate into the next print's path. Skip + leave the pending event
        # in place (so a manual operator-driven retry can still pick it up
        # if they explicitly choose to). The arm-gate block below will see
        # ``_plate_auto_cleared_by_swap=False`` and raise the manual-clear
        # gate as it would for a non-swap printer.
        logger.info(
            "[SWAP] Skipping change_table macro for printer %s archive %s: status=%s "
            "(operator must inspect + clear manually before next print)",
            printer_id,
            archive_id,
            data.get("status"),
        )
    elif "swap_mode_change_table" in swap_events:
        try:
            async with async_session() as db:
                from backend.app.models.printer import Printer as _SwapPrinter

                _sw_result = await db.execute(select(_SwapPrinter).where(_SwapPrinter.id == printer_id))
                _sw_printer = _sw_result.scalar_one_or_none()
                if _sw_printer and _sw_printer.swap_mode_enabled:
                    from backend.app.models.print_queue import PrintQueueItem
                    from backend.app.services.macro_executor import find_swap_macro

                    macro = await find_swap_macro(db, "swap_mode_change_table", _sw_printer)
                    if macro and macro.gcode:
                        logger.info("[SWAP] Running change_table macro '%s' for printer %s", macro.name, printer_id)
                        _sw_ok, _sw_msg = await printer_manager.execute_macro_and_wait(
                            printer_id, macro.gcode, macro.name
                        )
                        if _sw_ok:
                            # Table swapped successfully — skip arming the
                            # plate-clear gate so the scheduler doesn't wait
                            # for manual confirmation.
                            _plate_auto_cleared_by_swap = True
                            logger.info("[SWAP] change_table done — plate auto-cleared for printer %s", printer_id)
                        if not _sw_ok:
                            logger.error("[SWAP] change_table macro failed: %s — will pause queue", _sw_msg)
                            # Set waiting_reason on the next pending queue item so the
                            # scheduler doesn't try to start it on an un-swapped table.
                            _next = await db.execute(
                                select(PrintQueueItem)
                                .where(PrintQueueItem.queue_id == printer_id)
                                .where(PrintQueueItem.status == "pending")
                                .order_by(PrintQueueItem.position)
                                .limit(1)
                            )
                            _next_item = _next.scalar_one_or_none()
                            if _next_item:
                                _next_item.waiting_reason = f"Swap macro failed: {_sw_msg}"
                                await db.commit()
        except Exception as e:
            logger.error("[SWAP] change_table macro error: %s", e)
        finally:
            # Idempotency: tick swap_mode_change_table off the pending
            # checklist (variant 2 — drop just THIS event from the list,
            # not the whole key). A re-trigger of on_print_complete (MQTT
            # replay, reconnect flap) finds the event gone and skips
            # firing. If the list becomes empty, the key drops entirely
            # for clean state. Per-event removal is what keeps the
            # pending list a faithful checklist of "what's still to do".
            if archive_id:
                try:
                    async with async_session() as db:
                        from backend.app.models.archive import PrintArchive as _PASwapClear
                        from backend.app.services.archive import remove_swap_pending_event

                        _arc = await db.get(_PASwapClear, archive_id)
                        if _arc is not None and remove_swap_pending_event(_arc, "swap_mode_change_table"):
                            await db.commit()
                except Exception as e:
                    logger.debug(
                        "[SWAP] failed to remove swap_mode_change_table from pending for archive %s: %s", archive_id, e
                    )

    # Update queue item status early - must run before the archive_id early-return
    # so queue items don't get stuck in "printing" when archive lookup fails.
    try:
        async with async_session() as db:
            from backend.app.models.print_queue import PrintQueueItem

            result = await db.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.queue_id == printer_id)
                .where(PrintQueueItem.status == "printing")
            )
            printing_items = list(result.scalars().all())
            if len(printing_items) > 1:
                logger.warning(
                    "BUG: Multiple queue items in 'printing' status for printer %s: %s",
                    printer_id,
                    [(i.id, i.archive_id, i.library_file_id) for i in printing_items],
                )
            queue_item = printing_items[0] if printing_items else None
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
                # #1111: pre-RUNNING failures (FAILED from PREPARE/SLICING)
                # arrive without a queue_item.error_message because the print
                # never reached the runtime layer that normally populates it.
                # Synthesise from the live HMS list so the user sees e.g.
                # "[0500_4038] The nozzle diameter in sliced file is not
                # consistent with the current nozzle setting" instead of a
                # blank reason.
                if queue_status == "failed" and not queue_item.error_message:
                    queue_item.error_message = _format_hms_error_summary(data.get("hms_errors") or [])

                # Bump usage counters on the source library file so admins can
                # sort by "last printed" and (eventually) auto-purge stale
                # files — #1008.
                await _bump_library_file_usage_if_completed(db, queue_item, queue_status)

                # Update queue status and counters
                from backend.app.services.queue_counters import (
                    set_queue_error,
                    set_queue_idle,
                    update_queue_counters,
                )

                if queue_status == "failed":
                    await set_queue_error(db, queue_item.queue_id, failed_item_id=queue_item.id)
                elif queue_status == "completed":
                    await set_queue_idle(db, queue_item.queue_id)
                else:
                    # cancelled - set idle (user intentionally stopped)
                    await set_queue_idle(db, queue_item.queue_id)
                await update_queue_counters(db, queue_item.queue_id)
                await db.commit()
                logger.info("Updated queue item %s status to %s", queue_item.id, queue_status)

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
                            await library_trash_service.hard_delete_now(db, _cali_lib_file)
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
                if queue_item.auto_off_after:
                    result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
                    plugs = list(result.scalars().all())
                    enabled_plugs = [p for p in plugs if p.enabled]
                    if enabled_plugs:
                        logger.info("Auto-off requested for printer %s, waiting for cooldown...", printer_id)

                        async def cooldown_and_poweroff(pid: int, plug_ids: list[int]):
                            # Wait for nozzle to cool down
                            await printer_manager.wait_for_cooldown(pid, target_temp=50.0, timeout=600)
                            # Re-fetch plugs in new session and turn off each one
                            async with async_session() as new_db:
                                for plug_id in plug_ids:
                                    result = await new_db.execute(select(SmartPlug).where(SmartPlug.id == plug_id))
                                    p = result.scalar_one_or_none()
                                    if p and p.enabled:
                                        service = await smart_plug_manager.get_service_for_plug(p, new_db)
                                        success = await service.turn_off(p)
                                        if success:
                                            logger.info("Powered off printer %s via smart plug '%s'", pid, p.name)
                                        else:
                                            logger.warning(
                                                "Failed to power off printer %s via smart plug '%s'", pid, p.name
                                            )

                        asyncio.create_task(cooldown_and_poweroff(printer_id, [p.id for p in enabled_plugs]))

                # Auto-cleanup: completed queue items now live on via their
                # linked archive (counters re-computed from print_archives
                # post-m019). Failed / cancelled / skipped stay put so the
                # operator can retry from the queue UI.
                if queue_status == "completed" and queue_item.archive_id is not None:
                    _completed_item_id = queue_item.id
                    _completed_archive_id = queue_item.archive_id
                    _completed_queue_id = queue_item.queue_id
                    from backend.app.services.queue_counters import detach_print_queue_refs

                    await detach_print_queue_refs(db, [queue_item.id])
                    await db.delete(queue_item)
                    await db.commit()
                    logger.info(
                        "Auto-cleaned completed queue item %s (archive %s, queue %s)",
                        _completed_item_id,
                        _completed_archive_id,
                        _completed_queue_id,
                    )

                # Queue may now be empty — fire the queue-completed
                # notification. Must live in the ``if queue_item:`` branch:
                # only a finished *queue* item can empty the queue (an
                # external print never consumes a queue item, so the old
                # placement in the ``else`` branch never fired here).
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
                            PrintQueueItem.queue_id == printer_id,
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
            else:
                # No queue_item was printing → this was an external or
                # direct-dispatch print.  Still flip queue.status back to
                # idle/error so the UI's current-print card goes away and
                # pending items unblock.
                from backend.app.models.printer_queue import PrinterQueue
                from backend.app.services.queue_counters import set_queue_error, set_queue_idle

                _pq = (
                    await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
                ).scalar_one_or_none()
                if _pq and _pq.status == "printing":
                    status_raw = data.get("status", "completed")
                    if status_raw == "aborted":
                        status_raw = "cancelled"
                    if status_raw == "failed":
                        await set_queue_error(db, _pq.id)
                    else:
                        await set_queue_idle(db, _pq.id)
                    await db.commit()
                    logger.info(
                        "External/direct print finished on printer %s → queue %s status=%s",
                        printer_id,
                        _pq.id,
                        _pq.status,
                    )

                # Direct-dispatch / external prints also count toward
                # LibraryFile usage when we can trace the archive back to a
                # library file. Queue-less reprints, direct library prints,
                # and external prints whose archive got backfilled via
                # attach_3mf_to_archive all land here. Gated on
                # status=='completed' to match the queue branch.
                direct_status = data.get("status", "completed")
                if direct_status == "aborted":
                    direct_status = "cancelled"
                if direct_status == "completed" and archive_id:
                    from backend.app.models.archive import PrintArchive as _PaLib

                    lib_id = await db.scalar(select(_PaLib.library_file_id).where(_PaLib.id == archive_id))
                    if lib_id is not None:
                        await _bump_library_file_usage(db, lib_id)
                        await db.commit()
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

            # Check threshold setting
            async with async_session() as db:
                threshold_str = await get_setting(db, "bed_cooled_threshold")
            threshold = float(threshold_str) if threshold_str else 35.0

            # Check if any provider has on_bed_cooled enabled (early exit if none)
            async with async_session() as db:
                providers = await notification_service._get_providers_for_event(db, "on_bed_cooled", printer_id)
                if not providers:
                    logger.debug("[BED-COOL] No providers enabled for bed_cooled on printer %s", printer_id)
                    return

            logger.info("[BED-COOL] Monitoring bed temp for printer %s (threshold: %.0f°C)", printer_id, threshold)

            # Request a fresh full status so we get current bed_temper
            printer_manager.request_status_update(printer_id)

            max_polls = 120  # 120 * 15s = 30 min timeout
            for poll_num in range(max_polls):
                await asyncio.sleep(15)

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
            _bed_cooldown_tasks.pop(printer_id, None)

    # Only start bed cooldown for completed prints
    if data.get("status") == "completed":
        # Cancel any existing task for this printer
        existing_task = _bed_cooldown_tasks.pop(printer_id, None)
        if existing_task and not existing_task.done():
            existing_task.cancel()
        task = asyncio.create_task(_background_bed_cooldown())
        _bed_cooldown_tasks[printer_id] = task

    if not archive_id:
        logger.warning("Could not find archive for print complete: filename=%s, subtask=%s", filename, subtask_name)

        # Still send print-complete/failed/stopped notifications even without an archive.
        # Try to enrich with queue/library-file data so user-specific emails work too.
        async def _notify_no_archive():
            try:
                async with async_session() as db:
                    from backend.app.models.library import LibraryFile
                    from backend.app.models.print_queue import PrintQueueItem
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer_obj = result.scalar_one_or_none()
                    p_name = printer_obj.name if printer_obj else f"Printer {printer_id}"

                    # Try to find the most-recent queue item for this printer so we can
                    # recover created_by_id and estimated print time.
                    # NOTE: By the time this task runs the queue item status has already
                    # been updated to a terminal state (completed/failed/cancelled), so
                    # we look for recently-completed items (within the last 5 minutes).
                    no_archive_data: dict | None = None
                    try:
                        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                        q_result = await db.execute(
                            select(PrintQueueItem)
                            .where(PrintQueueItem.queue_id == printer_id)
                            .where(PrintQueueItem.status.in_(["completed", "failed", "cancelled"]))
                            .where(PrintQueueItem.completed_at >= cutoff)
                            .order_by(PrintQueueItem.completed_at.desc())
                            .limit(1)
                        )
                        queue_item = q_result.scalar_one_or_none()
                        if queue_item:
                            no_archive_data = {"created_by_id": queue_item.created_by_id}
                            # Pull estimated time from library file when available
                            if queue_item.library_file_id:
                                lib_result = await db.execute(
                                    select(LibraryFile).where(LibraryFile.id == queue_item.library_file_id)
                                )
                                lib_file = lib_result.scalar_one_or_none()
                                if lib_file and lib_file.print_time_seconds:
                                    no_archive_data["print_time_seconds"] = lib_file.print_time_seconds
                    except Exception as lookup_err:
                        logger.debug(
                            "[NOTIFY-BG] Could not look up queue item for no-archive notification: %s", lookup_err
                        )

                    ps = data.get("status", "completed")
                    logger.info(
                        "[NOTIFY-BG] Sending notification without archive: printer=%s, status=%s", printer_id, ps
                    )
                    await notification_service.on_print_complete(
                        printer_id, p_name, ps, data, db, archive_data=no_archive_data
                    )

                    # Send user-specific email if we have a created_by_id
                    if no_archive_data and no_archive_data.get("created_by_id"):
                        raw_filename = data.get("subtask_name") or data.get("filename", "Unknown")
                        await _dispatch_user_print_email(
                            ps,
                            no_archive_data["created_by_id"],
                            p_name,
                            raw_filename,
                            db,
                        )
                    logger.info("[NOTIFY-BG] Completed (no-archive path)")
            except Exception as e:
                logger.warning("[NOTIFY-BG] Failed to send notification without archive: %s", e, exc_info=True)

        task = asyncio.create_task(_notify_no_archive())
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
            if _print_user_id is not None:
                from backend.app.models.archive import PrintArchive as _ArchiveForAttr

                _attr_archive = await db.get(_ArchiveForAttr, archive_id)
                if _attr_archive is not None and _attr_archive.created_by_id is None:
                    _attr_archive.created_by_id = _print_user_id
                    await db.commit()

            # Auto-detect failure reason via curated short-code map (see
            # _HMS_FAILURE_REASONS). Module-based heuristics mislabel H2D
            # user-cancels (module 0x0C cancel echo) as "Layer shift".
            hms_errors = data.get("hms_errors", []) if status == "failed" else None
            if hms_errors:
                logger.info("[ARCHIVE] HMS errors at failure: %s", hms_errors)
            failure_reason = derive_failure_reason(status, hms_errors)
            if failure_reason:
                logger.info("[ARCHIVE] failure_reason=%r (status=%s)", failure_reason, status)
            elif status == "failed" and hms_errors:
                logger.info("[ARCHIVE] HMS errors present but none matched a known failure-reason short code")

            await service.update_archive_status(
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
                    datetime.now(timezone.utc) if status in ("completed", "failed", "aborted", "cancelled") else None
                ),
                failure_reason=failure_reason,
            )
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

    # Track filament consumption from AMS remain% deltas (skip if Spoolman handles usage)
    usage_results: list[dict] = []
    # Prefer ams_mapping captured from MQTT request topic (works for all print sources)
    stored_ams_mapping = data.get("ams_mapping")
    # Fallback to _print_ams_mappings for queue/reprint (set before print starts)
    if not stored_ams_mapping and archive_id:
        stored_ams_mapping = _print_ams_mappings.pop(archive_id, None)
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

    # Report filament usage to Spoolman if print completed successfully
    if data.get("status") == "completed":
        try:
            await _report_spoolman_usage(printer_id, archive_id)
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
                )
        except Exception as e:
            logger.debug("[SPOOLMAN] Cleanup failed: %s", e)

    # Run slow operations as background tasks to avoid blocking the event loop
    # These operations can take 5-10+ seconds and would freeze the UI if awaited

    async def _background_energy_calculation():
        """Calculate and save energy usage in background.

        Reads the starting kWh from the archive row (#941: persisted so a mid-print
        backend restart no longer loses per-print energy data).
        """
        try:
            logger.info("[ENERGY-BG] Starting energy calculation for archive %s", archive_id)
            async with async_session() as db:
                from backend.app.models.archive import PrintArchive

                archive = await db.get(PrintArchive, archive_id)
                if archive is None:
                    logger.warning("[ENERGY-BG] Archive %s no longer exists", archive_id)
                    return
                starting_kwh = archive.energy_start_kwh
                if starting_kwh is None:
                    logger.info("[ENERGY-BG] No start kWh recorded for archive %s", archive_id)
                    return

                plug_result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
                plug = plug_result.scalar_one_or_none()
                if plug is None:
                    logger.info("[ENERGY-BG] No smart plug for printer %s", printer_id)
                    return

                energy = await _get_plug_energy(plug, db)
                logger.info("[ENERGY-BG] Energy response: %s", energy)
                if not energy or energy.get("total") is None:
                    logger.warning("[ENERGY-BG] No 'total' in energy response")
                    return

                energy_used = round(energy["total"] - starting_kwh, 4)
                logger.info("[ENERGY-BG] Per-print energy: %s kWh", energy_used)
                if energy_used < 0:
                    logger.warning(
                        "[ENERGY-BG] Negative energy delta for archive %s (start=%s, end=%s) - counter reset?",
                        archive_id,
                        starting_kwh,
                        energy["total"],
                    )
                    return

                from backend.app.api.routes.settings import get_setting

                energy_cost_per_kwh = await get_setting(db, "energy_cost_per_kwh")
                cost_per_kwh = float(energy_cost_per_kwh) if energy_cost_per_kwh else 0.15
                archive.energy_kwh = energy_used
                archive.energy_cost = round(energy_used * cost_per_kwh, 3)
                await db.commit()
                logger.info("[ENERGY-BG] Saved: %s kWh, cost=%s", energy_used, archive.energy_cost)
        except Exception as e:
            logger.warning("[ENERGY-BG] Failed: %s", e)

    async def _background_finish_photo() -> str | None:
        """Capture finish photo in background. Returns photo filename if captured."""
        try:
            logger.info("[PHOTO-BG] Starting finish photo capture for archive %s", archive_id)

            from backend.app.api.routes.camera import _active_chamber_streams, _active_streams, get_buffered_frame

            async with async_session() as db:
                from backend.app.api.routes.settings import get_setting

                capture_enabled = await get_setting(db, "capture_finish_photo")

                if capture_enabled is None or capture_enabled.lower() == "true":
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()

                    if printer and archive_id:
                        from backend.app.models.archive import PrintArchive

                        result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                        archive = result.scalar_one_or_none()

                        if archive:
                            import uuid
                            from datetime import datetime
                            from pathlib import Path

                            if archive.file_path:
                                archive_dir = app_settings.base_dir / Path(archive.file_path).parent
                            else:
                                logger.warning("[PHOTO-BG] Archive %s has no file_path, using fallback dir", archive_id)
                                archive_dir = app_settings.archive_dir / str(archive.id)
                            photo_filename = None

                            # Check for external camera first
                            if printer.external_camera_enabled and printer.external_camera_url:
                                logger.info("[PHOTO-BG] Using external camera")
                                from backend.app.services.external_camera import capture_frame

                                frame_data = await capture_frame(
                                    printer.external_camera_url,
                                    printer.external_camera_type or "mjpeg",
                                    snapshot_url=printer.external_camera_snapshot_url,
                                )
                                if frame_data:
                                    photos_dir = archive_dir / "photos"
                                    photos_dir.mkdir(parents=True, exist_ok=True)
                                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                    photo_filename = f"finish_{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
                                    photo_path = photos_dir / photo_filename
                                    await asyncio.to_thread(photo_path.write_bytes, frame_data)
                                    logger.info("[PHOTO-BG] Saved external camera frame: %s", photo_filename)
                            else:
                                # Check if camera stream is active - use buffered frame to avoid freeze
                                # Check both RTSP streams (_active_streams) and chamber image streams (_active_chamber_streams)
                                active_for_printer = [k for k in _active_streams if k.startswith(f"{printer_id}-")]
                                active_chamber_for_printer = [
                                    k for k in _active_chamber_streams if k.startswith(f"{printer_id}-")
                                ]
                                buffered_frame = get_buffered_frame(printer_id)

                                if (active_for_printer or active_chamber_for_printer) and buffered_frame:
                                    # Use frame from active stream
                                    logger.info("[PHOTO-BG] Using buffered frame from active stream")
                                    photos_dir = archive_dir / "photos"
                                    photos_dir.mkdir(parents=True, exist_ok=True)
                                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                    photo_filename = f"finish_{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
                                    photo_path = photos_dir / photo_filename
                                    await asyncio.to_thread(photo_path.write_bytes, buffered_frame)
                                    logger.info("[PHOTO-BG] Saved buffered frame: %s", photo_filename)
                                else:
                                    # No active stream - capture new frame
                                    from backend.app.services.camera import capture_finish_photo

                                    photo_filename = await capture_finish_photo(
                                        printer_id=printer_id,
                                        ip_address=printer.ip_address,
                                        access_code=printer.access_code,
                                        model=printer.model,
                                        archive_dir=archive_dir,
                                    )

                            if photo_filename:
                                photos = archive.photos or []
                                photos.append(photo_filename)
                                archive.photos = photos
                                await db.commit()
                                logger.info("[PHOTO-BG] Saved: %s", photo_filename)
                                return photo_filename
            return None
        except Exception as e:
            logger.warning("[PHOTO-BG] Failed: %s", e)
            return None

    asyncio.create_task(_background_energy_calculation())
    # Photo capture task - result will be used by notifications
    photo_task = asyncio.create_task(_background_finish_photo())
    log_timing("Background tasks scheduled (energy, photo)")

    # Also run smart plug, notifications, and maintenance as background tasks
    print_status = data.get("status", "completed")

    async def _background_smart_plug():
        """Handle smart plug automation in background."""
        try:
            logger.info("[AUTO-OFF-BG] Starting smart plug automation for printer %s", printer_id)
            async with async_session() as db:
                await smart_plug_manager.on_print_complete(printer_id, print_status, db)
                logger.info("[AUTO-OFF-BG] Completed")
        except Exception as e:
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
                    if archive:
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
                            from backend.app.api.routes.settings import get_setting

                            external_url = await get_setting(db, "external_url")
                            if external_url:
                                external_url = external_url.rstrip("/")
                                archive_data["finish_photo_url"] = (
                                    f"{external_url}/api/v1/archives/{archive_id}/photos/{finish_photo_filename}"
                                )
                            else:
                                # Fallback to relative URL (won't work for external services)
                                archive_data["finish_photo_url"] = (
                                    f"/api/v1/archives/{archive_id}/photos/{finish_photo_filename}"
                                )

                            # Read finish photo bytes for image attachment (e.g. Pushover)
                            try:
                                from pathlib import Path

                                photo_path = (
                                    app_settings.base_dir
                                    / Path(archive.file_path).parent
                                    / "photos"
                                    / finish_photo_filename
                                )
                                if photo_path.exists():
                                    photo_bytes = await asyncio.to_thread(photo_path.read_bytes)
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
                    for item in items_needing_attention:
                        try:
                            await mqtt_relay.on_maintenance_alert(
                                printer_id=printer_id,
                                printer_name=printer_name,
                                maintenance_type=item["name"],
                                current_value=0,  # Not easily available here
                                threshold=0,  # Not easily available here
                            )
                        except Exception:
                            pass  # Don't fail if MQTT fails
                else:
                    logger.info("[MAINT-BG] Completed (no items need attention)")
        except Exception as e:
            logger.warning("[MAINT-BG] Failed: %s", e)

    asyncio.create_task(_background_smart_plug())
    asyncio.create_task(_background_maintenance_check())

    # Notification task waits for photo capture to complete first (with timeout)
    async def _photo_then_notify():
        """Wait for photo capture, then send notification with photo URL."""
        finish_photo = None
        try:
            finish_photo = await asyncio.wait_for(photo_task, timeout=45)
            logger.info("[PHOTO-NOTIFY] Photo task returned: %s", finish_photo)
        except TimeoutError:
            logger.warning("[PHOTO-NOTIFY] Photo capture timed out after 45s, sending notification without photo")
        except Exception as e:
            logger.warning("[PHOTO-NOTIFY] Photo task failed: %s", e)
        try:
            await _background_notifications(finish_photo)
        except Exception as e:
            logger.error("[PHOTO-NOTIFY] Notification sending failed: %s", e, exc_info=True)

    asyncio.create_task(_photo_then_notify())

    # Stitch external camera layer timelapse if session was active
    print_status = data.get("status", "completed")

    async def _background_layer_timelapse():
        """Stitch layer timelapse and attach to archive."""
        from backend.app.services.layer_timelapse import cancel_session, on_print_complete as tl_complete

        try:
            if print_status == "completed":
                logger.info("[LAYER-TL] Stitching layer timelapse for printer %s", printer_id)
                timelapse_path = await tl_complete(printer_id)
                if timelapse_path and archive_id:
                    logger.info("[LAYER-TL] Attaching timelapse %s to archive %s", timelapse_path, archive_id)
                    async with async_session() as db:
                        service = ArchiveService(db)
                        timelapse_data = await asyncio.to_thread(timelapse_path.read_bytes)
                        await service.attach_timelapse(archive_id, timelapse_data, "layer_timelapse.mp4")
                        # Clean up the temp file
                        await asyncio.to_thread(timelapse_path.unlink, missing_ok=True)
                        logger.info("[LAYER-TL] Layer timelapse attached successfully")
                elif timelapse_path:
                    # Timelapse created but no archive - just clean up
                    await asyncio.to_thread(timelapse_path.unlink, missing_ok=True)
            else:
                # Print failed or cancelled - cancel timelapse session
                cancel_session(printer_id)
                logger.info(
                    "[LAYER-TL] Cancelled layer timelapse for printer %s (status: %s)", printer_id, print_status
                )
        except Exception as e:
            logger.warning("[LAYER-TL] Failed: %s", e)
            # Try to cancel session on error
            try:
                cancel_session(printer_id)
            except Exception:
                pass  # Best-effort timelapse session cancellation on error

    asyncio.create_task(_background_layer_timelapse())

    log_timing("All background tasks scheduled")

    # Auto-scan for timelapse if recording was active during the print
    if archive_id and data.get("timelapse_was_active") and data.get("status") == "completed":
        logger.info("[TIMELAPSE] Timelapse was active during print, scheduling auto-scan for archive %s", archive_id)
        # Schedule timelapse scan as background task with retries
        # The printer needs time to encode the video after print completion
        baseline = _timelapse_baselines.pop(printer_id, None)
        asyncio.create_task(_scan_for_timelapse_with_retries(archive_id, baseline))
        log_timing("Timelapse scan scheduled")

    # Arm the plate-clear gate if the printer is configured to require it
    # and no swap path already cleared the plate. Persisted to DB so an
    # Auto Off power cycle can't let the queue bypass the confirmation (#961).
    if archive_id and not _plate_auto_cleared_by_swap:
        try:
            async with async_session() as db:
                _r = await db.execute(select(Printer.require_plate_clear).where(Printer.id == printer_id))
                _require = _r.scalar_one_or_none()
            if _require:
                printer_manager.set_awaiting_plate_clear(printer_id, True)
                logger.info("[PLATE] Armed awaiting_plate_clear gate for printer %s", printer_id)
        except Exception as e:
            logger.warning("[PLATE] Failed to arm awaiting_plate_clear: %s", e)

    logger.info("[CALLBACK] on_print_complete finished for printer %s, archive %s", printer_id, archive_id)


# AMS sensor history recording
_ams_history_task: asyncio.Task | None = None
AMS_HISTORY_INTERVAL = 300  # Record every 5 minutes
AMS_HISTORY_RETENTION_DAYS = 30  # Keep data for 30 days
_ams_cleanup_counter = 0  # Track recordings to trigger periodic cleanup
# Track alarm cooldowns (printer_id:ams_id:type -> last_alarm_time)
_ams_alarm_cooldown: dict[str, datetime] = {}
AMS_ALARM_COOLDOWN_MINUTES = 60  # Don't send same alarm more than once per hour


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
                result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
                printers = result.scalars().all()

                # Get alarm thresholds from settings
                humidity_threshold = 60.0  # Default: fair threshold
                temp_threshold = 35.0  # Default: fair threshold
                result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_fair"))
                setting = result.scalar_one_or_none()
                if setting:
                    try:
                        humidity_threshold = float(setting.value)
                    except (ValueError, TypeError):
                        pass  # Keep default threshold if stored value is invalid
                result = await db.execute(select(Settings).where(Settings.key == "ams_temp_fair"))
                setting = result.scalar_one_or_none()
                if setting:
                    try:
                        temp_threshold = float(setting.value)
                    except (ValueError, TypeError):
                        pass  # Keep default threshold if stored value is invalid

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

                        # Get humidity (prefer humidity_raw)
                        humidity_raw = ams_data.get("humidity_raw")
                        humidity_idx = ams_data.get("humidity")
                        humidity = None
                        if humidity_raw is not None:
                            try:
                                humidity = float(humidity_raw)
                            except (ValueError, TypeError):
                                pass  # Skip unparseable humidity; will try fallback
                        if humidity is None and humidity_idx is not None:
                            try:
                                humidity = float(humidity_idx)
                            except (ValueError, TypeError):
                                pass  # Skip unparseable humidity index value

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
                            humidity_raw=float(humidity_raw) if humidity_raw else None,
                            temperature=temperature,
                        )
                        db.add(history)
                        recorded_count += 1

                        # Generate AMS label and determine if it's AMS-HT (A, B, C, D or HT-A for AMS-Lite/Hub)
                        is_ams_ht = ams_id >= 128
                        if is_ams_ht:
                            ams_label = f"HT-{chr(65 + (ams_id - 128))}"
                        else:
                            ams_label = f"AMS-{chr(65 + ams_id)}"

                        # Check humidity alarm (only if above threshold)
                        if humidity is not None and humidity > humidity_threshold:
                            cooldown_key = f"{printer.id}:{ams_id}:humidity"
                            last_alarm = _ams_alarm_cooldown.get(cooldown_key)
                            now = datetime.now(timezone.utc)
                            if (
                                last_alarm is None
                                or (now - last_alarm).total_seconds() >= AMS_ALARM_COOLDOWN_MINUTES * 60
                            ):
                                _ams_alarm_cooldown[cooldown_key] = now
                                logger.info(
                                    f"Sending humidity alarm for {printer.name} {ams_label}: {humidity}% > {humidity_threshold}%"
                                )
                                try:
                                    # Call different notification method based on AMS type
                                    if is_ams_ht:
                                        await notification_service.on_ams_ht_humidity_high(
                                            printer.id, printer.name, ams_label, humidity, humidity_threshold, db
                                        )
                                    else:
                                        await notification_service.on_ams_humidity_high(
                                            printer.id, printer.name, ams_label, humidity, humidity_threshold, db
                                        )
                                except Exception as e:
                                    logger.warning("Failed to send humidity alarm: %s", e)

                        # Check temperature alarm (only if above threshold)
                        if temperature is not None and temperature > temp_threshold:
                            cooldown_key = f"{printer.id}:{ams_id}:temperature"
                            last_alarm = _ams_alarm_cooldown.get(cooldown_key)
                            now = datetime.now(timezone.utc)
                            if (
                                last_alarm is None
                                or (now - last_alarm).total_seconds() >= AMS_ALARM_COOLDOWN_MINUTES * 60
                            ):
                                _ams_alarm_cooldown[cooldown_key] = now
                                logger.info(
                                    f"Sending temperature alarm for {printer.name} {ams_label}: {temperature}°C > {temp_threshold}°C"
                                )
                                try:
                                    # Call different notification method based on AMS type
                                    if is_ams_ht:
                                        await notification_service.on_ams_ht_temperature_high(
                                            printer.id, printer.name, ams_label, temperature, temp_threshold, db
                                        )
                                    else:
                                        await notification_service.on_ams_temperature_high(
                                            printer.id, printer.name, ams_label, temperature, temp_threshold, db
                                        )
                                except Exception as e:
                                    logger.warning("Failed to send temperature alarm: %s", e)

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
                    retention_days = int(setting.value) if setting else AMS_HISTORY_RETENTION_DAYS

                    cutoff = datetime.utcnow() - timedelta(days=retention_days)
                    result = await db.execute(delete(AMSSensorHistory).where(AMSSensorHistory.recorded_at < cutoff))
                    await db.commit()
                    if result.rowcount > 0:
                        logger.info(
                            f"Cleaned up {result.rowcount} old AMS sensor history entries (older than {retention_days} days)"
                        )

            # Wait until next recording interval
            await asyncio.sleep(AMS_HISTORY_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("AMS history recording failed: %s", e)
            await asyncio.sleep(60)  # Wait a bit before retrying


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


# Printer runtime tracking
_runtime_tracking_task: asyncio.Task | None = None
RUNTIME_TRACKING_INTERVAL = 30  # Update every 30 seconds


async def track_printer_runtime():
    """Background task to track printer active runtime (RUNNING/PAUSE states)."""
    logger = logging.getLogger(__name__)

    # Wait for MQTT connections to establish on startup
    await asyncio.sleep(15)

    while True:
        try:
            from backend.app.models.printer import Printer

            async with async_session() as db:
                # Get all active printers
                result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
                printers = result.scalars().all()

                now = datetime.now(timezone.utc)
                updated_count = 0

                needs_commit = False

                for printer in printers:
                    # Get current state from printer manager
                    state = printer_manager.get_status(printer.id)
                    if not state:
                        logger.debug("[%s] Runtime tracking: no state available", printer.name)
                        continue
                    if not state.connected:
                        logger.debug("[%s] Runtime tracking: not connected", printer.name)
                        continue

                    # Check if printer is in an active state (RUNNING or PAUSE)
                    if state.state in ("RUNNING", "PAUSE"):
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
                        # Printer is idle/offline - clear last_runtime_update
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


# Camera stream orphan cleanup
_camera_cleanup_task: asyncio.Task | None = None
CAMERA_CLEANUP_INTERVAL = 60


async def _camera_cleanup_loop():
    """Periodically clean up orphaned ffmpeg processes."""
    from backend.app.api.routes.camera import cleanup_orphaned_streams

    while True:
        try:
            await cleanup_orphaned_streams()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger(__name__).warning("Camera stream cleanup failed: %s", e)
        await asyncio.sleep(CAMERA_CLEANUP_INTERVAL)


def start_camera_cleanup():
    global _camera_cleanup_task
    if _camera_cleanup_task is None:
        _camera_cleanup_task = asyncio.create_task(_camera_cleanup_loop())
        logging.getLogger(__name__).info("Camera stream cleanup started")


def stop_camera_cleanup():
    global _camera_cleanup_task
    if _camera_cleanup_task:
        _camera_cleanup_task.cancel()
        _camera_cleanup_task = None
        logging.getLogger(__name__).info("Camera stream cleanup stopped")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Install Windows-only asyncio Proactor cleanup-RST filter (#1113) before
    # anything else can spawn tasks that might trip it. The filter is a no-op
    # on non-Windows hosts.
    from backend.app.core.asyncio_handlers import install_proactor_reset_filter

    install_proactor_reset_filter()

    await init_db()

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
    from backend.app.services.makerworld import (
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
    printer_manager.set_ams_change_callback(on_ams_change)

    # Layer change callback for external camera timelapse
    async def on_layer_change(printer_id: int, layer_num: int):
        """Capture timelapse frame on layer change + first layer notification."""
        from backend.app.services.layer_timelapse import on_layer_change as tl_layer_change

        await tl_layer_change(printer_id, layer_num)

        # First layer complete notification (layer_num >= 2 means layer 1 is done)
        if 2 <= layer_num <= 5 and not _first_layer_notified.get(printer_id, False):
            _first_layer_notified[printer_id] = True
            try:
                async with async_session() as db:
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer).where(Printer.id == printer_id))
                    printer = result.scalar_one_or_none()
                    if not printer:
                        return
                    printer_name = printer.name
                    client = printer_manager.get_client(printer_id)
                    state = client.state if client else None
                    filename = (state.subtask_name or state.gcode_file or "Unknown") if state else "Unknown"
                    total_layers = state.total_layers if state else 0

                    image_data = await _capture_snapshot_for_notification(
                        printer_id, printer, logging.getLogger(__name__)
                    )
                    await notification_service.on_first_layer_complete(
                        printer_id, printer_name, filename, total_layers, db, image_data=image_data
                    )
            except Exception as e:
                logging.getLogger(__name__).warning("First layer notification failed: %s", e)

    printer_manager.set_layer_change_callback(on_layer_change)

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

    # Start the print scheduler
    asyncio.create_task(print_scheduler.run())

    # Start the auto-queue scheduler — routes pending auto items to idle
    # printers; assignment hands off to print_scheduler/background_dispatch.
    asyncio.create_task(auto_queue_scheduler.run())

    # Start background dispatch worker for send/start operations
    await background_dispatch.start()

    # Start the 3MF download retry service (fallback archives with file_path="").
    # The startup sweep does sequential FTP calls (up to 60s each) and MUST NOT
    # block lifespan — run it as a fire-and-forget task so uvicorn accepts
    # requests immediately.
    from backend.app.services.archive_download_retry import archive_download_retry

    asyncio.create_task(archive_download_retry.start())

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

    asyncio.create_task(_reconcile_stagger_on_startup())

    # Locale reconciliation: if the system language setting has drifted from
    # what's seeded in `maintenance_types` / `notification_templates` (e.g.
    # user ran an older version that seeded EN defaults then switched the
    # system language to UK without re-triggering the settings-PATCH flow,
    # or a restore brought in EN rows under a UK config), realign once at
    # startup.  Idempotent — safe to run every boot.  Fire-and-forget.
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
        except Exception as e:
            logging.getLogger(__name__).warning("Locale startup reconciliation failed: %s", e)

    asyncio.create_task(_reconcile_locale_on_startup())

    # Start the smart plug scheduler for time-based on/off
    smart_plug_manager.start_scheduler()

    # Resume any pending auto-offs that were interrupted by restart
    await smart_plug_manager.resume_pending_auto_offs()

    # Rehydrate plate-clear gates from DB so an Auto Off power cycle during a
    # pending confirmation can't let the queue auto-dispatch onto a dirty plate (#961).
    await printer_manager.load_awaiting_plate_clear_from_db()

    # Start the notification digest scheduler
    notification_service.start_digest_scheduler()

    # Start the Git backup scheduler
    await git_backup_service.start_scheduler()

    # Start the local backup scheduler (#884)
    await local_backup_service.start_scheduler()

    # Start AMS history recording
    start_ams_history_recording()

    # Start printer runtime tracking
    start_runtime_tracking()

    # Start camera stream orphan cleanup
    start_camera_cleanup()

    # Start expected-print TTL eviction (prevents memory leak when prints are
    # registered but on_print_start never fires)
    start_expected_prints_cleanup()

    # Initialize virtual printer manager and sync from DB
    from backend.app.services.virtual_printer import virtual_printer_manager

    virtual_printer_manager.set_session_factory(async_session)
    virtual_printer_manager.set_printer_manager(printer_manager)
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

        await obico_detection_service.start()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to start Obico detection service: %s", e)

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

    yield

    # Shutdown
    # Stop Telegram bot
    try:
        from backend.app.services.telegram_bot import stop_telegram_bot

        await stop_telegram_bot()
    except Exception:
        pass
    print_scheduler.stop()
    auto_queue_scheduler.stop()
    await background_dispatch.stop()
    smart_plug_manager.stop_scheduler()
    try:
        from backend.app.services.obico_detection import obico_detection_service

        obico_detection_service.stop()
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
        from backend.app.services.archive_purge import archive_purge_service

        archive_purge_service.stop_scheduler()
    except Exception:
        pass
    notification_service.stop_digest_scheduler()
    git_backup_service.stop_scheduler()
    local_backup_service.stop_scheduler()
    stop_ams_history_recording()
    stop_runtime_tracking()
    stop_camera_cleanup()
    # Tear down all camera fan-out broadcasters (#1089) so subscribers exit
    # cleanly and pump tasks don't outlive the asyncio loop.
    try:
        from backend.app.services.camera_fanout import shutdown_all_broadcasters

        await shutdown_all_broadcasters()
    except Exception as e:
        logging.warning("Failed to shut down camera broadcasters: %s", e)
    stop_expected_prints_cleanup()
    printer_manager.disconnect_all()
    await close_spoolman_client()

    # Stop all virtual printer services
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
    from backend.app.services.makerworld import (
        set_shared_http_client as _set_shared_makerworld_http_client_off,
    )

    _set_shared_makerworld_http_client_off(None)
    await _shared_makerworld_http_client.aclose()

    # Checkpoint WAL and close all database connections
    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        logging.info("WAL checkpoint completed")
    except Exception as e:
        logging.warning("WAL checkpoint failed: %s", e)
    await engine.dispose()


app = FastAPI(
    title=app_settings.app_name,
    description="Archive and manage Bambu Lab 3MF files",
    version=APP_VERSION,
    lifespan=lifespan,
)


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
    "/api/v1/auth/forgot-password",  # Password reset for advanced auth
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

# Route patterns that are public (read-only display data)
# These are checked with "in path" - needed because browsers load images/videos
# via <img src> and <video src> which don't include Authorization headers
PUBLIC_API_PATTERNS = [
    # Thumbnails
    "/thumbnail",  # /archives/{id}/thumbnail, /library/files/{id}/thumbnail
    "/plate-thumbnail/",  # /archives/{id}/plate-thumbnail/{plate_id}
    # Images and media
    "/photos/",  # /archives/{id}/photos/{filename}
    "/project-image/",  # /archives/{id}/project-image/{path}
    "/qrcode",  # /archives/{id}/qrcode
    "/timelapse",  # /archives/{id}/timelapse (video)
    "/cover",  # /printers/{id}/cover
    "/icon",  # /external-links/{id}/icon
    # Camera (streams loaded via <img> tag)
    "/camera/stream",  # /printers/{id}/camera/stream
    "/camera/snapshot",  # /printers/{id}/camera/snapshot
    # Slicer token-authenticated downloads - protocol handlers (bambustudioopen://,
    # orcaslicer://) cannot send auth headers. These endpoints validate a short-lived
    # download token in the URL path instead.
    "/dl/",  # /archives/{id}/dl/{token}/{filename}, /library/files/{id}/dl/{token}/{filename}
    # 2FA + OIDC endpoints consumed by the login page before the user has a JWT.
    # /verify trades a pre-auth token for a JWT; /oidc/providers lists enabled
    # OIDC buttons; /oidc/authorize/{id} starts the PKCE flow; /oidc/callback
    # lands from the identity provider; /oidc/exchange swaps the bridge token
    # for a JWT. All of these carry their own short-lived token binding so the
    # auth-middleware can skip them safely.
    "/auth/2fa/verify",
    "/auth/2fa/send-code",
    "/auth/oidc/providers",
    "/auth/oidc/authorize/",
    "/auth/oidc/callback",
    "/auth/oidc/exchange",
    # Obico ML API fetches JPEG frames by one-shot nonce (issue #172 follow-up).
    # The nonce itself is the credential: 32-byte random, single-use, ~30s TTL.
    "/obico/cached-frame/",  # /obico/cached-frame/{nonce}
    # MakerWorld thumbnail proxy (B.5 — 0.5.x cycle). <img> tags can't send
    # Authorization headers and would 401 every image; the upstream is
    # MakerWorld's *public* CDN (anyone visiting makerworld.com can fetch
    # without auth) and the route's SSRF guard restricts the upstream host
    # to the MakerWorld CDN allowlist, so this can't be abused as a
    # generic open proxy.
    "/makerworld/thumbnail",
]


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

    # Allow public patterns (read-only display data like thumbnails)
    for pattern in PUBLIC_API_PATTERNS:
        if pattern in path:
            return await call_next(request)

    # --- Auth gate ---------------------------------------------------------
    auth_header = request.headers.get("Authorization")
    x_api_key = request.headers.get("X-API-Key")

    # Check for API key auth first
    if x_api_key or (auth_header and auth_header.startswith("Bearer bb_")):
        # API key authentication - let the request through to be validated by route handler
        # API keys are validated per-route since they have different permission levels
        return await call_next(request)

    # Check for JWT auth
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate JWT token
    import jwt

    try:
        from backend.app.core.auth import ALGORITHM, SECRET_KEY

        token = auth_header.replace("Bearer ", "")
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise ValueError("No username in token")

        # Verify user exists and is active
        async with async_session() as db:
            from backend.app.core.auth import get_user_by_username

            user = await get_user_by_username(db, username)
            if not user or not user.is_active:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "User not found or inactive"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
    except jwt.ExpiredSignatureError:
        return JSONResponse(
            status_code=401,
            content={"detail": "Token has expired"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except (jwt.InvalidTokenError, ValueError, Exception):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await call_next(request)


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
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not _TRUSTED_FRAME_ORIGINS:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        # FastAPI's built-in Swagger UI / ReDoc pages load assets from
        # cdn.jsdelivr.net and bootstrap with an inline <script>, so the
        # default CSP would render a blank page (audit A.38).
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "img-src 'self' data: blob: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "connect-src 'self'; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "worker-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; " + _frame_ancestors("'none'")
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-src 'self' http: https:; " + _frame_ancestors("'none'")
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


# API routes
app.include_router(auth.router, prefix=app_settings.api_prefix)
app.include_router(mfa.router, prefix=app_settings.api_prefix)
app.include_router(users.router, prefix=app_settings.api_prefix)
app.include_router(groups.router, prefix=app_settings.api_prefix)
app.include_router(printers.router, prefix=app_settings.api_prefix)
# archive_purge must come BEFORE archives so its `/archives/trash/*` routes
# don't get swallowed by archives' `/archives/{archive_id}` catch-all.
app.include_router(archive_purge.router, prefix=app_settings.api_prefix)
app.include_router(archives.router, prefix=app_settings.api_prefix)
app.include_router(inventory.router, prefix=app_settings.api_prefix)
app.include_router(labels.router, prefix=app_settings.api_prefix)
app.include_router(settings_routes.router, prefix=app_settings.api_prefix)
app.include_router(cloud.router, prefix=app_settings.api_prefix)
app.include_router(local_presets.router, prefix=app_settings.api_prefix)
app.include_router(slicer_presets.router, prefix=app_settings.api_prefix)
app.include_router(slice_jobs.router, prefix=app_settings.api_prefix)
app.include_router(makerworld.router, prefix=app_settings.api_prefix)
app.include_router(smart_plugs.router, prefix=app_settings.api_prefix)
app.include_router(print_queue.router, prefix=app_settings.api_prefix)
app.include_router(print_options_preferences.router, prefix=app_settings.api_prefix)
app.include_router(auto_queue.router, prefix=app_settings.api_prefix)
app.include_router(background_dispatch_routes.router, prefix=app_settings.api_prefix)
app.include_router(kprofiles.router, prefix=app_settings.api_prefix)
app.include_router(notifications.router, prefix=app_settings.api_prefix)
app.include_router(notification_templates.router, prefix=app_settings.api_prefix)
app.include_router(user_notifications.router, prefix=app_settings.api_prefix)
app.include_router(spoolman.router, prefix=app_settings.api_prefix)
app.include_router(spoolman_inventory.router, prefix=app_settings.api_prefix)
app.include_router(updates.router, prefix=app_settings.api_prefix)
app.include_router(macros.router, prefix=app_settings.api_prefix)
app.include_router(maintenance.router, prefix=app_settings.api_prefix)
app.include_router(camera.router, prefix=app_settings.api_prefix)
app.include_router(external_links.router, prefix=app_settings.api_prefix)
app.include_router(projects.router, prefix=app_settings.api_prefix)
app.include_router(library.router, prefix=app_settings.api_prefix)
app.include_router(library_notes.router, prefix=app_settings.api_prefix)
app.include_router(library_trash.router, prefix=app_settings.api_prefix)
# archive_purge router is registered above before archives.router (route order).
app.include_router(api_keys.router, prefix=app_settings.api_prefix)
app.include_router(webhook.router, prefix=app_settings.api_prefix)
app.include_router(ams_history.router, prefix=app_settings.api_prefix)
app.include_router(ams_settings_routes.router, prefix=app_settings.api_prefix)
app.include_router(printer_settings_routes.router, prefix=app_settings.api_prefix)
app.include_router(filament_calibration_routes.router, prefix=app_settings.api_prefix)
app.include_router(system.router, prefix=app_settings.api_prefix)
app.include_router(support.router, prefix=app_settings.api_prefix)
app.include_router(bug_report.router, prefix=app_settings.api_prefix)
app.include_router(websocket.router, prefix=app_settings.api_prefix)
app.include_router(discovery.router, prefix=app_settings.api_prefix)
app.include_router(firmware.router, prefix=app_settings.api_prefix)
app.include_router(git_backup.router, prefix=app_settings.api_prefix)
app.include_router(local_backup.router, prefix=app_settings.api_prefix)
app.include_router(metrics.router, prefix=app_settings.api_prefix)
app.include_router(obico.router, prefix=app_settings.api_prefix)
app.include_router(virtual_printers.router, prefix=app_settings.api_prefix)
app.include_router(printer_queues.router, prefix=app_settings.api_prefix)
app.include_router(telegram.router, prefix=app_settings.api_prefix)


# Serve static files (React build)
if app_settings.static_dir.exists() and any(app_settings.static_dir.iterdir()):
    app.mount(
        "/assets",
        StaticFiles(directory=app_settings.static_dir / "assets"),
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


@app.get("/manifest.json")
async def serve_manifest():
    """Serve PWA manifest."""
    manifest_file = app_settings.static_dir / "manifest.json"
    if manifest_file.exists():
        return FileResponse(manifest_file, media_type="application/manifest+json")
    return {"error": "Manifest not found"}


@app.get("/sw.js")
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
