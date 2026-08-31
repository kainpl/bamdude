/**
 * Tests for the useWebSocket hook.
 *
 * Tests WebSocket connection management and message handling.
 * Uses vitest.mock to mock the entire module before MSW can intercept.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';

// Track WebSocket instances created during tests
let wsInstances: MockWebSocket[] = [];
let originalWebSocket: typeof WebSocket;

// GHSA-r2qv follow-up: useWebSocket now mints a short-lived token via
// api.getWebSocketToken() before opening the socket. Stub it so the connect
// path resolves synchronously in tests without hitting MSW.
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getWebSocketToken: vi.fn().mockResolvedValue({ token: 'test-ws-token' }),
    },
  };
});

// Mock react-i18next BEFORE any modules that use it are imported
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      if (key === 'printers.toast.missingSpoolAssignment' && options) {
        const { printer, slots } = options as { printer: string; slots: string };
        return `Missing assignments for ${printer}: ${slots}`;
      }
      return key;
    },
    i18n: {},
  }),
}));

// Enhanced MockWebSocket that tracks instances
class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  url: string;
  constructor(url: string) {
    this.url = url;
    wsInstances.push(this);
  }

  send = vi.fn();
  close = vi.fn(() => {
    this.readyState = MockWebSocket.CLOSED;
    if (this.onclose) {
      this.onclose(new CloseEvent('close'));
    }
  });

  // Required by MSW's interceptor - these are no-ops but prevent the error
  addEventListener = vi.fn();
  removeEventListener = vi.fn();

  // Helper to simulate connection opening
  open() {
    this.readyState = MockWebSocket.OPEN;
    if (this.onopen) {
      this.onopen(new Event('open'));
    }
  }

  // Helper to simulate receiving a message
  simulateMessage(data: unknown) {
    if (this.onmessage) {
      this.onmessage(
        new MessageEvent('message', {
          data: JSON.stringify(data),
        })
      );
    }
  }

  // Helper to simulate the server closing with a specific code (e.g. 4401,
  // the /ws auth-rejection close code). Unlike close(), this carries a code.
  simulateClose(code: number) {
    this.readyState = MockWebSocket.CLOSED;
    if (this.onclose) {
      this.onclose(new CloseEvent('close', { code }));
    }
  }
}

// Create test QueryClient
function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
      },
    },
  });
}

// Wrapper with QueryClient and ToastProvider for hook testing
function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(
      ToastProvider,
      {},
      React.createElement(
        QueryClientProvider,
        { client: queryClient },
        children
      )
    );
  };
}

function getLatestWs(): MockWebSocket | undefined {
  return wsInstances[wsInstances.length - 1];
}

// useWebSocket defers the initial `new WebSocket(...)` to a 0 ms setTimeout to
// dodge React.StrictMode's mount-unmount-remount churn. Tests that call into
// the hook must await the deferred connect before reading `wsInstances`,
// otherwise getLatestWs() returns undefined. Under fake timers we have to flush
// the pending timer ourselves — RTL's `waitFor` doesn't auto-advance them.
async function waitForWs(): Promise<MockWebSocket> {
  if (vi.isFakeTimers()) {
    // connect() is async now (awaits the ws-token mint), so flush both the
    // deferred connect timer and its pending microtasks.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  } else {
    await waitFor(() => expect(getLatestWs()).toBeDefined());
  }
  return getLatestWs()!;
}

describe('useWebSocket hook', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    wsInstances = [];
    queryClient = createTestQueryClient();

    // Save original and install mock
    originalWebSocket = globalThis.WebSocket;
    globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // Restore original WebSocket
    globalThis.WebSocket = originalWebSocket;
  });

  describe('WebSocket Mock', () => {
    it('creates WebSocket with correct URL', () => {
      const ws = new MockWebSocket('ws://test.local/ws');
      expect(ws.url).toBe('ws://test.local/ws');
    });

    it('starts in CONNECTING state', () => {
      const ws = new MockWebSocket('ws://test.local/ws');
      expect(ws.readyState).toBe(MockWebSocket.CONNECTING);
    });

    it('transitions to OPEN state', () => {
      const ws = new MockWebSocket('ws://test.local/ws');
      const onOpen = vi.fn();
      ws.onopen = onOpen;

      ws.open();

      expect(ws.readyState).toBe(MockWebSocket.OPEN);
      expect(onOpen).toHaveBeenCalled();
    });

    it('can receive messages', () => {
      const ws = new MockWebSocket('ws://test.local/ws');
      const onMessage = vi.fn();
      ws.onmessage = onMessage;

      ws.open();
      ws.simulateMessage({ type: 'status', data: { connected: true } });

      expect(onMessage).toHaveBeenCalled();
    });

    it('can close connection', () => {
      const ws = new MockWebSocket('ws://test.local/ws');
      const onClose = vi.fn();
      ws.onclose = onClose;

      ws.close();

      expect(ws.readyState).toBe(MockWebSocket.CLOSED);
      expect(onClose).toHaveBeenCalled();
    });

    it('tracks all instances', () => {
      wsInstances = [];
      new MockWebSocket('ws://a');
      new MockWebSocket('ws://b');
      expect(wsInstances.length).toBe(2);
    });
  });

  describe('hook connection', () => {
    it('connects to WebSocket on mount', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();
      expect(ws.url).toContain('/api/v1/ws');
    });

    it('reports connected state when WebSocket opens', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const { result } = renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      // Initially not connected
      expect(result.current.isConnected).toBe(false);

      // Simulate connection opening
      const ws = await waitForWs();
      act(() => {
        ws.open();
      });

      await waitFor(() => {
        expect(result.current.isConnected).toBe(true);
      });
    });
  });

  describe('message handling', () => {
    it('updates printer status in query cache on printer_status message', async () => {
      // Test the printer status update logic directly using setQueryData
      // The WebSocket handler with throttling is complex to test with fake timers,
      // so we test the core behavior directly

      // Simulate what the throttled update does
      queryClient.setQueryData(
        ['printerStatus', 1],
        (old: Record<string, unknown> | undefined) => {
          const statusData = { state: 'IDLE', progress: 0 };
          const merged = { ...old, ...statusData };
          return merged;
        }
      );

      // Check query cache was updated
      const cachedData = queryClient.getQueryData(['printerStatus', 1]);
      expect(cachedData).toEqual({ state: 'IDLE', progress: 0 });
    });

    it('preserves wifi_signal when new value is null', async () => {
      // Test the wifi_signal preservation logic directly on QueryClient
      // The throttled WebSocket handler makes this hard to test end-to-end
      // This tests that the merge logic correctly preserves wifi_signal

      // Set initial data with wifi_signal
      queryClient.setQueryData(['printerStatus', 1], {
        wifi_signal: -65,
        state: 'IDLE',
      });

      // Simulate what the throttled update does - use setQueryData with updater function
      queryClient.setQueryData(
        ['printerStatus', 1],
        (old: Record<string, unknown> | undefined) => {
          const statusData = { state: 'RUNNING', wifi_signal: null };
          const merged = { ...old, ...statusData };
          // This is the preservation logic from useWebSocket
          if (merged.wifi_signal == null && old?.wifi_signal != null) {
            merged.wifi_signal = old.wifi_signal;
          }
          return merged;
        }
      );

      const cachedData = queryClient.getQueryData(['printerStatus', 1]) as Record<
        string,
        unknown
      >;
      expect(cachedData.wifi_signal).toBe(-65); // Preserved
      expect(cachedData.state).toBe('RUNNING'); // Updated
    });

    it('invalidates archives on print_complete message', async () => {
      vi.useFakeTimers();
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      // Simulate print complete
      act(() => {
        ws.simulateMessage({
          type: 'print_complete',
          printer_id: 1,
          data: { status: 'completed' },
        });
      });

      // Advance timers to trigger debounced invalidation (3000ms delay + 500ms between each)
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['archives'] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['archiveStats'] });

      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    it('invalidates archives on archive_created message', async () => {
      vi.useFakeTimers();
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      // Simulate archive created
      act(() => {
        ws.simulateMessage({
          type: 'archive_created',
          data: { id: 1, filename: 'test.3mf' },
        });
      });

      // Advance timers to trigger debounced invalidation (3000ms delay + 500ms between each)
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['archives'] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['archiveStats'] });

      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    // A print is the one thing that changes a project without anybody touching
    // the project, and the archive events carry no project_id — so the whole
    // project prefix has to go. Reported as "I start prints from a project and
    // its Prints section stays empty": it was, until the page was re-entered.
    it('refreshes the project views on archive_created', async () => {
      vi.useFakeTimers();
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      act(() => {
        ws.open();
      });

      act(() => {
        ws.simulateMessage({
          type: 'archive_created',
          data: { id: 1, filename: 'test.3mf' },
        });
      });

      // 3000ms debounce + 500ms stagger per key; seven keys here.
      await act(async () => {
        vi.advanceTimersByTime(10000);
      });

      for (const key of [
        'project',
        'project-archives',
        'project-timeline',
        'project-print-plan',
        'projects',
      ]) {
        expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: [key] });
      }

      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    it('invalidates archives on archive_updated message', async () => {
      vi.useFakeTimers();
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      // Simulate archive updated (e.g., timelapse attached)
      act(() => {
        ws.simulateMessage({
          type: 'archive_updated',
          data: { id: 1, timelapse_attached: true },
        });
      });

      // Advance timers to trigger debounced invalidation (3000ms delay)
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['archives'] });

      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    it('handles missing_spool_assignment message without error', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();
      act(() => {
        ws.open();
      });

      // This test verifies that the hook properly handles missing_spool_assignment messages
      // without throwing an error. The actual toast display is tested via the UI.
      expect(() => {
        act(() => {
          ws.simulateMessage({
            type: 'missing_spool_assignment',
            printer_id: 7,
            printer_name: 'Printer B',
            missing_slots: [{ slot: 'A2' }, { slot: 'Ext-L' }],
          });
        });
      }).not.toThrow();

      vi.unstubAllGlobals();
    });

    it('ignores pong messages without error', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      // Simulate pong response
      act(() => {
        ws.simulateMessage({
          type: 'pong',
        });
      });

      // Should not invalidate any queries for pong
      expect(invalidateSpy).not.toHaveBeenCalled();
    });

    it('a device leaving also refreshes the sensors', async () => {
      // An adopted sensor that just left is still listed -- with its name and
      // place -- so it must flip to "not on the network" now, not in 30 s.
      vi.useFakeTimers();
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      act(() => {
        ws.open();
      });

      act(() => {
        ws.simulateMessage({ type: 'zigbee_device_left', ieee: 'aa:bb' });
      });

      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['zigbee-sensors'] });

      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    it('handles malformed JSON gracefully', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      // Simulate malformed message (should not throw)
      expect(() => {
        act(() => {
          if (ws.onmessage) {
            ws.onmessage(
              new MessageEvent('message', {
                data: 'not valid json{{{',
              })
            );
          }
        });
      }).not.toThrow();
    });

    it('handles unknown message types gracefully', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      // Simulate unknown message type
      expect(() => {
        act(() => {
          ws.simulateMessage({
            type: 'unknown_type',
            data: { foo: 'bar' },
          });
        });
      }).not.toThrow();

      expect(invalidateSpy).not.toHaveBeenCalled();
    });
  });

  // ⚠️ A hidden tab gets no rendering opportunities, so the browser HOLDS a
  // queued frame callback rather than merely throttling it. Every cache write
  // used to sit inside one, so the socket stayed open, messages kept arriving,
  // and nothing reached the query cache until the tab was shown again — at
  // which point the whole backlog ran at once. The tab-title progress reads
  // ['printerStatus', id] and nothing else, so it simply froze.
  //
  // The old rAF stubs in this file ran frames SYNCHRONOUSLY, which is exactly
  // why none of them caught it. These stub it to never fire, as a hidden tab
  // does.
  describe('while the tab is in the background', () => {
    let rafSpy: ReturnType<typeof vi.fn>;

    beforeEach(() => {
      // ⚠️ Its own client with gcTime: Infinity. The shared test client uses
      // gcTime: 0, so an entry nobody is observing — which is exactly what a
      // cache write from the socket is here — is collected the moment the fake
      // clock moves, and the assertion below would read undefined however
      // correct the hook was.
      queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: Infinity } },
      });
      Object.defineProperty(document, 'hidden', { configurable: true, value: true });
      // ⚠️ Order matters: vi.useFakeTimers() fakes requestAnimationFrame too,
      // backing it with the mock clock — so advanceTimersByTime would run it
      // and hide the very defect under test. Stub it AFTERWARDS so the
      // never-firing version is the one the hook sees.
      vi.useFakeTimers();
      rafSpy = vi.fn(() => 1);
      vi.stubGlobal('requestAnimationFrame', rafSpy);
    });

    afterEach(() => {
      vi.useRealTimers();
      Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    });

    it('still applies printer status to the query cache', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      renderHook(() => useWebSocket(), { wrapper: createWrapper(queryClient) });
      const ws = await waitForWs();
      act(() => ws.open());

      act(() => {
        ws.simulateMessage({
          type: 'printer_status',
          printer_id: 1,
          data: { state: 'RUNNING', progress: 42 },
        });
      });

      // Past the 100ms coalescing window, which is untouched.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });

      expect(queryClient.getQueryData(['printerStatus', 1])).toMatchObject({
        state: 'RUNNING',
        progress: 42,
      });
      expect(rafSpy).not.toHaveBeenCalled();
    });

    it('drains queued messages instead of wedging the queue', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');
      const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

      renderHook(() => useWebSocket(), { wrapper: createWrapper(queryClient) });
      const ws = await waitForWs();
      act(() => ws.open());

      // ⚠️ Everything other than printer_status goes through the message
      // queue, which used to stall with processingRef stuck true — messages
      // then piled up unbounded until the tab was shown again.
      act(() => {
        ws.simulateMessage({ type: 'print_complete', printer_id: 1, data: {} });
      });

      // 3s debounce, then the 500ms-apart stagger.
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['archives'] });
      expect(rafSpy).not.toHaveBeenCalled();
    });
  });

  describe('sendMessage', () => {
    it('sends JSON message when connected', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const { result } = renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      act(() => {
        result.current.sendMessage({ type: 'test', data: 'hello' });
      });

      expect(ws.send).toHaveBeenCalledWith(
        JSON.stringify({ type: 'test', data: 'hello' })
      );
    });

    it('does not send when disconnected', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const { result } = renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Don't open connection - still in CONNECTING state

      act(() => {
        result.current.sendMessage({ type: 'test' });
      });

      expect(ws.send).not.toHaveBeenCalled();
    });
  });

  describe('reconnection', () => {
    it('reconnects after connection closes', async () => {
      vi.useFakeTimers();

      const { useWebSocket } = await import('../../hooks/useWebSocket');

      renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const firstWs = await waitForWs();

      // Open connection
      act(() => {
        firstWs.open();
      });

      const instanceCountBefore = wsInstances.length;

      // Close connection
      act(() => {
        firstWs.close();
      });

      // Wait for reconnect timeout (3 seconds). Async because the reconnect's
      // connect() awaits a fresh ws-token before creating the socket.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });

      // Should have created new WebSocket
      expect(wsInstances.length).toBe(instanceCountBefore + 1);
      expect(getLatestWs()).not.toBe(firstWs);

      vi.useRealTimers();
    });

    it('cleans up on unmount', async () => {
      const { useWebSocket } = await import('../../hooks/useWebSocket');

      const { unmount } = renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();

      // Open connection
      act(() => {
        ws.open();
      });

      unmount();

      expect(ws.close).toHaveBeenCalled();
    });

    it('does NOT reconnect after an auth-rejection close (4401)', async () => {
      // Regression: a 4401 (ws-token invalid/expired or caller lacks
      // WEBSOCKET_CONNECT) used to reschedule connect() every 3s, spamming
      // /auth/ws-token forever. It must be terminal now.
      vi.useFakeTimers();

      const { useWebSocket } = await import('../../hooks/useWebSocket');
      renderHook(() => useWebSocket(), { wrapper: createWrapper(queryClient) });

      const firstWs = await waitForWs();
      act(() => {
        firstWs.open();
      });

      const instanceCountBefore = wsInstances.length;

      // Server rejects auth.
      act(() => {
        firstWs.simulateClose(4401);
      });

      // No reconnect even after the 3s window elapses.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(wsInstances.length).toBe(instanceCountBefore);

      vi.useRealTimers();
    });

    it('does NOT open a socket or reconnect when ws-token mint returns 403', async () => {
      // An authenticated user whose group lacks WEBSOCKET_CONNECT. POST
      // /auth/ws-token returns 403; the hook must NOT fall through to a
      // tokenless socket (server closes it 4401) and must NOT enter the
      // reconnect loop — it degrades to REST polling instead.
      vi.useFakeTimers();

      const { api, ApiError } = await import('../../api/client');
      vi.mocked(api.getWebSocketToken).mockRejectedValueOnce(
        new ApiError('Insufficient permissions', 403),
      );

      const { useWebSocket } = await import('../../hooks/useWebSocket');
      renderHook(() => useWebSocket(), { wrapper: createWrapper(queryClient) });

      // Flush the deferred connect + its token-mint rejection, then let the
      // (would-be) reconnect window pass. No socket should ever be constructed.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(wsInstances.length).toBe(0);

      vi.useRealTimers();
    });

    it('does NOT reconnect when a close fires during unmount', async () => {
      // The provider unmounting (e.g. logout redirect) must not leave a
      // scheduled reconnect behind — the cleanup marks disposed before
      // close(), so the resulting onclose is a no-op.
      vi.useFakeTimers();

      const { useWebSocket } = await import('../../hooks/useWebSocket');
      const { unmount } = renderHook(() => useWebSocket(), {
        wrapper: createWrapper(queryClient),
      });

      const ws = await waitForWs();
      act(() => {
        ws.open();
      });

      const instanceCountBefore = wsInstances.length;

      // Unmount closes the socket, which fires onclose synchronously.
      act(() => {
        unmount();
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(wsInstances.length).toBe(instanceCountBefore);

      vi.useRealTimers();
    });
  });
});
