/**
 * How long until the farm is free — wall-clock, not a sum of print times.
 *
 * The stats bar used to add up every pending item's duration, which answers a
 * question nobody asked: with four printers and eight queued jobs it reported
 * 88 minutes for work that finishes in 22. It also counted only the per-printer
 * tier, so jobs still waiting in Auto-Queue's staging area contributed nothing,
 * and it ignored the prints already running.
 *
 * This is a greedy makespan estimate — the textbook list-scheduling
 * approximation, and about as much precision as a queue of estimates deserves:
 *
 *   1. Each printer starts with what it already owes: the remainder of the print
 *      in progress, plus every item sitting in its own queue.
 *   2. Staged Auto-Queue jobs are then handed out longest-first, each to the
 *      eligible printer that is free soonest. Longest-first matters — placing a
 *      three-hour job last strands it after everything else and overstates the
 *      finish by hours.
 *   3. The answer is the printer that finishes last.
 *
 * Eligibility respects ``target_model``, because Auto-Queue does: a job for an
 * A1 Mini cannot shorten a P1S's day. A job with no target model never routes at
 * all (see the auto-queue notes), so it is left out rather than silently
 * assigned to someone.
 */

/** Same fallback the timeline uses for an item whose duration is unknown. */
const DEFAULT_DURATION_SEC = 2 * 60 * 60;

export interface EstimateQueue {
  printer_id: number;
  printer_model?: string | null;
  status: string;
}

export interface EstimateItem {
  printer_id?: number | null;
  print_time_seconds?: number | null;
  started_at?: string | null;
}

export interface EstimateStagedItem {
  target_model?: string | null;
  print_time_seconds?: number | null;
}

export interface EstimateInput {
  queues: EstimateQueue[] | undefined;
  /** Per-printer items with status ``pending``. */
  pendingItems: EstimateItem[] | undefined;
  /** Per-printer items with status ``printing``. */
  printingItems: EstimateItem[] | undefined;
  /** Auto-queue items with status ``pending`` — not yet routed anywhere. */
  stagedItems: EstimateStagedItem[] | undefined;
  /** Epoch ms. Injected so tests do not depend on the clock. */
  now: number;
}

function duration(seconds: number | null | undefined): number {
  return seconds && seconds > 0 ? seconds : DEFAULT_DURATION_SEC;
}

function sameModel(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

/** A queue that refuses new work must not appear to absorb any. */
function acceptsNewWork(queue: EstimateQueue): boolean {
  return queue.status !== 'paused' && queue.status !== 'error';
}

export function estimateWallClockSeconds({
  queues,
  pendingItems,
  printingItems,
  stagedItems,
  now,
}: EstimateInput): number {
  if (!queues?.length) return 0;

  const load = new Map<number, number>();
  for (const q of queues) load.set(q.printer_id, 0);

  const add = (printerId: number | null | undefined, seconds: number) => {
    if (printerId == null || !load.has(printerId)) return;
    load.set(printerId, (load.get(printerId) ?? 0) + seconds);
  };

  // 1. What is already on each printer.
  for (const item of printingItems ?? []) {
    const total = duration(item.print_time_seconds);
    const startedAt = item.started_at ? Date.parse(item.started_at) : NaN;
    // An unparseable or missing start is treated as "just began" rather than
    // "already done" — overstating slightly beats reporting a farm as free
    // while it is still printing.
    const elapsed = Number.isFinite(startedAt) ? Math.max(0, (now - startedAt) / 1000) : 0;
    add(item.printer_id, Math.max(0, total - elapsed));
  }
  for (const item of pendingItems ?? []) {
    add(item.printer_id, duration(item.print_time_seconds));
  }

  // 2. Hand out the staging area, longest job first.
  const staged = [...(stagedItems ?? [])].sort(
    (a, b) => duration(b.print_time_seconds) - duration(a.print_time_seconds),
  );
  const open = queues.filter(acceptsNewWork);
  for (const item of staged) {
    const eligible = open.filter(q => sameModel(q.printer_model, item.target_model));
    if (!eligible.length) continue;
    const target = eligible.reduce((best, q) =>
      (load.get(q.printer_id) ?? 0) < (load.get(best.printer_id) ?? 0) ? q : best,
    );
    add(target.printer_id, duration(item.print_time_seconds));
  }

  // 3. The last printer to finish is when the farm is free.
  return Math.max(0, ...load.values());
}
