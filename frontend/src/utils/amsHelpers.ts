/**
 * AMS (Automatic Material System) helper utilities for Bambu Lab printers.
 * These functions handle color normalization, slot labeling, and tray ID calculations
 * for AMS, AMS-HT, and external spool configurations.
 */
import { parseUTCDate } from './date';

/**
 * Normalize color format from various sources.
 * API returns "RRGGBBAA" (8-char), 3MF uses "#RRGGBB" (7-char with hash).
 * This normalizes to "#RRGGBB" format.
 */
export function normalizeColor(color: string | null | undefined): string {
  if (!color) return '#808080';
  // Result is "#RRGGBB" for opaque colours and "#RRGGBBAA" when alpha < FF —
  // CSS accepts both forms on `fill` / `backgroundColor`, and preserving alpha
  // lets transparent filaments render translucent instead of collapsing to
  // solid black (#1545). Comparison helpers use normalizeColorForCompare which
  // still strips alpha, so type/colour matching is unaffected.
  const clean = color.replace('#', '');
  if (clean.length >= 8 && clean.substring(6, 8).toLowerCase() !== 'ff') {
    return `#${clean.substring(0, 8)}`;
  }
  return `#${clean.substring(0, 6)}`;
}

/**
 * Normalize color for comparison (case-insensitive, strip hash and alpha).
 */
export function normalizeColorForCompare(color: string | undefined): string {
  if (!color) return '';
  return color.replace('#', '').toLowerCase().substring(0, 6);
}

/**
 * AMS unit label using the codebase convention: "AMS-A / AMS-B / ..." for
 * regular AMS, "HT-A / HT-B / ..." for AMS-HT (single-tray modules with
 * IDs starting at 128). `trayCount` is required because the type can't be
 * inferred from the id alone — regular AMS IDs 0-3 can collide with the
 * normalized HT range otherwise.
 */
export function getAmsLabel(amsId: number | string, trayCount: number): string {
  const id = typeof amsId === 'string' ? parseInt(amsId, 10) : amsId;
  const safeId = isNaN(id) ? 0 : id;
  if (safeId === 255) return 'External';
  // A2L "AMS Lite": the backend normalises its physical unit id 16 to 6 at
  // ingest (see bambu_mqtt.normalize_am_unit_id). No regular AMS uses id 6, so
  // this is a safe, self-scoping label for the Lite's 4-slot unit.
  if (safeId === 6) return 'AMS Lite';
  const isHt = trayCount === 1;
  const normalizedId = safeId >= 128 ? safeId - 128 : safeId;
  const letter = String.fromCharCode(65 + normalizedId);
  return isHt ? `HT-${letter}` : `AMS-${letter}`;
}

/**
 * Filament type equivalence groups.
 * Types within the same group are interchangeable on the printer side
 * (e.g., Bambu Lab firmware treats PA-CF and PA12-CF as compatible).
 */
const FILAMENT_TYPE_GROUPS: string[][] = [
  ['PA-CF', 'PA12-CF', 'PAHT-CF'],
];

const _equivalenceMap: Record<string, string> = {};
for (const group of FILAMENT_TYPE_GROUPS) {
  const canonical = group[0];
  for (const t of group) {
    _equivalenceMap[t.toUpperCase()] = canonical.toUpperCase();
  }
}

/**
 * Get the canonical filament type for equivalence matching.
 * Types in the same group (e.g., PA-CF / PA12-CF / PAHT-CF) return the same canonical type.
 */
export function canonicalFilamentType(type: string | undefined): string {
  if (!type) return '';
  const upper = type.toUpperCase();
  return _equivalenceMap[upper] ?? upper;
}

/**
 * Check if two filament types are compatible (same type or same equivalence group).
 */
export function filamentTypesCompatible(a: string | undefined, b: string | undefined): boolean {
  return canonicalFilamentType(a) === canonicalFilamentType(b);
}

