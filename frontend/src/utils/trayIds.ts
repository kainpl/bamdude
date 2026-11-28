/**
 * A printer's tray numbering, in one place — the twin of
 * `backend/app/utils/tray_ids.py` (Bambu Studio's numbering, protocol only).
 *
 * A global tray id names one filament source: what `tray_now` reports, what a
 * print's `ams_mapping` holds and what the card highlights. Studio derives it
 * from the unit's AMS type (`DevAmsType`, bits 0-3 of the unit's `info`; the
 * backend sends it as `ams_type`):
 *
 *   AMS / AMS Lite / AMS 2 Pro (1, 2, 3)   ams_id * 4 + slot
 *   AMS-HT (4, units 128-135)              ams_id
 *   A2L AMS Lite (5, stored as unit 6)     24 + slot
 *   AMS 3H (6, unit 32)                    28 + slot
 *   AMS 3P (7, unit 33)                    37 + slot
 *   external input                         its own vir_slot id
 *
 * A direct-connected nozzle rack (H3C, P3C): one toolhead, rack AMS slot k
 * feeds PrintTool k + 1, external input v feeds PrintTool 256 - v (`Ext-n`).
 * Every external input is stored as (255, index): 254 -> 0, 255 -> 1,
 * 247..253 -> 2..8 — not monotonic in the PrintTool, so labels and order go
 * through the PrintTool.
 *
 * ⚠️ Nothing outside this module computes, splits or labels a tray number by
 * hand. No mask helpers here: the backend decodes the bit masks (bits up to
 * 45 — JavaScript shifts work modulo 32).
 */

export const AmsType = {
  EXT_SPOOL: 0,
  AMS: 1,
  AMS_LITE: 2,
  N3F: 3, // AMS 2 Pro
  N3S: 4, // AMS-HT
  AMS_LITE_MIXED: 5, // A2L AMS Lite
  ZMULTI_A: 6, // AMS 3H
  ZMULTI_B: 7, // AMS 3P
} as const;
export type AmsTypeValue = (typeof AmsType)[keyof typeof AmsType];

const ZMULTI_TRAY_BASE: Record<number, number> = { [AmsType.ZMULTI_A]: 28, [AmsType.ZMULTI_B]: 37 };
const ZMULTI_UNIT_IDS: Record<number, number> = { [AmsType.ZMULTI_A]: 32, [AmsType.ZMULTI_B]: 33 };
const ZMULTI_NAMES: Record<number, string> = { [AmsType.ZMULTI_A]: 'AMS-3H', [AmsType.ZMULTI_B]: 'AMS-3P' };
export const MAX_RACK_SLOTS = 9;

export const A2L_LITE_UNIT_ID = 6;
const A2L_LITE_GLOBAL_BASE = 24;
const AMS_HT_FIRST_ID = 128;
const AMS_HT_LAST_ID = 135;
export const EXTERNAL_AMS_ID = 255;
export const EXTERNAL_DEPUTY_ID = 254;
export const EXTERNAL_MAIN_ID = 255;
const DIRECT_RACK_EXTERNAL_FIRST = 247;
export const NO_TRAY = 255;

export function isAmsHtId(amsId: number): boolean {
  return amsId >= AMS_HT_FIRST_ID && amsId <= AMS_HT_LAST_ID;
}

export function isZmulti(amsType: number | null | undefined): boolean {
  return amsType === AmsType.ZMULTI_A || amsType === AmsType.ZMULTI_B;
}

/** The type of a unit whose `info` was never seen, told from its id (a deviation from Studio, see the backend). */
export function fallbackAmsType(amsId: number): number {
  if (amsId === A2L_LITE_UNIT_ID) return AmsType.AMS_LITE_MIXED;
  if (amsId === 32) return AmsType.ZMULTI_A;
  if (amsId === 33) return AmsType.ZMULTI_B;
  if (amsId >= AMS_HT_FIRST_ID) return AmsType.N3S;
  return AmsType.AMS;
}

