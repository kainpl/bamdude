import { describe, it, expect } from 'vitest';
import { estimateWallClockSeconds } from '../../utils/queueEstimate';

const NOW = Date.parse('2026-07-30T12:00:00Z');
const H = 3600;

const minis = [
  { printer_id: 1, printer_model: 'A1 Mini', status: 'idle' },
  { printer_id: 2, printer_model: 'A1 Mini', status: 'idle' },
  { printer_id: 3, printer_model: 'A1 Mini', status: 'idle' },
  { printer_id: 4, printer_model: 'A1 Mini', status: 'idle' },
];

function estimate(over: Partial<Parameters<typeof estimateWallClockSeconds>[0]> = {}) {
  return estimateWallClockSeconds({
    queues: minis,
    pendingItems: [],
    printingItems: [],
    stagedItems: [],
    now: NOW,
    ...over,
  });
}

describe('estimateWallClockSeconds', () => {
  it('is zero for an idle farm', () => {
    expect(estimate()).toBe(0);
  });

  it('spreads staged work across printers instead of adding it up', () => {
    // The bug this replaces: eight one-hour jobs on four printers reported as
    // eight hours, when the farm is free in two.
    const staged = Array.from({ length: 8 }, () => ({ target_model: 'A1 Mini', print_time_seconds: H }));
    expect(estimate({ stagedItems: staged })).toBe(2 * H);
  });

  it('counts the print already running', () => {
    // Started 20 minutes into a one-hour print: 40 minutes left.
    const printing = [{
      printer_id: 1,
      print_time_seconds: H,
      started_at: new Date(NOW - 20 * 60 * 1000).toISOString(),
    }];
    expect(estimate({ printingItems: printing })).toBe(40 * 60);
  });

  it('counts per-printer pending items on their own printer', () => {
    const pending = [
      { printer_id: 1, print_time_seconds: H },
      { printer_id: 1, print_time_seconds: H },
      { printer_id: 2, print_time_seconds: H },
    ];
    // Printer 1 owes two hours, printer 2 one — the farm is free in two.
    expect(estimate({ pendingItems: pending })).toBe(2 * H);
  });

  it('fills the printer that is free soonest', () => {
    const pending = [{ printer_id: 1, print_time_seconds: 3 * H }];
    const staged = [{ target_model: 'A1 Mini', print_time_seconds: H }];
    // The staged hour must go to an empty printer, not behind the three-hour one.
    expect(estimate({ pendingItems: pending, stagedItems: staged })).toBe(3 * H);
  });

  it('places the longest job first', () => {
    // Greedy in arrival order would put 2h and 1h on one printer each, then
    // strand the 3h behind one of them. Longest-first keeps the makespan at 3h.
    const staged = [
      { target_model: 'A1 Mini', print_time_seconds: 2 * H },
      { target_model: 'A1 Mini', print_time_seconds: H },
      { target_model: 'A1 Mini', print_time_seconds: 3 * H },
    ];
    expect(estimate({ queues: minis.slice(0, 2), stagedItems: staged })).toBe(3 * H);
  });

  it('respects target_model — a mini job cannot shorten a P1S day', () => {
    const mixed = [
      { printer_id: 1, printer_model: 'A1 Mini', status: 'idle' },
      { printer_id: 2, printer_model: 'P1S', status: 'idle' },
    ];
    const staged = [
      { target_model: 'A1 Mini', print_time_seconds: H },
      { target_model: 'A1 Mini', print_time_seconds: H },
    ];
    // Both land on the single mini: two hours, not one.
    expect(estimateWallClockSeconds({
      queues: mixed, pendingItems: [], printingItems: [], stagedItems: staged, now: NOW,
    })).toBe(2 * H);
  });

  it('matches the model case-insensitively', () => {
    const staged = [{ target_model: 'a1 mini', print_time_seconds: H }];
    expect(estimate({ queues: minis.slice(0, 1), stagedItems: staged })).toBe(H);
  });

  it('ignores a staged job that can never be routed', () => {
    // No target model never routes at all; a model nobody owns cannot run here.
    const staged = [
      { target_model: null, print_time_seconds: 5 * H },
      { target_model: 'X1C', print_time_seconds: 5 * H },
    ];
    expect(estimate({ stagedItems: staged })).toBe(0);
  });

  it('does not hand work to a paused or errored queue', () => {
    const queues = [
      { printer_id: 1, printer_model: 'A1 Mini', status: 'paused' },
      { printer_id: 2, printer_model: 'A1 Mini', status: 'error' },
      { printer_id: 3, printer_model: 'A1 Mini', status: 'idle' },
    ];
    const staged = Array.from({ length: 2 }, () => ({ target_model: 'A1 Mini', print_time_seconds: H }));
    // Only printer 3 accepts, so both hours land there.
    expect(estimateWallClockSeconds({
      queues, pendingItems: [], printingItems: [], stagedItems: staged, now: NOW,
    })).toBe(2 * H);
  });

  it('falls back to two hours for an unknown duration', () => {
    expect(estimate({ stagedItems: [{ target_model: 'A1 Mini', print_time_seconds: null }] })).toBe(2 * H);
  });

  it('treats a print with no start time as just begun', () => {
    // Better to overstate slightly than to report a printing farm as free.
    const printing = [{ printer_id: 1, print_time_seconds: H, started_at: null }];
    expect(estimate({ printingItems: printing })).toBe(H);
  });

  it('never returns a negative remainder for an overdue print', () => {
    const printing = [{
      printer_id: 1,
      print_time_seconds: H,
      started_at: new Date(NOW - 5 * H * 1000).toISOString(),
    }];
    expect(estimate({ printingItems: printing })).toBe(0);
  });
});