/**
 * Check if two colors are visually similar within a threshold.
 * Uses RGB component comparison with configurable tolerance.
 * @param color1 - First hex color
 * @param color2 - Second hex color
 * @param threshold - Maximum difference per RGB component (default: 40)
 */
export function colorsAreSimilar(
  color1: string | undefined,
  color2: string | undefined,
  threshold = 40
): boolean {
  const hex1 = normalizeColorForCompare(color1);
  const hex2 = normalizeColorForCompare(color2);
  if (!hex1 || !hex2 || hex1.length < 6 || hex2.length < 6) return false;

  const r1 = parseInt(hex1.substring(0, 2), 16);
  const g1 = parseInt(hex1.substring(2, 4), 16);
  const b1 = parseInt(hex1.substring(4, 6), 16);
  const r2 = parseInt(hex2.substring(0, 2), 16);
  const g2 = parseInt(hex2.substring(2, 4), 16);
  const b2 = parseInt(hex2.substring(4, 6), 16);

  return (
    Math.abs(r1 - r2) <= threshold &&
    Math.abs(g1 - g2) <= threshold &&
    Math.abs(b1 - b2) <= threshold
  );
}

/**
 * Format slot label for display in the UI.
 * @param amsId - AMS unit ID (0-3 for regular AMS, 128+ for AMS-HT)
 * @param trayId - Tray/slot ID within the AMS unit (0-3)
 * @param isHt - Whether this is an AMS-HT unit (single tray)
 * @param isExternal - Whether this is the external spool holder
 */
export function formatSlotLabel(
  amsId: number,
  trayId: number,
  isHt: boolean,
  isExternal: boolean
): string {
  if (isExternal) return 'Ext';
  // Convert AMS ID to letter (A, B, C, D)
  // AMS-HT uses IDs starting at 128
  const letter = String.fromCharCode(65 + (amsId >= 128 ? amsId - 128 : amsId));
  if (isHt) return `HT-${letter}`;
  return `${letter}${trayId + 1}`;
}

/**
 * Classify an AMS slot that has no configured filament type.
 *
 * A slot with no ``tray_type`` used to render identically to a truly empty slot,
 * so a spool that was physically loaded but never had its filament type set
 * looked empty (#1694). This distinguishes the two:
 *   - configured slot (tray_type set)  → null
 *   - `exists` (firmware tray_exist_bits) when present — authoritative
 *   - else the firmware tray state (9 = empty, 10 = present-but-not-fed):
 *     state 9 or 10 → 'physical' (render "-"/Empty), otherwise 'reset'
 *     (render "?", amber accent)
 *
 * `exists` is firmware's authoritative presence signal (what BambuStudio uses):
 * a non-RFID spool the standard AMS can't identify is physically present
 * (exists === true) but carries an empty tray_type and state=9 — structurally
 * identical to a truly empty slot — so without the bitmask it rendered "Empty"
 * where Studio correctly shows "?" (upstream #2527). AMS-HT and missing-bitmask
 * paths keep the old state heuristic.
 *
 * The 'reset' bucket also covers the "no firmware state available" case: without
 * proof the slot is physically empty we err toward "?" rather than silently "-".
 */
export function getEmptySlotKind(
  tray: { tray_type?: string | null; state?: number | null; exists?: boolean | null } | undefined
): 'physical' | 'reset' | null {
  if (tray?.tray_type) return null;
  if (tray?.exists === true) return 'reset';
  if (tray?.exists === false) return 'physical';
  const state = tray?.state ?? null;
  return state === 9 || state === 10 ? 'physical' : 'reset';
}

/**
 * Resolve the installed nozzle diameter feeding a given AMS unit, so the
 * Configure-AMS-Slot picker filters filament presets by the nozzle actually on
 * the machine instead of assuming 0.4mm (upstream #1899).
 *
 * On dual-nozzle printers (H2D) each AMS is bound to one extruder via
 * `ams_extruder_map` (amsId → extruder index, 0=right, 1=left), so we read that
 * nozzle's diameter. Single-nozzle printers have no map entry and fall back to
 * the primary nozzle (index 0). Returns undefined when the printer hasn't
 * reported nozzle hardware yet, letting the caller keep its own default.
 * Diameter is the bare decimal string the status carries, e.g. "0.4" / "0.6".
 */
