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
    "motor_noise",  # bit 3 — LIVE runtime gate (BS parity): home_flag bit 21
    # (P1/X1 series) OR fun bitfield bit 10 (H2/X2 series); the JSON
    # support_motor_noise_cali is only the offline default
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
    authoritative code<->model mapping keeps this independent of
    ``PRINTER_MODEL_ID_MAP`` (which was corrected to agree with these configs:
    C11=P1P, C12=P1S, BL-P001=X1C, BL-P002=X1).
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


def supports_safety_options(model: str | None) -> bool:
    """Whether the model exposes BS's Safety Options dialog — the top-level
    ``support_safety_options`` flag in the mirrored per-model config. Currently
    true only for X2D (N6) and P2S (N7). Gates our Safety tab (BS ANDs it with
    the live ``fun`` bit-12 door-check support)."""
    cfg = load_printer_config(model)
    return bool(cfg and cfg.get("support_safety_options"))


def air_print_detection_position(model: str | None) -> str | None:
    """BS ``air_print_detection_position`` config string — routes the non-visual
    air-print toggle. ``"print_option"`` → show it in Print Options (e.g. A2L);
    ``"ams_setting"`` → it lives in the AMS dialog instead (A1/A1 mini). None when
    the model doesn't declare it."""
    cfg = load_printer_config(model)
    val = cfg.get("air_print_detection_position") if cfg else None
    return val if isinstance(val, str) else None


def get_device_support_flags(model: str | None) -> dict:
    """The ``support_*`` device-capability flags for a model (from the config's
    ``print`` block). Empty dict when no config matches (unknown model)."""
    cfg = load_printer_config(model)
    if not cfg:
        return {}
    print_block = cfg.get("print")
    return print_block if isinstance(print_block, dict) else {}


def has_remote_storage_toggle(model: str | None, live_support: dict | None = None) -> bool:
    """Whether the printer exposes a reachable control for the "Store sent files
    on external storage" option.

    BambuStudio renders that toggle only for models declaring
    ``support_save_remote_print_file_to_storage`` in their config (BS
    ``SupportSaveRemote``, default false), so on a model that doesn't declare it
    the option cannot be switched on from the slicer at all — and P1P/P1S have no
    on-printer screen to reach it from either. ``store_to_sdcard`` (home_flag bit
    11) is then stuck at False with nothing the user can do about it, which the
    ``external_storage`` diagnostic must report as ``skip`` rather than a
    permanently-red, unresolvable ``fail`` (upstream #2524).

    Hybrid, like :func:`resolve_device_calibrations`: the printer's live
    ``PrinterState.print_option_support`` wins wherever it actually reported the
    capability, with the mirrored BS config as the base. Unknown models default
    to True so the check keeps working on anything not mirrored yet.

    Divergence from upstream, which hardcodes a ``{P1S, P1P}`` model set: reading
    BS's own configs covers those two and additionally **A2L** and **X1E** (which
    likewise never declare the flag), and it self-heals — a firmware that starts
    reporting the capability reactivates the check with no code change.
    """
    if live_support and "save_remote_to_storage" in live_support:
        return bool(live_support["save_remote_to_storage"])
    if load_printer_config(model) is None:
        return True
    return bool(get_device_support_flags(model).get("support_save_remote_print_file_to_storage"))


def supports_nozzle_flow_type(model: str | None, live_flow_type_supported: bool | None = None) -> bool:
    """Whether a K-profile's Standard / High Flow choice means anything here.

    A K-profile is filed under a ``nozzle_id`` of the form ``HS00-0.4`` (Standard)
    or ``HH00-0.4`` (High Flow), so the flow type is part of the profile's
    identity on a printer where both exist — and a field that cannot be saved on
    one where the firmware discards it.

    BambuStudio's own gate for this exact dialog is
    ``CaliHistoryDialog.cpp::support_nozzle_volume``, and it is two questions:

    1. ``DevPrinterConfigUtil::support_disable_cali_flow_type(printer_type)`` —
       the top-level flag in the mirrored per-model config. When set, the answer
       is no, full stop. In the configs we ship that is **X1C, X1, P1P, P1S and
       X1E**.
    2. otherwise ``MachineObject::is_nozzle_flow_type_supported()``, i.e.
       ``is_enable_np | has_extra_flow_type`` — live, from the push. Pass it as
       ``live_flow_type_supported``; the printer has to have spoken for this to
       be answerable at all.

    ⚠️ **Deliberate divergence from upstream**, which hardcodes
    ``{A1, A1 mini, A2L}`` as the models *without* the choice — very nearly the
    opposite of this list. They are not misreading their data; they derived it
    from a different one (``len(nozzle_volume) // len(nozzle_diameter)`` over the
    machine presets) than the calibration dialog's own gate. Since the screen we
    mirror is that dialog, its gate is the one to mirror, and reading the config
    we already ship keeps this in data rather than in a Python set that has to be
    revisited for every new model.

    Unknown model, or a printer that has not spoken yet: **False** — the honest
    answer to "does this machine offer the choice" is not yet known, and offering
    it means letting the user set a value the firmware may silently discard.
    Note this is the opposite default from :func:`has_remote_storage_toggle`,
    where an unknown model defaults permissive: there the cost of guessing wrong
    is a diagnostic that stays red, here it is a saved profile that reads back
    different from what was chosen.
    """
    cfg = load_printer_config(model)
    if cfg and cfg.get("support_disable_cali_flow_type"):
        return False
    return bool(live_flow_type_supported)


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
