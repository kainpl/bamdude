import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, type FarmForecast } from '../../api/client';
import type { MonitorSnapshot, MonitorView } from './types';
import { liveStatusPriorityIds, prioritizeLiveStatusEntries } from '../../utils/liveStatusPriority';

const MONITOR_SNAPSHOT_APPLY_CHUNK_SIZE = 10;

async function kioskRead<T>(endpoint: 'snapshot' | 'forecast', token: string, signal: AbortSignal, view?: MonitorView): Promise<T> {
  const response = await fetch(`/api/v1/monitor/kiosk/${endpoint}${view ? `?view=${view}` : ''}`, {
    headers: { Authorization: `Bearer ${token}` }, signal, credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
  });
  // Never include a server message, URL or token in a kiosk error/cache key.
  if (!response.ok) throw new ApiError('Monitor request failed', response.status);
  return response.json();
}

export interface MonitorResource<T> { data?: T; error: unknown; updatedAt: number; loading: boolean; terminal: boolean }
const empty = { error: null, updatedAt: 0, loading: true, terminal: false };
export function terminalError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

// The monitor keeps its small projection locally, never in PrinterStatus's
// full-object cache. Exactly one in-flight request per feed; all timers and
// listeners belong to this window and are disposed with it.
export function useMonitorResource<T>(read: (signal: AbortSignal) => Promise<T>, period: number, enabled: boolean, authenticated: boolean) {
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<MonitorResource<T> & { read?: typeof read }>({ ...empty });
  useEffect(() => {
    if (!enabled) return;
    let disposed = false, busy = false, stopped = false, failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const run = async () => {
      if (disposed || busy || stopped) return;
      clearTimeout(timer);
      busy = true;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 10000);
      try {
        const data = await read(controller.signal);
        if (!disposed) {
          failures = 0;
          setState({ data, read, error: null, updatedAt: Date.now(), loading: false, terminal: false });
        }
      } catch (error) {
        if (!disposed) {
          failures++;
          stopped = terminalError(error);
          setState(old => ({ ...(stopped || old.read !== read ? empty : old), read,
            error, loading: false, terminal: stopped }));
          if (stopped) window.dispatchEvent(new CustomEvent('bamdude:monitor-invalidated', { detail: error }));
        }
      } finally {
        clearTimeout(timeout);
        busy = false;
        if (!disposed && !stopped) timer = setTimeout(run, Math.min(30000, period * 2 ** Math.min(failures, 3)));
      }
    };
    const resume = () => { if (document.visibilityState !== 'hidden') void run(); };
    const stop = (error: unknown) => {
      stopped = true; disposed = true;
      clearTimeout(timer); controller?.abort();
      setState({ ...empty, read, loading: false, terminal: true, error });
    };
    const invalidate = () => { if (authenticated) stop(new ApiError('Signed out', 401)); };
    const monitorInvalidated = (event: Event) => stop((event as CustomEvent).detail);
    const storage = (e: StorageEvent) => { if (e.key === 'auth_token' && e.newValue === null) invalidate(); };
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('online', resume);
    window.addEventListener('bamdude:auth-invalidated', invalidate);
    window.addEventListener('bamdude:monitor-invalidated', monitorInvalidated);
    window.addEventListener('storage', storage);
    void run();
    return () => {
      disposed = true; clearTimeout(timer); controller?.abort();
      document.removeEventListener('visibilitychange', resume);
      window.removeEventListener('online', resume);
      window.removeEventListener('bamdude:auth-invalidated', invalidate);
      window.removeEventListener('bamdude:monitor-invalidated', monitorInvalidated);
      window.removeEventListener('storage', storage);
    };
  }, [read, period, enabled, authenticated, reload]);
  const resource = state.read === read && enabled ? state : { ...empty };
  return { ...resource, retry: () => setReload(n => n + 1) };
}

export function useMonitorSnapshot(view: MonitorView, token: string | null) {
  const read = useCallback((signal: AbortSignal) => token !== null ? kioskRead<MonitorSnapshot>('snapshot', token, signal, view) :
    api.getMonitorSnapshot(view, signal), [view, token]);
  const resource = useMonitorResource(read, 5000, true, token === null);
  const prioritized = usePrioritizedMonitorSnapshot(resource.data);
  return { ...resource, data: prioritized };
}

/**
 * The monitor is deliberately a compact polling feed, not a second WebSocket
 * client. On refresh, commit tiles mounted in its virtual grid first, then
 * merge the rest in ten-printer tasks so a full snapshot cannot monopolize a
 * paint opportunity.
 */
function usePrioritizedMonitorSnapshot(snapshot: MonitorSnapshot | undefined) {
  const [displayed, setDisplayed] = useState<MonitorSnapshot>();
  const latest = useRef<MonitorSnapshot | undefined>(undefined);

  useEffect(() => {
    if (!snapshot) {
      latest.current = undefined;
      setDisplayed(undefined);
      return;
    }
    const previous = latest.current;
    if (!previous || previous.view !== snapshot.view) {
      latest.current = snapshot;
      setDisplayed(snapshot);
      return;
    }

    const oldById = new Map(previous.printers.map(printer => [printer.printer_id, printer]));
    const applied = new Map<number, MonitorSnapshot['printers'][number]>();
    const priorityIds = liveStatusPriorityIds();
    const remaining = snapshot.printers.filter((printer) => {
      const old = oldById.get(printer.printer_id);
      if (!old || priorityIds.has(printer.printer_id)) {
        applied.set(printer.printer_id, printer);
        return false;
      }
      applied.set(printer.printer_id, old);
      return true;
    });
    const publish = () => {
      const next = { ...snapshot, printers: snapshot.printers.map(printer => applied.get(printer.printer_id) ?? printer) };
      latest.current = next;
      setDisplayed(next);
    };
    publish();

    let cancelled = false;
    const applyNext = () => {
      if (cancelled || remaining.length === 0) return;
      const entries: Array<[number, MonitorSnapshot['printers'][number]]> = remaining.map(printer => [printer.printer_id, printer]);
      const ordered = prioritizeLiveStatusEntries(entries);
      const chunk = ordered.slice(0, MONITOR_SNAPSHOT_APPLY_CHUNK_SIZE);
      const chosen = new Set(chunk.map(([id]) => id));
      remaining.splice(0, remaining.length, ...remaining.filter(printer => !chosen.has(printer.printer_id)));
      for (const [id, printer] of chunk) applied.set(id, printer);
      publish();
      if (remaining.length > 0) window.setTimeout(applyNext, 0);
    };
    if (remaining.length > 0) window.setTimeout(applyNext, 0);
    return () => { cancelled = true; };
  }, [snapshot]);

  // First load must show the full snapshot once, so the priority registry has
  // cards to observe. Subsequent refreshes use the staged value above.
  return displayed ?? snapshot;
}

export function useMonitorForecast(token: string | null, enabled: boolean) {
  const read = useCallback((signal: AbortSignal) => token !== null ? kioskRead<FarmForecast>('forecast', token, signal) :
    api.getMonitorForecast(signal), [token]);
  return useMonitorResource(read, 30000, enabled, token === null);
}
