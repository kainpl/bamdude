"""Filament Track Switch: which inlet each AMS feeds, and whether the switch is set up.

A leaf reader over ``PrinterState`` shared by the status shapers (REST and
WebSocket), the broadcast key and the load route, so none of them re-derives
the rule (upstream 7a42e0a7 / 9500c046, BambuStudio ``DevFilaSwitch``).
"""

from __future__ import annotations

from typing import Any


def inlet_bindings(state: Any) -> dict[str, str]:
    """``{ams_id: "A" | "B"}`` — the switch inlet each AMS is plumbed into.

    Empty unless a switch is installed: the bits are recorded from every AMS
    reporting extruder 0xE (``PrinterState.ams_switch_inlet_seen``), and without
    a switch 0xE is an uninitialised unit whose bits 24-27 carry nothing.
    """
    fila_switch = getattr(state, "fila_switch", None)
    if not getattr(fila_switch, "installed", False):
        return {}
    return dict(getattr(state, "ams_switch_inlet_seen", {}) or {})


def switch_ready(state: Any) -> bool:
    """BambuStudio's ``DevFilaSwitch::IsReady``: installed, and every AMS on an inlet.

    Until the operator binds each AMS on the printer's Manual AMS Setup screen
    the switch cannot route a load, so Studio refuses one outright — and so do
    we. An AMS still reporting a real extruder id has no inlet and reads as not
    ready, exactly Studio's rule (only its 0xE branch ever sets a switcher
    position). No AMS reported yet is ready: there is no slot to load from.
    """
    fila_switch = getattr(state, "fila_switch", None)
    if not getattr(fila_switch, "installed", False):
        return False
    bindings = inlet_bindings(state)
    raw_data = getattr(state, "raw_data", None) or {}
    units = raw_data.get("ams") if isinstance(raw_data, dict) else None
    if not isinstance(units, list):
        return True
    return all(str(unit.get("id")) in bindings for unit in units if isinstance(unit, dict))
