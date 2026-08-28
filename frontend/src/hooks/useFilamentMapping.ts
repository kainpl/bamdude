import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getColorName } from '../utils/colors';
import {
  sortByRemainAscending,
  normalizeColor,
  normalizeColorForCompare,
  colorsAreSimilar,
  formatSlotLabel,
  getGlobalTrayId,
  matchLoadedExtruderTray,
  filamentTypesCompatible,
} from '../utils/amsHelpers';
import { api } from '../api/client';
import type { PrinterStatus } from '../api/client';

/**
 * Build loaded filaments list from printer status (non-hook version).
 * Extracts filaments from all AMS units (regular and HT) and external spool.
 */
export function buildLoadedFilaments(printerStatus: PrinterStatus | undefined): LoadedFilament[] {
  const filaments: LoadedFilament[] = [];
  const amsExtruderMap = printerStatus?.ams_extruder_map;
  // Dual-nozzle detection (#1257 — upstream `080176c6`). The backend always
  // emits a 2-entry `nozzles` array (default-stub second entry on single-nozzle
  // printers), so `length` alone is not a reliable signal. Real second-nozzle
  // hardware populates `nozzles[1].nozzle_diameter` from the MQTT
  // `right_nozzle_diameter` field; without that, the entry stays at its empty
  // default. Belt-and-braces: a populated `ams_extruder_map` (dual-nozzle with
  // AMS) and `vt_tray.length > 1` (only dual-nozzle hardware exposes multiple
  // external feeds) each independently imply dual-nozzle — keep them as
  // fallbacks for any firmware rev that surfaces one signal but not the other.
  // Affects X2D, H2D, X2 Pro running without AMS: previously they fell through
  // to `extruderId=undefined` for external spools and the nozzle-aware filter
  // rejected every candidate because `undefined !== 0/1`.
  const hasDualNozzle =
    Boolean(printerStatus?.nozzles?.[1]?.nozzle_diameter)
    || (amsExtruderMap && Object.keys(amsExtruderMap).length > 0)
    || (printerStatus?.vt_tray?.length ?? 0) > 1;

  // Add filaments from all AMS units (regular and HT)
  printerStatus?.ams?.forEach((amsUnit) => {
    const isHt = amsUnit.tray.length === 1; // AMS-HT has single tray
    amsUnit.tray.forEach((tray) => {
      if (tray.tray_type) {
        const color = normalizeColor(tray.tray_color);
        filaments.push({
          type: tray.tray_type,
          color,
          colorName: getColorName(color),
          amsId: amsUnit.id,
          trayId: tray.id,
          isHt,
          isExternal: false,
          label: formatSlotLabel(amsUnit.id, tray.id, isHt, false),
          globalTrayId: getGlobalTrayId(amsUnit.id, tray.id, false),
          trayInfoIdx: tray.tray_info_idx || '',
          traySubBrands: tray.tray_sub_brands || '',
          extruderId: amsExtruderMap?.[String(amsUnit.id)],
          remain: tray.remain ?? -1,
        });
      }
    });
  });

  // Add external spool(s) if loaded
  for (const extTray of printerStatus?.vt_tray ?? []) {
    if (extTray.tray_type) {
      const color = normalizeColor(extTray.tray_color);
      const trayId = extTray.id ?? 254;
      const hasDualExternal = (printerStatus?.vt_tray?.length ?? 0) > 1;
      filaments.push({
        type: extTray.tray_type,
        color,
        colorName: getColorName(color),
        amsId: -1,
        trayId: trayId - 254,
        isHt: false,
        isExternal: true,
        label: hasDualExternal ? (trayId === 254 ? 'Ext-L' : 'Ext-R') : 'External',
        globalTrayId: trayId,
        trayInfoIdx: extTray.tray_info_idx || '',
        traySubBrands: extTray.tray_sub_brands || '',
        extruderId: hasDualNozzle ? (255 - trayId) : undefined,
        remain: extTray.remain ?? -1,
      });
    }
  }

  return filaments;
}

/**
 * Compute AMS mapping for a printer given filament requirements and printer status.
 * This is a non-hook version that can be called imperatively (e.g., in a loop for multiple printers).
 *
 * Priority: unique tray_info_idx match > exact color match > similar color match > type-only match
 *
 * The tray_info_idx is a filament type identifier stored in the 3MF file when the user
 * slices (e.g., "GFA00" for generic PLA, "P4d64437" for custom presets). If the same
 * tray_info_idx appears in only ONE available tray, we use that tray. If multiple trays
 * have the same tray_info_idx (e.g., two spools of generic PLA), we fall back to color
 * matching among those trays.
 *
 * @param filamentReqs - Required filaments from the 3MF file
 * @param printerStatus - Current printer status with AMS information
 * @returns AMS mapping array or undefined if no mapping needed
 */
