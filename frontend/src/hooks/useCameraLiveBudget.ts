import { useEffect, useState, useSyncExternalStore } from 'react';

type Transport = 'unknown' | 'http/1.x' | 'h2' | 'h3';
const listeners = new Set<() => void>();
const requests = new Map<symbol, number>();
let transport: Transport = 'unknown';
let observer: PerformanceObserver | undefined;
const notify = () => listeners.forEach((listener) => listener());
const limit = () => transport === 'h2' || transport === 'h3' ? 16 : 2;

// Inspect the browser-facing API hop, not the HTML protocol, URL scheme or
// the backend's ASGI scope (which may describe a reverse proxy's upstream hop).
function observeEntries(entries: PerformanceEntry[]) {
  let next = transport;
  for (const entry of entries) {
    if (entry.entryType !== 'resource') continue;
    const url = new URL(entry.name, window.location.href);
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) continue;
    const protocol = (entry as PerformanceResourceTiming).nextHopProtocol;
    // Once an HTTP/1 hop is seen, stay conservative for this page lifetime.
    if (protocol?.startsWith('http/1.')) next = 'http/1.x';
    else if (next !== 'http/1.x' && (protocol === 'h2' || protocol === 'h3')) next = protocol;
  }
  if (next !== transport) {
    transport = next;
    notify();
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    observeEntries(performance.getEntriesByType('resource'));
    if (typeof PerformanceObserver !== 'undefined') {
      try {
        observer = new PerformanceObserver((list) => observeEntries(list.getEntries()));
        observer.observe({ type: 'resource', buffered: true });
      } catch {
        observer?.disconnect();
        observer = undefined;
      }
    }
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      observer?.disconnect();
      observer = undefined;
    }
  };
}

function allocation(id: symbol) {
  let remaining = limit();
  for (const [key, count] of requests) {
    const granted = Math.min(remaining, count);
    if (key === id) return granted;
    remaining -= granted;
  }
  return -1;
}

/** One document-wide budget shared by walls and floating camera windows.
 * HTTP/1 and unknown keep room for REST and the two-slot snapshot queue.
 * Separate browser tabs are deliberately not claimed to share this budget. */
export function useCameraLiveBudget(requested: number) {
  const [id] = useState(() => Symbol('camera-viewer'));
  const granted = useSyncExternalStore(subscribe, () => allocation(id), () => 0);
  const protocol = useSyncExternalStore(subscribe, () => transport, () => 'unknown' as Transport);
  const count = Math.max(0, Math.min(16, Math.floor(requested) || 0));
  useEffect(() => {
    requests.set(id, count);
    notify();
    return () => {
      requests.delete(id);
      notify();
    };
  }, [id, count]);
  return { granted: Math.max(0, Math.min(granted, count)), ready: granted >= 0, protocol, limit: limit() };
}
