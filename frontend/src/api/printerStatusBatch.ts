import type { PrinterStatus } from './client';

interface PendingStatus {
  id: number;
  live: Partial<PrinterStatus>;
  resolve: (status: PrinterStatus) => void;
  reject: (error: unknown) => void;
  signal?: AbortSignal;
  onAbort?: () => void;
  generation: number;
}

// Keep the network-efficient 100-printer request, but do not resolve a whole
// farm's React Query observers in one turn. Each yield lets the browser paint
// and run already-queued WebSocket callbacks before the next card slice lands.
export const STATUS_APPLY_CHUNK_SIZE = 10;

/** Coalesce reads from cards/title/wall in the same browser turn. No cache:
 * permissions and credentials are checked by request() on every batch.
 * `windowMs` widens "the same turn" to a few milliseconds: status timers
 * aligned on one tick still fire as separate tasks, a millisecond apart.
 */
export function createPrinterStatusBatcher(
  readOne: (id: number, signal?: AbortSignal) => Promise<PrinterStatus>,
  readMany: (ids: number[], signal?: AbortSignal) => Promise<Record<string, PrinterStatus>>,
  missing: () => Error,
  generation: () => number = () => 0,
  windowMs = 0,
) {
  let queued: PendingStatus[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  const inFlight = new Set<PendingStatus>();
  let activeChunk: { waiters: Set<PendingStatus>; controller: AbortController } | null = null;

  const release = (item: PendingStatus) => {
    inFlight.delete(item);
    if (item.signal && item.onAbort) item.signal.removeEventListener('abort', item.onAbort);
  };

  const flush = async () => {
    timer = undefined;
    const items = queued.filter(item => inFlight.has(item));
    queued = [];
    const ids = [...new Set(items.map(item => item.id))];
    try {
      // Preserve the existing endpoint for a detail dialog reading one printer.
      // Larger farms are chunked sequentially to bound request concurrency.
      for (let offset = 0; offset < ids.length; offset += 100) {
        const chunk = ids.slice(offset, offset + 100);
        const waiters = items.filter(item => chunk.includes(item.id) && inFlight.has(item));
        if (waiters.length === 0) continue;
        const controller = new AbortController();
        activeChunk = { waiters: new Set(waiters), controller };
        let rows: Record<string, PrinterStatus>;
        try {
          rows = chunk.length === 1
            ? { [chunk[0]]: await readOne(chunk[0], controller.signal) }
            : await readMany(chunk, controller.signal);
        } catch (error) {
          if (controller.signal.aborted && activeChunk.waiters.size === 0) continue;
          throw error;
        } finally {
          activeChunk = null;
        }
        for (let waiterOffset = 0; waiterOffset < waiters.length; waiterOffset += STATUS_APPLY_CHUNK_SIZE) {
          for (const item of waiters.slice(waiterOffset, waiterOffset + STATUS_APPLY_CHUNK_SIZE)) {
            if (!inFlight.has(item)) continue;
            if (item.generation !== generation()) {
              release(item);
              item.reject(new DOMException('Session changed', 'AbortError'));
              continue;
            }
            const row = rows[item.id];
            // A slow REST reply must not overwrite MQTT data that arrived after
            // the request began. Keep REST-only enrichment (archive/plate IDs).
            if (row) {
              const merged = { ...row, ...item.live };
              // Match the WS cache merge: omitted signal telemetry must not
              // erase the last measured signal from the REST snapshot.
              if (merged.wifi_signal == null && row.wifi_signal != null) merged.wifi_signal = row.wifi_signal;
              item.resolve(merged);
            } else item.reject(missing());
            release(item);
          }
          if (waiterOffset + STATUS_APPLY_CHUNK_SIZE < waiters.length) {
            await new Promise<void>(resolve => window.setTimeout(resolve, 0));
          }
        }
      }
    } catch (error) {
      for (const item of items) {
        if (inFlight.has(item)) {
          release(item);
          item.reject(error);
        }
      }
    }
  };

  return {
    get: (id: number, signal?: AbortSignal) => new Promise<PrinterStatus>((resolve, reject) => {
      if (signal?.aborted) {
        reject(signal.reason);
        return;
      }
      const item: PendingStatus = { id, live: {}, resolve, reject, signal, generation: generation() };
      item.onAbort = () => {
        queued = queued.filter(candidate => candidate !== item);
        release(item);
        item.reject(signal?.reason);
        if (activeChunk?.waiters.delete(item) && activeChunk.waiters.size === 0) activeChunk.controller.abort();
        if (queued.length === 0 && timer !== undefined) {
          clearTimeout(timer);
          timer = undefined;
        }
      };
      queued.push(item);
      inFlight.add(item);
      signal?.addEventListener('abort', item.onAbort, { once: true });
      timer ??= setTimeout(() => { void flush(); }, windowMs);
    }),
    update: (id: number, status: Partial<PrinterStatus>) => {
      for (const item of inFlight) {
        if (item.id === id) {
          const signal = item.live.wifi_signal;
          Object.assign(item.live, status);
          if (item.live.wifi_signal == null && signal != null) item.live.wifi_signal = signal;
        }
      }
    },
  };
}