export function computeAmsMapping(
  filamentReqs: { filaments: FilamentRequirement[] } | undefined,
  printerStatus: PrinterStatus | undefined,
  preferLowest = false
): number[] | undefined {
  const loadedFilaments = buildLoadedFilaments(printerStatus);
  if (loadedFilaments.length === 0) return undefined;

  // FTS routes any AMS slot to any extruder, so per-nozzle slot restriction
  // doesn't apply when it's installed (upstream #1162).
  const ftsActive = printerStatus?.fila_switch?.installed === true;

  // No manual overrides on this path — it maps a printer the user is not looking
  // at (per-printer fan-out), so there is no panel in which to override a slot.
  return buildAmsMapping(
    buildFilamentComparison(
      filamentReqs,
      loadedFilaments,
      {},
      ftsActive,
      printerStatus?.tray_now,
      preferLowest,
    )
  );
}

/**
 * Represents a loaded filament in the printer's AMS/HT/External spool holder.
 */
export interface LoadedFilament {
  type: string;
  color: string;
  colorName: string;
  amsId: number;
  trayId: number;
  isHt: boolean;
  isExternal: boolean;
  label: string;
  globalTrayId: number;
  /** Unique spool identifier (e.g., "GFA00", "P4d64437") */
  trayInfoIdx?: string;
  /** Filament subtype name (e.g., "PLA Basic", "PLA Matte", "PETG HF") */
  traySubBrands?: string;
  /** Extruder ID for dual-nozzle printers (0=right, 1=left) */
  extruderId?: number;
  /**
   * Remaining filament, 0-100. ``-1`` when the printer cannot measure it
   * (no RFID / calibration off). Only consumed by the "prefer lowest
   * remaining filament" auto-match tiebreaker.
   */
  remain?: number;
}

/**
 * Represents a required filament from the 3MF file.
 */
export interface FilamentRequirement {
  slot_id: number;
  type: string;
  color: string;
  used_grams: number;
  /** Unique spool identifier from slicing (e.g., "GFA00", "P4d64437") */
  tray_info_idx?: string;
  /** Target nozzle for dual-nozzle printers (0=right, 1=left) */
  nozzle_id?: number;
}

/**
 * Status of filament comparison between required and loaded.
 */
export type FilamentStatus = 'match' | 'type_only' | 'mismatch' | 'empty';

/**
 * Result of comparing a required filament with loaded filaments.
 */
export interface FilamentComparison extends FilamentRequirement {
  loaded: LoadedFilament | undefined;
  hasFilament: boolean;
  typeMatch: boolean;
  colorMatch: boolean;
  status: FilamentStatus;
  isManual: boolean;
}

interface FilamentRequirementsResponse {
  filaments: FilamentRequirement[];
}

interface UseFilamentMappingResult {
  /** List of all filaments loaded in the printer */
  loadedFilaments: LoadedFilament[];
  /** Comparison results for each required filament */
  filamentComparison: FilamentComparison[];
  /** AMS mapping array for the print command */
  amsMapping: number[] | undefined;
  /** Whether any required filament type is not loaded */
  hasTypeMismatch: boolean;
  /** Whether any required filament has a color mismatch */
  hasColorMismatch: boolean;
}

/**
 * Hook to build loaded filaments list from printer status.
 * Extracts filaments from all AMS units (regular and HT) and external spool.
 *
 * Internal — only ``useFilamentMapping`` consumes it; nobody outside this
 * module needs the bare loaded-list view.
 */
function useLoadedFilaments(
  printerStatus: PrinterStatus | undefined
): LoadedFilament[] {
  return useMemo(() => {
    return buildLoadedFilaments(printerStatus);
  }, [printerStatus]);
}

/**
 * Hook to compare required filaments with loaded filaments and build AMS mapping.
 * Handles both auto-matching and manual overrides.
 *
 * @param filamentReqs - Required filaments from the 3MF file
 * @param printerStatus - Current printer status with AMS information
 * @param manualMappings - Manual slot overrides (slot_id -> globalTrayId)
 */
/**
 * Compare required filaments with loaded filaments (non-hook version).
 *
 * Tray assignment is **stateful across the list** - a tray matched to one slot is
 * not offered to the next - so this must run over exactly the slots of ONE print,
 * never a union of several plates. Two plates that share a colour on different
 * slots would otherwise compete for the same tray and one would fall through to
 * a worse match, or to none (upstream #2551).
 *
 * Extracted so the per-plate path, the per-printer fan-out (`computeAmsMapping`)
 * and the panel hook (`useFilamentMapping`) all run the identical matcher.
 */
