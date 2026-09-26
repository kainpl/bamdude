/**
 * The order the auto-queue distributor places pending jobs in.
 *
 * The backend decides it in SQL (`AutoQueueScheduler._fetch_pending`):
 *
 *     SJF on:  ORDER BY target_model, been_jumped DESC,
 *                       print_time_seconds ASC NULLS LAST, position
 *     SJF off: ORDER BY position
 *
 * The auto-queue panel sorted by position alone, so with Shortest-Job-First on
 * it listed an order the distributor was not using (upstream #3043). The same
 * ORDER BY, here, is what the list shows.
 */

interface OrderableAutoQueueItem {
  target_model?: string | null;
  been_jumped?: boolean;
  print_time_seconds?: number | null;
  position: number;
}

/**
 * @param sjfEnabled the `queue_shortest_first` setting.
 */
export function compareAutoQueueOrder(a: OrderableAutoQueueItem, b: OrderableAutoQueueItem, sjfEnabled: boolean): number {
  if (sjfEnabled) {
    // Each model is its own contest: a job only competes with others for the
    // same printers. Grouped by name for reading.
    const byModel = (a.target_model ?? '').localeCompare(b.target_model ?? '');
    if (byModel !== 0) return byModel;
    // Starvation guard: a job something shorter was placed ahead of goes first
    // next time, whatever the print times say.
    const jumped = (b.been_jumped ? 1 : 0) - (a.been_jumped ? 1 : 0);
    if (jumped !== 0) return jumped;
    // Shortest first; an unknown duration sorts last, not as a zero-second print.
    const aTime = a.print_time_seconds ?? Infinity;
    const bTime = b.print_time_seconds ?? Infinity;
    if (aTime !== bTime) return aTime < bTime ? -1 : 1;
  }
  return a.position - b.position;
}
