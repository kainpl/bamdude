/**
 * What the UI says about a job's own copy of its file (spec §6/§8/§10, m173).
 *
 * Once a file is accepted into a queue, BamDude keeps its own copy under the
 * data directory and the job prints THAT — so the operator has two new
 * questions, and both are answered from here so that every screen answers them
 * with the same words:
 *
 *   1. *Is this job self-contained?* — `source_storage` on the queue and
 *      auto-queue rows, rendered by `QueueSourceIndicator`.
 *   2. *Did my add actually happen?* — the refusal taxonomy below, rendered by
 *      the print dialog.
 *
 * ⚠️ **Nothing here matches an error MESSAGE.** The server already translates
 * its own prose, and branching on a sentence breaks the moment the language
 * changes (CLAUDE.md: never branch on `ApiError.message`). The code is the
 * contract; the sentence is ours to write, because only the client knows the
 * operator was ADDING and therefore what "it did not happen" means for them.
 */

import { ApiError, type QueueSourceStorage } from '../api/client';

export type { QueueSourceStorage };

/** The four states worth a mark on a row. `exempt` deliberately has none:
 *  an external or calibration print never had a supported source to copy, so a
 *  badge there would invite the operator to fix something that is not wrong. */
export type MarkedQueueSourceStorage = 'ready' | 'preparing' | 'legacy' | 'broken';

const MARKED: readonly MarkedQueueSourceStorage[] = ['ready', 'preparing', 'legacy', 'broken'];

/**
 * The state to mark on a row, or `null` for "say nothing".
 *
 * `null` covers three things on purpose: an `exempt` row, a row from a server
 * that predates the field, and any value a newer server invents. Silence is the
 * safe default — a wrong badge about file safety is worse than no badge.
 */
export function markedStorage(state: QueueSourceStorage | null | undefined): MarkedQueueSourceStorage | null {
  return state && (MARKED as readonly string[]).includes(state) ? (state as MarkedQueueSourceStorage) : null;
}

/** Does this row hold a verified copy of its own bytes? */
export function isSelfContained(state: QueueSourceStorage | null | undefined): boolean {
  return state === 'ready';
}

/**
 * Every refusal an add can be answered with (spec §6), in the order the
 * backend's own taxonomy declares them. Kept as a closed list because an
 * unknown code must fall back to the server's sentence rather than render
 * `queueSpool.reason.<something>` at the operator.
 */
export const QUEUE_SOURCE_REASONS = [
  'source_copy_busy',
  'source_spool_replaced',
  'source_unreadable',
  'source_changed',
  'source_invalid',
  'source_copy_timeout',
  'source_spool_no_space',
  'source_spool_write_failed',
  'source_copy_failed',
] as const;

export type QueueSourceReason = (typeof QUEUE_SOURCE_REASONS)[number];

/** The refusal code of a failed call, when it is one of ours. */
export function queueSourceReasonOf(error: unknown): QueueSourceReason | null {
  const code = error instanceof ApiError ? error.code : undefined;
  return code && (QUEUE_SOURCE_REASONS as readonly string[]).includes(code) ? (code as QueueSourceReason) : null;
}

/**
 * Was the outcome of this call unknown, rather than refused?
 *
 * An `ApiError` means the server answered, so the answer is the truth. Anything
 * else — `fetch` rejecting outright — means the request may well have been
 * committed before the connection died, and §5 is explicit: the UI refreshes
 * the list and does NOT repeat the POST, because a second POST is a second job.
 */
export function isUnknownOutcome(error: unknown): boolean {
  return !(error instanceof ApiError);
}

type Translate = (key: string, opts?: Record<string, unknown>) => string;

/**
 * One sentence saying why the copy did not happen.
 *
 * Ours when we recognise the code; the server's own prose otherwise — it is
 * already localized, and inventing a generic line would throw away the only
 * information a future refusal carries.
 */
export function queueSourceReasonText(t: Translate, error: unknown): string {
  const reason = queueSourceReasonOf(error);
  if (reason) return t(`queueSpool.reason.${reason}`);
  const message = error instanceof Error ? error.message : '';
  return message || t('queueSpool.reason.source_copy_failed');
}

