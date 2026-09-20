import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../../api/client';
import { useMonitorResource } from '../../../features/monitor/useMonitorSnapshot';

describe('monitor polling lifecycle', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-10T14:00:00Z')); });
  afterEach(() => { vi.useRealTimers(); });
  it('clears both feeds when either loses authorization', async () => {
    const snapshot = vi.fn<() => Promise<string>>().mockResolvedValue('private snapshot');
    const forecast = vi.fn<() => Promise<string>>().mockResolvedValue('private forecast');
    const { result, unmount } = renderHook(() => ({
      snapshot: useMonitorResource(snapshot, 5000, true, false),
      forecast: useMonitorResource(forecast, 30000, true, false),
    }));
    await act(async () => {});
    forecast.mockRejectedValue(new ApiError('revoked', 401));
    await act(() => vi.advanceTimersByTimeAsync(30000));
    expect(result.current.snapshot.data).toBeUndefined();
    expect(result.current.forecast.data).toBeUndefined();
    expect(result.current.snapshot.terminal).toBe(true);
    const calls = snapshot.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(60000));
    expect(snapshot).toHaveBeenCalledTimes(calls);
    unmount();
  });
  it('uses one snapshot cycle regardless of fleet size and keeps stale data on a network error', async () => {
    const read = vi.fn<() => Promise<{ printers: unknown[] }>>().mockResolvedValue({ printers: Array.from({ length: 50 }) });
    const { result, unmount } = renderHook(() => useMonitorResource(read, 5000, true, false));
    await act(async () => {});
    expect(read).toHaveBeenCalledTimes(1);
    const timestamp = result.current.updatedAt;
    read.mockRejectedValue(new Error('network'));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(read).toHaveBeenCalledTimes(2);
    expect(result.current.data?.printers).toHaveLength(50);
    expect(result.current.updatedAt).toBe(timestamp);
    expect(result.current.terminal).toBe(false);
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(60000));
    expect(read).toHaveBeenCalledTimes(2);
  });
  it.each([401, 403])('erases protected data and stops after HTTP %s', async status => {
    const read = vi.fn().mockResolvedValue('private data');
    const { result, unmount } = renderHook(() => useMonitorResource(read, 5000, true, false));
    await act(async () => {});
    read.mockRejectedValue(new ApiError('denied', status));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(result.current.terminal).toBe(true); expect(result.current.data).toBeUndefined();
    await act(() => vi.advanceTimersByTimeAsync(3600000));
    expect(read).toHaveBeenCalledTimes(2);
    unmount();
  });
  it('does not overlap requests on online/visibility events and aborts on unmount', async () => {
    let signal: AbortSignal | undefined;
    const read = vi.fn((s: AbortSignal) => { signal = s; return new Promise<string>(() => {}); });
    const { unmount } = renderHook(() => useMonitorResource(read, 5000, true, false));
    await act(async () => { window.dispatchEvent(new Event('online')); document.dispatchEvent(new Event('visibilitychange')); });
    expect(read).toHaveBeenCalledTimes(1); expect(signal?.aborted).toBe(false);
    unmount(); expect(signal?.aborted).toBe(true);
  });
  it('a signed-out working window clears the authenticated monitor', async () => {
    const read = vi.fn().mockResolvedValue('private data');
    const { result, unmount } = renderHook(() => useMonitorResource(read, 5000, true, true));
    await act(async () => {});
    act(() => window.dispatchEvent(new Event('bamdude:auth-invalidated')));
    expect(result.current.data).toBeUndefined(); expect(result.current.terminal).toBe(true);
    unmount();
  });
});
