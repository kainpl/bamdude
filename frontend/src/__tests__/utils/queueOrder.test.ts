/**
 * The queue order every surface that lists printer queues goes through.
 *
 * The two ETA keys need more than the queue row — a status lookup and the
 * server's forecast — and a caller without them (the copy-queue dialog before
 * its statuses land) must still get a stable order, not a crash.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { readStoredQueueSort, sortQueues } from '../../utils/queueOrder';
import type { PrinterQueue } from '../../api/client';

function queue(id: number, printer_name: string, over: Partial<PrinterQueue> = {}): PrinterQueue {
  return {
    id,
    printer_id: id,
    printer_name,
    printer_model: 'P1S',
    printer_location: null,
    status: 'idle',
    last_activity_at: null,
    current_item_id: null,
    pending_count: 0,
    completed_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    skipped_count: 0,
    total_count: 0,
    created_at: '2026-09-09T00:00:00Z',
    updated_at: '2026-09-09T00:00:00Z',
    ...over,
  } as PrinterQueue;
}

const queues = [queue(1, 'Alpha'), queue(2, 'Bravo'), queue(3, 'Charlie')];
const connected = { connected: true, state: 'RUNNING', remaining_time: 600 };

describe('sortQueues', () => {
  it('orders by the server free-at with the queue counted, soonest free first', () => {
    const forecast: Record<number, { free_seconds: number; unknown_prints: number }> = {
      1: { free_seconds: 7200, unknown_prints: 0 },
      2: { free_seconds: 3600, unknown_prints: 0 },
      3: { free_seconds: 0, unknown_prints: 0 },
    };
    const sorted = sortQueues(queues, 'freeAt', true, {
      statusOf: () => connected,
      forecastOf: (id) => forecast[id],
    });
    expect(sorted.map((q) => q.printer_name)).toEqual(['Bravo', 'Alpha', 'Charlie']);
    // Descending is the same walk reversed, as every other key here.
    const reversed = sortQueues(queues, 'freeAt', false, { statusOf: () => connected, forecastOf: (id) => forecast[id] });
    expect(reversed.map((q) => q.printer_name)).toEqual(['Charlie', 'Alpha', 'Bravo']);
  });

  it('orders by the current job alone under «eta», the queue behind it not counted', () => {
    const statuses: Record<number, { connected: boolean; state: string; remaining_time: number | null }> = {
      1: { connected: true, state: 'RUNNING', remaining_time: 900 },
      2: { connected: true, state: 'RUNNING', remaining_time: 300 },
      3: { connected: true, state: 'IDLE', remaining_time: null },
    };
    const sorted = sortQueues(queues, 'eta', true, { statusOf: (id) => statuses[id] });
    expect(sorted.map((q) => q.printer_name)).toEqual(['Bravo', 'Alpha', 'Charlie']);
  });

  it('orders by the first tag the printer wears, untagged last — the printers page rule', () => {
    const tagged = [
      queue(1, 'Alpha', { printer_tags: [{ id: 2, name: 'Phase 2', color: null }] }),
      queue(2, 'Bravo'),
      queue(3, 'Charlie', { printer_tags: [{ id: 3, name: 'Phase 3', color: null }, { id: 1, name: 'Phase 1', color: '#ff0000' }] }),
      queue(4, 'Delta', { printer_tags: [{ id: 2, name: 'Phase 2', color: null }] }),
    ];
    // Charlie's first tag by name is Phase 1, whatever order the server sent them in.
    expect(sortQueues(tagged, 'tag', true).map((q) => q.printer_name)).toEqual(['Charlie', 'Alpha', 'Delta', 'Bravo']);
    expect(sortQueues(tagged, 'tag', false).map((q) => q.printer_name)).toEqual(['Bravo', 'Delta', 'Alpha', 'Charlie']);
  });

  it('falls through to the name order when a caller has no context for an ETA key', () => {
    expect(sortQueues(queues, 'eta', true).map((q) => q.printer_name)).toEqual(['Alpha', 'Bravo', 'Charlie']);
    expect(sortQueues(queues, 'freeAt', true).map((q) => q.printer_name)).toEqual(['Alpha', 'Bravo', 'Charlie']);
  });

  it('returns a copy, never the caller\'s array', () => {
    const input = [...queues];
    const sorted = sortQueues(input, 'freeAt', false);
    expect(sorted).not.toBe(input);
    expect(input.map((q) => q.printer_name)).toEqual(['Alpha', 'Bravo', 'Charlie']);
  });
});

describe('readStoredQueueSort', () => {
  beforeEach(() => localStorage.clear());

  it('accepts the two ETA keys and refuses anything the dropdown does not offer', () => {
    localStorage.setItem('queueSortBy', 'freeAt');
    expect(readStoredQueueSort().sortBy).toBe('freeAt');
    localStorage.setItem('queueSortBy', 'eta');
    expect(readStoredQueueSort().sortBy).toBe('eta');
    localStorage.setItem('queueSortBy', 'tag');
    expect(readStoredQueueSort().sortBy).toBe('tag');
    localStorage.setItem('queueSortBy', 'remaining');
    expect(readStoredQueueSort().sortBy).toBe('name');
  });
});