/** The type of unit `amsId`: `amsType` when it is a known unit type, else the id's; 128-135 is always an AMS-HT. */
export function amsTypeOf(amsId: number, amsType?: number | null): number {
  if (isAmsHtId(amsId)) return AmsType.N3S;
  if (typeof amsType === 'number' && amsType >= AmsType.AMS && amsType <= AmsType.ZMULTI_B) return amsType;
  return fallbackAmsType(amsId);
}

// --- numbers ---------------------------------------------------------------

/** The global id of the external input stored as (255, index): 0 -> 254, 1 -> 255, 2..8 -> 247..253. */
export function externalTrayId(slotIndex: number): number {
  if (slotIndex >= 2 && slotIndex <= 8) return DIRECT_RACK_EXTERNAL_FIRST + slotIndex - 2;
  return EXTERNAL_DEPUTY_ID + slotIndex;
}

/** The (255, index) an external input is stored under: 254 -> 0, 255 -> 1, 247..253 -> 2..8. */
export function externalSlotIndex(trayId: number): number {
  if (trayId >= DIRECT_RACK_EXTERNAL_FIRST && trayId < EXTERNAL_DEPUTY_ID) {
    return trayId - DIRECT_RACK_EXTERNAL_FIRST + 2;
  }
  return trayId - EXTERNAL_DEPUTY_ID;
}

/** Whether a global id can only be an external input: 254/255 anywhere, 247-253 on a direct rack. */
export function isExternalId(trayId: number): boolean {
  return trayId >= DIRECT_RACK_EXTERNAL_FIRST && trayId <= EXTERNAL_MAIN_ID;
}

/** The PrintTool (1-based) a direct rack's external input feeds: 256 - id. */
export function externalPrintTool(trayId: number): number {
  return EXTERNAL_MAIN_ID + 1 - trayId;
}

/** A direct rack's external input as Studio names it: `Ext-{PrintTool}`. */
export function externalLabel(trayId: number): string {
  return `Ext-${externalPrintTool(trayId)}`;
}

/** The global tray id of slot `slot` of unit `amsId`; (255, n) / (254, n) is an external input. */
export function globalTrayId(amsId: number, slot: number, amsType?: number | null): number {
  if (amsId === EXTERNAL_AMS_ID || amsId === EXTERNAL_DEPUTY_ID) return externalTrayId(slot);
  const kind = amsTypeOf(amsId, amsType);
  if (kind === AmsType.N3S) return amsId;
  if (kind === AmsType.AMS_LITE_MIXED) return A2L_LITE_GLOBAL_BASE + slot;
  if (isZmulti(kind)) return ZMULTI_TRAY_BASE[kind] + slot;
  return amsId * 4 + slot;
}

/** (unit, slot) of a regular AMS's global id. */
export function regularSlotKey(trayId: number): [number, number] {
  return [Math.floor(trayId / 4), trayId % 4];
}

/** The slot within its unit a regular AMS / AMS-HT global id names (an AMS-HT's one tray is 0). */
export function localSlot(trayId: number): number {
  return trayId >= AMS_HT_FIRST_ID ? 0 : trayId % 4;
}

// --- labels ----------------------------------------------------------------

/** A..: AMS units 0-25 and AMS-HT 128-135; the id itself otherwise. */
export function unitLetter(amsId: number): string {
  if (isAmsHtId(amsId)) return String.fromCharCode(65 + amsId - AMS_HT_FIRST_ID);
  if (amsId >= 0 && amsId < 26) return String.fromCharCode(65 + amsId);
  return String(amsId);
}

/** A unit's short name: AMS-A, HT-A, AMS-3H, AMS-3P. */
export function unitName(amsId: number, amsType?: number | null): string {
  const kind = amsTypeOf(amsId, amsType);
  if (isZmulti(kind)) return ZMULTI_NAMES[kind];
  if (kind === AmsType.N3S) return `HT-${unitLetter(amsId)}`;
  return `AMS-${unitLetter(amsId)}`;
}

