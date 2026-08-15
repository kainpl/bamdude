"""Preheat / heat-soak stage (upstream Bambuddy #1468).

Heats the bed — and, on supported printers, the chamber — and holds at
temperature *before* a queued print starts, giving engineering filaments the
heat-soak they need for adhesion and warp control. ``M191`` (wait-for-chamber)
is silently ignored by Bambu firmware, so the soak has to run at the
orchestration layer, on the idle printer, between FTP upload and ``start_print``.

**BamDude adaptation.** Upstream runs this as a method on its ``PrintScheduler``
(its dispatch layer). BamDude has a single dispatch layer — ``background_dispatch``
— and ``print_scheduler._start_print`` is only a thin wrapper, so the stage lives
here as a standalone coroutine that ``background_dispatch`` calls from both the
reprint and library-file paths. The per-item overrides arrive via the dispatch
``options`` dict (the same channel as ``bed_levelling`` / ``nozzle_offset_cali``)
rather than off a live ORM row. The wait and soak loops are cancel-aware (BamDude
dispatch supports mid-flight cancel — a 20-minute radiant soak must abort cleanly)
via an optional ``cancel_check`` callback that raises when a cancel is requested.

Resolution order:
  1. ``preheat_override`` ('off' skips; 'inherit' → global ``preheat_enabled``;
     'on' forces the stage on even when the global is off).
  2. Chamber target — explicit per-item override if non-null; else the max of
     ``preheat_filament_targets[normalize(tray_type)]`` across loaded AMS slots;
     else 0 (skips the chamber phase, keeps bed phase + soak). Mixed PA+PLA picks
     PA's 50 (max-across-slots — PA's chamber requirement is binding).
  3. Three hardware tiers branch the wait:
     - Chamber heater (H2C/H2D/H2D Pro/H2S/X2D/X1E → ``supports_chamber_heater``):
       ``set_ctt`` to target, then wait for the chamber sensor to reach it (or timeout).
     - Chamber sensor only (X1C/P2S → ``supports_chamber_temp`` ∧ ¬heater): no setpoint,
       wait for radiant bed warm-up to raise the sensor OR fall through on timeout.
     - No chamber sensor (P1S/P1P/A1/A1 Mini): bed only, hold for the soak timer.
  4. Airduct flap (H2C/H2D/H2D Pro/H2S/X2D/P2S → ``supports_airduct``) is flipped to
     match the target — heating when chamber_target > 0, cooling otherwise — before
     the setpoint, since Bambu firmware does NOT auto-switch the flap and the default cooling
     mode vents the chamber and fights the heater. Idempotent (skipped when already there).

Best-effort throughout: printer drops, refused set_ctt / set_airduct, missing bed
temperature all log and return cleanly. The normal upload + start path runs after
this returns regardless. Only ``cancel_check`` is allowed to propagate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from backend.app.services.printer_manager import (
    printer_manager,
    supports_airduct,
    supports_chamber_heater,
    supports_chamber_temp,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)

# Bundled defaults for preheat_filament_targets (#1468). Values are the chamber-
# temperature recommendations BambuStudio ships for the matching filament profile;
# users can override the whole map via Settings → Printing → Preheat & Heat Soak.
# "default" applies when a loaded tray's normalised type isn't in the map (rare —
# Bambu RFID-tagged spools always carry a known type). Mirrors the frontend
# DEFAULT_PREHEAT_FILAMENT_TARGETS in utils/preheatFilamentTargets.ts.
DEFAULT_PREHEAT_FILAMENT_TARGETS: dict[str, int] = {
    "PLA": 0,
    "PETG": 0,
    "PETG-CF": 40,
    "ABS": 45,
    "ASA": 45,
    "PA": 50,
    "PA-CF": 55,
    "PC": 50,
    "PC-FR": 50,
    "TPU": 0,
    "PVA": 0,
    "default": 0,
}

# Wait-loop tuning.
_BED_TOLERANCE = 2.0  # °C — floating-point + heater hysteresis slack
_CHAMBER_TOLERANCE = 2.0
_POLL_INTERVAL = 3.0  # seconds — frequent enough for responsive logging + cancel


async def _get_setting_str(db: AsyncSession, key: str) -> str | None:
    """Read a raw settings value. Lazy import of ``get_setting`` mirrors the rest of
    background_dispatch — the settings route pulls in schemas that would otherwise
    create an import cycle at module load."""
    from backend.app.api.routes.settings import get_setting

    return await get_setting(db, key)


async def _get_bool_setting(db: AsyncSession, key: str, default: bool) -> bool:
    raw = await _get_setting_str(db, key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() == "true"


async def _get_int_setting(db: AsyncSession, key: str, default: int) -> int:
    raw = await _get_setting_str(db, key)
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return default


async def _get_preheat_filament_targets(db: AsyncSession) -> dict[str, int]:
    """Parse the user-configured filament→chamber-target map, falling back to the
    bundled defaults on missing / malformed JSON. Keys are uppercased and the
    'DEFAULT' fallback is always present so the resolution loop can index it."""
    raw = await _get_setting_str(db, "preheat_filament_targets")
    if not raw:
        return {k.upper(): v for k, v in DEFAULT_PREHEAT_FILAMENT_TARGETS.items()}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("preheat_filament_targets unparseable, using defaults: %s", exc)
        return {k.upper(): v for k, v in DEFAULT_PREHEAT_FILAMENT_TARGETS.items()}
    out: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            out[str(key).upper()] = int(value)
        except (TypeError, ValueError):
            continue
    if "DEFAULT" not in out:
        out["DEFAULT"] = DEFAULT_PREHEAT_FILAMENT_TARGETS["default"]
    return out


def _normalize_filament_type(tray_type: str) -> str:
    """Reduce a printer-reported tray_type to a preset-lookup key. Mirrors the
    drying-preset normalisation (split-at-space, upper-case): "PLA Basic" → "PLA",
    "PA-CF" stays "PA-CF" (no space to split on)."""
    return tray_type.split()[0].upper() if tray_type else ""


def _derive_chamber_target(printer: Printer, targets: dict[str, int]) -> int:
    """Max chamber target across loaded AMS trays. Returns 0 when no AMS data is
    available (external-spool prints) or every loaded slot maps to 0 — the chamber
    phase then short-circuits. Reads the same ``raw_data['ams'][].tray[]`` shape the
    dispatcher's AMS mapping uses; empty / RFID-less slots contribute nothing."""
    state = printer_manager.get_status(printer.id)
    if state is None:
        return 0
    ams_list = (state.raw_data or {}).get("ams") if state.raw_data else None
    # Older Bambu firmware nests AMS as {"ams": {"ams": [...]}} — accept both.
    if isinstance(ams_list, dict):
        ams_list = ams_list.get("ams") or []
    if not isinstance(ams_list, list):
        return 0
    best = 0
    for ams in ams_list:
        trays = (ams.get("tray") or []) if isinstance(ams, dict) else []
        for tray in trays:
            if not isinstance(tray, dict):
                continue
            normalised = _normalize_filament_type(tray.get("tray_type") or "")
            if not normalised:
                continue
            target = targets.get(normalised, targets.get("DEFAULT", 0))
            if target > best:
                best = target
    return best


