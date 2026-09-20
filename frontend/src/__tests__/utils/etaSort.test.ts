/**
 * The two ETA orders both pages share.
 *
 * «ETA (job)» reads the live status only; «ETA (queue)»
 * reads the server's per-printer forecast row plus the status's offline bit.
 * Both comparators answer 0 for peers so the caller's name order breaks the
 * tie — a page must never invent its own tiers again.
 */

import { describe, it, expect } from 'vitest';
import { compareCurrentJobEta, compareFreeAt, forecastById, type EtaStatus, type FreeAtRow } from '../../utils/etaSort';

const printing = (remaining: number | null): EtaStatus => ({ connected: true, state: 'RUNNING', remaining_time: remaining });
const idle: EtaStatus = { connected: true, state: 'IDLE', remaining_time: null };
const offline: EtaStatus = { connected: false, state: null, remaining_time: null };
const row = (free_seconds: number, unknown_prints = 0): FreeAtRow => ({ free_seconds, unknown_prints });

function order<T>(items: T[], compare: (a: T, b: T) => number): T[] {
  return [...items].sort(compare);
}

describe('compareCurrentJobEta', () => {
  it('puts the printer finishing next first, then printing-without-ETA, idle, offline', () => {
    const named = [
      { name: 'idle', status: idle },
      { name: 'offline', status: offline },
      { name: 'later', status: printing(90) },
      { name: 'no-eta', status: printing(null) },
      { name: 'soon', status: printing(10) },
      { name: 'unknown', status: undefined },
    ];
    const sorted = order(named, (a, b) => compareCurrentJobEta(a.status, b.status) || a.name.localeCompare(b.name));
    expect(sorted.map((x) => x.name)).toEqual(['soon', 'later', 'no-eta', 'idle', 'offline', 'unknown']);
  });

  it('answers 0 for peers so the caller breaks the tie', () => {
    expect(compareCurrentJobEta(idle, idle)).toBe(0);
    expect(compareCurrentJobEta(printing(null), printing(0))).toBe(0);
    // No status at all is offline — the same reading the offline filter gives it.
    expect(compareCurrentJobEta(undefined, offline)).toBe(0);
  });
});

describe('compareFreeAt', () => {
  it('puts the printer free soonest first — running print plus its queue — then unknown, free now, offline', () => {
    const named = [
      { name: 'free-now', row: row(0), status: idle },
      { name: 'busy-3h', row: row(3 * 3600), status: printing(600) },
      { name: 'busy-1h', row: row(3600), status: printing(3000) },
      { name: 'no-estimate', row: row(0, 1), status: printing(null) },
      { name: 'offline-owes', row: row(7200), status: offline },
    ];
    const sorted = order(named, (a, b) => compareFreeAt(a.row, b.row, a.status, b.status) || a.name.localeCompare(b.name));
    // busy-1h leads busy-3h although its CURRENT job ends later: the queue behind counts here.
    expect(sorted.map((x) => x.name)).toEqual(['busy-1h', 'busy-3h', 'no-estimate', 'free-now', 'offline-owes']);
  });

  it('reads a printer with no forecast row yet as free now, and answers 0 for peers', () => {
    expect(compareFreeAt(undefined, row(0), idle, idle)).toBe(0);
    expect(compareFreeAt(undefined, row(3600), idle, idle)).toBeGreaterThan(0);
    expect(compareFreeAt(row(0, 2), row(0, 5), idle, idle)).toBe(0);
  });
});

describe('forecastById', () => {
  it('indexes the rows by printer, and is empty while the forecast has not loaded', () => {
    expect(forecastById(undefined).size).toBe(0);
    const map = forecastById({
      free_at: null,
      free_seconds: 3600,
      unknown_prints: 0,
      printers: [
        { printer_id: 7, free_at: null, free_seconds: 3600, unknown_prints: 0 },
        { printer_id: 9, free_at: null, free_seconds: 0, unknown_prints: 1 },
      ],
    });
    expect(map.get(7)?.free_seconds).toBe(3600);
    expect(map.get(9)?.unknown_prints).toBe(1);
    expect(map.has(8)).toBe(false);
  });
});
