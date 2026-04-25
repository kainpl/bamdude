import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useToast } from '../contexts/ToastContext';
import { useConnection } from '../contexts/ConnectionContext';
import { useTranslation } from 'react-i18next';

interface WebSocketMessage {
  type: string;
  printer_id?: number;
  data?: Record<string, unknown>;
  printer_name?: string;
  missing_slots?: Array<{ slot?: string }>;
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const queryClient = useQueryClient();
  const [isConnected, setIsConnectedLocal] = useState(false);
  const { setIsConnected: setIsConnectedShared } = useConnection();
  // Single setter that updates both the local hook state (used by callers
  // who consume `useWebSocket` directly) and the shared ConnectionContext
  // (used by indicators / banners across the tree).
  const setIsConnected = useCallback((connected: boolean) => {
    setIsConnectedLocal(connected);
    setIsConnectedShared(connected);
  }, [setIsConnectedShared]);
  // Exposes the current connection's "send a ping right now" hook to code
  // outside the connect() closure (specifically the visibility handler).
  // Re-set on every connect, cleared on every close.
  const sendPingRef = useRef<(() => void) | null>(null);
  const lastMissingSpoolWarningRef = useRef<Map<number, string>>(new Map());
  const { showToast } = useToast();
  const { t } = useTranslation();

  // Debounce invalidations to prevent rapid re-render cascades
  const pendingInvalidations = useRef<Set<string>>(new Set());
  const invalidationTimeoutRef = useRef<number | null>(null);

  // Throttle printer status updates to prevent freeze during rapid messages
  const pendingPrinterStatus = useRef<Map<number, Record<string, unknown>>>(new Map());
  const printerStatusTimeoutRef = useRef<number | null>(null);

  // Throttle message processing to prevent browser freeze
  const messageQueueRef = useRef<WebSocketMessage[]>([]);
  const processingRef = useRef(false);

  // Use ref for handleMessage to avoid stale closure in connect
  const handleMessageRef = useRef<(message: WebSocketMessage) => void>(() => {});

