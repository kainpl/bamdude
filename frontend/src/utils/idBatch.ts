/**
 * One request per tick for reads that every printer card makes by its own id.
 *
 * Each card keeps its own query (`['amsLabels', id]`, …) and its own cache entry; this only
 * turns the cards' `queryFn` calls of one tick into one request to a route that answers for
 * several ids. A transport, never a cache: nothing is kept once the answer is handed out.
 *
 * An id the answer does not name gets `missing(id)` — a value when "nothing for this printer"
 * is an ordinary answer, an `Error` when the single-printer route would have refused. A failed
 * request rejects every id that waited on it.
 */
export function createIdBatcher<T>(
  fetchBatch: (ids: number[]) => Promise<Record<string, T>>,
  missing: (id: number) => T | Error,
  { delayMs = 20, maxIds = 100 }: { delayMs?: number; maxIds?: number } = {},
): (id: number) => Promise<T> {
  type Waiter = { resolve: (value: T) => void; reject: (error: unknown) => void };
  let queued = new Map<number, Waiter[]>();
  let timer: ReturnType<typeof setTimeout> | null = null;

  const send = (ids: number[], waiting: Map<number, Waiter[]>) => {
    fetchBatch(ids).then(
      (answer) => {
        for (const id of ids) {
          const value = Object.prototype.hasOwnProperty.call(answer, String(id)) ? answer[String(id)] : missing(id);
          for (const waiter of waiting.get(id) ?? []) {
            if (value instanceof Error) waiter.reject(value);
            else waiter.resolve(value);
          }
        }
      },
      (error: unknown) => {
        for (const id of ids) for (const waiter of waiting.get(id) ?? []) waiter.reject(error);
      },
    );
  };

  const flush = () => {
    timer = null;
    const waiting = queued;
    queued = new Map();
    const ids = [...waiting.keys()].sort((a, b) => a - b);
    for (let start = 0; start < ids.length; start += maxIds) send(ids.slice(start, start + maxIds), waiting);
  };

  return (id: number) =>
    new Promise<T>((resolve, reject) => {
      const list = queued.get(id);
      if (list) list.push({ resolve, reject });
      else queued.set(id, [{ resolve, reject }]);
      if (timer === null) timer = setTimeout(flush, delayMs);
    });
}
