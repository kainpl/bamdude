"""Derive AMS-settings row visibility from printer model + reported state.

⚠️ **Four of these are no longer answered from a model list, and that matters.**
``_HAS_RFID_AMS`` was one answer serving four different questions, and BS asks
each of them differently (``AMSSetting.cpp``):

  - ``insertion_update`` — did the printer report the setting at all, is the AMS
    running the Lite personality, is the model's ``use_ams_type`` "f1".
  - ``power_on_update`` — BS never gates it; the dialog's existence is the gate.
  - ``remain_capacity`` — ``support_update_remain`` AND NOT ``fun2`` bit 6, with
    an override that forces it on for a non-Lite AMS personality.
  - ``auto_switch_filament`` — ``support_filament_backup``.
  - ``firmware_switch`` — the device's own list of firmwares.

The set survives only as the fallback for the window before the printer has
spoken, and for a model whose config omits a key. A machine can be handed a
different AMS; a model list cannot know that, which is why none of the above is
decided by the badge on the front of the printer.

``air_print_detect`` and ``reorder`` are still model-answered — BS reads
``air_print_detection_position`` / ``support_ams_settings_reorder`` from the
config, and we have not measured those against ours yet.

Tests in ``test_ams_capabilities.py`` and ``test_ams_remain_capacity_parity.py``
lock the behaviour.
"""

from typing import TypedDict

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.utils.printer_configs import get_device_support_flags, load_printer_config


class AmsSupports(TypedDict):
    insertion_update: bool
    power_on_update: bool
    remain_capacity: bool
    auto_switch_filament: bool
    air_print_detect: bool
    firmware_switch: bool
    reorder: bool


def _norm(model: str | None) -> str:
    if not model:
        return ""
    return model.strip().upper().replace(" ", "").replace("-", "")


_HAS_RFID_AMS = frozenset(
    {
        "X1",
        "X1C",
        "X1E",
        "P1P",
        "P1S",
        "P2S",
        "X2D",
        "A1",
        "H2D",
        "H2DPRO",
        "H2C",
        "H2S",
    }
)

_A1_MINI = frozenset({"A1MINI"})
_A1_FULL = frozenset({"A1"})
_H2_FAMILY = frozenset({"H2D", "H2DPRO", "H2C", "H2S"})


# BS ``DevAmsSystemFirmwareSwitch``: the A1's AMS carries two personalities.
AMS_FIRMWARE_IDX_LITE = 0
AMS_FIRMWARE_IDX_AMS = 1


def _flag(state: PrinterState, printer_model: str | None, live_key: str, cfg_key: str, fallback: bool) -> bool:
    """One support answer, from the printer first and the mirrored config second.

    BS reads these as named bools off the push payload. The config carries the
    same keys and is the only source before the printer has spoken, but the live
    value wins — a config describes the model, and an AMS can be swapped.

    ⚠️ The config is consulted ONLY once the firmware version is known. Its
    blocks are layered, and without a version the merge can return nothing but
    the 2023 base — which says False for flags a later firmware turned on. For
    ``support_update_remain`` that is exactly the X1 and X1C, so reading it in
    the window before ``get_version`` answers would take a working control away
    from them. ``fallback`` (the old model heuristic) covers that window, and
    covers a model whose config omits the key at all.
    """
    live = (getattr(state, "print_option_support", None) or {}).get(live_key)
    if isinstance(live, bool):
        return live
    if isinstance(state.firmware_version, str) and state.firmware_version:
        cfg = get_device_support_flags(printer_model, state.firmware_version).get(cfg_key)
        if isinstance(cfg, bool):
            return cfg
    return fallback


