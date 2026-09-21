import type { RoutingPreview } from '../../api/client';
import type { FilamentRequirement, LoadedFilament } from '../../hooks/useFilamentMapping';
import { filamentRequirementMatches, filamentTypesCompatible, normalizeColor } from '../../utils/amsHelpers';
import { getColorName } from '../../utils/colors';

/**
 * Can what is selected print this plate as the trays stand right now?
 *
 * ⚠️ **The verdict is taken from the full assignment under the current policy,
 * never from one word of channel status** (spec Д6). Both of the obvious
 * shortcuts are wrong, and were the first design:
 *
 * - `type_only` does NOT mean "a spool was found": under a strict colour no
 *   source is picked at all and the mapping keeps its `-1`;
 * - `mismatch` does NOT mean "the material is missing":
 *   `filamentRequirementMatches` answers `false` for a pure profile veto
 *   (`strict_profile_match` with two different ids) while the required material
 *   is sitting in the tray.
 *
 * So for a chosen printer the question asked is the mapping this dialog would
 * SEND — a `-1` in a used channel — and the status words are consulted only to
 * NAME the reason. For auto mode the answer is the backend resolver's, carried
 * in the routing preview.
 */
export type FeasibilityState = 'unknown' | 'blocked_now' | 'blocked_target' | 'ok';

/**
 * The refusal vocabulary, mirroring `backend/app/data/filament_routing_*.json`
 * one for one. Ruling R3: no endpoint hands out a per-printer sentence for a
 * printer the operator picked here, so the sentences are mirrored as frontend
 * locale keys under `filamentRouting.feasibility.reason.*` — two vocabularies
 * to keep in step, and the codes are the seam.
 */
export type FeasibilityCode =
  | 'material_mismatch'
  | 'variant_mismatch'
  | 'color_mismatch'
  | 'nozzle_mismatch'
  | 'distinct_sources_required'
  | 'model_mismatch';

export interface FeasibilityReason {
  code: FeasibilityCode;
  /** The used channel this is about; absent for a whole-target refusal. */
  slot?: number;
  wanted?: string;
  /** The trays, as a sentence. `''` means "known to be empty", absent means "not asked". */
  loaded?: string;
  /**
   * The backend's own already-localised sentence, when the verdict came from
   * the routing preview. Rendered as is — re-deriving it here would be a third
   * vocabulary.
   */
  message?: string;
}

export interface FeasibilityVerdict {
  state: FeasibilityState;
  reason?: FeasibilityReason;
}

export const FEASIBLE: FeasibilityVerdict = { state: 'ok' };
/** Offline, telemetry in flight, an evaluation that could not cover everything. Never blocks. */
export const UNKNOWN: FeasibilityVerdict = { state: 'unknown' };

/**
 * Worst wins, and "worst" is: a target that is wrong > a plate that cannot print
 * now > not knowing > fine. A submission writes one row per (plate, printer),
 * so one pair that cannot print is a row that will sit there — and an unknown
 * beside a proven refusal does not rescue it.
 */
const SEVERITY: Record<FeasibilityState, number> = { ok: 0, unknown: 1, blocked_now: 2, blocked_target: 3 };

export function worstVerdict(a: FeasibilityVerdict, b: FeasibilityVerdict): FeasibilityVerdict {
  return SEVERITY[b.state] > SEVERITY[a.state] ? b : a;
}

/** Sources listed in a refusal before it stops naming them one by one (backend `_LISTED_SOURCES`). */
const LISTED_SOURCES = 4;

/**
 * How one filament is named in a refusal — the mirror of the backend's
 * `describe_source`, plus the colour, which the operator needs here because a
 * colour refusal is one of the five answers and "PETG; loaded: PETG" would name
 * nothing at all.
 */
export function describeSource(material?: string, variant?: string, color?: string): string {
  const head = variant ? `${material || '?'} (${variant})` : material || '?';
  const named = color ? getColorName(normalizeColor(color)) : '';
  return named ? `${head} ${named}` : head;
}

function describeRequirement(req: FilamentRequirement): string {
  // With a base-material match allowed the profile id is not part of the
  // question, so naming it in the refusal would point at the wrong thing.
  return describeSource(req.type, req.ignore_profile ? undefined : req.tray_info_idx, req.color);
}

function describeLoaded(loaded: LoadedFilament[]): string {
  const listed = loaded.map((f) => describeSource(f.type, f.trayInfoIdx, f.color));
  const head = listed.slice(0, LISTED_SOURCES).join(', ');
  return listed.length > LISTED_SOURCES ? `${head}, +${listed.length - LISTED_SOURCES}` : head;
}

/**
 * Why this used channel came out of the mapping with no source.
 *
 * ⚠️ Naming only. The mapping has already decided that it is unprintable; this
 * decides which of the five sentences the operator reads, and a wrong guess
 * here is a misleading message, never a wrong button.
 *
 * `taken` are the trays the other channels of the SAME plate already hold —
 * assignment is stateful, so a channel can be starved by its neighbour while
 * the tray it wants is in plain sight, and "no compatible source" would be a
 * lie about it.
 */