export function resolveSlotNozzleDiameter(
  status: {
    nozzles?: { nozzle_diameter?: string }[];
    ams_extruder_map?: Record<string, number>;
  } | null | undefined,
  amsId: number,
): string | undefined {
  const nozzles = status?.nozzles;
  if (!nozzles || nozzles.length === 0) return undefined;
  const extruderIdx = status?.ams_extruder_map?.[String(amsId)] ?? 0;
  const diameter = nozzles[extruderIdx]?.nozzle_diameter || nozzles[0]?.nozzle_diameter;
  return diameter || undefined;
}

/**
 * Calculate global tray ID for MQTT command.
 * Used in the ams_mapping array sent to the printer.
 * @param amsId - AMS unit ID (0-3 for regular AMS, 128+ for AMS-HT)
 * @param trayId - Tray/slot ID within the AMS unit
 * @param isExternal - Whether this is the external spool holder
 * @returns Global tray ID (0-15 for AMS, 128+ for AMS-HT, 254 for external)
 */
export function getGlobalTrayId(
  amsId: number,
  trayId: number,
  isExternal: boolean
): number {
  if (isExternal) return 254 + trayId;
  // AMS-HT units have IDs starting at 128 with a single tray - use ID directly
  if (amsId >= 128) return amsId;
  return amsId * 4 + trayId;
}

/**
 * Get fill bar color based on spool fill level.
 * Matches PrintersPage thresholds and Bambu Lab brand green.
 */
export function getFillBarColor(fillLevel: number): string {
  if (fillLevel > 50) return '#00ae42'; // Green - good
  if (fillLevel >= 15) return '#f59e0b'; // Amber - warning (<= 50%)
  return '#ef4444'; // Red - critical (< 15%)
}

/**
 * Calculate fill level from Spoolman weight data.
 * Used as the first source in the Spoolman → Inventory → AMS fill chain.
 */
export function getSpoolmanFillLevel(
  linkedSpool: { remaining_weight: number | null; filament_weight: number | null } | undefined
): number | null {
  if (!linkedSpool?.remaining_weight || !linkedSpool?.filament_weight
      || linkedSpool.filament_weight <= 0) return null;
  return Math.min(100, Math.round(
    (linkedSpool.remaining_weight / linkedSpool.filament_weight) * 100
  ));
}

function toFixedHex(value: number, width: number): string {
  const safe = Number.isFinite(value) ? Math.max(0, Math.trunc(value)) : 0;
  return safe.toString(16).toUpperCase().padStart(width, '0').slice(-width);
}

// 32-bit FNV-1a hash -> 8-char hex (stable for alphanumeric serials)
function hashSerialToHex32(serial: string): string {
  const input = (serial || '').trim().toUpperCase();
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).toUpperCase().padStart(8, '0');
}

/**
 * Generate a stable fallback spool tag for slots without RFID identifiers.
 * Returns a 16-char hex string derived from the printer serial + slot position.
 */
export function getFallbackSpoolTag(printerSerial: string, amsId: number, trayId: number): string {
  return `${hashSerialToHex32(printerSerial)}${toFixedHex(amsId, 4)}${toFixedHex(trayId, 4)}`;
}

/**
 * Check if a scheduled time is a placeholder far-future date.
 * Placeholder dates (more than 6 months out) are treated as ASAP.
 */
export function isPlaceholderDate(scheduledTime: string | null | undefined): boolean {
  if (!scheduledTime) return false;
  const sixMonthsFromNow = Date.now() + 180 * 24 * 60 * 60 * 1000;
  return (parseUTCDate(scheduledTime)?.getTime() ?? 0) > sixMonthsFromNow;
}