async def planned_stage_seconds(db: AsyncSession, *, override: str = "inherit") -> int:
    """Worst case wall-clock this stage puts between the archive row and ``start_print``.

    Lives here rather than at the caller because it is the same two settings the
    stage itself reads, and a third phase added later must not have to be
    remembered in two files.

    ⚠️ **Both halves, not just the soak.** ``preheat_max_wait_seconds`` (900)
    dwarfs ``preheat_soak_seconds`` (300), and it is not an error path: on a
    printer with a chamber sensor but no heater (X1C, P2S) the chamber is warmed
    radiantly by the bed, which the docstring above measures at 15–30 minutes, so
    the wait routinely runs to its limit. Sizing anything off the soak alone
    would be off by the larger term.

    Returns 0 when the stage will not run — the caller can add this
    unconditionally.
    """
    override = (override or "inherit").lower()
    if override == "off":
        return 0
    if override != "on" and not await _get_bool_setting(db, "preheat_enabled", default=False):
        return 0
    return await _get_int_setting(db, "preheat_max_wait_seconds", default=900) + await _get_int_setting(
        db, "preheat_soak_seconds", default=300
    )


async def preheat_and_soak(
    db: AsyncSession,
    printer: Printer,
    archive: PrintArchive | None,
    *,
    options: dict[str, Any] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> None:
    """Run the preheat + heat-soak stage on the idle printer (see module docstring).

    ``options`` carries the per-item ``preheat_override`` /
    ``preheat_chamber_target_override`` (populated by ``print_scheduler._start_print``).
    ``cancel_check`` — if given — is called each poll iteration and raises to abort a
    long wait/soak when the dispatch is cancelled; that exception is allowed to
    propagate (everything else is swallowed)."""
    opts = options or {}
    override = str(opts.get("preheat_override") or "inherit").lower()
    if override == "off":
        return
    if override == "inherit":
        if not await _get_bool_setting(db, "preheat_enabled", default=False):
            return
    # override == "on" forces the stage on regardless of the global setting.

    max_wait = await _get_int_setting(db, "preheat_max_wait_seconds", default=900)
    soak_seconds = await _get_int_setting(db, "preheat_soak_seconds", default=300)

    # Chamber target: explicit per-item override wins; explicit 0 means "no chamber
    # even if the filament wants it"; otherwise derive from loaded AMS filament types.
    explicit_target = opts.get("preheat_chamber_target_override")
    if explicit_target is not None and explicit_target > 0:
        chamber_target = int(explicit_target)
        chamber_source = "item-override"
    elif explicit_target == 0:
        chamber_target = 0
        chamber_source = "item-override-zero"
    else:
        targets = await _get_preheat_filament_targets(db)
        chamber_target = _derive_chamber_target(printer, targets)
        chamber_source = "filament-map"

    bed_target = int(archive.bed_temperature) if archive and archive.bed_temperature else 0
    if bed_target <= 0:
        logger.info("Preheat skipped for printer %s — archive has no bed_temperature metadata", printer.id)
        return

    client = printer_manager.get_client(printer.id)
    if client is None:
        logger.warning("Preheat skipped for printer %s — client unavailable", printer.id)
        return

    model = printer.model or ""
    has_heater = supports_chamber_heater(model)
    has_sensor = supports_chamber_temp(model)
    do_chamber = chamber_target > 0 and (has_heater or has_sensor)

    logger.info(
        "Preheat starting on printer %s — bed=%d°C chamber_target=%d°C (source=%s override=%s "
        "model=%s heater=%s sensor=%s) max_wait=%ds soak=%ds",
        printer.id,
        bed_target,
        chamber_target if do_chamber else 0,
        chamber_source,
        override,
        model,
        has_heater,
        has_sensor,
        max_wait,
        soak_seconds,
    )

    try:
        client.set_bed_temperature(bed_target)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Preheat bed M140 failed on printer %s: %s", printer.id, exc)
        return

    # Airduct flap: flip to heating before the setpoint when the preheat wants chamber heat,
    # back to cooling otherwise (a PLA-only print on an H2D that previously ran ABS
    # would else stay sealed in heating mode). Idempotent via the current-state read.
    if supports_airduct(model):
        desired_airduct = "heating" if chamber_target > 0 else "cooling"
        desired_id = 1 if desired_airduct == "heating" else 0
        current_state = printer_manager.get_status(printer.id)
        current_airduct = getattr(current_state, "airduct_mode", None) if current_state else None
        if current_airduct != desired_id:
            try:
                client.set_airduct_mode(desired_airduct)
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("Preheat airduct %s mode failed on printer %s: %s", desired_airduct, printer.id, exc)

    if do_chamber and has_heater:
        try:
            client.set_chamber_temperature(chamber_target)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("Preheat chamber set_ctt failed on printer %s: %s", printer.id, exc)

    # Release the pooled DB connection before the (potentially many-minute)
    # heat-soak wait below (#2572). Every setting this stage needs was read
    # above; the wait/soak loop only polls printer_manager state and sleeps — it
    # never touches the DB. Without this the caller's transaction sat "idle in
    # transaction" for the whole soak, pinning one pooled connection per
    # preheating printer. expire_on_commit=False keeps printer/archive readable;
    # there are no pending writes to lose here.
    await db.commit()

    # Wait for convergence. Bed warm-up is fast; chamber via set_ctt a few minutes;
    # chamber via bed radiation can take 20+. "Converged" = bed reached target AND
    # (no chamber phase, no sensor, or sensor reached target).
    deadline = asyncio.get_event_loop().time() + max_wait
    while True:
        if cancel_check is not None:
            cancel_check()
        state = printer_manager.get_status(printer.id)
        if state is None:
            logger.warning("Preheat lost state during wait on printer %s", printer.id)
            break

        temps = state.temperatures or {}
        bed_now = float(temps.get("bed", 0) or 0)
        chamber_now = float(temps.get("chamber", 0) or 0)
        bed_ok = bed_now >= bed_target - _BED_TOLERANCE

        if not do_chamber:
            chamber_ok = True  # phase disabled or model has neither sensor nor heater
        elif not has_sensor:
            chamber_ok = True  # P1S etc — can't read, rely on the soak timer
        else:
            chamber_ok = chamber_now >= chamber_target - _CHAMBER_TOLERANCE

        if bed_ok and chamber_ok:
            logger.info(
                "Preheat target reached on printer %s (bed=%.1f chamber=%.1f) — entering soak",
                printer.id,
                bed_now,
                chamber_now,
            )
            break

        if asyncio.get_event_loop().time() >= deadline:
            logger.info(
                "Preheat max_wait reached on printer %s (bed=%.1f/%d chamber=%.1f/%d) — falling through to soak",
                printer.id,
                bed_now,
                bed_target,
                chamber_now,
                chamber_target if do_chamber else 0,
            )
            break

        await asyncio.sleep(_POLL_INTERVAL)

    # Soak — chunked so a cancel aborts within one poll interval instead of after
    # the full hold.
    if soak_seconds > 0:
        logger.info("Preheat soak on printer %s — holding for %ds", printer.id, soak_seconds)
        remaining = float(soak_seconds)
        while remaining > 0:
            if cancel_check is not None:
                cancel_check()
            chunk = min(_POLL_INTERVAL, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    logger.info("Preheat complete on printer %s — proceeding to start", printer.id)
