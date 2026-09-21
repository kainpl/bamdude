import type { PrinterRoutingPreview, RoutingPreview } from '../../api/client';

/** Backend assignment verdict, shared by the button and silent submission. */
export type FeasibilityState = 'unknown' | 'blocked_now' | 'blocked_target' | 'ok';

/**
 * Local fallback vocabulary, checked against the backend by a drift guard.
 * Both preview paths normally supply their own localised refusal sentence.
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

/** The full backend assignment is authoritative; a populated UI array is not. */
export function targetFeasibility(target: PrinterRoutingPreview['targets'][number] | undefined): FeasibilityVerdict {
  if (!target || target.status === 'unknown') return UNKNOWN;
  if (target.status === 'compatible') return FEASIBLE;
  return {
    state: target.reason?.code === 'model_mismatch' ? 'blocked_target' : 'blocked_now',
    reason: target.reason ? { code: target.reason.code as FeasibilityCode, message: target.reason.message } : undefined,
  };
}

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
  // ⚠️ The accumulator starts EMPTY, not at `FEASIBLE`: a preview whose every
  // plate was skipped evaluated nothing, and "nothing was evaluated" is not
  // "everything is fine" — it is `unknown`, which is what an unevaluated farm
  // has always been told to answer. Seeding `UNKNOWN` instead would be wrong
  // the other way: `unknown` is WORSE than `ok` under `SEVERITY`, so it would
  // swallow every genuine `ok` and the function could never return one.
  let worst: FeasibilityVerdict | undefined;
  for (const plate of preview.plates) {
    // A plate that could not be read at all is already what `routingSourceReady`
    // holds the button on; it is not a compatibility answer.
    if (plate.status !== 'ok') continue;
    const verdict = platePreviewVerdict(plate, advisoryUnavailable);
    worst = worst ? worstVerdict(worst, verdict) : verdict;
  }
  return worst ?? UNKNOWN;
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
