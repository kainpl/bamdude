import type { PrinterStatus } from './client';

interface PendingStatus {
  id: number;
  live: Partial<PrinterStatus>;
  resolve: (status: PrinterStatus) => void;
  reject: (error: unknown) => void;
}

/** Coalesce reads from cards/title/wall in the same browser turn. No cache:
 * permissions and credentials are checked by request() on every batch.
 */
export function createPrinterStatusBatcher(
  readOne: (id: number) => Promise<PrinterStatus>,
  readMany: (ids: number[]) => Promise<Record<string, PrinterStatus>>,
  missing: () => Error,
) {
  let queued: PendingStatus[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  const inFlight = new Set<PendingStatus>();

  const flush = async () => {
    timer = undefined;
    const items = queued;
    queued = [];
    const ids = [...new Set(items.map(item => item.id))];
    try {
      // Preserve the existing endpoint for a detail dialog reading one printer.
      // Larger farms are chunked sequentially to bound request concurrency.
      for (let offset = 0; offset < ids.length; offset += 100) {
        const chunk = ids.slice(offset, offset + 100);
        const rows = chunk.length === 1
          ? { [chunk[0]]: await readOne(chunk[0]) }
          : await readMany(chunk);
        for (const item of items.filter(item => chunk.includes(item.id))) {
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
          inFlight.delete(item);
        }
      }
    } catch (error) {
      for (const item of items) {
        if (inFlight.delete(item)) item.reject(error);
      }
    }
  };

  return {
    get: (id: number) => new Promise<PrinterStatus>((resolve, reject) => {
      const item = { id, live: {}, resolve, reject };
      queued.push(item);
      inFlight.add(item);
      timer ??= setTimeout(() => { void flush(); }, 0);
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
