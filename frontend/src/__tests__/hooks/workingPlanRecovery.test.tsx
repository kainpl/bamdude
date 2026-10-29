import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, type WorkingPlan, type WorkingEntry } from '../../api/client';
import { useWorkingPlan } from '../../hooks/useWorkingPlan';

const entry = (count: number): WorkingEntry => ({ entry_id: 'one', line_id: 1, plate_id: 2, count,
  target: { kind: 'auto' }, source_token: 'source', manual: true, protected_count: count });
const initial: WorkingPlan = { schema_version: 1, revision: 1, saved_revision: 0,
  entries: [entry(20)], saved_at: null, saved_by: null,
  epoch: 0, saved_epoch: 0, active_operation: null, inputs_token: 'old-inputs', issues: [], changed: false };
const fresh = { ...initial, inputs_token: 'new-inputs' };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}
afterEach(() => vi.restoreAllMocks());

async function conflicted() {
  const get = vi.spyOn(api, 'getWorkingPlan').mockResolvedValue(initial);
  const write = vi.spyOn(api, 'writeWorkingPlan').mockRejectedValue(new Error('inputs_changed'));
  const hook = renderHook(() => useWorkingPlan(1, true));
  await waitFor(() => expect(hook.result.current.state).toEqual(initial));
  await act(async () => {
    hook.result.current.change([entry(18)]);
    await expect(hook.result.current.flush()).rejects.toThrow('inputs_changed');
  });
  expect(hook.result.current.dirty).toBe(true);
  return { ...hook, get, write };
}

describe('confirmed read must recover independently of background focus', () => {
  it('control: ordinary confirmed reload clears conflict without writing', async () => {
    const { result, get, write } = await conflicted();
    get.mockResolvedValue(fresh);
    await act(async () => { await result.current.reload(); });
    expect(result.current.error).toBeUndefined();
    expect(result.current.dirty).toBe(false);
    expect(result.current.state?.inputs_token).toBe('new-inputs');
    expect(write).toHaveBeenCalledTimes(1);
  });

  it.each(['focus-first', 'reload-first'])('focus during confirmed reload must not keep editor blocked: %s', async order => {
    const { result, get, write } = await conflicted();
    const reload = deferred<WorkingPlan>();
    const focus = deferred<WorkingPlan>();
    get.mockReset().mockReturnValueOnce(reload.promise).mockReturnValueOnce(focus.promise);
    let recovering!: Promise<WorkingPlan>;
    act(() => { recovering = result.current.reload(); });
    act(() => { window.dispatchEvent(new Event('focus')); });
    expect(get).toHaveBeenCalledTimes(1);
    await act(async () => {
      if (order === 'focus-first') { focus.resolve(fresh); await focus.promise; reload.resolve(fresh); }
      else { reload.resolve(fresh); await recovering; focus.resolve(fresh); }
      await recovering;
    });
    expect(result.current.error).toBeUndefined();
    expect(result.current.dirty).toBe(false);
    await act(async () => { await expect(result.current.flush()).resolves.toBeDefined(); });
    expect(write).toHaveBeenCalledTimes(1);
  });

  it('shares a pending recovery and accepts a lower revision after restore', async () => {
    const { result, get, write } = await conflicted();
    const read = deferred<WorkingPlan>();
    get.mockReset().mockReturnValueOnce(read.promise);
    let first!: Promise<WorkingPlan>, second!: Promise<WorkingPlan>;
    act(() => { first = result.current.reload(); second = result.current.reload(); });
    expect(first).toBe(second);
    expect(result.current.recovering).toBe(true);
    await act(async () => { await result.current.refreshInputs(); });
    expect(get).toHaveBeenCalledTimes(1);
    await act(async () => { read.resolve({ ...fresh, revision: 0, entries: [] }); await first; });
    expect(result.current.state?.revision).toBe(0);
    expect(result.current.dirty).toBe(false);
    expect(result.current.error).toBeUndefined();
    expect(result.current.recovering).toBe(false);
    expect(write).toHaveBeenCalledTimes(1);
  });

  it('ignores an older background response after manual recovery accepted', async () => {
    const { result, get } = await conflicted();
    const background = deferred<WorkingPlan>(), manual = deferred<WorkingPlan>();
    get.mockReset().mockReturnValueOnce(background.promise).mockReturnValueOnce(manual.promise);
    let bg!: Promise<void>, accepted!: Promise<WorkingPlan>;
    act(() => { bg = result.current.refreshInputs(); });
    act(() => { accepted = result.current.reload(); });
    await act(async () => { manual.resolve(fresh); await accepted; });
    await act(async () => { background.resolve({ ...initial, revision: 999 }); await bg; });
    expect(result.current.state).toEqual(fresh);
    expect(result.current.error).toBeUndefined();
    expect(result.current.dirty).toBe(false);
  });

  it('rejects an unmounted recovery instead of reporting accepted old state', async () => {
    const { result, get, unmount } = await conflicted();
    const read = deferred<WorkingPlan>();
    get.mockReturnValueOnce(read.promise);
    let accepted!: Promise<WorkingPlan>;
    act(() => { accepted = result.current.reload(); });
    unmount();
    await act(async () => {
      read.resolve(fresh);
      await expect(accepted).rejects.toThrow('workingPlan.conflict');
    });
  });
});