def _remain_capacity(state: PrinterState, printer_model: str | None, fallback: bool) -> bool:
    """BS: ``is_support_update_remain && !is_support_update_remain_hide_display``.

    ⚠️ With an override that a model list cannot express. BS forces support ON
    whenever the AMS is *running* the non-Lite personality
    (``DeviceManager.cpp``: ``GetCurrentFirmwareIdxRun() == IDX_AMS_AMS2_AMSHT``
    → ``is_support_update_remain = true``). So the A1 — whose config says False,
    because its stock unit is the AMS Lite — gets the toggle back the moment a
    real AMS 2 or AMS HT is attached. The config answers "what ships with this
    model", never "what this machine can do right now".

    The hide-display half is a separate condition, not a restatement: a printer
    can support the feature and still be told not to offer it.
    """
    if state.ams_firmware_idx_run == AMS_FIRMWARE_IDX_AMS:
        supported = True
    else:
        supported = _flag(state, printer_model, "update_remain", "support_update_remain", fallback)
    hidden = bool((getattr(state, "print_option_support", None) or {}).get("update_remain_hide_display"))
    return supported and not hidden


def _insertion_update(state: PrinterState, printer_model: str | None) -> bool:
    """BS ``AMSSetting::update_insert_material_read_mode`` — three refusals.

    1. The printer never reported the setting. BS holds it as
       ``std::optional<bool>`` and hides on an empty one; ``None`` here says the
       same thing, which is why that field is nullable.
    2. The AMS is *selected* to run the Lite personality — no RFID, so nothing
       to read on insertion. Checked only when the machine offers a firmware
       switch at all (BS's ``SupportSwitchFirmware()`` = a non-empty list).
    3. Otherwise, a model whose config says ``use_ams_type: "f1"`` — the A1
       family's own AMS type, which has no insertion read either.

    ⚠️ **BS has a fourth condition we cannot evaluate and deliberately omit.**
    It also hides the checkbox when the ``ams_f1/0`` module's firmware is at
    least ``00.00.07.89``, where the behaviour became unconditional and the
    toggle is redundant. We never learn that version: ``ams_f1`` is not in
    ``_AMS_MODULE_PREFIXES``, so the version cache never holds it. Omitting the
    branch offers a toggle newer firmware does not need — a cosmetic over-offer,
    not a wrong action, which is the safe direction to be wrong in. Add the
    branch together with the prefix, not before.
    """
    if state.ams_insertion_update is None:
        return False
    if state.ams_firmwares:
        if state.ams_firmware_idx_sel == AMS_FIRMWARE_IDX_LITE:
            return False
    elif state.firmware_version:
        cfg = load_printer_config(printer_model, state.firmware_version) or {}
        if (cfg.get("use_ams_type") or "") == "f1":
            return False
    return True


def compute_ams_supports(state: PrinterState, printer_model: str | None) -> AmsSupports:
    """Return per-flag visibility for the AMS Settings dialog.

    Most flags are still answered from the model. ``firmware_switch`` is not —
    see below.
    """
    m = _norm(printer_model)
    has_rfid = m in _HAS_RFID_AMS
    is_a1_mini = m in _A1_MINI
    is_a1_full = m in _A1_FULL
    is_h2 = m in _H2_FAMILY

    return AmsSupports(
        insertion_update=_insertion_update(state, printer_model),
        # BS does not gate this one at all (``update_starting_read_mode`` has no
        # condition) — it does not have to, because the dialog only exists on a
        # machine that has an AMS. We are an HTTP surface with no such wrapper,
        # so the server-side equivalent of "there is an AMS" is "the printer
        # reported the setting". Same answer, made reachable from outside a
        # window.
        power_on_update=state.ams_power_on_update is not None or has_rfid,
        remain_capacity=_remain_capacity(state, printer_model, has_rfid),
        auto_switch_filament=_flag(state, printer_model, "filament_backup", "support_filament_backup", has_rfid),
        air_print_detect=(is_a1_mini or is_a1_full),
        # **Asked of the device, not of the model.** BS's whole support test is
        # ``SupportSwitchFirmware() = !m_firmwares.empty()`` — the printer either
        # offers a list of firmwares to switch between, or it does not. The
        # previous ``m in _A1_FULL`` was a guess about which machines have the
        # feature, and it decided the question for a control that reflashes
        # hardware. A model list cannot know that an AMS was swapped, that the
        # firmware predates the feature, or that Bambu shipped it elsewhere.
        firmware_switch=bool(state.ams_firmwares),
        reorder=is_h2,
    )
