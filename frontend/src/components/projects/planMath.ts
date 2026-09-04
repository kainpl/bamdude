import type { LinePlan, PlanPartCount } from '../../api/client';

/**
 * The plan's what-if arithmetic — the ONLY place the client computes anything
 * about an order (pass-3 design decision 9).
 *
 * ⚠️ **These are projections of a plan the operator is shaping, not order
 * figures.** Everything the order *reports* — need, usable, in progress,
 * remaining, surplus — stays server-computed and displayed verbatim (pass-2
 * decision 8). What this file answers is the different question "what would
 * happen if I printed these counts", asked of a plan that has not been sent
 * anywhere yet. Nothing here is ever written back or shown as an order figure.
 *
 * ⚠️ **`PlanRow.useful` is NOT the plate's per-print yield.** The engine
 * aggregates it over the row's prints and clips each print to what was still
 * outstanding at that pick, so a row of 1 print against 5 outstanding parts
 * reports `useful = 5` for a plate that makes 10. Dividing it back is
 * impossible — the clipped print is indistinguishable from a full one. So the
 * per-print yields come in separately, from the product's plate recipes
 * (`api.getProductPlates`), restricted to the parts this line counts; without
 * them the surplus is reported as unknown rather than guessed.
 */
export interface PlanProjection {
  /** `null` while some counted row's plate yield is unknown — the caller then
   *  shows the server's `surplus_after` rather than a number it made up. */
  surplusAfter: PlanPartCount[] | null;
  prints: number;
  /** `null` as soon as one counted row has no estimate: a partial sum would
   *  read as a promise. Mirrors `plan_engine._totals`. */
  seconds: number | null;
  /** A row with no figure contributes nothing rather than voiding the column —
   *  again the engine's own rule. */
  grams: number;
  /** `null` when no counted row could be costed (no farm filament rate, or no
   *  weight to price), never 0.00 — that would read as "this plan is free". */
  cost: number | null;
}

/** `plate_id → parts made by ONE print of that plate, toward this line. */
export type YieldByPlate = Record<number, PlanPartCount[]>;

/**
 * What the operator typed in a count box, as a count.
 *
 * ⚠️ **`Number(value) || 0` is not enough, and the way it fails is silent.**
 * `Number('1e999')` is `Infinity`, which is truthy, so it went straight through
 * — and the row then rendered `Infinityh NaMm` while every total below it went
 * to `NaN`. A value the browser cannot represent is not an edit at all, so the
 * previous count stands; the same goes for anything that is not a number.
 *
 * An EMPTY box is the one non-number that does mean something: it is zero, not
 * a refusal, or the operator could never clear the field to type a new figure.
 */
export function parseCount(value: string, previous: number): number {
  if (value.trim() === '') return 0;
  const n = Number(value);
  if (!Number.isFinite(n)) return previous;
  return Math.max(0, Math.trunc(n));
}

/**
 * Project one line's plan at the counts the operator currently has on screen.
 *
 * `counts` overrides a row's server count by `plate_id`; a row absent from it
 * keeps the count the server planned. A count below zero is floored at zero —
 * a row at zero is kept on screen but contributes nothing, to totals or to
 * surplus, and is excluded from the enqueue body.
 */
export function projectPlan(line: LinePlan, counts: Record<number, number>, yields: YieldByPlate): PlanProjection {
  let prints = 0;
  let seconds = 0;
  let timeUnknown = false;
  let grams = 0;
  let cost = 0;
  let costed = false;
  let yieldsKnown = true;

  const made = new Map<number, number>();
  const names = new Map<number, string>();

  for (const row of line.rows) {
    const n = Math.max(0, Math.trunc(counts[row.plate_id] ?? row.count));
    if (n === 0) continue;

    prints += n;
    if (row.print_time_seconds == null || row.time_unknown) timeUnknown = true;
    else seconds += n * row.print_time_seconds;
    if (row.filament_used_grams != null) grams += n * row.filament_used_grams;
    if (row.cost != null) {
      cost += n * row.cost;
      costed = true;
    }

    const perPrint = yields[row.plate_id];
    if (perPrint === undefined) {
      yieldsKnown = false;
      continue;
    }
    for (const entry of perPrint) {
      made.set(entry.part_id, (made.get(entry.part_id) ?? 0) + n * entry.count);
      names.set(entry.part_id, entry.name);
    }
  }

  // `outstanding_before` carries non-zero entries only, so a counted part the
  // line no longer needs is simply absent from it — and everything the plan
  // makes of it is surplus. That is the engine's own loop, which walks the
  // outstanding map INCLUDING its zeros.
  const want = new Map(line.outstanding_before.map((p) => [p.part_id, p.count]));
  for (const p of line.outstanding_before) names.set(p.part_id, p.name);

  const surplusAfter: PlanPartCount[] = [];
  for (const [partId, total] of [...made.entries()].sort((a, b) => a[0] - b[0])) {
    const over = total - (want.get(partId) ?? 0);
    if (over > 0) surplusAfter.push({ part_id: partId, name: names.get(partId) ?? '', count: over });
  }

  return {
    surplusAfter: yieldsKnown ? surplusAfter : null,
    prints,
    seconds: timeUnknown ? null : seconds,
    grams: Math.round(grams * 100) / 100,
    cost: costed ? Math.round(cost * 100) / 100 : null,
  };
}
