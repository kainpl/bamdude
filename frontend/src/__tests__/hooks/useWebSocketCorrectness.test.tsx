import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query';
import { createPrinterStatusBatcher } from '../../api/printerStatusBatch';
import type { PrinterStatus } from '../../api/client';
import { clearLiveStatusPriority, setLiveStatusPriority } from '../../utils/liveStatusPriority';

const mocks = vi.hoisted(() => ({ setConnected: vi.fn(), toast: vi.fn(), recordLive: vi.fn() }));
vi.mock('../../api/client', () => ({
  api: { getWebSocketToken: vi.fn().mockResolvedValue({ token: 'local-test' }) },
  ApiError: class extends Error {},
  recordLivePrinterStatus: (...args: unknown[]) => mocks.recordLive(...args),
}));
vi.mock('../../contexts/ToastContext', () => ({ useToast: () => ({ showToast: mocks.toast }) }));
vi.mock('../../contexts/ConnectionContext', () => ({ useConnection: () => ({ setIsConnected: mocks.setConnected }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

import { useWebSocket, wsReconnectDelay } from '../../hooks/useWebSocket';

class Socket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  send = vi.fn();

  constructor(_url: string) { Socket.instances.push(this); }
  close() { this.readyState = 3; this.onclose?.({ code: 1000, reason: '' }); }
  open() { this.readyState = Socket.OPEN; this.onopen?.(); }
  emit(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

let client: QueryClient;
beforeEach(() => {
  vi.useFakeTimers();
  Socket.instances = [];
  mocks.recordLive.mockReset();
  vi.stubGlobal('WebSocket', Socket);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function mounted() {
  const hook = renderHook(() => useWebSocket(), {
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  const socket = Socket.instances.at(-1)!;
  act(() => socket.open());
  return { ...hook, socket };
}

function statuses(socket: Socket, count = 20) {
  for (let id = 1; id <= count; id++) {
    socket.emit({ type: 'printer_status', printer_id: id, data: { state: 'RUNNING', progress: 1 } });
  }
}

describe('WebSocket cache and lifecycle contract', () => {
  it('uses bounded exponential reconnect delays with jitter', () => {
    expect([0, 1, 2, 3, 4].map(attempt => wsReconnectDelay(attempt, 1)))
      .toEqual([3000, 6000, 12000, 24000, 30000]);
    expect(wsReconnectDelay(0, 0)).toBe(2250);
  });

  it('holds hidden farm invalidations until one visible catch-up', async () => {
    const { socket } = await mounted();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const key = ['queue', 'summary'];
    client.setQueryData(key, 'old');
    const read = vi.fn().mockResolvedValue('new');
    const observer = new QueryObserver(client, {
      queryKey: key, queryFn: read, staleTime: Infinity,
      refetchOnWindowFocus: false, refetchOnReconnect: false,
    });
    const unsubscribe = observer.subscribe(() => {});
    try {
      act(() => { for (let index = 0; index < 100; index += 1) socket.emit({ type: 'queue_changed' }); });
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      expect(read).not.toHaveBeenCalled();
      expect(client.getQueryState(key)?.isInvalidated).toBe(true);
      visibility.mockReturnValue('visible');
      act(() => document.dispatchEvent(new Event('visibilitychange')));
      await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
      expect(read).toHaveBeenCalledTimes(1);
      expect(client.getQueryData(key)).toBe('new');
    } finally {
      visibility.mockRestore();
      unsubscribe();
    }
  });

  it('does not refetch a fresh farm summary on a quick Alt+Tab', async () => {
    await mounted();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const key = ['queue', 'summary'];
    client.setQueryData(key, 'fresh');
    const read = vi.fn().mockResolvedValue('new');
    const observer = new QueryObserver(client, { queryKey: key, queryFn: read, staleTime: Infinity, refetchOnWindowFocus: false });
    const unsubscribe = observer.subscribe(() => {});
    try {
      act(() => document.dispatchEvent(new Event('visibilitychange')));
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(read).not.toHaveBeenCalled();
    } finally {
      visibility.mockRestore();
      unsubscribe();
    }
  });

  it('turns 100 queue events during a slow farm read into one follow-up read', async () => {
    const { socket } = await mounted();
    const key = ['queue', 'all', 'pending'];
    let finishFirst!: (value: string) => void;
    const read = vi.fn()
      .mockImplementationOnce(() => new Promise<string>(resolve => { finishFirst = resolve; }))
      .mockResolvedValue('fresh');
    const observer = new QueryObserver(client, {
      queryKey: key, queryFn: read, refetchOnWindowFocus: false, refetchOnReconnect: false,
    });
    const unsubscribe = observer.subscribe(() => {});
    try {
      expect(read).toHaveBeenCalledTimes(1);
      act(() => { for (let index = 0; index < 100; index += 1) socket.emit({ type: 'queue_changed', printer_id: (index % 50) + 1 }); });
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      // The slow read is neither cancelled nor joined by a second one.
      expect(read).toHaveBeenCalledTimes(1);
      await act(async () => { finishFirst('old'); await Promise.resolve(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      expect(read).toHaveBeenCalledTimes(2);
      expect(client.getQueryData(key)).toBe('fresh');
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(read).toHaveBeenCalledTimes(2);
    } finally {
      unsubscribe();
    }
  });

  it('answers a simultaneous visible and online resume with one farm read', async () => {
    await mounted();
    const key = ['queue', 'summary'];
    client.setQueryData(key, 'old', { updatedAt: Date.now() - 60_000 });
    const read = vi.fn().mockResolvedValue('new');
    const observer = new QueryObserver(client, {
      queryKey: key, queryFn: read, staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false,
    });
    const unsubscribe = observer.subscribe(() => {});
    try {
      act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
        window.dispatchEvent(new Event('online'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(read).toHaveBeenCalledTimes(1);
      expect(client.getQueryData(key)).toBe('new');
    } finally {
      unsubscribe();
    }
  });
  it.each([50, 100, 500])('commits a %i-printer bootstrap before ACK', async count => {
    const { socket } = await mounted();
    act(() => {
      statuses(socket, count);
      socket.emit({ type: 'initial_status_complete', bootstrap_id: `fleet-${count}` });
    });
    expect(socket.send.mock.calls.some(([value]) => JSON.parse(value).type === 'initial_status_applied')).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(110); });
    for (let id = 1; id <= count; id++) {
      expect(client.getQueryData(['printerStatus', id])).toBeDefined();
    }
    expect(socket.send.mock.calls.map(([value]) => JSON.parse(value)).filter(message => message.type === 'initial_status_applied')).toHaveLength(1);
  });

  it('isolates ten synthetic viewers during a 100-printer burst', async () => {
    const viewers = Array.from({ length: 10 }, () => {
      const cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
      const hook = renderHook(() => useWebSocket(), {
        wrapper: ({ children }) => <QueryClientProvider client={cache}>{children}</QueryClientProvider>,
      });
      return { cache, hook };
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(Socket.instances).toHaveLength(10);
    act(() => Socket.instances.forEach((socket, index) => {
      socket.open();
      statuses(socket, 100);
      socket.emit({ type: 'initial_status_complete', bootstrap_id: `viewer-${index}` });
    }));
    await act(async () => { await vi.advanceTimersByTimeAsync(110); });
    viewers.forEach(({ cache, hook }, index) => {
      expect(cache.getQueryData(['printerStatus', 100])).toBeDefined();
      expect(cache.getQueryCache().findAll({ queryKey: ['printerStatus'] })).toHaveLength(100);
      expect(Socket.instances[index].send.mock.calls.map(([value]) => JSON.parse(value))
        .filter(message => message.type === 'initial_status_applied')).toHaveLength(1);
      hook.unmount();
      cache.clear();
    });
  });

  it('does not ACK a marker until the active chunk and pre-marker tail commit', async () => {
    const { socket } = await mounted();
    act(() => { statuses(socket); vi.advanceTimersByTime(100); });
    expect(client.getQueryData(['printerStatus', 20])).toBeUndefined();
    await act(async () => {
      socket.emit({ type: 'printer_status', printer_id: 21, data: { progress: 2 } });
      socket.emit({ type: 'initial_status_complete', bootstrap_id: 'barrier' });
      await Promise.resolve();
    });
    const acknowledgements = () => socket.send.mock.calls.map(([value]) => JSON.parse(value))
      .filter(message => message.type === 'initial_status_applied');
    expect(acknowledgements()).toHaveLength(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    expect(client.getQueryData(['printerStatus', 20])).toBeDefined();
    expect(client.getQueryData(['printerStatus', 21])).toBeDefined();
    expect(acknowledgements()).toHaveLength(1);
  });

  it('does not extend a marker target with statuses received afterward', async () => {
    const { socket } = await mounted();
    act(() => { statuses(socket); vi.advanceTimersByTime(100); });
    let postMarkerAtAck: unknown = 'not-sent';
    socket.send.mockImplementation(value => {
      if (JSON.parse(value).type === 'initial_status_applied') {
        postMarkerAtAck = client.getQueryData(['printerStatus', 100]);
      }
    });
    act(() => {
      socket.emit({ type: 'initial_status_complete', bootstrap_id: 'fixed-target' });
      for (let id = 21; id <= 100; id++) {
        socket.emit({ type: 'printer_status', printer_id: id, data: { progress: id } });
      }
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    expect(client.getQueryData(['printerStatus', 20])).toBeDefined();
    expect(postMarkerAtAck).toBeUndefined();
    expect(client.getQueryData(['printerStatus', 100])).toBeDefined();
  });

  it('commits the offscreen round tail despite new visible-printer traffic', async () => {
    const { socket } = await mounted();
    setLiveStatusPriority('ws-fairness', [1]);
    let incoming = 0;
    const unsubscribe = client.getQueryCache().subscribe(event => {
      if (event.type === 'updated' && event.query.queryKey[0] === 'printerStatus'
        && event.query.queryKey[1] !== 1 && incoming < 100) {
        socket.emit({ type: 'printer_status', printer_id: 1, data: { progress: ++incoming } });
      }
    });
    try {
      act(() => statuses(socket, 100));
      await act(async () => { await vi.advanceTimersByTimeAsync(120); });
      expect(incoming).toBeGreaterThan(50);
      expect(client.getQueryData(['printerStatus', 100])).toBeDefined();
    } finally {
      unsubscribe();
      clearLiveStatusPriority('ws-fairness');
    }
  });

  it('does not repopulate the cache after unmount during a status chunk', async () => {
    const { socket, unmount } = await mounted();
    act(() => { statuses(socket); vi.advanceTimersByTime(100); });
    unmount();
    client.clear();
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    expect(client.getQueryData(['printerStatus', 20])).toBeUndefined();
  });

  it('isolates a failing ordinary domain handler from the following frame', async () => {
    const { socket } = await mounted();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    mocks.toast.mockImplementationOnce(() => { throw new Error('toast failed'); });
    act(() => {
      socket.emit({ type: 'macro_executed', data: { message: 'test', success: true } });
      socket.emit({ type: 'inbox_item' });
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['inbox'] });
    mocks.toast.mockReset();
  });

  it('marks continuously dirty library views stale within five seconds', async () => {
    const { socket } = await mounted();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    for (let index = 0; index < 16; index++) {
      act(() => {
        socket.emit({ type: 'library_file_added' });
        if (index % 4 === 0) socket.emit({ type: 'archive_created' });
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      if (index === 2) {
        expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['library-files'] }));
      }
    }
    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['library-files'] }));
    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['library-stats'] }));
    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['archives'] }));
    expect(invalidate.mock.calls.filter(([options]) => options?.queryKey?.[0] === 'library-files').length).toBeGreaterThanOrEqual(5);
  });

  it('never commits an older staged status over a newer received patch', async () => {
    const { socket } = await mounted();
    act(() => { statuses(socket); vi.advanceTimersByTime(100); });
    act(() => {
      socket.emit({ type: 'printer_status', printer_id: 20, data: { progress: 2 } });
      client.setQueryData(['printerStatus', 20], { progress: 2 });
    });
    const observed: number[] = [];
    const unsubscribe = client.getQueryCache().subscribe(event => {
      if (event.query.queryKey[0] === 'printerStatus' && event.query.queryKey[1] === 20) {
        observed.push((event.query.state.data as { progress: number }).progress);
      }
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    unsubscribe();
    expect(observed).not.toContain(1);
    expect((client.getQueryData(['printerStatus', 20]) as { progress: number }).progress).toBe(2);
  });

  it('preserves earlier-only fields, REST enrichment and non-null wifi across partial patches', async () => {
    const { socket } = await mounted();
    client.setQueryData(['printerStatus', 1], { wifi_signal: -51, rest_only: 'enriched' });
    act(() => {
      socket.emit({ type: 'printer_status', printer_id: 1, data: { state: 'RUNNING', progress: 100, early_only: 'kept' } });
      socket.emit({ type: 'printer_status', printer_id: 1, data: { progress: 0, wifi_signal: null } });
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(client.getQueryData(['printerStatus', 1])).toMatchObject({
      state: 'RUNNING', progress: 0, early_only: 'kept', rest_only: 'enriched', wifi_signal: -51,
    });
  });

  it('keeps a delayed real status-batcher REST reply in sync with the hook', async () => {
    const { socket } = await mounted();
    let finishRest!: (value: PrinterStatus) => void;
    const batch = createPrinterStatusBatcher(
      () => new Promise<PrinterStatus>(resolve => { finishRest = resolve; }),
      async () => ({}),
      () => new Error('missing'),
    );
    mocks.recordLive.mockImplementation(batch.update);
    const rest = batch.get(20);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    act(() => { statuses(socket); vi.advanceTimersByTime(100); });
    act(() => socket.emit({ type: 'printer_status', printer_id: 20, data: { progress: 0, wifi_signal: null } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    finishRest({ state: 'IDLE', progress: 80, wifi_signal: -40, rest_only: 'archive' } as unknown as PrinterStatus);
    const merged = await rest;
    expect(merged).toMatchObject({ state: 'RUNNING', progress: 0, wifi_signal: -40, rest_only: 'archive' });
    expect(client.getQueryData(['printerStatus', 20])).toMatchObject({ state: 'RUNNING', progress: 0 });
  });

  it('does not duplicate an empty marker ACK and cancels old socket chunks on reconnect', async () => {
    const { socket } = await mounted();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    act(() => {
      socket.emit({ type: 'initial_status_complete', bootstrap_id: 'empty' });
      socket.emit({ type: 'initial_status_complete', bootstrap_id: 'empty' });
      socket.emit({ type: 'library_file_added' });
      statuses(socket);
      vi.advanceTimersByTime(100);
      socket.close();
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(3001); });
    const replacement = Socket.instances.at(-1)!;
    act(() => replacement.open());
    act(() => socket.emit({ type: 'printer_status', printer_id: 99, data: { progress: 99 } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    expect(socket.send.mock.calls.map(([value]) => JSON.parse(value)).filter(message => message.type === 'initial_status_applied')).toHaveLength(1);
    expect(client.getQueryData(['printerStatus', 20])).toBeUndefined();
    expect(client.getQueryData(['printerStatus', 99])).toBeUndefined();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['library-files'], refetchType: 'none' });
    act(() => replacement.emit({ type: 'printer_status', printer_id: 20, data: { progress: 0 } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(client.getQueryData(['printerStatus', 20])).toMatchObject({ progress: 0 });
  });

  it('refuses bootstrap success when a status cannot enter the cache', async () => {
    const { socket } = await mounted();
    const write = vi.spyOn(client, 'setQueryData').mockImplementationOnce(() => { throw new Error('cache failed'); });
    act(() => {
      socket.emit({ type: 'printer_status', printer_id: 1, data: { state: 'RUNNING' } });
      socket.emit({ type: 'initial_status_complete', bootstrap_id: 'failed' });
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(110); });
    expect(socket.readyState).toBe(3);
    expect(socket.send.mock.calls.map(([value]) => JSON.parse(value))
      .filter(message => message.type === 'initial_status_applied')).toHaveLength(0);
    write.mockRestore();
  });

  it('paces active reads and caps outstanding WS refetches at four', async () => {
    const { socket } = await mounted();
    const started: number[] = [];
    const reads = vi.fn(() => {
      started.push(performance.now());
      return new Promise<string>(() => {});
    });
    const observers = Array.from({ length: 8 }, (_, index) => {
      const queryKey = ['library-files', index];
      client.setQueryData(queryKey, 'old');
      const observer = new QueryObserver(client, { queryKey, queryFn: reads, staleTime: Infinity });
      return observer.subscribe(() => {});
    });
    act(() => socket.emit({ type: 'library_file_added' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(3001); });
    expect(reads).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(reads).toHaveBeenCalledTimes(4);
    expect(started.slice(1).every((at, index) => at - started[index] >= 500)).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(reads).toHaveBeenCalledTimes(4);
    observers.forEach(unsubscribe => unsubscribe());
  });

  it('marks inactive and disabled library queries stale without fetching them', async () => {
    const { socket } = await mounted();
    const read = vi.fn().mockResolvedValue('new');
    client.setQueryData(['library-files', 'inactive'], 'old');
    client.setQueryData(['library-files', 'disabled'], 'old');
    const disabled = new QueryObserver(client, {
      queryKey: ['library-files', 'disabled'], queryFn: read, enabled: false,
    });
    const unsubscribe = disabled.subscribe(() => {});
    act(() => socket.emit({ type: 'library_file_added' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(read).not.toHaveBeenCalled();
    expect(client.getQueryState(['library-files', 'inactive'])?.isInvalidated).toBe(true);
    expect(client.getQueryState(['library-files', 'disabled'])?.isInvalidated).toBe(true);
    unsubscribe();
  });

  it('consumes an overdue dirty deadline on visibility resume without resetting it', async () => {
    let monotonic = 0;
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => monotonic);
    try {
      const { socket } = await mounted();
      client.setQueryData(['library-files', 'offscreen'], 'old');
      act(() => socket.emit({ type: 'library_file_added' }));
      expect(client.getQueryState(['library-files', 'offscreen'])?.isInvalidated).toBe(false);
      monotonic = 6000; // Simulate suspended timers: no timer callback has run.
      act(() => document.dispatchEvent(new Event('visibilitychange')));
      expect(client.getQueryState(['library-files', 'offscreen'])?.isInvalidated).toBe(true);
    } finally {
      clock.mockRestore();
    }
  });

  it('does not duplicate a pending or running WS read on focus refresh', async () => {
    const { socket } = await mounted();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const queryKey = ['library-files', 'focus'];
    client.setQueryData(queryKey, 'old');
    const read = vi.fn(() => new Promise<string>(() => {}));
    const observer = new QueryObserver(client, { queryKey, queryFn: read, staleTime: Infinity });
    const unsubscribe = observer.subscribe(() => {});
    try {
      act(() => socket.emit({ type: 'library_file_added' }));
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      act(() => document.dispatchEvent(new Event('visibilitychange')));
      expect(read).not.toHaveBeenCalled();
      await act(async () => { await vi.advanceTimersByTimeAsync(2001); });
      expect(read).toHaveBeenCalledTimes(1);
      act(() => document.dispatchEvent(new Event('visibilitychange')));
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(read).toHaveBeenCalledTimes(1);
    } finally {
      visibility.mockRestore();
      unsubscribe();
    }
  });

  it('does not cancel a polling read and follows it with one fresh read', async () => {
    const { socket } = await mounted();
    let finishFirst!: (value: string) => void;
    const reads = vi.fn()
      .mockImplementationOnce(() => new Promise<string>(resolve => { finishFirst = resolve; }))
      .mockResolvedValue('fresh');
    const observer = new QueryObserver(client, { queryKey: ['library-files', 1], queryFn: reads });
    const unsubscribe = observer.subscribe(() => {});
    expect(reads).toHaveBeenCalledTimes(1);
    act(() => socket.emit({ type: 'library_file_added' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(3001); });
    expect(reads).toHaveBeenCalledTimes(1);
    await act(async () => { finishFirst('old'); await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(501); });
    expect(reads).toHaveBeenCalledTimes(2);
    expect(client.getQueryData(['library-files', 1])).toBe('fresh');
    unsubscribe();
  });

  it('does not spin on a failed WS-triggered read', async () => {
    const { socket } = await mounted();
    const queryKey = ['library-files', 'failed'];
    client.setQueryData(queryKey, 'old');
    const read = vi.fn().mockRejectedValue(new Error('offline'));
    const observer = new QueryObserver(client, { queryKey, queryFn: read, staleTime: Infinity });
    const unsubscribe = observer.subscribe(() => {});
    act(() => socket.emit({ type: 'library_file_added' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(read).toHaveBeenCalledTimes(1);
    expect(client.getQueryState(queryKey)?.error).toBeTruthy();
    unsubscribe();
  });

  it('does not launch queued reads after unmount', async () => {
    const { socket, unmount } = await mounted();
    const reads = vi.fn(() => new Promise<string>(() => {}));
    const unsubscribe = Array.from({ length: 5 }, (_, index) => {
      const queryKey = ['library-files', index];
      client.setQueryData(queryKey, 'old');
      return new QueryObserver(client, { queryKey, queryFn: reads, staleTime: Infinity }).subscribe(() => {});
    });
    act(() => socket.emit({ type: 'library_file_added' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(3001); });
    expect(reads).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(reads).toHaveBeenCalledTimes(1);
    unsubscribe.forEach(stop => stop());
  });

  it('cancels its own running read so a late reply cannot fill the old owner cache', async () => {
    const { socket, unmount } = await mounted();
    let finish!: (value: string) => void;
    const queryKey = ['library-files', 'owned'];
    client.setQueryData(queryKey, 'old');
    const observer = new QueryObserver(client, {
      queryKey, queryFn: () => new Promise<string>(resolve => { finish = resolve; }), staleTime: Infinity,
    });
    const unsubscribe = observer.subscribe(() => {});
    act(() => socket.emit({ type: 'library_file_added' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(3001); });
    unmount();
    await act(async () => { finish('late'); await Promise.resolve(); });
    expect(client.getQueryData(queryKey)).toBe('old');
    unsubscribe();
  });

  it('cancels deferred stale marks and paced reads on unmount', async () => {
    const { socket, unmount } = await mounted();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    act(() => socket.emit({ type: 'library_file_added' }));
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(invalidate).not.toHaveBeenCalled();
  });
});
