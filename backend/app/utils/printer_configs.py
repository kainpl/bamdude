"""Loader for the mirrored BambuStudio per-model printer config JSONs.

BamDude ships **byte-for-byte** copies of BambuStudio's
``resources/printers/<code>.json`` under ``backend/app/data/printers/`` (see the
README there). Each file is keyed by firmware version; the ``"00.00.00.00"``
block is the base / default config, and its ``print`` sub-object carries the
per-model DEVICE capability flags (``support_*_calibration``,
``support_bed_leveling``, chamber, camera, …).

Reading capabilities from these files keeps per-model knowledge in **data**, not
hardcoded Python — and, because the copies are verbatim, re-syncing is a folder
re-copy + ``git diff`` against a fresh BambuStudio checkout (byte-identical
unless BS actually changed something). See CLAUDE.md → "Bambu Studio printer
configs" and the folder README.

Only device-capability reads live here; the tri-state print-calibration matrix
in ``printer_models.py`` stays as-is for now (it predates this loader).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from backend.app.utils.printer_models import normalize_printer_model

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "data" / "printers"

# The seven device-calibration keys, in MQTT ``option`` bit order (0..6).
# Consumed by the calibration command builder, the resolver, and the frontend.
DEVICE_CALIBRATIONS = (
    "lidar",  # bit 0 (xcam_cali) — needs support_lidar_calibration AND support_ai_monitoring
    "bed_leveling",  # bit 1 — support_bed_leveling != 0
    "vibration",  # bit 2 — always available (BS never gates)
    "motor_noise",  # bit 3 — support_motor_noise_cali
    "nozzle_offset",  # bit 4 (nozzle_cali) — support_nozzle_offset_calibration
    "high_temp_heatbed",  # bit 5 (bed_cali) — support_high_tempbed_calibration
    "clump_pos",  # bit 6 (clumppos_cali) — support_clump_position_calibration
)


def _norm(s: str | None) -> str:
    return (s or "").strip().upper().replace(" ", "").replace("-", "")


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _base_block(data: dict) -> dict | None:
    """The base config block — firmware-version-keyed; ``"00.00.00.00"`` is the
    default. Falls back to the first block for any oddly-keyed file."""
    base = data.get("00.00.00.00") or next(iter(data.values()), None)
    return base if isinstance(base, dict) else None


@lru_cache(maxsize=1)
def _model_index() -> dict[str, str]:
    """Normalized model key -> config file stem, built by scanning the mirrored
    JSONs themselves.

    Keys per file: the file stem (= internal code, e.g. ``N6``), the config's
    ``model_id``, and the SHORT display name (``Bambu Lab X2D`` -> ``X2D`` via
    ``normalize_printer_model``). Using the JSONs' own ``display_name`` as the
    authoritative code<->model mapping makes this immune to the stale
    ``PRINTER_MODEL_ID_MAP`` (whose C11/C12 rows are wrong — C11 is P1P, C12 is
    P1S per the BS configs, not X1C/X1).
    """
    index: dict[str, str] = {}
    for path in sorted(_CONFIG_DIR.glob("*.json")):
        stem = path.stem
        if stem == "filaments_blacklist":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("printer config %s unreadable: %s", path.name, exc)
            continue
        base = _base_block(data) if isinstance(data, dict) else None
        if base is None:
            continue
        index[_norm(stem)] = stem  # file stem / internal code (N6)
        model_id = base.get("model_id")
        if isinstance(model_id, str):
            index.setdefault(_norm(model_id), stem)
        display_name = base.get("display_name")
        if isinstance(display_name, str):
            short = normalize_printer_model(display_name) or display_name  # strips "Bambu Lab "
            index.setdefault(_norm(short), stem)
    return index


@lru_cache(maxsize=128)
def load_printer_config(model: str | None) -> dict | None:
    """Return the base (``"00.00.00.00"``) config block for a model, or ``None``
    if no mirrored JSON matches. Accepts a display name (``X2D`` / ``Bambu Lab
    X2D``) or internal code (``N6``). Cached — the files are static app assets.
    """
    if not model:
        return None
    stem = _model_index().get(_norm(model))
    if not stem:
        # Also try the "Bambu Lab X" long form directly.
        short = normalize_printer_model(model)
        stem = _model_index().get(_norm(short)) if short else None
    if not stem:
        return None
    try:
        data = json.loads((_CONFIG_DIR / f"{stem}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("printer config %s.json unreadable: %s", stem, exc)
        return None
    return _base_block(data) if isinstance(data, dict) else None


def get_device_support_flags(model: str | None) -> dict:
    """The ``support_*`` device-capability flags for a model (from the config's
    ``print`` block). Empty dict when no config matches (unknown model)."""
    cfg = load_printer_config(model)
    if not cfg:
        return {}
    print_block = cfg.get("print")
    return print_block if isinstance(print_block, dict) else {}


def device_calibration_availability(model: str | None) -> dict[str, bool]:
    """Base per-model availability of the seven device calibrations, read from
    the mirrored BambuStudio config.

    ``bed_leveling`` and ``vibration`` are always available (BS never gates them;
    an unknown model still shows those two). The live ``support_*`` MQTT flags
    override individual entries via :func:`resolve_device_calibrations`.
    """
    f = get_device_support_flags(model)
    return {
        # Lidar needs BOTH the lidar-cali flag and AI-monitoring (BS gate).
        "lidar": bool(f.get("support_lidar_calibration")) and bool(f.get("support_ai_monitoring")),
        # Missing field defaults to 1 (on/off) so an unknown model still offers it.
        "bed_leveling": _as_int(f.get("support_bed_leveling", 1)) != 0,
        "vibration": True,
        "motor_noise": bool(f.get("support_motor_noise_cali")),
        "nozzle_offset": bool(f.get("support_nozzle_offset_calibration")),
        "high_temp_heatbed": bool(f.get("support_high_tempbed_calibration")),
        "clump_pos": bool(f.get("support_clump_position_calibration")),
    }


def resolve_device_calibrations(model: str | None, live_support: dict | None = None) -> dict[str, bool]:
    """Hybrid availability: the mirrored-config per-model base, overridden by the
    printer's live ``support_*`` MQTT status flags wherever the printer reported
    them. This mirrors BambuStudio, which resolves every gate from live status
    (with the shipped JSON as the default). ``live_support`` keys are the raw
    MQTT field names (as stashed in ``PrinterState.device_cali_support``).
    """
    avail = device_calibration_availability(model)
    if not live_support:
        return avail
    if "support_bed_leveling" in live_support:
        avail["bed_leveling"] = _as_int(live_support["support_bed_leveling"]) != 0
    if "support_motor_noise_cali" in live_support:
        avail["motor_noise"] = bool(live_support["support_motor_noise_cali"])
    if "support_nozzle_offset_calibration" in live_support:
        avail["nozzle_offset"] = bool(live_support["support_nozzle_offset_calibration"])
    if "support_high_tempbed_calibration" in live_support:
        avail["high_temp_heatbed"] = bool(live_support["support_high_tempbed_calibration"])
    if "support_clump_position_calibration" in live_support:
        avail["clump_pos"] = bool(live_support["support_clump_position_calibration"])
    # Lidar = lidar_cali AND ai_monitoring; fall back to the base JSON value for
    # whichever half the printer didn't report.
    if "support_lidar_calibration" in live_support or "support_ai_monitoring" in live_support:
        base_flags = get_device_support_flags(model)
        lidar_c = live_support.get("support_lidar_calibration", base_flags.get("support_lidar_calibration"))
        ai_mon = live_support.get("support_ai_monitoring", base_flags.get("support_ai_monitoring"))
        avail["lidar"] = bool(lidar_c) and bool(ai_mon)
    return avail