/** One attempt that did not land, and what the operator calls its target. */
export interface QueueAddFailure {
  /** Printer name, plus the plate when several were sent. */
  label: string;
  error: unknown;
}

/**
 * Everything the operator needs after an add that did not fully land, in the
 * order they need it: *is there a job now?* first, then a reason per failure.
 *
 * `added` counts the requests that landed and `total` the ones attempted — an
 * add is all-or-nothing per request (§5: a copy failure leaves no runnable
 * row), so "1 of 3 landed" is the whole truth about the other two.
 *
 * ⚠️ **A reason belongs to the printers it came from, never to all of them.**
 * One busy spool and one offline printer are two different answers, and
 * appending only the first as if it explained both hid the second entirely and
 * told the operator to "try again in a moment" about a machine that will never
 * accept the job. Failures are therefore grouped by reason and each group names
 * its printers — except the one unambiguous case (nothing landed, one reason,
 * every failure answered), where a bare sentence reads better and cannot be
 * misattributed.
 *
 * ⚠️ **An unanswered request is not a refusal**, so it leads with the
 * "it is not clear" sentence — but an answered refusal beside it keeps its own
 * reason. Losing an actionable reason to a sibling's uncertainty is the same
 * defect as misattributing one.
 */
export function queueAddOutcomeText(
  t: Translate,
  outcome: {
    added: number;
    total: number;
    failures: readonly QueueAddFailure[];
    /** True when the dialog stayed open and unticked what already landed. */
    deselected?: boolean;
    /**
     * Copies the retry now asks for, when the dialog had to correct the quantity
     * to keep it honest (`total` mode only).
     *
     * Said out loud because the number on screen changes by itself: an operator
     * who typed 10, saw 4 land and then reads 6 in the field is owed the reason.
     */
    stillMissing?: number | null;
  },
): string {
  const { added, total, failures, deselected = false, stillMissing = null } = outcome;
  const answered = failures.filter((f) => !isUnknownOutcome(f.error));
  const unanswered = failures.filter((f) => isUnknownOutcome(f.error));

  // Insertion-ordered, so the first printer's reason still reads first.
  const groups = new Map<string, string[]>();
  for (const failure of answered) {
    const reason = queueSourceReasonText(t, failure.error);
    const labels = groups.get(reason);
    if (labels) labels.push(failure.label);
    else groups.set(reason, [failure.label]);
  }

  const parts: string[] = [];
  if (unanswered.length > 0) {
    parts.push(
      added > 0
        ? t('queueSpool.failure.uncertainPartial', { success: added, total })
        : t('queueSpool.failure.uncertain'),
    );
  } else if (added > 0) {
    parts.push(t('queueSpool.failure.partial', { success: added, failed: answered.length, total }));
  } else {
    parts.push(t('queueSpool.failure.nothingQueued'));
  }

  const bare = added === 0 && unanswered.length === 0 && groups.size === 1;
  for (const [reason, labels] of groups) {
    parts.push(bare ? reason : t('queueSpool.failure.reasonFor', { printers: labels.join(', '), reason }));
  }
  if (deselected) parts.push(t('queueSpool.failure.deselected'));
  if (stillMissing != null) parts.push(t('queueSpool.failure.quantityLeft', { count: stillMissing }));
  return parts.join(' ');
}

/**
 * How many of a set of jobs survive their source being deleted, and how many go
 * with it (spec §10: a mixed selection must say truthfully which is which).
 *
 * Counted from the rows themselves, never guessed from the kind of source: a
 * ready snapshot is the only proof, and `preparing` / `legacy` / `broken` all
 * still depend on the original in one way or another.
 */
export function splitBySelfContained(
  states: readonly (QueueSourceStorage | null | undefined)[],
): { selfContained: number; needsOriginal: number } {
  let selfContained = 0;
  let needsOriginal = 0;
  for (const state of states) {
    if (isSelfContained(state)) selfContained += 1;
    else needsOriginal += 1;
  }
  return { selfContained, needsOriginal };
}
