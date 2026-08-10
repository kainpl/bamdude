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

# A ``DEVICE_CALIBRATIONS`` tuple used to sit here, listing the same seven keys
# in MQTT bit order above a comment claiming three consumers. It had none — the
# command builder names its bits inline (grep ``clumppos_cali`` in
# ``bambu_mqtt.py``) and the resolver below writes its own dict. A third copy of
# a bit order that nothing reads is worse than no copy: editing it, on the
# strength of that comment, changes nothing on the wire.


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


def _deep_merge(src: dict, dst: dict) -> None:
    """BS ``json_diff::merge_objects`` — recursive for objects, overwrite for
    everything else. Mutates ``dst``."""
    for key, value in src.items():
        if key not in dst:
            dst[key] = value
        elif isinstance(value, dict) and isinstance(dst[key], dict):
            _deep_merge(value, dst[key])
        else:
            dst[key] = value


def _merged_block(data: dict, firmware_version: str | None) -> dict | None:
    """Every block up to and including ``firmware_version``, merged in order.

    BS ``json_diff::load_compatible_settings``::

        settings_base.clear();
        for (auto iter = versions.begin(); iter != versions.end(); ++iter) {
            if (iter.key() > printer_version) break;
            merge_objects(*iter, settings_base);
        }

    …and that merged tree — not the base block — is what ``DevConfig::ParseConfig``
    and friends read. Reading only ``"00.00.00.00"`` understates a printer by
    every flag a later firmware turned on: **thirteen** of them on an X1C
    (``support_ai_monitoring``, ``support_filament_backup``,
    ``support_build_plate_marker_detect``, ``support_ams_humidity``,
    ``support_send_to_sd``, …), seven on a P1P, and ``ipcam`` on the A-series.

    ⚠️ **String comparison, exactly as BS does it.** Bambu's keys are
    zero-padded fixed-width (``01.05.06.01``), so lexicographic ordering is
    numeric ordering here. A key that ever stopped being padded would sort
    wrongly — the same trap BS carries, and worth knowing rather than silently
    "improving" into a version parser that disagrees with the reference.

    ``firmware_version`` of ``None`` means "not reported yet", and returns the
    base block alone. That is deliberately the old behaviour: a capability we
    cannot justify from the printer's own version should be understated for the
    seconds before the first push rather than guessed at.
    """
    base = _base_block(data)
    if base is None or not firmware_version:
        return base

    merged: dict = {}
    for key in sorted(data):
        if key > firmware_version:
            break
        block = data[key]
        if isinstance(block, dict):
            _deep_merge(block, merged)
    # A version older than every block still gets the base — BS's loop would
    # merge nothing at all, but a config with no applicable block is a printer
    # we still have to answer questions about.
    return merged or base


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
def load_printer_config(model: str | None, firmware_version: str | None = None) -> dict | None:
    """Return the config block for a model, or ``None`` if no mirrored JSON
    matches. Accepts a display name (``X2D`` / ``Bambu Lab X2D``) or internal
    code (``N6``).

    With ``firmware_version`` (the printer's OTA version — BS's
    ``parse_version()``, which is our ``PrinterState.firmware_version``) every
    block up to that version is merged, as BS does. Without it, the base block
    alone — which is what every caller got before, so a caller with no live
    state keeps its current answer instead of a guessed one.
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
    return _merged_block(data, firmware_version) if isinstance(data, dict) else None


def printer_arch(model: str | None) -> str:
    """The model's kinematic architecture — ``"core_xy"`` or ``"i3"``.

    Byte-for-byte the question BS answers in
    ``DevPrinterConfigUtil::get_printer_arch``, **including its default**::

        if   arch_str == "i3"      -> ARCH_I3
        elif arch_str == "core_xy" -> ARCH_CORE_XY
        else                       -> ARCH_CORE_XY

    So an unknown model, an unreadable config and an unrecognised value all
    answer ``core_xy``. That default is the safe one for the only consumer that
    matters: ``core_xy`` means "do not invert the Z sign", i.e. leave the
    caller's g-code alone rather than flip it on a machine we cannot identify.
    """
    cfg = load_printer_config(model) or {}
    value = cfg.get("printer_arch")
    if not isinstance(value, str):
        value = (cfg.get("print") or {}).get("printer_arch") if isinstance(cfg.get("print"), dict) else None
    return "i3" if value == "i3" else "core_xy"


def printer_series(model: str | None) -> str:
    """The model's ``printer_series`` string — ``series_x1`` / ``series_p1p`` /
    ``series_o`` / … — as BS reads it in
    ``DevPrinterConfigUtil::get_printer_series_str``.

    Used for BS's two hardcoded capability clamps (see
    ``_apply_series_calibration_clamps`` in ``bambu_mqtt``). ⚠️ Note the X2D
    (``N6``) reports ``series_x1``, **not** ``series_o`` — the O-series clamp
    covers the H2 family only, and a guess by model name would get that wrong.
    """
    cfg = load_printer_config(model) or {}
    value = cfg.get("printer_series")
    return value if isinstance(value, str) else ""


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


def get_device_support_flags(model: str | None, firmware_version: str | None = None) -> dict:
    """The ``support_*`` device-capability flags for a model (from the config's
    ``print`` block). Empty dict when no config matches (unknown model).

    Pass ``firmware_version`` wherever a live printer is in hand: without it the
    answer is the 2023 base block, which understates every model whose later
    firmware turned flags on.
    """
    cfg = load_printer_config(model, firmware_version)
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


# BambuStudio's own fan labels, mapped to i18n keys under ``printers.fans.*``.
# The config strings are BS's English; the vocabulary is small and stable, so
# the ones we recognise are translated and anything new falls through as BS
# wrote it — a label from a model we have not seen reads as words rather than as
# a missing key, and re-syncing the configs cannot break the screen.
_BS_FAN_LABEL_KEYS = {
    "left(aux)": "leftAux",
    "right(aux)": "rightAux",
    "left(filter)": "leftFilter",
    "right(filter)": "rightFilter",
    "left(heating)": "leftHeating",
    "right(heating)": "rightHeating",
    "chamber": "chamber",
}


def airduct_fan_label(model: str | None, mode: int, sub_mode: int, part_id: int) -> tuple[str | None, str | None]:
    """What this printer calls a given airduct fan, right now.

    Returns ``(i18n_key, raw_bs_label)``. Either may be ``None``: an unmapped
    label yields ``(None, "Something New")`` so the caller can still show it, and
    a model whose config names nothing yields ``(None, None)``.

    ⚠️ **The same part id is a different fan on different models — and in
    different modes.** From the configs we mirror:

    ======  =========  ==============  ==================================
    model   mode       part 2          part 10
    ======  =========  ==============  ==================================
    P2S     0 cooling  Right(Aux)      **Left(Aux)**
    P2S     1 heating  Right(Filter)   **Left(Aux)**
    X2D     0 cooling  **Left(Aux)**   Right(Aux) / Right(Filter) by subMode
    X2D     1 heating  **Left(Heating)**  Right(Filter)
    ======  =========  ==============  ==================================

    So on the X2D part 10 is the **right**-hand fan — mirrored from the P2S.
    Upstream hardcodes "part 10 = Left Auxiliary" for both, which is correct on
    the P2S they measured against and wrong on the X2D named in the same commit.
    Reading the config is not tidiness here; it is the difference between a
    correct label and a confidently wrong one.

    Mode ``-1`` in the config means "any mode" and is the fallback — that is how
    X1/P1S name part 3 "Chamber".
    """
    cfg = load_printer_config(model)
    fans = cfg.get("fan") if cfg else None
    if not isinstance(fans, dict):
        return None, None

    for mode_key in (str(mode), "-1"):
        by_part = fans.get(mode_key)
        if not isinstance(by_part, dict):
            continue
        value = by_part.get(str(part_id))
        # A dict here means the label depends on the sub-mode as well (X2D
        # part 10 in cooling: 0 → Right(Aux), 1 → Right(Filter)).
        if isinstance(value, dict):
            value = value.get(str(sub_mode)) or value.get("-1")
        if isinstance(value, str) and value:
            return _BS_FAN_LABEL_KEYS.get(value.strip().lower()), value
    return None, None


def device_calibration_availability(model: str | None, firmware_version: str | None = None) -> dict[str, bool]:
    """Base per-model availability of the seven device calibrations, read from
    the mirrored BambuStudio config.

    ``bed_leveling`` and ``vibration`` are always available (BS never gates them;
    an unknown model still shows those two). The live ``support_*`` MQTT flags
    override individual entries via :func:`resolve_device_calibrations`.
    """
    f = get_device_support_flags(model, firmware_version)
    return {
        # Lidar needs BOTH the lidar-cali flag and AI-monitoring (BS gate).
        "lidar": bool(f.get("support_lidar_calibration")) and bool(f.get("support_ai_monitoring")),
        # Missing field defaults to 1 (on/off) so an unknown model still offers it.
        "bed_leveling": _as_int(f.get("support_bed_leveling", 1)) != 0,
        "vibration": True,
        # ⚠️ Offline default only. The live gate is BS parity and wins:
        # ``home_flag`` bit 21 (P1/X1) OR the ``fun`` bitfield bit 10 (H2/X2) —
        # grep ``is_support_motor_noise_cali`` in ``bambu_mqtt.py``.
        "motor_noise": bool(f.get("support_motor_noise_cali")),
        "nozzle_offset": bool(f.get("support_nozzle_offset_calibration")),
        "high_temp_heatbed": bool(f.get("support_high_tempbed_calibration")),
        "clump_pos": bool(f.get("support_clump_position_calibration")),
    }


def resolve_device_calibrations(
    model: str | None, live_support: dict | None = None, firmware_version: str | None = None
) -> dict[str, bool]:
    """Hybrid availability: the mirrored-config per-model base, overridden by the
    printer's live ``support_*`` MQTT status flags wherever the printer reported
    them. This mirrors BambuStudio, which resolves every gate from live status
    (with the shipped JSON as the default). ``live_support`` keys are the raw
    MQTT field names (as stashed in ``PrinterState.device_cali_support``).
    """
    avail = device_calibration_availability(model, firmware_version)
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
