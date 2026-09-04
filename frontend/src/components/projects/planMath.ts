import type { LinePlan, PlanAlternative, PlanPartCount, PlanRow } from '../../api/client';

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

/** `row plate_id → the plate the operator set that row to print`, which is
 *  either the row's own or one of its alternatives. A row absent from the map
 *  prints the plate the engine picked. */
export type ChosenByRow = Record<number, number>;

/** `row plate_id → plate_id → how many prints go on that file`. Only rows the
 *  operator has actually split appear; see `rowDistribution`. */
export type SplitByRow = Record<number, Record<number, number>>;

/** Everything a row's figures come from, whichever file it is set to.
 *
 *  It IS `PlanAlternative` — deliberately, because the whole point of an
 *  alternative is that it stands in for the row it hangs on. Named separately
 *  so a reader of `chosenPlate` is not told the row's own plate is an
 *  alternative to itself. */
export type ChosenPlate = PlanAlternative;

/**
 * The plate a row is currently set to print.
 *
 * ⚠️ **An unknown choice falls back to the row's own plate rather than to
 * nothing.** A choice is made against a plan that the farm can move under it at
 * any moment (`project-plan` is invalidated by every print event anywhere), and
 * while the block's reseed drops such a choice, the render between the two
 * would otherwise show a blank row — or, worse, enqueue a plate this row cannot
 * print.
 */
export function chosenPlate(row: PlanRow, chosen?: number): ChosenPlate {
  const alternative = chosen == null || chosen === row.plate_id ? undefined : row.alternatives.find((a) => a.plate_id === chosen);
  if (alternative) return alternative;
  return {
    plate_id: row.plate_id,
    library_file_id: row.library_file_id,
    plate_index: row.plate_index,
    filename: row.filename,
    printer_model: row.printer_model,
    print_time_seconds: row.print_time_seconds,
    filament_used_grams: row.filament_used_grams,
    cost: row.cost,
    time_unknown: row.time_unknown,
  };
}

/**
 * How one row's prints are distributed over files, as enqueue items would have
 * them: `[{ plate_id, count }]`, files with nothing on them left out.
 *
 * Without a split that is the whole count on the chosen file — one item, which
 * is what the plan block has always sent. With one it is several, and that is
 * the only way a line's work reaches two printer MODELS: the auto-queue routes
 * an item by its `target_model`, so a file only ever reaches the printers it
 * was sliced for.
 *
 * The order is the row's own — its plate first, then its alternatives as the
 * server sorted them — so the body a test reads back is stable.
 */
export function rowDistribution(
  row: PlanRow,
  count: number,
  chosen: number | undefined,
  split: Record<number, number> | undefined,
): { plate_id: number; count: number }[] {
  const clamp = (n: number) => Math.max(0, Math.trunc(n));
  if (!split) {
    const n = clamp(count);
    return n > 0 ? [{ plate_id: chosenPlate(row, chosen).plate_id, count: n }] : [];
  }
  return [row.plate_id, ...row.alternatives.map((a) => a.plate_id)]
    .map((plateId) => ({ plate_id: plateId, count: clamp(split[plateId] ?? 0) }))
    .filter((entry) => entry.count > 0);
}

/** What a split currently adds up to — the number that has to equal the row's
 *  count before it can be queued. */
export function splitTotal(split: Record<number, number> | undefined): number {
  return Object.values(split ?? {}).reduce((sum, n) => sum + Math.max(0, Math.trunc(n)), 0);
}

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
export function projectPlan(
  line: LinePlan,
  counts: Record<number, number>,
  yields: YieldByPlate,
  chosen: ChosenByRow = {},
): PlanProjection {
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

    // ⚠️ The FIGURES follow the file the row is set to, the YIELD never does:
    // an alternative is by construction a plate making the same counted parts,
    // so switching files changes what the farm spends and nothing about what
    // comes off it. A split (`rowDistribution`) is deliberately NOT projected
    // here — it is a routing decision taken at the moment of queueing, and the
    // row's figures stay those of the one file it is showing.
    const plate = chosenPlate(row, chosen[row.plate_id]);
    prints += n;
    if (plate.print_time_seconds == null || plate.time_unknown) timeUnknown = true;
    else seconds += n * plate.print_time_seconds;
    if (plate.filament_used_grams != null) grams += n * plate.filament_used_grams;
    if (plate.cost != null) {
      cost += n * plate.cost;
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
