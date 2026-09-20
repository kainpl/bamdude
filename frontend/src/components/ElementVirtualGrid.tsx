import { Fragment, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useAdaptiveVirtualGridOverscan, useDevVirtualGridMetrics } from '../hooks/useAdaptiveVirtualGrid';

interface ElementVirtualGridProps<T> {
  items: readonly T[];
  scrollElement: HTMLElement | null;
  columns: number;
  estimateRowHeight: number;
  rowGap: number;
  className: string;
  rowClassName: string;
  gridStyle?: CSSProperties;
  getItemKey: (item: T) => string | number;
  renderItem: (item: T) => ReactNode;
  forceVirtualized?: boolean;
  virtualizeAfter?: number;
  baseOverscan?: number;
  debugId: string;
}

/** The monitor's own scroll pane equivalent of WindowVirtualGrid. */
export function ElementVirtualGrid<T>({
  items,
  scrollElement,
  columns,
  estimateRowHeight,
  rowGap,
  className,
  rowClassName,
  gridStyle,
  getItemKey,
  renderItem,
  forceVirtualized,
  virtualizeAfter = 18,
  baseOverscan = 2,
  debugId,
}: ElementVirtualGridProps<T>) {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const virtualized = forceVirtualized ?? items.length > virtualizeAfter;
  const rowCount = Math.ceil(items.length / columns);
  const overscan = useAdaptiveVirtualGridOverscan(baseOverscan, scrollElement);
  const virtualizer = useVirtualizer({
    count: virtualized ? rowCount : 0,
    getScrollElement: () => scrollElement,
    estimateSize: () => estimateRowHeight,
    overscan,
    gap: rowGap,
    useFlushSync: false,
  });
  const virtualRows = virtualizer.getVirtualItems();
  const mountedItemCount = useMemo(
    () => virtualized
      ? virtualRows.reduce((count, row) => count + Math.min(columns, items.length - row.index * columns), 0)
      : items.length,
    [columns, items.length, virtualRows, virtualized],
  );
  useDevVirtualGridMetrics(debugId, element, items.length, mountedItemCount, columns, virtualized);

  if (!virtualized) {
    return <div ref={setElement} className={className} style={gridStyle}>
      {items.map(item => renderItem(item))}
    </div>;
  }

  return <div ref={setElement} className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
    {virtualRows.map(row => (
      <div
        key={row.key}
        ref={virtualizer.measureElement}
        data-index={row.index}
        className={rowClassName}
        style={{
          ...gridStyle,
          position: 'absolute',
          top: 0,
          left: 0,
          width: '100%',
          transform: `translateY(${row.start}px)`,
        }}
      >
        {items.slice(row.index * columns, (row.index + 1) * columns).map(item => (
          <Fragment key={getItemKey(item)}>{renderItem(item)}</Fragment>
        ))}
      </div>
    ))}
  </div>;
}
