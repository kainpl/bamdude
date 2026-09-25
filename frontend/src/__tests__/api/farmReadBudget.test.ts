import { describe, expect, it, vi } from 'vitest';
import {
  FarmReadBudget, farmPollInterval, farmPollJitterMs, farmReadRetry, farmReadRetryDelay, farmStatusPollInterval,
} from '../../api/farmReadBudget';

const tick = async () => { await Promise.resolve(); await Promise.resolve(); };

describe('farm background read budget', () => {
  it('admits at most four network reads, FIFO, without starving queued keys', async () => {
    const budget = new FarmReadBudget(4);
    const finish: Array<(value: number) => void> = [];
    const order: number[] = [];
    const reads = Array.from({ length: 7 }, (_, id) => budget.read(String(id), () => {
      order.push(id);
      return new Promise<number>(resolve => { finish[id] = resolve; });
    }));
    expect(order).toEqual([0, 1, 2, 3]);
    expect(budget.snapshot()).toMatchObject({ inFlight: 4, queued: 3, started: 4 });
    finish[0](0);
    await tick();
    expect(order).toEqual([0, 1, 2, 3, 4]);
    for (let id = 1; id < 7; id += 1) { finish[id](id); await tick(); }
    expect(await Promise.all(reads)).toEqual([0, 1, 2, 3, 4, 5, 6]);
    expect(budget.snapshot()).toMatchObject({ inFlight: 0, queued: 0 });
  });

  it('coalesces a key but aborts transport only after its last waiter leaves', async () => {
    const budget = new FarmReadBudget(1);
    const first = new AbortController();
    const second = new AbortController();
    let transport!: AbortSignal;
    const run = vi.fn((signal: AbortSignal) => {
      transport = signal;
      return new Promise<number>(() => {});
    });
    const a = budget.read('same', run, first.signal);
    const b = budget.read('same', run, second.signal);
    first.abort();
    await expect(a).rejects.toMatchObject({ name: 'AbortError' });
    expect(transport.aborted).toBe(false);
    second.abort();
    await expect(b).rejects.toMatchObject({ name: 'AbortError' });
    await tick();
    expect(transport.aborted).toBe(true);
    expect(run).toHaveBeenCalledTimes(1);
    expect(budget.snapshot()).toMatchObject({ coalesced: 1, cancelled: 2, inFlight: 0 });
  });

  it('removes a cancelled queued read without calling its transport', async () => {
    const budget = new FarmReadBudget(1);
    let finish!: (value: number) => void;
    const active = budget.read('active', () => new Promise<number>(resolve => { finish = resolve; }));
    const controller = new AbortController();
    const run = vi.fn(async () => 2);
    const queued = budget.read('queued', run, controller.signal);
    controller.abort();
    await expect(queued).rejects.toMatchObject({ name: 'AbortError' });
    expect(budget.snapshot().queued).toBe(0);
    finish(1);
    await active;
    expect(run).not.toHaveBeenCalled();
  });

  it('releases a slot even if transport ignores the deadline signal', async () => {
    const budget = new FarmReadBudget(1, 20);
    const hung = budget.read('hung', () => new Promise<number>(() => {}));
    const next = budget.read('next', async () => 2);
    await expect(hung).rejects.toMatchObject({ name: 'TimeoutError' });
    await expect(next).resolves.toBe(2);
    expect(budget.snapshot()).toMatchObject({ timedOut: 1, inFlight: 0 });
  });

  it('rejects old queued and active work on auth identity change', async () => {
    const budget = new FarmReadBudget(1);
    const active = budget.read('old-a', () => new Promise<number>(() => {}));
    const queued = budget.read('old-b', async () => 2);
    budget.reset();
    await expect(active).rejects.toMatchObject({ name: 'AbortError' });
    await expect(queued).rejects.toMatchObject({ name: 'AbortError' });
    await tick();
    await expect(budget.read('new', async () => 3)).resolves.toBe(3);
  });

  it('retries only transient reads and respects Retry-After without a second retry loop', () => {
    expect(farmReadRetry(1, { status: 429 })).toBe(true);
    expect(farmReadRetryDelay(1, { retryAfterMs: 7_000 })).toBe(7_000);
    expect(farmReadRetry(1, { status: 503 })).toBe(true);
    expect(farmReadRetry(2, { status: 503 })).toBe(false);
    expect(farmReadRetry(1, { status: 403 })).toBe(false);
    expect(farmReadRetry(1, new DOMException('cancelled', 'AbortError'))).toBe(false);
  });

  it('backs off a failed periodic query without disabling the observer timer', () => {
    expect(farmPollJitterMs).toBeGreaterThanOrEqual(0);
    expect(farmPollJitterMs).toBeLessThanOrEqual(1000);
    expect(farmPollInterval(10_000, { state: { error: null, fetchFailureCount: 0 } })).toBe(10_000 + farmPollJitterMs);
    expect(farmPollInterval(10_000, { state: { error: new Error('offline'), fetchFailureCount: 2 } })).toBe(40_000 + farmPollJitterMs);
  });

  it('puts drifting status timers on one tab grid so due printers share a batch', () => {
    const now = 1_000_000 + farmPollJitterMs;
    const state = (dataUpdatedAt: number, error: unknown = null, fetchFailureCount = 0, errorUpdatedAt = 0) =>
      ({ state: { dataUpdatedAt, error, fetchFailureCount, errorUpdatedAt } });
    // Cards whose last reply landed at different moments of the previous
    // period all fire on the same next tick.
    const fireAt = (updatedAt: number) => now + farmStatusPollInterval(5_000, state(updatedAt), now);
    expect(new Set([now - 4_900, now - 3_000, now - 1_234, now - 1].map(fireAt))).toEqual(new Set([now + 5_000]));
    expect((fireAt(now - 1) - farmPollJitterMs) % 5_000).toBe(0);
    // A key overdue since long ago joins the next tick, never "now" in a loop.
    expect(farmStatusPollInterval(5_000, state(now - 60_000), now)).toBe(5_000);
    // The reply to a tick's own read keeps the cadence instead of doubling it.
    expect(farmStatusPollInterval(5_000, state(now + 50), now + 50)).toBe(4_950);
    // A live WebSocket write mid-period defers only this printer, past the next tick.
    expect(now + 2_500 + farmStatusPollInterval(5_000, state(now + 2_500), now + 2_500)).toBe(now + 10_000);
    // No answer yet: a full period, not a one-millisecond re-arm.
    expect(farmStatusPollInterval(5_000, state(0), now)).toBe(5_000);
    // A failed read backs off from the failure, stays on the grid, and a key
    // that never succeeded does not spin either.
    const failed = farmStatusPollInterval(5_000, state(now - 30_000, new Error('offline'), 2, now), now);
    expect(failed).toBe(20_000);
    expect((now + failed - farmPollJitterMs) % 5_000).toBe(0);
    expect(farmStatusPollInterval(5_000, state(0, new Error('offline'), 1, now), now)).toBe(10_000);
  });
});
