import type { PrinterQueue } from '../api/client';
import { byLocationName } from './locationOrder';

export type QueueSortOption = 'name' | 'status' | 'model' | 'location';

const SORT_OPTIONS: readonly QueueSortOption[] = ['name', 'status', 'model', 'location'];

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
): T[] {
  const sorted = [...queues];

  switch (sortBy) {
    case 'name':
      sorted.sort((a, b) => (a.printer_name || '').localeCompare(b.printer_name || ''));
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
  }

  if (!sortAsc) sorted.reverse();
  return sorted;
}