/**
 * Sort key for the "prefer lowest remaining filament" setting.
 *
 * Ascending by remaining percentage, with unknown (``-1`` / absent — no RFID,
 * calibration off) pushed past the 0-100 range so it is only chosen when
 * nothing measurable qualifies. Byte-for-byte the backend's rule in
 * ``auto_queue_ams.py``: ``f.get("remain", -1) if f.get("remain", -1) >= 0 else 101``.
 * The two must agree — the backend applies it when it computes a mapping for
 * AutoQueue, the frontend when the Print dialog pins one.
 */
export function remainSortKey(f: { remain?: number }): number {
  const r = f.remain ?? -1;
  return r >= 0 ? r : 101;
}

/**
 * Sort candidates so the emptiest spool wins, WITHOUT disturbing match
 * precedence: callers scan the sorted list once per tier (exact colour →
 * similar → type-only), so this only ever breaks ties inside a tier and can
 * never promote a worse-matching spool over a better one.
 */
export function sortByRemainAscending<T extends { remain?: number }>(items: T[]): T[] {
  return [...items].sort((a, b) => remainSortKey(a) - remainSortKey(b));
}

/**
 * Auto-match a filament requirement to a loaded filament, respecting nozzle constraints.
 * Used by both single-printer (FilamentMapping) and multi-printer (InlineMappingEditor) paths.
 *
 * ``preferLowest`` mirrors the ``prefer_lowest_filament`` setting. It matters
 * here because the dispatcher will NOT re-derive a mapping the Print dialog
 * already pinned — ``_ensure_ams_mapping`` returns early on a resolved mapping
 * so a manual override is never clobbered — so if this function ignores the
 * setting, the setting is simply ignored on the whole Print → pick printer →
 * Add to queue path.
 */
export function autoMatchFilament(
  req: { type?: string; color?: string; nozzle_id?: number | null },
  loadedFilaments: { globalTrayId: number; type?: string; color?: string; extruderId?: number; remain?: number }[],
  usedTrayIds: Set<number>,
  preferredTrayId?: number | null,
  preferLowest = false,
): typeof loadedFilaments[number] | undefined {
  const byNozzle = filterFilamentsByNozzle(loadedFilaments, req.nozzle_id);
  const nozzleFilaments = preferLowest ? sortByRemainAscending(byNozzle) : byNozzle;

  // One-colour print: prefer the spool already loaded in the extruder
  // (tray_now) over an AMS swap. Type still gates. See matchLoadedExtruderTray.
  if (preferredTrayId != null) {
    const extruderTray = matchLoadedExtruderTray(
      req,
      nozzleFilaments.filter((f) => !usedTrayIds.has(f.globalTrayId)),
      preferredTrayId,
    );
    if (extruderTray) return extruderTray;
  }

  const exactMatch = nozzleFilaments.find(
    (f) =>
      !usedTrayIds.has(f.globalTrayId) &&
      filamentTypesCompatible(f.type, req.type) &&
      normalizeColorForCompare(f.color) === normalizeColorForCompare(req.color)
  );
  // ⚠️ `reduce`, not `find`: among the spools the tolerance admits, take the
  // one that LOOKS closest rather than the one that happens to sit first. The
  // scheduler does the same, and the two must agree or this dialog promises a
  // spool the dispatch would not pick.
  const similarMatch = exactMatch
    ? undefined
    : nozzleFilaments
        .filter(
          (f) =>
            !usedTrayIds.has(f.globalTrayId) &&
            filamentTypesCompatible(f.type, req.type) &&
            colorsAreSimilar(f.color, req.color)
        )
        .reduce<typeof loadedFilaments[number] | undefined>(
          (best, f) => nearerColour(best, f, req.color),
          undefined
        );
  const typeOnlyMatch =
    exactMatch || similarMatch
      ? undefined
      : nozzleFilaments.find(
          (f) => !usedTrayIds.has(f.globalTrayId) && filamentTypesCompatible(f.type, req.type)
        );
  return exactMatch ?? similarMatch ?? typeOnlyMatch;
}