export function buildFilamentComparison(
  filamentReqs: { filaments: FilamentRequirement[] } | undefined,
  loadedFilaments: LoadedFilament[],
  manualMappings: Record<number, number>,
  ftsActive = false,
  trayNow?: number,
  preferLowest = false,
): FilamentComparison[] {
  if (!filamentReqs?.filaments || filamentReqs.filaments.length === 0) return [];

  // One-colour print: default the auto-mapping to the spool already loaded
  // in the extruder (tray_now) rather than slot 0. See matchLoadedExtruderTray.
  const isSingleFilament = filamentReqs.filaments.length === 1;

  // Track which trays have been assigned to avoid duplicates
  // First, mark all manually assigned trays as used
  const usedTrayIds = new Set<number>(Object.values(manualMappings));

  return filamentReqs.filaments.map((req) => {
    const slotId = req.slot_id || 0;

    // Check if there's a manual override for this slot
    if (slotId > 0 && manualMappings[slotId] !== undefined) {
      const manualTrayId = manualMappings[slotId];
      const manualLoaded = loadedFilaments.find((f) => f.globalTrayId === manualTrayId);

      if (manualLoaded) {
        const typeMatch = filamentTypesCompatible(manualLoaded.type, req.type);
        const colorMatch =
          normalizeColorForCompare(manualLoaded.color) === normalizeColorForCompare(req.color) ||
          colorsAreSimilar(manualLoaded.color, req.color);

        let status: FilamentStatus;
        if (typeMatch && colorMatch) {
          status = 'match';
        } else if (typeMatch) {
          status = 'type_only';
        } else {
          status = 'mismatch';
        }

        return {
          ...req,
          loaded: manualLoaded,
          hasFilament: true,
          typeMatch,
          colorMatch,
          status,
          isManual: true,
        };
      }
    }

    // Auto-match: Find a loaded filament
    // Priority: unique tray_info_idx match > exact color match > similar color match > type-only match
    // IMPORTANT: Exclude trays that are already assigned (manually or auto)
    const reqTrayInfoIdx = req.tray_info_idx || '';

    // Get available trays (not already used)
    let available = loadedFilaments.filter((f) => !usedTrayIds.has(f.globalTrayId));

    // Nozzle-aware filtering: restrict to trays on the correct nozzle.
    // This is a hard filter - cross-nozzle assignment causes print failures.
    // Skip when an FTS is installed: it can route any slot to either extruder.
    if (req.nozzle_id != null && !ftsActive) {
      available = available.filter((f) => f.extruderId === req.nozzle_id);
    }

    // "Prefer lowest remaining filament": drain the emptiest compatible spool
    // first. Sorting the candidate pool rather than each match tier keeps the
    // priority order above intact — this only ever breaks ties WITHIN a tier
    // and can never promote a worse match. Same key the backend uses in
    // auto_queue_ams.py, so a mapping pinned here and one computed there agree.
    if (preferLowest) {
      available = sortByRemainAscending(available);
    }

    const extruderTray = isSingleFilament
      ? matchLoadedExtruderTray(req, available, trayNow)
      : undefined;

    let exactMatch: LoadedFilament | undefined;
    let similarMatch: LoadedFilament | undefined;
    let typeOnlyMatch: LoadedFilament | undefined;

    // Trays carrying the slicer's tray_info_idx.
    //
    // A unique idx match used to be taken as definitive, on the premise
    // "same preset = same spool = same colour" (#2687). The premise is false:
    // an idx names the filament VARIANT, not a spool — GFA00 is PLA Basic,
    // GFA01 PLA Matte, GFA17 PLA Translucent, in every colour Bambu sells. With
    // one Matte spool loaded, every Matte requirement matched it whatever colour
    // it was, and the comparison below never ran — so the panel showed "(Ready)"
    // with a green tick for red-required-on-green-loaded, while picking that
    // same tray by hand reported the mismatch honestly.
    //
    // The asymmetry gave it away: the >1 branch already compared colour. Only
    // uniqueness was trusted to imply it. Every idx candidate is classified the
    // same way now, so the variant still decides *selection* among colour-
    // agreeing trays (#2650 — Basic is not Matte) without deciding the verdict.
    let idxTypeOnly: LoadedFilament | undefined;
    if (reqTrayInfoIdx) {
      const idxMatches = available.filter((f) => f.trayInfoIdx === reqTrayInfoIdx);
      exactMatch = idxMatches.find(
        (f) =>
          filamentTypesCompatible(f.type, req.type) &&
          normalizeColorForCompare(f.color) === normalizeColorForCompare(req.color)
      );
      if (!exactMatch) {
        similarMatch = idxMatches.find(
          (f) =>
            filamentTypesCompatible(f.type, req.type) &&
            colorsAreSimilar(f.color, req.color)
        );
      }
      if (!exactMatch && !similarMatch) {
        // Right variant, wrong colour. Held back as a last resort so it cannot
        // block the search below from finding a correctly-coloured tray.
        idxTypeOnly = idxMatches.find(
          (f) => filamentTypesCompatible(f.type, req.type)
        );
      }
    }

    // If no idx match, do standard type/color matching on all available trays
    if (!exactMatch && !similarMatch && !typeOnlyMatch) {
      exactMatch = available.find(
        (f) =>
          filamentTypesCompatible(f.type, req.type) &&
          normalizeColorForCompare(f.color) === normalizeColorForCompare(req.color)
      );
      if (!exactMatch) {
        similarMatch = available.find(
          (f) =>
            filamentTypesCompatible(f.type, req.type) &&
            colorsAreSimilar(f.color, req.color)
        );
      }
      if (!exactMatch && !similarMatch) {
        typeOnlyMatch = available.find(
          (f) => filamentTypesCompatible(f.type, req.type)
        );
      }
    }

    const loaded =
      extruderTray || exactMatch || similarMatch || typeOnlyMatch || idxTypeOnly || undefined;

    // Mark this tray as used so it won't be assigned to another slot
    if (loaded) {
      usedTrayIds.add(loaded.globalTrayId);
    }

    const hasFilament = !!loaded;
    // Every match path (cascade tiers + extruderTray) requires a
    // type-compatible tray, so anything loaded is a type match.
    const typeMatch = hasFilament;
    // #2687: judge the colour on the tray we actually picked, never on which
    // branch found it. A requirement with no colour at all is not a mismatch —
    // the 3MF simply did not ask for one, and any loaded colour satisfies it.
    const requiredColor = normalizeColorForCompare(req.color);
    const colorMatch =
      hasFilament &&
      (!requiredColor ||
        normalizeColorForCompare(loaded?.color) === requiredColor ||
        colorsAreSimilar(loaded?.color, req.color));

    // Status: match (type+colour), type_only (type ok, colour off), mismatch (type not found)
    const status: FilamentStatus = !hasFilament ? 'mismatch' : colorMatch ? 'match' : 'type_only';

    return {
      ...req,
      loaded,
      hasFilament,
      typeMatch,
      colorMatch,
      status,
      isManual: false,
    };
  });
}

