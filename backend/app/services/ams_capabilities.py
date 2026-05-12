"""Derive AMS-settings row visibility from printer model + reported state.

BS computes this from a per-printer JSON in ``resources/printers/*.json``
(``support_update_remain``, ``support_filament_backup``,
``support_ams_settings_reorder``, ``air_print_detection_position``). We
inline a model->capabilities table sourced from BS:

  - X1 family (X1, X1C, X1E) — full AMS w/ RFID; air-print lives in Print
    Options, NOT AMS Settings.
  - P1 / P2 family — full AMS w/ RFID; air-print in Print Options.
  - A1 — AMS w/ RFID + firmware-switch + air-print here.
  - A1 Mini — AMS Lite, NO RFID; air-print here.
  - H2D family — full AMS, supports AMS reorder.

Extending: when BamDude meets a new printer model, append it here. Tests
in ``test_ams_capabilities.py`` lock the behaviour.
"""

from typing import TypedDict

from backend.app.services.bambu_mqtt import PrinterState


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


def compute_ams_supports(state: PrinterState, printer_model: str | None) -> AmsSupports:
    """Return per-flag visibility for the AMS Settings dialog.

    ``state`` is the live MQTT state (currently unused — kept on the signature
    so a future cfg-bit gate can refine the answer without an API change).
    """
    m = _norm(printer_model)
    has_rfid = m in _HAS_RFID_AMS
    is_a1_mini = m in _A1_MINI
    is_a1_full = m in _A1_FULL
    is_h2 = m in _H2_FAMILY

    return AmsSupports(
        insertion_update=has_rfid,
        power_on_update=has_rfid,
        remain_capacity=has_rfid,
        auto_switch_filament=has_rfid,
        air_print_detect=(is_a1_mini or is_a1_full),
        firmware_switch=is_a1_full,
        reorder=is_h2,
    )
