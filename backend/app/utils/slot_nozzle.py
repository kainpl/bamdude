"""Which nozzle an AMS slot feeds — its extruder, diameter and flow type.

A K profile belongs to a nozzle: BambuStudio identifies one by filament +
extruder + nozzle diameter + nozzle flow type (Standard / High Flow / …), and
filters a slot's candidates by the diameter and flow of the nozzle that slot
feeds (``AMSMaterialsSetting.cpp``). So every binder has to ask about THAT
nozzle, not "the printer's".

They used to rebuild it by hand, six times over: ``state.nozzles[0]`` for the
diameter whatever the slot, a ``getattr(state, "nozzle_volume_type",
"standard")`` for the flow — an attribute the printer state never had, so every
machine read "standard" and a High Flow calibration was never found (upstream
e5a18bf5) — and, on the RFID path and for the external holder, extruder 0.

``state.nozzles`` is indexed by extruder id (0 = the right / main hotend, 1 =
the left), the same convention the calibration sync and the frontend's
``resolveSlotNozzleDiameter`` read it by.
"""

from __future__ import annotations

from dataclasses import dataclass


def slot_extruder(state, ams_id: int, tray_id: int) -> int | None:
    """The extruder an AMS slot feeds, or None when the printer does not say.

    None on every printer without an extruder map (single-nozzle) and for an
    AMS the map does not name (it reports 0xE — uninitialised, or behind a
    Filament Track Switch, whose inlet binding we do not parse). The external
    holder names its side directly: tray 0 is Ext-L (extruder 1), tray 1 is
    Ext-R (extruder 0).
    """
    extruder_map = getattr(state, "ams_extruder_map", None)
    if not extruder_map:
        return None
    if ams_id == 255:
        return 1 - tray_id if tray_id in (0, 1) else None
    mapped = extruder_map.get(str(ams_id))
    try:
        return int(mapped) if mapped is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SlotNozzle:
    # None = the printer does not say which hotend this slot feeds. Kept apart
    # from 0 so "unknown" is never written down as "the right-hand nozzle".
    extruder: int | None
    # As the printer reported it ("0.4"); "0.4" when it reported nothing.
    diameter: str
    # BambuStudio's NozzleVolumeType ("standard" / "high_flow" / "tpu_high_flow"
    # / "e3d_high_flow"), or None when the printer reports its nozzle by
    # material name only (X1C / P1 / A1 send "hardened_steel", no flow class).
    flow: str | None

    @property
    def extruder_or_default(self) -> int:
        """0 when unknown — right on a single-nozzle machine, the binders' old
        default on a dual."""
        return 0 if self.extruder is None else self.extruder

    @property
    def diameter_float(self) -> float:
        try:
            return float(self.diameter)
        except (TypeError, ValueError):
            return 0.4

    @property
    def flow_or_standard(self) -> str:
        """The flow to look a calibration up under. A printer that reports no
        flow class files its calibration table without one either, and the sync
        stores those rows as Standard — so Standard is what finds them."""
        return self.flow or "standard"


def slot_nozzle(state, ams_id: int, tray_id: int) -> SlotNozzle:
    """The nozzle an AMS slot (or the external holder, ``ams_id`` 255) feeds.

    A mapped hotend that has not reported a diameter yet falls back to the main
    nozzle, for the diameter and the flow alike — both describe one nozzle.
    """
    extruder = slot_extruder(state, ams_id, tray_id)
    nozzles = list(getattr(state, "nozzles", None) or [])
    nozzle = None
    if extruder is not None and 0 <= extruder < len(nozzles) and getattr(nozzles[extruder], "nozzle_diameter", ""):
        nozzle = nozzles[extruder]
    elif nozzles:
        nozzle = nozzles[0]
    diameter = str(getattr(nozzle, "nozzle_diameter", "") or "") or "0.4"
    flow = str(getattr(nozzle, "nozzle_flow", "") or "") or None
    return SlotNozzle(extruder=extruder, diameter=diameter, flow=flow)