// --- one printer's layout --------------------------------------------------

type UnitLike = { id: number | string; ams_type?: number | null };

export interface TrayLayout {
  units: Array<[number, number]>;
  directRack: boolean;
  rackSize: number;
  /** A direct rack: `tray_now` 255 is PrintTool 1's input, not "nothing" (the toolhead's `snow` said so). */
  mainInputLoaded: boolean;
}

export const NO_LAYOUT: TrayLayout = { units: [], directRack: false, rackSize: 0, mainInputLoaded: false };

/** The layout the status reports: its units (with `ams_type`), `direct_rack`, `rack_size`, `direct_rack_input_loaded`. */
export function trayLayoutOf(
  status:
    | {
        ams?: UnitLike[] | null;
        direct_rack?: boolean | null;
        rack_size?: number | null;
        direct_rack_input_loaded?: boolean | null;
      }
    | null
    | undefined,
): TrayLayout {
  if (!status) return NO_LAYOUT;
  const units: Array<[number, number]> = [];
  for (const unit of status.ams ?? []) {
    const id = typeof unit.id === 'string' ? parseInt(unit.id, 10) : unit.id;
    if (Number.isNaN(id) || units.some(([uid]) => uid === id)) continue;
    units.push([id, amsTypeOf(id, unit.ams_type)]);
  }
  const directRack = Boolean(status.direct_rack);
  return {
    units,
    directRack,
    rackSize: status.rack_size ?? 0,
    mainInputLoaded: directRack && status.direct_rack_input_loaded === true,
  };
}

/**
 * Whether a `tray_now` value names a loaded filament source: 0-254 anywhere; 255 only
 * on a direct rack whose toolhead `snow` said PrintTool 1's input is the tray now.
 */
export function namesLoadedSource(layout: TrayLayout, trayNow: number | null | undefined): boolean {
  if (typeof trayNow !== 'number' || trayNow < 0) return false;
  if (trayNow < NO_TRAY) return true;
  return trayNow === NO_TRAY && layout.directRack && layout.mainInputLoaded;
}

export function layoutAmsType(layout: TrayLayout, amsId: number): number {
  const found = layout.units.find(([uid]) => uid === amsId);
  return found ? found[1] : fallbackAmsType(amsId);
}

/** How many slots unit `amsId` may have: a rack AMS the rack size, an AMS-HT 1, else 4. */
export function slotLimit(layout: TrayLayout, amsId: number): number {
  const kind = layoutAmsType(layout, amsId);
  if (isZmulti(kind)) return layout.directRack && layout.rackSize ? layout.rackSize : MAX_RACK_SLOTS;
  if (kind === AmsType.N3S) return 1;
  return 4;
}

export function layoutGlobalTrayId(layout: TrayLayout, amsId: number, slot: number): number {
  if (amsId === EXTERNAL_AMS_ID) return externalTrayId(slot);
  return globalTrayId(amsId, slot, layoutAmsType(layout, amsId));
}

/** Whether global tray `trayId` is one of this printer's external inputs. */
export function isExternal(layout: TrayLayout, trayId: number): boolean {
  if (trayId === EXTERNAL_DEPUTY_ID || trayId === EXTERNAL_MAIN_ID) return true;
  return layout.directRack && trayId >= DIRECT_RACK_EXTERNAL_FIRST && trayId < EXTERNAL_DEPUTY_ID;
}

