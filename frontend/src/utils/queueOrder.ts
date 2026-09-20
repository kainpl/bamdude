import type { PrinterQueue } from '../api/client';
import { byLocationName } from './locationOrder';
import { compareCurrentJobEta, compareFreeAt, type EtaStatus, type FreeAtRow } from './etaSort';

export type QueueSortOption = 'name' | 'status' | 'model' | 'location' | 'tag' | 'eta' | 'freeAt';

const SORT_OPTIONS: readonly QueueSortOption[] = ['name', 'status', 'model', 'location', 'tag', 'eta', 'freeAt'];

/**
 * What the two ETA orders read beyond the queue row itself. Both lookups are
 * optional: a caller without them (a dialog that only mirrors the screen's
 * order) gets every queue as a peer and falls through to the name order,
 * never a crash on a sort key the screen behind it may have picked.
 */
export interface QueueSortContext {
  /** The printer's live status, from the query cache — «ETA (job)» reads it, «ETA (queue)» reads its offline bit. */
  statusOf?: (printerId: number) => EtaStatus | undefined;
  /** The server's per-printer «free at» row — «ETA (queue)» reads it. */
  forecastOf?: (printerId: number) => FreeAtRow | undefined;
}

/** Printing first, then whatever needs attention, then the idle machines. */
const STATUS_ORDER: Record<string, number> = { printing: 0, error: 1, paused: 2, idle: 3 };

/**
 * The order the Queues screen is in right now.
 *
 * The two localStorage keys the screen writes are the only record of the
 * operator's chosen order, so a dialog opened from that screen reads them
 * rather than picking its own default — a target list in a different order
 * from the cards behind it is a list you have to re-learn.
 *
 * Validates rather than casting: a stale or hand-edited key would otherwise
 * fall through every branch of the sort and leave the list in fetch order.
 */
export function readStoredQueueSort(): { sortBy: QueueSortOption; sortAsc: boolean } {
  const stored = localStorage.getItem('queueSortBy') as QueueSortOption | null;
  return {
    sortBy: stored && SORT_OPTIONS.includes(stored) ? stored : 'name',
    sortAsc: localStorage.getItem('queueSortAsc') !== 'false',
  };
}

/**
 * One comparator for every surface that lists printer queues.
 *
 * ⚠️ Returns a copy. It is called from `useMemo` bodies where the input is the
 * query cache's own array, and sorting that in place mutates what every other
 * consumer of the query is holding.
 */
export function sortQueues<T extends PrinterQueue>(
  queues: readonly T[],
  sortBy: QueueSortOption,
  sortAsc: boolean,
  context: QueueSortContext = {},
): T[] {
  const sorted = [...queues];
  const byName = (a: T, b: T) => (a.printer_name || '').localeCompare(b.printer_name || '');

  switch (sortBy) {
    case 'name':
      sorted.sort(byName);
      break;
    case 'eta':
      // The printer finishing its CURRENT job next on top; the queue behind it
      // does not count. Same tiers as the printers page — one comparator.
      sorted.sort(
        (a, b) => compareCurrentJobEta(context.statusOf?.(a.printer_id), context.statusOf?.(b.printer_id)) || byName(a, b),
      );
      break;
    case 'freeAt':
      // The printer free SOONEST on top — running print plus everything queued
      // behind it, as the server's forecast simulates it (the stats bar's number,
      // per printer).
      sorted.sort(
        (a, b) =>
          compareFreeAt(
            context.forecastOf?.(a.printer_id),
            context.forecastOf?.(b.printer_id),
            context.statusOf?.(a.printer_id),
            context.statusOf?.(b.printer_id),
          ) || byName(a, b),
      );
      break;
    case 'status':
      sorted.sort((a, b) => {
        const aOrder = STATUS_ORDER[a.status] ?? 4;
        const bOrder = STATUS_ORDER[b.status] ?? 4;
        if (aOrder !== bOrder) return aOrder - bOrder;
        if (b.pending_count !== a.pending_count) return b.pending_count - a.pending_count;
        return (a.printer_name || '').localeCompare(b.printer_name || '');
      });
      break;
    case 'model':
      sorted.sort((a, b) => (a.printer_model || '').localeCompare(b.printer_model || ''));
      break;
    case 'location':
      sorted.sort(byLocationName((queue) => queue.printer_location?.path));
      break;
    case 'tag': {
      // By the alphabetically-first tag the printer wears, untagged last —
      // the printers page's rule. The grouped view lists a multi-tagged
      // printer under each of its tags; this flat order is what the timeline,
      // the copy-queue dialog and the ungrouped fallback read.
      const firstTag = (queue: T) =>
        [...(queue.printer_tags ?? [])].map((tag) => tag.name).sort((a, b) => a.localeCompare(b))[0] ?? '';
      sorted.sort((a, b) => {
        const ta = firstTag(a);
        const tb = firstTag(b);
        if (!ta && tb) return 1;
        if (ta && !tb) return -1;
        return ta.localeCompare(tb) || byName(a, b);
      });
      break;
    }
  }

  if (!sortAsc) sorted.reverse();
  return sorted;
}