/**
 * Build the AMS mapping array the print command expects from a comparison list.
 *
 * Format: position = `slot_id - 1` (0-indexed), value = global tray ID, or -1
 * for a slot with no tray. Returns `undefined` when there is nothing to map, so
 * callers can distinguish "no mapping" from "a mapping of nothing".
 */
export function buildAmsMapping(comparisons: FilamentComparison[]): number[] | undefined {
  if (comparisons.length === 0) return undefined;

  // Find the max slot_id to determine array size
  const maxSlotId = Math.max(...comparisons.map((f) => f.slot_id || 0));
  if (maxSlotId <= 0) return undefined;

  // Create array with -1 for all positions
  const mapping = new Array(maxSlotId).fill(-1);

  // Fill in tray IDs at correct positions (slot_id - 1)
  comparisons.forEach((f) => {
    if (f.slot_id && f.slot_id > 0) {
      mapping[f.slot_id - 1] = f.loaded?.globalTrayId ?? -1;
    }
  });

  return mapping;
}

export function useFilamentMapping(
  filamentReqs: FilamentRequirementsResponse | undefined,
  printerStatus: PrinterStatus | undefined,
  manualMappings: Record<number, number>
): UseFilamentMappingResult {
  const loadedFilaments = useLoadedFilaments(printerStatus);
  // The dispatcher will not re-derive a mapping the dialog already pinned
  // (`_ensure_ams_mapping` returns early on a resolved one so a manual override
  // survives), so "prefer lowest remaining filament" has to be honoured HERE or
  // it is honoured nowhere on this path. Reads the ['settings'] query the modal
  // already has cached.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const preferLowest = settings?.prefer_lowest_filament ?? false;

  // FTS routes any AMS slot to any extruder, so per-nozzle slot restriction
  // doesn't apply when it's installed (upstream #1162).
  const ftsActive = printerStatus?.fila_switch?.installed === true;
  const trayNow = printerStatus?.tray_now;

  const filamentComparison = useMemo(
    () => buildFilamentComparison(filamentReqs, loadedFilaments, manualMappings, ftsActive, trayNow, preferLowest),
    [filamentReqs, loadedFilaments, manualMappings, ftsActive, trayNow, preferLowest]
  );

  // Build AMS mapping from matched filaments
  const amsMapping = useMemo(() => buildAmsMapping(filamentComparison), [filamentComparison]);

  const hasTypeMismatch = filamentComparison.some((f) => f.status === 'mismatch');
  const hasColorMismatch = filamentComparison.some((f) => f.status === 'type_only');

  return {
    loadedFilaments,
    filamentComparison,
    amsMapping,
    hasTypeMismatch,
    hasColorMismatch,
  };
}
