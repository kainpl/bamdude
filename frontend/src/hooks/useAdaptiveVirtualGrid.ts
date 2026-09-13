import { useEffect, useState } from 'react';

function scrollOffset(element: Window | HTMLElement): number {
  return element instanceof Window ? element.scrollY : element.scrollTop;
}

/** Keep one extra row nearby while an operator is flick-scrolling, without permanently mounting it. */
export function useAdaptiveVirtualGridOverscan(baseOverscan: number, scrollElement: Window | HTMLElement | null): number {
  const [overscan, setOverscan] = useState(baseOverscan);

  useEffect(() => {
    if (!scrollElement) return;
    let previousOffset = scrollOffset(scrollElement);
    let previousAt = performance.now();
    let settleTimer: number | undefined;
    const onScroll = () => {
      const now = performance.now();
      const velocity = Math.abs(scrollOffset(scrollElement) - previousOffset) / Math.max(1, now - previousAt);
      previousOffset = scrollOffset(scrollElement);
      previousAt = now;
      setOverscan(velocity > 1.2 ? baseOverscan + 1 : baseOverscan);
      window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(() => setOverscan(baseOverscan), 140);
    };
    scrollElement.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      scrollElement.removeEventListener('scroll', onScroll);
      window.clearTimeout(settleTimer);
    };
  }, [baseOverscan, scrollElement]);

  return overscan;
}

/** DevTools-only event for profiling virtual grids without adding operator UI. */
export function useDevVirtualGridMetrics(
  debugId: string,
  element: HTMLElement | null,
  sourceItemCount: number,
  mountedItemCount: number,
  columns: number,
  virtualized: boolean,
) {
  useEffect(() => {
    if (!import.meta.env.DEV || !element) return;
    const frame = window.requestAnimationFrame(() => {
      const mark = `bamdude:virtual-grid:${debugId}:commit`;
      performance.mark(mark);
      window.dispatchEvent(new CustomEvent('bamdude:virtual-grid-metrics', {
        detail: {
          id: debugId,
          sourceItemCount,
          mountedItemCount,
          columns,
          virtualized,
          gridHeight: element.clientHeight,
        },
      }));
    });
    return () => window.cancelAnimationFrame(frame);
  }, [debugId, element, sourceItemCount, mountedItemCount, columns, virtualized]);
}