/**
 * For a single-filament (one-colour) print, return the spool currently
 * loaded into the extruder (`tray_now`) when it is among the available
 * candidates and type-compatible with the requirement.
 *
 * Rationale: for a one-colour job the operator treats colour as cosmetic
 * and would rather print from whatever spool is already loaded than trigger
 * an AMS swap. Callers use this ahead of the colour-match cascade so the
 * auto-mapping defaults to the loaded slot instead of slot 0. Type still
 * gates — a PETG job won't match a PLA loaded slot, and the caller then
 * falls back to normal type/colour matching.
 *
 * `candidates` must already be filtered to free + correct-nozzle trays.
 * `trayNow` 255 is the Bambu sentinel for "nothing loaded" and is ignored.
 */
export function matchLoadedExtruderTray<T extends { globalTrayId: number; type?: string }>(
  req: { type?: string },
  candidates: T[],
  trayNow: number | null | undefined,
): T | undefined {
  if (trayNow == null || trayNow === 255) return undefined;
  return candidates.find(
    (f) => f.globalTrayId === trayNow && filamentTypesCompatible(f.type, req.type),
  );
}

/**
 * Filter loaded filaments to those valid for a given nozzle requirement.
 * For single-nozzle printers (nozzle_id is null/undefined), returns all filaments.
 */
export function filterFilamentsByNozzle<T extends { extruderId?: number }>(
  loadedFilaments: T[],
  nozzleId: number | undefined | null,
): T[] {
  return loadedFilaments.filter(
    (f) => nozzleId == null || f.extruderId === nozzleId
  );
}

/**
 * List the distinct nozzle diameters the printer actually reports (#2618).
 *
 * Mirrors the backend `print_scheduler._installed_nozzle_diameters`: reads each
 * `status.nozzles[].nozzle_diameter`, skips the empty-string / non-positive
 * defaults that fill a NozzleInfo before MQTT has said anything, and dedupes
 * (two 0.4 hotends are one diameter to ask about).
 *
 * Returns e.g. `['0.4']` or `['0.4', '0.6']`. An **empty array means "the
 * printer has not told us its nozzle hardware"** — not "no nozzles". Callers
 * that fetch per-diameter must fall back to their own default, exactly as the
 * backend treats unknown as unknown rather than as a mismatch.
 *
 * Keeps the bare decimal string the status carries, so it can go straight into
 * `getKProfiles` without a round trip through Number.
 */
export function installedNozzleDiameters(
  status: { nozzles?: { nozzle_diameter?: string }[] } | null | undefined,
): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const nozzle of status?.nozzles ?? []) {
    const raw = (nozzle?.nozzle_diameter ?? '').trim();
    if (!raw || !(parseFloat(raw) > 0) || seen.has(raw)) continue;
    seen.add(raw);
    result.push(raw);
  }
  return result;
}

/**
 * Detect Bambu Lab RFID-tagged spool by tray_uuid (32 hex) or tag_uid (16 hex).
 *
 * Permissive zero-string check: any non-zero non-empty value returns true. The
 * function exists to suppress assign/unassign actions on RFID-managed slots
 * whose state is owned by the printer firmware — manual changes there would be
 * overwritten on the next RFID re-read (eye → pen icon in BambuStudio).
 */
export function isBambuLabSpool(tray: {
  tray_uuid?: string | null;
  tag_uid?: string | null;
} | null | undefined): boolean {
  if (!tray) return false;
  if (tray.tray_uuid && tray.tray_uuid !== '00000000000000000000000000000000') return true;
  if (tray.tag_uid && tray.tag_uid !== '0000000000000000') return true;
  return false;
}

export interface AmsTrayLike {
  id: number;
  tray_type: string | null | undefined;
  tray_sub_brands: string | null | undefined;
  tray_color: string | null | undefined;
  tray_info_idx: string | null | undefined;
}

export interface AmsUnitLike {
  id: number;
  tray: AmsTrayLike[];
}