/** (amsId, slot) of a rack AMS's global id, else null (28-36 / 37-45 unless a regular unit 7+ is listed). */
export function rackSlot(layout: TrayLayout, trayId: number): [number, number] | null {
  const listed = layout.units.find(([, kind]) => isZmulti(kind));
  let candidates: Array<[number, number]> = [];
  if (listed) candidates = [listed];
  else if (layout.directRack || !layout.units.some(([uid, kind]) => uid >= 7 && uid < 16 && !isZmulti(kind))) {
    candidates = [
      [ZMULTI_UNIT_IDS[AmsType.ZMULTI_A], AmsType.ZMULTI_A],
      [ZMULTI_UNIT_IDS[AmsType.ZMULTI_B], AmsType.ZMULTI_B],
    ];
  }
  for (const [uid, kind] of candidates) {
    const base = ZMULTI_TRAY_BASE[kind];
    if (trayId >= base && trayId < base + slotLimit(layout, uid)) return [uid, trayId - base];
  }
  return null;
}

/** The (amsId, trayId) global tray `trayId` is stored under. */
export function slotKey(layout: TrayLayout, trayId: number): [number, number] {
  if (isExternalId(trayId)) return [EXTERNAL_AMS_ID, externalSlotIndex(trayId)];
  if (trayId >= AMS_HT_FIRST_ID) return [trayId, 0];
  return rackSlot(layout, trayId) ?? regularSlotKey(trayId);
}

/** Whether a direct rack has external input `trayId`: its PrintTool is 1..rack size. */
export function externalInRack(layout: TrayLayout, trayId: number): boolean {
  const tool = externalPrintTool(trayId);
  return layout.directRack && tool >= 1 && tool <= layout.rackSize;
}

/** The PrintTool (1-based) a direct rack's source feeds; null off a direct rack. */
export function printTool(layout: TrayLayout, trayId: number): number | null {
  if (!layout.directRack) return null;
  const rack = rackSlot(layout, trayId);
  if (rack) return rack[1] + 1;
  if (trayId >= DIRECT_RACK_EXTERNAL_FIRST && trayId <= EXTERNAL_MAIN_ID) return externalPrintTool(trayId);
  return null;
}

/** A direct rack's source as people read it — `AMS-3H 5`, `Ext-3` — else null (callers keep theirs). */
export function rackLabel(layout: TrayLayout, trayId: number): string | null {
  if (!layout.directRack) return null;
  const rack = rackSlot(layout, trayId);
  if (rack) return `${unitName(rack[0], layoutAmsType(layout, rack[0]))} ${rack[1] + 1}`;
  if (trayId >= DIRECT_RACK_EXTERNAL_FIRST && trayId <= EXTERNAL_MAIN_ID) return externalLabel(trayId);
  return null;
}

/** Whether `(amsId, trayId)` can name a slot of this printer. */
export function acceptsSlot(layout: TrayLayout, amsId: number, trayId: number): boolean {
  if (trayId < 0) return false;
  if (amsId === EXTERNAL_AMS_ID) {
    if (trayId === 0 || trayId === 1) return true;
    return layout.directRack && trayId >= 2 && trayId <= 8 && externalInRack(layout, externalTrayId(trayId));
  }
  const kind = layoutAmsType(layout, amsId);
  if (isZmulti(kind)) {
    const listed = layout.units.some(([uid]) => uid === amsId);
    return (layout.directRack || listed) && trayId < slotLimit(layout, amsId);
  }
  return trayId <= 3;
}

/**
 * Whether a stage-C printer command — load, unload, an RFID re-read, drying, the
 * K-profile — on slot (amsId, trayId) is withheld (the backend's
 * `services/direct_rack_gate`): a rack AMS's slot, or a direct rack's external
 * input. The slot's filament setting is not withheld (N01 stage B sends it in
 * Bambu Studio's form).
 */
export function slotCommandWithheld(layout: TrayLayout, amsId: number): boolean {
  if (isZmulti(layoutAmsType(layout, amsId))) return true;
  return amsId === EXTERNAL_AMS_ID && layout.directRack;
}

/** A direct rack's external inputs in PrintTool order (Ext-1 first). */
export function externalsByPrintTool<T extends { id: number }>(trays: T[]): T[] {
  return [...trays].sort((a, b) => externalPrintTool(a.id) - externalPrintTool(b.id));
}
