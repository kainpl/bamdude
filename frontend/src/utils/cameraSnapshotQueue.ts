// Shared by snapshot tiles in this document, including multiple camera walls.
// Reserve HTTP/1.1 connections for live streams and ordinary API requests.
const MAX_ACTIVE_SNAPSHOTS = 2;
let active = 0;
const waiting: Array<() => void> = [];

/** Wait without opening an HTTP request. Each granted slot must be released. */
export function acquireCameraSnapshotSlot(signal: AbortSignal): Promise<() => void> {
  return new Promise((resolve, reject) => {
    const cancel = () => {
      const index = waiting.indexOf(start);
      if (index !== -1) waiting.splice(index, 1);
      reject(signal.reason);
    };
    const start = () => {
      signal.removeEventListener('abort', cancel);
      if (signal.aborted) {
        reject(signal.reason);
        return;
      }
      active += 1;
      let released = false;
      resolve(() => {
        if (released) return;
        released = true;
        active -= 1;
        waiting.shift()?.();
      });
    };
    if (signal.aborted) {
      reject(signal.reason);
    } else if (active < MAX_ACTIVE_SNAPSHOTS) {
      start();
    } else {
      waiting.push(start);
      signal.addEventListener('abort', cancel, { once: true });
    }
  });
}