function channelReason(
  req: FilamentRequirement,
  loaded: LoadedFilament[],
  ftsActive: boolean,
  taken: Set<number>,
): FeasibilityReason {
  const free = loaded.filter((f) => !taken.has(f.globalTrayId));
  // The same hard filter `buildFilamentComparison` applies: an FTS routes any
  // slot to either extruder, so it lifts the restriction entirely.
  const onNozzle = req.nozzle_id != null && !ftsActive ? free.filter((f) => f.extruderId === req.nozzle_id) : free;
  const matchesProfile = (f: LoadedFilament) => filamentRequirementMatches(req, f);
  const matchesMaterial = (f: LoadedFilament) => filamentTypesCompatible(f.type, req.type);

  let code: FeasibilityCode;
  if (loaded.length === 0) {
    code = 'material_mismatch';
  } else if (loaded.some(matchesProfile) && !free.some(matchesProfile)) {
    code = 'distinct_sources_required';
  } else if (free.some(matchesMaterial) && !onNozzle.some(matchesMaterial)) {
    code = 'nozzle_mismatch';
  } else if (onNozzle.some(matchesProfile)) {
    // Material and profile both agree, so the only thing left to have refused
    // the tray is the colour.
    code = 'color_mismatch';
  } else if (onNozzle.some(matchesMaterial)) {
    code = 'variant_mismatch';
  } else {
    code = 'material_mismatch';
  }

  return {
    code,
    slot: req.slot_id,
    wanted: describeRequirement(req),
    // Always present, `''` included: an empty AMS is an answer ("nothing is
    // loaded"), not an unasked question.
    loaded: describeLoaded(loaded),
  };
}

export interface PrinterFeasibilityInput {
  /** The plate's channels, already under the dialog's routing policy. */
  requirements: FilamentRequirement[];
  /** The mapping this dialog would send for this (plate, printer) pair. */
  mapping: number[] | undefined;
  loaded: LoadedFilament[];
  ftsActive: boolean;
}

/**
 * One (plate, printer) pair, judged by the mapping and nothing else.
 *
 * A plate with no used channel is printable by definition. A mapping that does
 * not exist while channels do is not a refusal — it is the fan-out case, where
 * the scheduler maps each plate against the printer it picks, and this dialog
 * has no answer to give.
 */
export function printerFeasibility({
  requirements,
  mapping,
  loaded,
  ftsActive,
}: PrinterFeasibilityInput): FeasibilityVerdict {
  const used = requirements.filter((req) => (req.slot_id ?? 0) > 0);
  if (used.length === 0) return FEASIBLE;
  if (!mapping) return UNKNOWN;

  const taken = new Set(mapping.filter((trayId) => Number.isFinite(trayId) && trayId >= 0));
  for (const req of used) {
    const trayId = mapping[req.slot_id - 1];
    if (Number.isFinite(trayId) && trayId >= 0) continue;
    return {
      state: 'blocked_now',
      reason: channelReason(req, loaded, ftsActive, taken),
    };
  }
  return FEASIBLE;
}

/** The refusal a group of printers agreed on most often, for the one line beside the button. */
function previewReason(groups: RoutingPreview['plates'][number]['groups']): FeasibilityReason | undefined {
  const tally = new Map<string, { message: string; count: number }>();
  for (const group of groups) {
    if (group.incompatible <= 0) continue;
    for (const reason of group.reasons) {
      const seen = tally.get(reason.code);
      if (seen) seen.count += reason.count;
      else tally.set(reason.code, { message: reason.message, count: reason.count });
    }
  }
  let best: { code: string; message: string; count: number } | undefined;
  for (const [code, entry] of tally) {
    if (!best || entry.count > best.count) best = { code, ...entry };
  }
  if (!best) return undefined;
  // The code vocabulary is the backend's and is wider than ours; the sentence
  // it already localised is what gets rendered, so an unmapped code is harmless.
  return { code: best.code as FeasibilityCode, message: best.message };
}

/**
 * Auto mode, from the routing preview — three-valued on purpose (spec Д6).
 *
 * ⚠️ **Zero compatible is not proof of incompatibility.** With every printer
 * offline the compatible counter is zero too, and the honest answer is "we do
 * not know". Worse, the counters can look complete when the evaluation is not:
 * a printer whose snapshot failed is SKIPPED rather than counted as unknown,
 * and the response says so once, at the top, with `advisory_unavailable`.
 * Hence the fourth conjunct — without it, one incompatible printer beside one
 * skipped printer would block.
 */
export function autoFeasibility(preview: RoutingPreview | undefined): FeasibilityVerdict {
  if (!preview) return UNKNOWN;
  const advisoryUnavailable = preview.advisory_unavailable === true;
  let worst = FEASIBLE;
  for (const plate of preview.plates) {
    // A plate that could not be read at all is already what `routingSourceReady`
    // holds the button on; it is not a compatibility answer.
    if (plate.status !== 'ok') continue;
    worst = worstVerdict(worst, platePreviewVerdict(plate, advisoryUnavailable));
  }
  return worst;
}

function platePreviewVerdict(
  plate: RoutingPreview['plates'][number],
  advisoryUnavailable: boolean,
): FeasibilityVerdict {
  const total = (key: 'compatible' | 'unknown' | 'incompatible') =>
    plate.groups.reduce((sum, group) => sum + (group[key] ?? 0), 0);
  const incompatible = total('incompatible');
  const compatible = total('compatible');
  const unknown = total('unknown');

  if (incompatible > 0 && compatible === 0 && unknown === 0 && !advisoryUnavailable) {
    return { state: 'blocked_now', reason: previewReason(plate.groups) };
  }
  if (advisoryUnavailable || unknown > 0) return UNKNOWN;
  // Zero READY with something compatible is an ordinary busy farm — the job
  // waits, which is what a queue is for.
  if (compatible > 0) return FEASIBLE;
  // Nothing evaluated at all: no printer of this model, nothing to conclude.
  return UNKNOWN;
}