/**
 * One row in the AMS Backup modal: a group of slots that back each other up
 * (length >= 2), or a single non-empty slot with no peer (length === 1).
 */
export interface BackupGroup {
  /** Stable key — same across renders for the same material+extruder. */
  key: string;
  /** Bambu preset ID (tray_info_idx) when matched on preset; null otherwise. */
  presetId: string | null;
  /** 0 = right / single, 1 = left. Scoping field for dual-nozzle. */
  extruder: number;
  /** Display name from the first slot's tray_sub_brands (or tray_type). */
  displayName: string;
  /** Tray colour from the first slot, for the swatch in the modal. */
  trayColor: string | null;
  /** Member slots, in (ams_id, slot_idx) order. */
  members: Array<{ amsId: number; slotIdx: number; globalTrayId: number }>;
}

/**
 * Canonicalise a hex colour for identity comparison. Mirrors the backend
 * `_normalize_color_for_id`. Strips the leading `#`, uppercases, and drops
 * the alpha channel when 8 chars long so `1A1A1AFF` matches `1A1A1A`.
 */
function normalizeColorForId(raw: string | null | undefined): string {
  let s = (raw || '').trim().replace(/^#/, '').toUpperCase();
  if (s.length === 8) s = s.slice(0, 6);
  return s;
}

/**
 * Compute backup pairs for the AMS Backup modal (#1762).
 *
 * Strict identity rule (mirrors backend `_material_identity_internal` /
 * `_material_identity_spoolman`): slots pair ONLY when they share the same
 * Bambu preset ID (`tray_info_idx`, e.g. "GFA00") AND the same colour. The
 * preset identifies the filament profile (PETG HF, PLA Basic, etc.); the
 * colour pins the variant — three PETG HF spools in different colours
 * absolutely don't back each other up. User-tagged spools without a preset
 * never pair — Bambu's firmware backup logic relies on the preset, and
 * pairing on cosmetic name/colour match alone would let two visually-
 * identical but materially-different spools be treated as backups.
 *
 * Empty slots are skipped entirely. Every non-empty slot is returned — slots
 * without a peer come back as 1-member entries so the modal can list them as
 * "Slots without a backup peer".
 *
 * On dual-extruder printers (H2D / H2C / X2D), pairs are scoped per extruder
 * side — the firmware can't cross extruders even with the global backup bit
 * set.
 */
export function computeBackupGroups(
  amsUnits: AmsUnitLike[] | undefined,
  amsExtruderMap: Record<string, number> | undefined,
  isDualNozzle: boolean,
): BackupGroup[] {
  if (!amsUnits || amsUnits.length === 0) return [];

  // Defensive dedup: ``status.ams`` is expected to be unique by `ams.id`, but
  // observed in the wild to occasionally contain duplicate entries (e.g. on
  // VP-aggregated switch printers or during MQTT partial-update merges). A
  // duplicate would surface as "AMS-A slot 1" rendered twice with different
  // materials, which is impossible physically and visually broken. First
  // occurrence per `ams.id` wins.
  const seenIds = new Set<number>();
  const uniqueAms: AmsUnitLike[] = [];
  for (const ams of amsUnits) {
    if (seenIds.has(ams.id)) continue;
    seenIds.add(ams.id);
    uniqueAms.push(ams);
  }

  const byKey = new Map<string, BackupGroup>();

  for (const ams of uniqueAms) {
    const extruder = isDualNozzle ? Number(amsExtruderMap?.[String(ams.id)] ?? 0) : 0;
    ams.tray.forEach((tray, slotIdx) => {
      if (!tray?.tray_type) return; // empty slot
      const preset = (tray.tray_info_idx || '').trim();
      const globalTrayId = getGlobalTrayId(ams.id, slotIdx, false);
      const member = { amsId: ams.id, slotIdx, globalTrayId };

      let key: string;
      let presetId: string | null;
      if (preset) {
        // Same Bambu profile is necessary but NOT sufficient — different colours
        // of the same PETG HF profile can't back each other up. Bake the colour
        // into the identity key, normalised to strip alpha and case.
        const color = normalizeColorForId(tray.tray_color);
        key = `preset:${preset}|color:${color}#${extruder}`;
        presetId = preset;
      } else {
        // No preset → never group with anything else. Unique-per-slot key.
        key = `unmatched:${ams.id}:${slotIdx}#${extruder}`;
        presetId = null;
      }

      const existing = byKey.get(key);
      if (existing) {
        existing.members.push(member);
      } else {
        byKey.set(key, {
          key,
          presetId,
          extruder,
          displayName: tray.tray_sub_brands || tray.tray_type || '',
          trayColor: tray.tray_color ?? null,
          members: [member],
        });
      }
    });
  }

  // Stable sort: extruder first (so the modal can section per side on
  // dual-nozzle), then pairs before lone slots, then by name, then by first
  // member's global tray id for deterministic rendering.
  return Array.from(byKey.values()).sort((a, b) => {
    if (a.extruder !== b.extruder) return a.extruder - b.extruder;
    const aLone = a.members.length === 1 ? 1 : 0;
    const bLone = b.members.length === 1 ? 1 : 0;
    if (aLone !== bLone) return aLone - bLone;
    if (a.displayName !== b.displayName) return a.displayName.localeCompare(b.displayName);
    return a.members[0].globalTrayId - b.members[0].globalTrayId;
  });
}

// --- Perceptual colour difference (CIEDE2000) --------------------------------
//
// Ranking spools by RGB distance rates a colour by how far apart the numbers
// are, which is not how far apart they look: RGB overweights blue badly, so a
// required green could take a purple over a green that was numerically further
// away. CIEDE2000 is the CIE's perceptual metric, and small differences — which
// is all this ever sees, since candidates are already inside a narrow tolerance
// — are exactly the regime its predecessors handle worst.
//
// ⚠️ Mirrored from `backend/app/utils/color_utils.py` (`perceptual_color_distance`),
// kept structurally identical so the two can be read side by side. They MUST
// agree: this dialog must not promise a spool the scheduler would not pick.

const D65_WHITE: readonly [number, number, number] = [0.95047, 1.0, 1.08883];
const LAB_DELTA = 6 / 29;

/** Convert `RRGGBB(AA)` to CIE L*a*b* under D65, or null if unusable. */
function hexToLab(hexColor: string | undefined): [number, number, number] | null {
  const cleaned = (hexColor ?? '').replace('#', '').trim().toLowerCase();
  if (cleaned.length < 6) return null;
  const channels: number[] = [];
  for (const i of [0, 2, 4]) {
    const value = parseInt(cleaned.substring(i, i + 2), 16);
    if (Number.isNaN(value)) return null;
    channels.push(value / 255);
  }

  // sRGB gamma -> linear light.
  const [r, g, b] = channels.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));

  const x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b;
  const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b;
  const z = 0.0193339 * r + 0.119192 * g + 0.9503041 * b;

  const f = (t: number) => (t > LAB_DELTA ** 3 ? Math.cbrt(t) : t / (3 * LAB_DELTA * LAB_DELTA) + 4 / 29);
  const [fx, fy, fz] = [x, y, z].map((v, i) => f(v / D65_WHITE[i]));
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

