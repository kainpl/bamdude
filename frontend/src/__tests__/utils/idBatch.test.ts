import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createIdBatcher } from '../../utils/idBatch';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('createIdBatcher', () => {
  it('turns one tick of reads into one request and hands each id its own answer', async () => {
    const fetchBatch = vi.fn(async (ids: number[]) => Object.fromEntries(ids.map((id) => [String(id), `v${id}`])));
    const load = createIdBatcher(fetchBatch, () => 'none');

    const answers = Promise.all([load(3), load(1), load(3)]);
    await vi.advanceTimersByTimeAsync(20);

    expect(await answers).toEqual(['v3', 'v1', 'v3']);
    expect(fetchBatch).toHaveBeenCalledTimes(1);
    expect(fetchBatch).toHaveBeenCalledWith([1, 3]);
  });

  it('gives an id the answer leaves out its "missing" value, or its error', async () => {
    const values = createIdBatcher<string[]>(async () => ({}), () => []);
    const errors = createIdBatcher<string>(async () => ({}), (id) => new Error(`no printer ${id}`));

    const value = values(7);
    const error = errors(7).catch((e: Error) => e.message);
    await vi.advanceTimersByTimeAsync(20);

    expect(await value).toEqual([]);
    expect(await error).toBe('no printer 7');
  });

  it('rejects every waiter of a failed request and asks again on the next tick', async () => {
    const fetchBatch = vi
      .fn<(ids: number[]) => Promise<Record<string, number>>>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ '1': 10 });
    const load = createIdBatcher(fetchBatch, () => 0);

    const failed = Promise.allSettled([load(1), load(2)]);
    await vi.advanceTimersByTimeAsync(20);
    expect((await failed).map((r) => r.status)).toEqual(['rejected', 'rejected']);

    const again = load(1);
    await vi.advanceTimersByTimeAsync(20);
    expect(await again).toBe(10);
    expect(fetchBatch).toHaveBeenCalledTimes(2);
  });

  it('splits a tick larger than the route takes', async () => {
    const fetchBatch = vi.fn(async (ids: number[]) => Object.fromEntries(ids.map((id) => [String(id), id])));
    const load = createIdBatcher(fetchBatch, () => -1, { maxIds: 2 });

    const answers = Promise.all([load(1), load(2), load(3)]);
    await vi.advanceTimersByTimeAsync(20);

    expect(await answers).toEqual([1, 2, 3]);
    expect(fetchBatch.mock.calls.map(([ids]) => ids)).toEqual([[1, 2], [3]]);
  });
});
