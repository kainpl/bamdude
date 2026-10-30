import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, type WorkingPlan, type WorkingEntry } from '../../api/client';
import { useWorkingPlan } from '../../hooks/useWorkingPlan';

const entry = (count: number): WorkingEntry => ({ entry_id: 'one', line_id: 1, plate_id: 2, count,
  target: { kind: 'auto' }, source_token: 'source', manual: true, protected_count: count });
const initial: WorkingPlan = { schema_version: 1, revision: 1, saved_revision: 4,
  entries: [entry(20)], saved_at: '2026-10-20T12:00:00', saved_by: 'operator',
  epoch: 0, saved_epoch: 0, active_operation: null, inputs_token: 'inputs', issues: [], changed: false };
afterEach(() => vi.restoreAllMocks());

describe('shared working draft writer', () => {
  it('keeps an unacknowledged edit on its original revision when focus discovers a foreign write', async () => {
    let stored = structuredClone(initial);
    vi.spyOn(api, 'getWorkingPlan').mockImplementation(async () => structuredClone(stored));
    const write = vi.spyOn(api, 'writeWorkingPlan');
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state?.revision).toBe(1));
    act(() => result.current.change([entry(18)]));
    stored = { ...stored, revision: 2, entries: [entry(14)] };
    await act(async () => { await result.current.refreshInputs(); });
    expect(result.current.error).toBeDefined();
    expect(result.current.dirty).toBe(true);
    expect(result.current.state?.revision).toBe(1);
    await act(async () => { await expect(result.current.flush()).rejects.toThrow(); });
    expect(write).not.toHaveBeenCalled();
  });
  it('does not start another autosave after a confirmed departure while the first write is pending', async () => {
    vi.spyOn(api, 'getWorkingPlan').mockResolvedValue(initial);
    let release!: (value: WorkingPlan) => void;
    const write = vi.spyOn(api, 'writeWorkingPlan').mockImplementation(() => new Promise(resolve => { release = resolve; }));
    const { result, unmount } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state).toEqual(initial));
    let flushed!: Promise<WorkingPlan>;
    act(() => { result.current.change([entry(18)]); flushed = result.current.flush(); });
    act(() => { result.current.change([entry(17)]); });
    unmount();
    await act(async () => { release({ ...initial, revision: 2, entries: [entry(18)] }); await flushed; });
    expect(write).toHaveBeenCalledTimes(1);
  });
  it('accepts a foreign revision after an acknowledged edit instead of retaining a phantom desired payload', async () => {
    let stored = structuredClone(initial);
    vi.spyOn(api, 'getWorkingPlan').mockImplementation(async () => structuredClone(stored));
    vi.spyOn(api, 'writeWorkingPlan').mockImplementation(async (_, body) => {
      stored = { ...stored, revision: stored.revision + 1, entries: body.entries };
      return structuredClone(stored);
    });
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state?.revision).toBe(1));
    await act(async () => { result.current.change([entry(18)]); await result.current.flush(); });
    expect(result.current.dirty).toBe(false);
    stored = { ...stored, revision: 3, entries: [entry(14)] };
    await act(async () => { await result.current.refreshInputs(); });
    expect(result.current.error).toBeUndefined();
    expect(result.current.state?.entries[0].count).toBe(14);
    expect(result.current.dirty).toBe(false);
  });

  it('adopts server identities for an added row without an autosave loop', async () => {
    vi.spyOn(api, 'getWorkingPlan').mockResolvedValue({ ...initial, revision: 0, entries: [] });
    const write = vi.spyOn(api, 'writeWorkingPlan').mockImplementation(async (_, body) => ({ ...initial,
      entries: body.entries.map(e => ({ ...e, source_token: 'server-source', line_token: 'server-line' })) }));
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state?.revision).toBe(0));
    await act(async () => { result.current.change([{ ...entry(2), source_token: undefined }]); await result.current.flush(); });
    expect(write).toHaveBeenCalledTimes(1);
    expect(result.current.dirty).toBe(false);
    expect(result.current.state?.entries[0].source_token).toBe('server-source');
  });

  it('ignores a context read that completes after a newer own write', async () => {
    let release!: (value: WorkingPlan) => void;
    vi.spyOn(api, 'getWorkingPlan').mockResolvedValueOnce(initial)
      .mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    let finishWrite!: () => void;
    vi.spyOn(api, 'writeWorkingPlan').mockImplementation((_, body) => new Promise(resolve => {
      finishWrite = () => resolve({ ...initial, revision: 2, entries: body.entries });
    }));
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state?.revision).toBe(1));
    let context!: ReturnType<typeof result.current.refreshInputs>, flushed!: Promise<WorkingPlan>;
    act(() => { result.current.change([entry(18)]); flushed = result.current.flush(); });
    act(() => { context = result.current.refreshInputs(); });
    await act(async () => { finishWrite(); });
    await waitFor(() => expect(result.current.state?.revision).toBe(2));
    await act(async () => { release(initial); await context; await flushed; });
    expect(result.current.state?.revision).toBe(2);
    expect(result.current.error).toBeUndefined();
  });
  it('serializes a new edit behind its pending write without moving the baseline', async () => {
    vi.spyOn(api, 'getWorkingPlan').mockResolvedValue(initial);
    let release!: (value: WorkingPlan) => void;
    const write = vi.spyOn(api, 'writeWorkingPlan').mockImplementationOnce(() => new Promise(resolve => { release = resolve; }))
      .mockImplementation(async (_, body) => ({ ...initial, revision: 3, entries: body.entries }));
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state).toEqual(initial));
    let flushed!: Promise<WorkingPlan>;
    act(() => { result.current.change([entry(18)]); flushed = result.current.flush(); });
    act(() => result.current.change([entry(17)]));
    expect(write).toHaveBeenCalledTimes(1);
    await act(async () => { release({ ...initial, revision: 2, entries: [entry(18)] }); await flushed; });
    expect(write).toHaveBeenCalledTimes(2);
    expect(write.mock.calls[1][1]).toMatchObject({ expected_revision: 2, entries: [entry(17)] });
    expect(result.current.state?.entries[0].count).toBe(17);
    expect(result.current.state?.saved_revision).toBe(4);
  });

  it('keeps local edits and stops autosave on conflict', async () => {
    vi.spyOn(api, 'getWorkingPlan').mockResolvedValue(initial);
    const write = vi.spyOn(api, 'writeWorkingPlan').mockRejectedValue(new Error('conflict'));
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state).toEqual(initial));
    await act(async () => { result.current.change([entry(18)]); await expect(result.current.flush()).rejects.toThrow('conflict'); });
    expect(result.current.dirty).toBe(true);
    expect(result.current.state?.entries[0].count).toBe(20);
    await act(async () => { result.current.change([entry(17)]); await expect(result.current.flush()).rejects.toThrow(); });
    expect(write).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeDefined();
  });

  it('saves the clicked baseline while keeping newer input as a draft', async () => {
    vi.spyOn(api, 'getWorkingPlan').mockResolvedValue(initial);
    let stored = initial;
    vi.spyOn(api, 'writeWorkingPlan').mockImplementation(async (_, body) => {
      stored = { ...stored, entries: body.entries, revision: stored.revision + 1 }; return stored;
    });
    let finish!: (value: WorkingPlan) => void;
    const save = vi.spyOn(api, 'actWorkingPlan').mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state).toEqual(initial));
    let confirming!: Promise<WorkingPlan>;
    act(() => { result.current.change([entry(18)]); confirming = result.current.save([entry(18)]); });
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    act(() => result.current.change([entry(17)]));
    await act(async () => { stored = { ...stored, saved_revision: 5 }; finish(stored); await confirming; });
    await waitFor(() => expect(result.current.state?.entries[0].count).toBe(17));
    expect(save.mock.calls[0][2].entries[0].count).toBe(18);
    expect(result.current.state?.saved_revision).toBe(5);
  });

  it('does not write on GET and does not rewrite equal values with different property order', async () => {
    vi.spyOn(api, 'getWorkingPlan').mockResolvedValue(initial);
    const write = vi.spyOn(api, 'writeWorkingPlan');
    const { result } = renderHook(() => useWorkingPlan(1, true));
    await waitFor(() => expect(result.current.state).toEqual(initial));
    expect(write).not.toHaveBeenCalled();
    await act(async () => { result.current.change([{ ...entry(20), count: 20 }]); await result.current.flush(); });
    expect(write).not.toHaveBeenCalled();
  });
});