  // Process message queue with throttling to prevent UI freeze
  const processMessageQueue = useCallback(() => {
    if (processingRef.current || messageQueueRef.current.length === 0) {
      return;
    }

    processingRef.current = true;

    const processNext = () => {
      const message = messageQueueRef.current.shift();
      if (message) {
        // Use requestAnimationFrame to yield to the browser
        requestAnimationFrame(() => {
          handleMessageRef.current(message);
          // Small delay between messages to prevent overwhelming the browser
          if (messageQueueRef.current.length > 0) {
            setTimeout(processNext, 16); // ~60fps
          } else {
            processingRef.current = false;
          }
        });
      } else {
        processingRef.current = false;
      }
    };

    processNext();
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/v1/ws`;

    const ws = new WebSocket(wsUrl);

    let pingInterval: number | null = null;
    // Pong-timeout watchdog: every ping starts a 10 s timer that's cleared
    // when the next pong arrives. If the timer expires, the socket is
    // considered silently dead (common when a backgrounded tab gets
    // throttled or when the OS suspends the connection without notifying
    // us via onclose) and we close → triggers the standard reconnect.
    let pongTimeout: number | null = null;

    const armPongTimeout = () => {
      if (pongTimeout) clearTimeout(pongTimeout);
      pongTimeout = window.setTimeout(() => {
        if (import.meta.env.MODE !== 'test') {
          console.warn('[WebSocket] No pong within 10s — closing dead socket');
        }
        try {
          ws.close();
        } catch {
          // Already closed; onclose handler will reconnect.
        }
      }, 10000);
    };

    const clearPongTimeout = () => {
      if (pongTimeout) {
        clearTimeout(pongTimeout);
        pongTimeout = null;
      }
    };

    const sendPing = () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
        armPongTimeout();
      }
    };

    ws.onopen = () => {
      if (import.meta.env.MODE !== 'test') console.log('[WebSocket] Connected');
      setIsConnected(true);
      // Expose the on-demand ping for the visibility handler.
      sendPingRef.current = sendPing;
      // Start ping interval — server replies with type='pong'.
      pingInterval = window.setInterval(sendPing, 30000);
    };

    ws.onmessage = (event) => {
      try {
        const message: WebSocketMessage = JSON.parse(event.data);
        // Pong from the server clears the watchdog. Don't queue or render —
        // it's keepalive plumbing, not user-visible state.
        if (message.type === 'pong') {
          clearPongTimeout();
          return;
        }
        // Handle printer_status directly (already throttled) to avoid queue delays
        // This prevents the "timelapse" effect where status updates are applied slowly
        if (message.type === 'printer_status' && message.printer_id !== undefined && message.data) {
          handleMessageRef.current(message);
        } else {
          // Queue other messages for throttled processing
          messageQueueRef.current.push(message);
          processMessageQueue();
        }
      } catch {
        // Ignore parse errors
      }
    };

    ws.onclose = (event) => {
      if (import.meta.env.MODE !== 'test') console.log('[WebSocket] Closed', event.code, event.reason);
      if (pingInterval) {
        clearInterval(pingInterval);
        pingInterval = null;
      }
      clearPongTimeout();
      sendPingRef.current = null;
      setIsConnected(false);
      wsRef.current = null;

      // Reconnect after 3 seconds
      reconnectTimeoutRef.current = window.setTimeout(() => {
        connect();
      }, 3000);
    };

    ws.onerror = (error) => {
      if (import.meta.env.MODE !== 'test') console.error('[WebSocket] Error', error);
      ws.close();
    };

    wsRef.current = ws;
  }, [processMessageQueue, setIsConnected]);

  // Throttled printer status update - coalesces rapid updates per printer
  const throttledPrinterStatusUpdate = useCallback((printerId: number, data: Record<string, unknown>) => {
    // Merge with any pending data for this printer
    const existing = pendingPrinterStatus.current.get(printerId) || {};
    pendingPrinterStatus.current.set(printerId, { ...existing, ...data });

    // Schedule update if not already scheduled
    if (!printerStatusTimeoutRef.current) {
      printerStatusTimeoutRef.current = window.setTimeout(() => {
        const updates = new Map(pendingPrinterStatus.current);
        pendingPrinterStatus.current.clear();
        printerStatusTimeoutRef.current = null;

        // Apply all pending updates
        requestAnimationFrame(() => {
          updates.forEach((statusData, id) => {
            queryClient.setQueryData(
              ['printerStatus', id],
              (old: Record<string, unknown> | undefined) => {
                const merged = { ...old, ...statusData };
                if (merged.wifi_signal == null && old?.wifi_signal != null) {
                  merged.wifi_signal = old.wifi_signal;
                }
                return merged;
              }
            );
          });
        });
      }, 100); // Update at most every 100ms
    }
  }, [queryClient]);

  // Debounced invalidation helper - coalesces multiple rapid invalidations
  const debouncedInvalidate = useCallback((queryKey: string) => {
    pendingInvalidations.current.add(queryKey);

    // Clear existing timeout
    if (invalidationTimeoutRef.current) {
      clearTimeout(invalidationTimeoutRef.current);
    }

    // Schedule invalidation after a delay (3s to prevent browser freeze on print completion)
    invalidationTimeoutRef.current = window.setTimeout(() => {
      const keys = Array.from(pendingInvalidations.current);
      pendingInvalidations.current.clear();
      invalidationTimeoutRef.current = null;

      // Invalidate queries one at a time with delays to prevent freeze
      let delay = 0;
      keys.forEach((key) => {
        setTimeout(() => {
          requestAnimationFrame(() => {
            queryClient.invalidateQueries({ queryKey: [key] });
          });
        }, delay);
        delay += 500; // 500ms between each invalidation
      });
    }, 3000);
  }, [queryClient]);

  const handleMessage = useCallback((message: WebSocketMessage) => {
    switch (message.type) {
      case 'printer_status':
        if (message.printer_id !== undefined && message.data) {
          throttledPrinterStatusUpdate(message.printer_id, message.data);
        }
        break;

      case 'print_start':
        // Refetch printer status immediately when print starts to get printable_objects_count
        if (message.printer_id !== undefined) {
          queryClient.invalidateQueries({ queryKey: ['printerStatus', message.printer_id] });
          // Update queue data (status, current print)
          debouncedInvalidate('queues');
          queryClient.invalidateQueries({ queryKey: ['queue', message.printer_id] });
        }
        break;

      case 'missing_spool_assignment': {
        if (message.printer_id === undefined || !Array.isArray(message.missing_slots)) {
          break;
        }

        const missingSlotLabels = message.missing_slots
          .map((slot) => (slot && typeof slot.slot === 'string' ? slot.slot : 'Unknown'))
          .filter((slot) => slot.length > 0);

        if (missingSlotLabels.length === 0) {
          lastMissingSpoolWarningRef.current.delete(message.printer_id);
          break;
        }

        const signature = missingSlotLabels.join('|');
        if (lastMissingSpoolWarningRef.current.get(message.printer_id) === signature) {
          break;
        }
        lastMissingSpoolWarningRef.current.set(message.printer_id, signature);

        const printerName = message.printer_name || `Printer ${message.printer_id}`;
        const toastMsg = t('printers.toast.missingSpoolAssignment', {
          printer: printerName,
          slots: missingSlotLabels.join(', '),
        });
        showToast(toastMsg, 'warning');
        break;
      }

      case 'print_complete':
        // Don't invalidate printerStatus here - it causes re-render cascade and browser freeze
        // The printer_status websocket messages will naturally update the status
        debouncedInvalidate('archives');
        debouncedInvalidate('archiveStats');
        // Update queue data (counters, status, pending items)
        debouncedInvalidate('queues');
        if (message.printer_id !== undefined) {
          queryClient.invalidateQueries({ queryKey: ['queue', message.printer_id] });
        }
        break;

      case 'archive_created':
        debouncedInvalidate('archives');
        debouncedInvalidate('archiveStats');
        break;

      case 'archive_updated':
        debouncedInvalidate('archives');
        break;

      case 'library_file_added':
        debouncedInvalidate('library-files');
        debouncedInvalidate('library-stats');
        break;

      case 'library_file_notes_changed': {
        // gh#3 - notes count changed somewhere; refresh file lists (which
        // carry notes_count) and any open per-file notes query.
        const fileId = (message as unknown as { data?: { file_id?: number } }).data?.file_id;
        debouncedInvalidate('library-files');
        if (typeof fileId === 'number') {
          queryClient.invalidateQueries({ queryKey: ['library-file-notes', fileId] });
        }
        break;
      }

      case 'pong':
        // Keepalive response, ignore
        break;

      case 'plate_not_empty':
        // Plate detection found objects - print was paused
        // Dispatch event for toast notification
        window.dispatchEvent(new CustomEvent('plate-not-empty', {
          detail: {
            printer_id: message.printer_id,
            printer_name: (message as unknown as { printer_name?: string }).printer_name,
            message: (message as unknown as { message?: string }).message,
          }
        }));
        break;

      case 'spool_assignment_changed':
        // Spool assigned/unassigned - refresh assignment data across all tabs
        debouncedInvalidate('spool-assignments');
        debouncedInvalidate('slotPresets');
        break;

      case 'spool_auto_assigned':
        // RFID tag matched - refresh inventory and assignment data
        debouncedInvalidate('inventory-spools');
        debouncedInvalidate('spool-assignments');
        break;

      case 'spool_usage_logged':
        // Filament consumption recorded - refresh spool data
        debouncedInvalidate('inventory-spools');
        break;

      case 'macro_executed': {
        // Macro execution result - show toast globally + dispatch for UI state
        const macroData = message.data as Record<string, unknown> | undefined;
        if (macroData) {
          showToast(
            String(macroData.message || 'Macro executed'),
            macroData.success ? 'success' : 'error',
          );
          window.dispatchEvent(new CustomEvent('macro-executed', {
            detail: macroData,
          }));
        }
        break;
      }

      case 'unknown_tag':
        // Unknown RFID tag detected - dispatch event for UI
        window.dispatchEvent(new CustomEvent('unknown-tag', {
          detail: {
            printer_id: (message as unknown as { printer_id?: number }).printer_id,
            ams_id: (message as unknown as { ams_id?: number }).ams_id,
            tray_id: (message as unknown as { tray_id?: number }).tray_id,
            tag_uid: (message as unknown as { tag_uid?: string }).tag_uid,
            tray_uuid: (message as unknown as { tray_uuid?: string }).tray_uuid,
          }
        }));
        break;

      case 'telegram_chat_registered':
        queryClient.invalidateQueries({ queryKey: ['telegram-chats'] });
        break;

      case 'background_dispatch':
        window.dispatchEvent(
          new CustomEvent('background-dispatch', {
            detail: (message as unknown as { data?: Record<string, unknown> }).data || {},
          })
        );
        break;

    }
  }, [queryClient, debouncedInvalidate, throttledPrinterStatusUpdate, showToast, t]);

  // Keep the ref updated with latest handleMessage
  useEffect(() => {
    handleMessageRef.current = handleMessage;
  }, [handleMessage]);

  useEffect(() => {
    // Defer the initial connect by a microtask so React.StrictMode's
    // mount-unmount-remount dance in dev doesn't create a transient
    // WebSocket that gets closed before its ``onopen`` fires (browsers
    // log a noisy "closed before connection established" warning + then
    // 1006 abnormal-close on that orphan socket). The cleanup below
    // cancels this timer if StrictMode unmounts us before it fires, so
    // only the surviving mount's connect actually runs. In production
    // (no StrictMode) this is a harmless 0 ms delay.
    const initTimer = window.setTimeout(connect, 0);

    // Visibility-sync: when the tab returns to the foreground we want
    // (a) fresh data (queries may have been throttled while hidden) and
    // (b) confidence that the WS socket is still alive — browsers can
    // silently kill long-idle sockets without firing onclose.
    //
    // Strategy: invalidate queries unconditionally (cheap, runs only once
    // per visibility flip) + send an immediate ping. The existing 10 s
    // pong-timeout watchdog inside connect() handles the "no pong came
    // back" case — if the socket was killed in the background, the
    // watchdog detects it within ~10 s and triggers the standard
    // reconnect path. Healthy sockets just receive a pong and carry on
    // without churning. No more reconnect flicker on every Alt+Tab.
    const onVisibilityChange = () => {
      if (document.visibilityState !== 'visible') return;
      queryClient.invalidateQueries({ refetchType: 'all' });
      sendPingRef.current?.();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      clearTimeout(initTimer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (invalidationTimeoutRef.current) {
        clearTimeout(invalidationTimeoutRef.current);
      }
      if (printerStatusTimeoutRef.current) {
        clearTimeout(printerStatusTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect, queryClient]);

  const sendMessage = useCallback((message: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
    }
  }, []);

  return { isConnected, sendMessage };
}