const toRadians = (deg: number) => (deg * Math.PI) / 180;
const toDegrees = (rad: number) => (rad * 180) / Math.PI;

/** CIEDE2000 difference between two L*a*b* triples, with kL = kC = kH = 1. */
function ciede2000(lab1: [number, number, number], lab2: [number, number, number]): number {
  const [l1, a1, b1] = lab1;
  const [l2, a2, b2] = lab2;

  const c1 = Math.hypot(a1, b1);
  const c2 = Math.hypot(a2, b2);
  const cBar7 = ((c1 + c2) / 2) ** 7;
  const gFactor = 0.5 * (1 - Math.sqrt(cBar7 / (cBar7 + 25 ** 7)));

  const a1p = (1 + gFactor) * a1;
  const a2p = (1 + gFactor) * a2;
  const c1p = Math.hypot(a1p, b1);
  const c2p = Math.hypot(a2p, b2);

  const hue = (ap: number, bp: number) => {
    if (ap === 0 && bp === 0) return 0;
    const deg = toDegrees(Math.atan2(bp, ap));
    return deg < 0 ? deg + 360 : deg;
  };

  const h1p = hue(a1p, b1);
  const h2p = hue(a2p, b2);

  const dlp = l2 - l1;
  const dcp = c2p - c1p;

  const chromaProduct = c1p * c2p;
  let dhp = 0;
  if (chromaProduct !== 0) {
    dhp = h2p - h1p;
    if (dhp > 180) dhp -= 360;
    else if (dhp < -180) dhp += 360;
  }
  const dhpBig = 2 * Math.sqrt(chromaProduct) * Math.sin(toRadians(dhp) / 2);

  const lBar = (l1 + l2) / 2;
  const cBar = (c1p + c2p) / 2;

  let hBar: number;
  if (chromaProduct === 0) hBar = h1p + h2p;
  else if (Math.abs(h1p - h2p) <= 180) hBar = (h1p + h2p) / 2;
  else if (h1p + h2p < 360) hBar = (h1p + h2p + 360) / 2;
  else hBar = (h1p + h2p - 360) / 2;

  const t =
    1 -
    0.17 * Math.cos(toRadians(hBar - 30)) +
    0.24 * Math.cos(toRadians(2 * hBar)) +
    0.32 * Math.cos(toRadians(3 * hBar + 6)) -
    0.2 * Math.cos(toRadians(4 * hBar - 63));

  const cBarP7 = cBar ** 7;
  const rc = 2 * Math.sqrt(cBarP7 / (cBarP7 + 25 ** 7));
  const sl = 1 + (0.015 * (lBar - 50) ** 2) / Math.sqrt(20 + (lBar - 50) ** 2);
  const sc = 1 + 0.045 * cBar;
  const sh = 1 + 0.015 * cBar * t;
  const rt = -Math.sin(toRadians(2 * (30 * Math.exp(-(((hBar - 275) / 25) ** 2))))) * rc;

  const dlTerm = dlp / sl;
  const dcTerm = dcp / sc;
  const dhTerm = dhpBig / sh;
  return Math.sqrt(dlTerm ** 2 + dcTerm ** 2 + dhTerm ** 2 + rt * dcTerm * dhTerm);
}

