import { useMemo } from 'react';
import type { Printer, PrinterQueue, PrintQueueItem } from '../api/client';

// Fallback duration when print_time_seconds is missing. Two hours is a rough
// estimate; slots rendered with this value are marked as "estimated" so the
// UI can dim them.
const DEFAULT_DURATION_SEC = 2 * 60 * 60;

export interface TimelineSlot {
  itemId: number;
  item: PrintQueueItem;
  /** Start time of this slot (ms since epoch). */
  startMs: number;
  /** End time (ms since epoch). */
  endMs: number;
  /** Duration used to build this slot in seconds. */
  durationSec: number;
  /** True when print_time_seconds was missing and we fell back to default. */
  estimated: boolean;
  /** True for the queue's actively-printing item. */
  active: boolean;
  /** True when the item has a scheduled_time in the future. */
  scheduledPin: boolean;
}

export interface TimelineLane {
  queue: PrinterQueue;
  printer: Printer | undefined;
  slots: TimelineSlot[];
}

export interface QueueTimelineResult {
  lanes: TimelineLane[];
  /** Axis tick times (ms since epoch). */
  ticks: number[];
  /** Window start (ms). */
  windowStartMs: number;
  /** Window end (ms). */
  windowEndMs: number;
  /** Window duration (ms). */
  windowDurationMs: number;
}

interface Input {
  queues: PrinterQueue[] | undefined;
  items: PrintQueueItem[] | undefined;
  printers: Printer[] | undefined;
  /** Current time (ms). Pass Date.now() or a fixed value for tests. */
  now: number;
  /** Visible range in hours (e.g. 12, 24, 72). */
  rangeHours: number;
  /** Where in the visible window "now" sits, 0..1. Default 0.08 (~1h slack). */
  nowOffset?: number;
}

/**
 * Derives per-printer timeline lanes from queue data. Pure calculation —
 * internal helper for ``useQueueTimeline`` (memoised for React callers).
 */
function buildQueueTimeline({
  queues,
  items,
  printers,
  now,
  rangeHours,
  nowOffset = 0.08,
}: Input): QueueTimelineResult {
  const windowDurationMs = rangeHours * 60 * 60 * 1000;
  const windowStartMs = now - windowDurationMs * nowOffset;
  const windowEndMs = windowStartMs + windowDurationMs;

  const tickIntervalMs = pickTickInterval(rangeHours);
  const ticks: number[] = [];
  const firstTick = Math.ceil(windowStartMs / tickIntervalMs) * tickIntervalMs;
  for (let t = firstTick; t <= windowEndMs; t += tickIntervalMs) ticks.push(t);

  if (!queues) {
    return { lanes: [], ticks, windowStartMs, windowEndMs, windowDurationMs };
  }

  const printerById = new Map<number, Printer>();
  (printers ?? []).forEach(p => printerById.set(p.id, p));

  const itemsByQueue = new Map<number, PrintQueueItem[]>();
  (items ?? []).forEach(it => {
    const list = itemsByQueue.get(it.queue_id);
    if (list) list.push(it);
    else itemsByQueue.set(it.queue_id, [it]);
  });

  // Sort printing → pending-by-position.
  const statusWeight: Record<string, number> = { printing: 0, pending: 1 };
  for (const list of itemsByQueue.values()) {
    list.sort((a, b) => {
      const wa = statusWeight[a.status] ?? 2;
      const wb = statusWeight[b.status] ?? 2;
      if (wa !== wb) return wa - wb;
      return (a.position ?? 0) - (b.position ?? 0);
    });
  }

  const lanes: TimelineLane[] = queues.map(queue => {
    const printer = printerById.get(queue.printer_id);
    const staggerMs = Math.max(0, (printer?.stagger_interval_minutes ?? 0)) * 60 * 1000;
    const rawItems = itemsByQueue.get(queue.id) ?? [];

    let cursor = now;
    const slots: TimelineSlot[] = [];

    for (const item of rawItems) {
      if (item.status !== 'pending' && item.status !== 'printing') continue;

      const duration = (item.print_time_seconds ?? 0) > 0
        ? (item.print_time_seconds ?? 0)
        : DEFAULT_DURATION_SEC;
      const estimated = !(item.print_time_seconds && item.print_time_seconds > 0);

      let startMs: number;
      let endMs: number;
      const active = item.status === 'printing';

      if (active) {
        // Anchor at started_at when known; otherwise collapse to "now".
        const startedAt = item.started_at ? Date.parse(item.started_at) : now;
        startMs = Number.isFinite(startedAt) ? startedAt : now;
        endMs = startMs + duration * 1000;
        if (endMs < now) endMs = now + 5 * 60 * 1000; // guard against stale data
        cursor = endMs + staggerMs;
      } else {
        const scheduled = item.scheduled_time ? Date.parse(item.scheduled_time) : NaN;
        const scheduledPin = Number.isFinite(scheduled) && scheduled > now;
        startMs = scheduledPin ? scheduled : cursor;
        if (startMs < cursor) startMs = cursor;
        endMs = startMs + duration * 1000;
        cursor = endMs + staggerMs;

        slots.push({
          itemId: item.id,
          item,
          startMs,
          endMs,
          durationSec: duration,
          estimated,
          active: false,
          scheduledPin,
        });
        continue;
      }

      slots.push({
        itemId: item.id,
        item,
        startMs,
        endMs,
        durationSec: duration,
        estimated,
        active,
        scheduledPin: false,
      });
    }

    return { queue, printer, slots };
  });

  return { lanes, ticks, windowStartMs, windowEndMs, windowDurationMs };
}

export function useQueueTimeline(input: Input): QueueTimelineResult {
  // `now` is the primary driver of re-computation; consumers should pass a
  // value that updates on a timer (e.g. every minute). Using granular deps
  // on purpose - passing `input` in full would defeat memoization.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => buildQueueTimeline(input), [
    input.queues,
    input.items,
    input.printers,
    input.now,
    input.rangeHours,
    input.nowOffset,
  ]);
}

function pickTickInterval(rangeHours: number): number {
  const hour = 60 * 60 * 1000;
  if (rangeHours <= 6) return 30 * 60 * 1000; // 30 min
  if (rangeHours <= 12) return hour;
  if (rangeHours <= 24) return 2 * hour;
  if (rangeHours <= 48) return 4 * hour;
  return 6 * hour;
}
