import { Fragment, useCallback, useLayoutEffect, useMemo, useState, type ReactNode } from 'react';
import { useWindowVirtualizer } from '@tanstack/react-virtual';
import { useAdaptiveVirtualGridOverscan, useDevVirtualGridMetrics } from '../hooks/useAdaptiveVirtualGrid';

interface WindowVirtualGridProps<T> {
  items: readonly T[];
  columns: number;
  estimateRowHeight: number;
  rowGap: number;
  className: string;
  rowClassName: string;
  getItemKey: (item: T) => string | number;
  renderItem: (item: T, virtualized: boolean) => ReactNode;
  /** A grouped view can virtualize its small sections once the whole page is large. */
  forceVirtualized?: boolean;
  virtualizeAfter?: number;
  baseOverscan?: number;
  testId?: string;
  debugId: string;
}

/**
 * A measured, window-scrolled grid. Each virtual item is a complete grid row,
 * so responsive columns and uneven card heights stay correct without making a
 * card itself less detailed.
 */
export function WindowVirtualGrid<T>({
  items,
  columns,
  estimateRowHeight,
  rowGap,
  className,
  rowClassName,
  getItemKey,
  renderItem,
  forceVirtualized,
  virtualizeAfter = 18,
  baseOverscan = 2,
  testId,
  debugId,
}: WindowVirtualGridProps<T>) {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const [scrollMargin, setScrollMargin] = useState(0);
  const virtualized = forceVirtualized ?? items.length > virtualizeAfter;
  const rowCount = Math.ceil(items.length / columns);
  const overscan = useAdaptiveVirtualGridOverscan(baseOverscan, window);
  const virtualizer = useWindowVirtualizer({
    count: virtualized ? rowCount : 0,
    estimateSize: () => estimateRowHeight,
    overscan,
    gap: rowGap,
    scrollMargin,
    useFlushSync: false,
  });
  const ref = useCallback((node: HTMLDivElement | null) => setElement(node), []);

  useLayoutEffect(() => {
    if (!element) return;
    const measureScrollMargin = () => {
      const next = element.getBoundingClientRect().top + window.scrollY;
      setScrollMargin(current => current === next ? current : next);
    };
    measureScrollMargin();
    const observer = new ResizeObserver(measureScrollMargin);
    observer.observe(element);
    window.addEventListener('resize', measureScrollMargin);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', measureScrollMargin);
    };
  }, [element]);

  const virtualRows = virtualizer.getVirtualItems();
  const mountedItemCount = useMemo(
    () => virtualized
      ? virtualRows.reduce((count, row) => count + Math.min(columns, items.length - row.index * columns), 0)
      : items.length,
    [columns, items.length, virtualRows, virtualized],
  );
  useDevVirtualGridMetrics(debugId, element, items.length, mountedItemCount, columns, virtualized);

  if (!virtualized) {
    return <div ref={ref} className={className} data-testid={testId}>
      {items.map(item => renderItem(item, false))}
    </div>;
  }

  return <div ref={ref} className="relative w-full" data-testid={testId} style={{ height: virtualizer.getTotalSize() }}>
    {virtualRows.map(row => (
      <div
        key={row.key}
        ref={virtualizer.measureElement}
        data-index={row.index}
        data-testid={testId ? `${testId}-row` : undefined}
        className={rowClassName}
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width: '100%',
          transform: `translateY(${row.start - virtualizer.options.scrollMargin}px)`,
        }}
      >
        {items.slice(row.index * columns, (row.index + 1) * columns).map(item => (
          <Fragment key={getItemKey(item)}>{renderItem(item, true)}</Fragment>
        ))}
      </div>
    ))}
  </div>;
}
