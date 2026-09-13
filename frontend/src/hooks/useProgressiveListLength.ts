import { useEffect, useState } from 'react';

// A PrinterCard owns several status-dependent queries and a substantial DOM
// tree. Mounting an entire farm in one commit makes the browser postpone input
// and WebSocket callbacks until React yields. Show the first viewport-sized
// slice immediately, then add a small slice per task.
export const INITIAL_PRINTER_CARD_COUNT = 12;
export const PRINTER_CARD_MOUNT_STEP = 8;
export const PRINTER_CARD_MOUNT_DELAY_MS = 16;

export function useProgressiveListLength(total: number): number {
  const [limit, setLimit] = useState(INITIAL_PRINTER_CARD_COUNT);

  useEffect(() => {
    if (limit >= total) return;

    const timer = window.setTimeout(() => {
      setLimit(current => Math.min(total, current + PRINTER_CARD_MOUNT_STEP));
    }, PRINTER_CARD_MOUNT_DELAY_MS);

    return () => window.clearTimeout(timer);
  }, [limit, total]);

  return Math.min(total, limit);
}
