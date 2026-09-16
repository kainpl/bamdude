"""Generation-scoped filament telemetry and immutable routing snapshots.

This cache deliberately starts empty on reconnect. Shared raw_data is retained
for the UI; a temperature or remain-only update cannot refresh its old spools.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field

from backend.app.services.ams_advertised_overlay import matches_live
from backend.app.utils.printer_models import is_dual_nozzle_model, is_nozzle_rack_model, normalize_model_name


@dataclass(frozen=True)
class FeedSource:
    id: int
    kind: str
    material: str
    color: str | None = None
    variant: str | None = None
    nozzles: tuple[int, ...] = ()
    remain: float = -1
    identity: str = ""


@dataclass(frozen=True)
class PrinterFeedSnapshot:
    printer_id: int
    model: str | None
    connected: bool
    generation: int
    revision: str
    ams_known: bool
    ams_present: bool
    external_known: bool
    sources: tuple[FeedSource, ...]
    nozzle_diameters: dict[int, tuple[float, ...]] = field(default_factory=dict)
    fts: bool = False
    backup_enabled: bool | None = None
    incomplete: bool = False

    @property
    def marker(self) -> tuple[int, str]:
        return self.generation, self.revision


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class FeedTelemetry:
    ams_known: bool = False
    ams_present: bool = False
    external_known: bool = False
    units: dict[int, dict] = field(default_factory=dict)
    external: dict[int, dict] = field(default_factory=dict)
    nozzles: dict[int, set[float]] = field(default_factory=dict)
    fts: bool = False

    def observe(self, payload: dict, model: str | None) -> None:
        report = payload.get("print", {})
        if not isinstance(report, dict):
            return
        # Calibration replies describe a queried diameter, not installed hardware.
        if report.get("command") not in (None, "push_status"):
            report = {}
        ams = report.get("ams", payload.get("ams"))
        if isinstance(ams, (dict, list)):
            envelope = ams if isinstance(ams, dict) else {"ams": ams}
            units = envelope.get("ams")
            bits = envelope.get("ams_exist_bits")
            if bits is not None:
                try:
                    mask = int(bits, 16) if isinstance(bits, str) else int(bits)
                    self.ams_known = True
                    self.ams_present = bool(mask)
                    if mask == 0:
                        self.units.clear()
                except (TypeError, ValueError):
                    pass
            if isinstance(units, list):
                self.ams_known = True
                if bits is None:
                    self.ams_present = bool(units)
                if not units:
                    self.units.clear()
                for unit in units:
                    if not isinstance(unit, dict):
                        continue
                    from backend.app.services.bambu_mqtt import normalize_am_unit_id

                    wire_uid = _integer(unit.get("id"))
                    uid = normalize_am_unit_id(wire_uid) if wire_uid is not None else None
                    if uid is None or not (0 <= uid < 16 or 128 <= uid <= 143):
                        continue
                    current = self.units.setdefault(uid, {"id": uid, "tray": []})
                    current["_wire_id"] = wire_uid
                    trays = {str(t.get("id")): dict(t) for t in current["tray"]}
                    if unit.get("tray") == []:
                        trays.clear()
                    for tray in unit.get("tray", []) or []:
                        if not isinstance(tray, dict):
                            continue
                        tray_id = _integer(tray.get("id"))
                        if tray_id is None or not 0 <= tray_id <= (0 if uid >= 128 else 3):
                            continue
                        tid = str(tray_id)
                        merged = {**trays.get(tid, {}), **tray}
                        if tray.get("tray_type") == "" or (
                            uid < 128 and "state" in tray and tray["state"] != 11 and "tray_type" not in tray
                        ):
                            merged = {"id": tray.get("id"), "tray_type": ""}
                        trays[tid] = merged
                    current.update({k: v for k, v in unit.items() if k != "tray"})
                    if "tray" in unit:
                        current["_trays_seen"] = True
                    current["tray"] = list(trays.values())
            if bits is not None:
                try:
                    mask = int(bits, 16) if isinstance(bits, str) else int(bits)
                    if mask == 0:
                        self.units.clear()
                    else:
                        # Regular AMS IDs are bit positions. HT units have a
                        # separate protocol range; do not infer their bit layout.
                        for uid in list(self.units):
                            if uid < 16 and not mask & (1 << self.units[uid].get("_wire_id", uid)):
                                del self.units[uid]
                except (TypeError, ValueError):
                    pass
            if "tray_exist_bits" in envelope:
                from backend.app.services.bambu_mqtt import apply_tray_exist_bits

                apply_tray_exist_bits(
                    list(self.units.values()),
                    envelope["tray_exist_bits"],
                    power_on_flag=envelope.get("power_on_flag", True),
                )
        dual = is_dual_nozzle_model(normalize_model_name(model))
        # vir_slot is the authoritative dual shape; vt_tray can be a one-tray echo.
        external = report.get("vir_slot", report.get("vt_tray"))
        if isinstance(external, dict):
            external = [external]
        if isinstance(external, list):
            self.external_known = True
            if not external:
                self.external.clear()
            for tray in external:
                if not isinstance(tray, dict):
                    continue
                tid = _integer(tray.get("id"), 254) if dual else 254
                if tid not in (254, 255):
                    continue
                merged = {**self.external.get(tid, {}), **tray, "id": tid}
                if tray.get("tray_type") == "":
                    merged = {"id": tid, "tray_type": ""}
                self.external[tid] = merged
        device = report.get("device") or {}
        if isinstance(device, dict):
            if "fila_switch" in device:
                self.fts = isinstance(device["fila_switch"], dict)
            nozzle = device.get("nozzle") or {}
            if isinstance(nozzle, dict) and isinstance(nozzle.get("info"), list):
                # A full info list replaces the installed/rack set, including empty.
                self.nozzles.clear()
                for entry in nozzle["info"]:
                    if not isinstance(entry, dict):
                        continue
                    nid = _integer(entry.get("id"))
                    if nid is None:
                        continue
                    if is_nozzle_rack_model(model):
                        from backend.app.utils.printer_models import (
                            FIXED_CARRIAGE_PHYSICAL_ID,
                            NOZZLE_RACK_EXTRUDER_INDEX,
                        )

                        if nid >= 16:
                            nid = NOZZLE_RACK_EXTRUDER_INDEX
                        elif nid == FIXED_CARRIAGE_PHYSICAL_ID:
                            nid = 1 - NOZZLE_RACK_EXTRUDER_INDEX
                    self._diameter(nid, entry.get("diameter"))
        for key, nid in (
            ("nozzle_diameter", 0),
            ("left_nozzle_diameter", 1),
            ("right_nozzle_diameter", 0),
            ("nozzle_diameter_2", 1),
        ):
            if key in report:
                self.nozzles.pop(nid, None)
                self._diameter(nid, report[key])

    def _diameter(self, nozzle_id, value):
        try:
            diameter = float(value)
            if 0 < diameter < 3:
                self.nozzles.setdefault(nozzle_id, set()).add(diameter)
        except (TypeError, ValueError):
            pass


def snapshot_from_state(printer_id: int, model: str | None, state, overlay=None) -> PrinterFeedSnapshot:
    normalized = normalize_model_name(model)
    dual = is_dual_nozzle_model(normalized)
    telemetry = getattr(state, "feed_telemetry", None)
    if not isinstance(telemetry, FeedTelemetry):
        telemetry = FeedTelemetry()
    sources = []
    # What the overlay actually changed, folded into the revision below: a slot
    # whose advertised profile is masked must invalidate every cached routing
    # decision taken while the live values were the only thing we knew.
    applied_overlay: list = []

    def add(tray, sid, kind, nozzles, slot_key=None):
        material = tray.get("tray_type")
        if not material:
            return
        try:
            remain = float(tray.get("remain", -1))
        except (TypeError, ValueError):
            remain = -1
        color, variant = tray.get("tray_color"), tray.get("tray_info_idx")
        entry = overlay.get(slot_key) if overlay and slot_key is not None else None
        if entry is not None and matches_live(entry, tray):
            material, color, variant = entry.actual_material, entry.actual_color, entry.actual_variant
            applied_overlay.append([slot_key[0], slot_key[1], material, color, variant])
        sources.append(
            FeedSource(
                id=sid,
                kind=kind,
                material=material,
                color=color,
                variant=variant,
                nozzles=nozzles,
                remain=remain,
                identity=str(tray.get("tray_uuid") or tray.get("tag_uid") or ""),
            )
        )

    for uid, unit in telemetry.units.items():
        try:
            extruder = (int(str(unit["info"]), 16) >> 8) & 0xF
        except (KeyError, ValueError):
            extruder = None
        nozzles = (0, 1) if dual and telemetry.fts else ((extruder,) if extruder in (0, 1) else ())
        if not dual:
            nozzles = (0,)
        for tray in unit.get("tray", []):
            tid = _integer(tray.get("id"))
            if tid is not None:
                # The overlay is keyed by (unit, slot) as the assignment rows
                # are, NOT by the global source id this snapshot sorts on.
                add(tray, uid if uid >= 128 else uid * 4 + tid, "ams", nozzles, slot_key=(uid, tid if uid < 128 else 0))
    for tid, tray in telemetry.external.items():
        nozzle = (255 - tid) if dual else 0
        add(tray, tid, "external", (nozzle,) if nozzle in (0, 1) else ())
    sources.sort(key=lambda s: s.id)
    incomplete = (
        (telemetry.ams_present and not telemetry.units)
        or any("tray_type" not in t for t in telemetry.external.values())
        or any(
            not u.get("_trays_seen") or any("tray_type" not in t for t in u["tray"]) for u in telemetry.units.values()
        )
    )
    diameters = {k: tuple(sorted(v)) for k, v in telemetry.nozzles.items()}
    payload = {
        "model": normalized,
        "ams_known": telemetry.ams_known,
        "ams_present": telemetry.ams_present,
        "external_known": telemetry.external_known,
        "nozzles": diameters,
        "fts": telemetry.fts,
        "incomplete": incomplete,
        "sources": [{k: v for k, v in asdict(s).items() if k != "remain"} for s in sources],
        "overlay": applied_overlay,
    }
    revision = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return PrinterFeedSnapshot(
        printer_id,
        normalized,
        bool(state and state.connected),
        getattr(state, "connection_generation", 0),
        revision,
        telemetry.ams_known,
        telemetry.ams_present,
        telemetry.external_known,
        tuple(sources),
        diameters,
        telemetry.fts,
        getattr(state, "ams_auto_switch_filament", None),
        incomplete,
    )