/**
 * Perceptual distance between two hex colours, or null if either is unusable.
 *
 * A CIEDE2000 delta-E: ~1.0 is the threshold of a just-noticeable difference,
 * so these numbers are far smaller than the RGB distances they replaced and
 * cannot be compared against an RGB threshold.
 */
export function colorDistance(color1: string | undefined, color2: string | undefined): number | null {
  const lab1 = hexToLab(color1);
  const lab2 = hexToLab(color2);
  if (lab1 === null || lab2 === null) return null;
  return ciede2000(lab1, lab2);
}

/**
 * Whichever of two candidates looks closer to `required`.
 *
 * ⚠️ Among the spools the tolerance already admits, the FIRST in tray order used
 * to win — and tray order is where spools happen to sit in the AMS, not how
 * close they look. Eligibility is untouched (still `colorsAreSimilar`); this
 * only reorders candidates that already qualified.
 */
export function nearerColour<T extends { color?: string }>(
  incumbent: T | undefined,
  candidate: T,
  required: string | undefined,
): T {
  if (!incumbent) return candidate;
  const incumbentDistance = colorDistance(incumbent.color, required);
  const candidateDistance = colorDistance(candidate.color, required);
  if (candidateDistance === null) return incumbent;
  if (incumbentDistance === null) return candidate;
  return candidateDistance < incumbentDistance ? candidate : incumbent;
}
