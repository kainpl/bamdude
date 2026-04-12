import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Pencil, XCircle, Layers } from 'lucide-react';
import { api } from '../../api/client';
import type { PrinterQueue, PrintQueueItem } from '../../api/client';
import { ContextMenu, type ContextMenuItem } from '../ContextMenu';
import { useToast } from '../../contexts/ToastContext';
import { useQueueTimeline, type TimelineSlot } from '../../hooks/useQueueTimeline';
import { TimelineAxis } from './TimelineAxis';
import { TimelineItem } from './TimelineItem';

const LANE_LABEL_WIDTH = 180;
const LANE_HEIGHT = 48;
const MIN_TRACK_WIDTH = 1200;

const RANGE_OPTIONS: { value: number; labelKey: string }[] = [
  { value: 12, labelKey: 'queue.timeline.range12h' },
  { value: 24, labelKey: 'queue.timeline.range24h' },
  { value: 72, labelKey: 'queue.timeline.range3d' },
];

interface Props {
  queues: PrinterQueue[] | undefined;
  items: PrintQueueItem[] | undefined;
  onEditItem: (item: PrintQueueItem) => void;
}

export function QueueTimelineView({ queues, items, onEditItem }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [rangeHours, setRangeHours] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem('queueTimelineRangeHours') ?? '', 10);
    return RANGE_OPTIONS.find(o => o.value === saved)?.value ?? 24;
  });

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    staleTime: 60_000,
  });

  // Re-render every minute to advance the "now" line.
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // Measure track width (ResizeObserver on the scroll container).
  const scrollRef = useRef<HTMLDivElement>(null);
  const [trackWidth, setTrackWidth] = useState<number>(MIN_TRACK_WIDTH);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => {
      const available = el.clientWidth - LANE_LABEL_WIDTH;
      setTrackWidth(Math.max(MIN_TRACK_WIDTH, available));
    };
    measure();
    const obs = new ResizeObserver(measure);
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const timeline = useQueueTimeline({ queues, items, printers, now, rangeHours });

  // Batch counts — so items can render "1/N" labels and we can offer "Cancel batch".
  const batchStats = useMemo(() => {
    const counts = new Map<string, number>();
    (items ?? []).forEach(it => {
      if (it.batch_id) counts.set(it.batch_id, (counts.get(it.batch_id) ?? 0) + 1);
    });
    return counts;
  }, [items]);

  const batchIndexInLane = useCallback(
    (laneSlots: TimelineSlot[], itemId: number, batchId: string) => {
      let idx = 0;
      for (const s of laneSlots) {
        if (s.item.batch_id !== batchId) continue;
        if (s.itemId === itemId) return idx;
        idx++;
      }
      return 0;
    },
    [],
  );

  // Mutations
  const cancelItem = useMutation({
    mutationFn: (id: number) => api.cancelQueueItem(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue'] });
      queryClient.invalidateQueries({ queryKey: ['queues'] });
      showToast(t('queue.timeline.cancelItemSuccess'));
    },
    onError: (err: Error) => showToast(err.message || t('queue.timeline.cancelItemFailed'), 'error'),
  });

  const cancelBatch = useMutation({
    mutationFn: async (batchId: string) => {
      const batchItems = (items ?? []).filter(it => it.batch_id === batchId && it.status === 'pending');
      await Promise.all(batchItems.map(it => api.cancelQueueItem(it.id)));
      return batchItems.length;
    },
    onSuccess: (count) => {
      queryClient.invalidateQueries({ queryKey: ['queue'] });
      queryClient.invalidateQueries({ queryKey: ['queues'] });
      showToast(t('queue.timeline.cancelBatchSuccess', { count }));
    },
    onError: (err: Error) => showToast(err.message || t('queue.timeline.cancelBatchFailed'), 'error'),
  });

  // Context menu state
  const [menu, setMenu] = useState<{ x: number; y: number; item: PrintQueueItem } | null>(null);

  const openMenu = (e: React.MouseEvent, item: PrintQueueItem) => {
    e.preventDefault();
    e.stopPropagation();
    setMenu({ x: e.clientX, y: e.clientY, item });
  };

  const menuItems: ContextMenuItem[] = menu
    ? [
        {
          label: t('queue.timeline.editItem'),
          icon: <Pencil className="w-4 h-4" />,
          onClick: () => onEditItem(menu.item),
          disabled: menu.item.status !== 'pending',
        },
        {
          label: t('queue.timeline.cancelItem'),
          icon: <XCircle className="w-4 h-4" />,
          onClick: () => cancelItem.mutate(menu.item.id),
          danger: true,
          disabled: menu.item.status !== 'pending',
        },
        ...(menu.item.batch_id && (batchStats.get(menu.item.batch_id) ?? 0) > 1
          ? [
              { divider: true, label: '', onClick: () => {} } as ContextMenuItem,
              {
                label: t('queue.timeline.cancelBatch', {
                  count: batchStats.get(menu.item.batch_id!) ?? 0,
                }),
                icon: <Layers className="w-4 h-4" />,
                onClick: () => cancelBatch.mutate(menu.item.batch_id!),
                danger: true,
              } as ContextMenuItem,
            ]
          : []),
      ]
    : [];

  if (!queues) return null;
  if (queues.length === 0) {
    return (
      <div className="text-center py-12 text-bambu-gray">
        {t('queueCard.noQueues')}
      </div>
    );
  }

  const pxPerMs = trackWidth / timeline.windowDurationMs;
  const nowLeft = (now - timeline.windowStartMs) * pxPerMs;

  return (
    <div className="border border-bambu-dark-tertiary rounded-lg bg-bambu-dark-secondary">
      {/* Range selector */}
      <div className="flex items-center justify-between gap-2 p-2 border-b border-bambu-dark-tertiary">
        <div className="text-xs text-bambu-gray">{t('queue.timeline.rangeLabel')}</div>
        <div className="flex gap-1">
          {RANGE_OPTIONS.map(opt => (
            <button
              key={opt.value}
              onClick={() => {
                setRangeHours(opt.value);
                localStorage.setItem('queueTimelineRangeHours', String(opt.value));
              }}
              className={`px-2 py-1 text-xs rounded border transition-colors ${
                rangeHours === opt.value
                  ? 'bg-bambu-green text-white border-bambu-green'
                  : 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray hover:text-white'
              }`}
            >
              {t(opt.labelKey)}
            </button>
          ))}
        </div>
      </div>

      {/* Scroll container */}
      <div ref={scrollRef} className="overflow-x-auto">
        <div style={{ width: LANE_LABEL_WIDTH + trackWidth, minWidth: '100%' }}>
          <TimelineAxis
            ticks={timeline.ticks}
            windowStartMs={timeline.windowStartMs}
            windowDurationMs={timeline.windowDurationMs}
            nowMs={now}
            laneLabelWidth={LANE_LABEL_WIDTH}
            trackWidth={trackWidth}
          />

          {/* Lanes */}
          <div className="relative">
            {timeline.lanes.map(lane => (
              <div
                key={lane.queue.id}
                className="flex border-b border-bambu-dark-tertiary/50 last:border-b-0"
                style={{ height: LANE_HEIGHT }}
              >
                {/* Label column */}
                <div
                  className="shrink-0 flex flex-col justify-center px-3 border-r border-bambu-dark-tertiary bg-bambu-dark-secondary sticky left-0 z-10"
                  style={{ width: LANE_LABEL_WIDTH }}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span
                      className={`w-2 h-2 rounded-full shrink-0 ${
                        lane.queue.status === 'printing' ? 'bg-blue-400' :
                        lane.queue.status === 'paused' ? 'bg-yellow-400' :
                        lane.queue.status === 'error' ? 'bg-red-400' :
                        'bg-bambu-gray'
                      }`}
                    />
                    <span className="text-sm text-white truncate">
                      {lane.queue.printer_name || `Printer ${lane.queue.printer_id}`}
                    </span>
                  </div>
                  <div className="text-[10px] text-bambu-gray truncate ml-4">
                    {lane.queue.pending_count} {t('queueCard.pending')}
                  </div>
                </div>

                {/* Track */}
                <div className="relative flex-1" style={{ width: trackWidth }}>
                  {/* Empty-lane hint */}
                  {lane.slots.length === 0 && (
                    <div className="absolute inset-0 flex items-center text-[11px] text-bambu-gray/70 pl-2">
                      {t('queue.timeline.emptyQueue')}
                    </div>
                  )}

                  {lane.slots.map(slot => {
                    const left = (slot.startMs - timeline.windowStartMs) * pxPerMs;
                    const width = (slot.endMs - slot.startMs) * pxPerMs;

                    // Clip: skip slots fully outside the visible window.
                    if (left + width < 0) return null;
                    if (left > trackWidth) return null;

                    const batchTotal = slot.item.batch_id
                      ? batchStats.get(slot.item.batch_id) ?? 1
                      : 1;
                    const batchIdx = slot.item.batch_id
                      ? batchIndexInLane(lane.slots, slot.itemId, slot.item.batch_id)
                      : 0;

                    return (
                      <TimelineItem
                        key={slot.itemId}
                        slot={slot}
                        left={left}
                        width={width}
                        batchIndex={batchIdx}
                        batchTotal={batchTotal}
                        onClick={() => {
                          if (slot.item.status === 'pending') onEditItem(slot.item);
                        }}
                        onContextMenu={(e) => openMenu(e, slot.item)}
                      />
                    );
                  })}
                </div>
              </div>
            ))}

            {/* "Now" line across all lanes */}
            <div
              className="absolute top-0 bottom-0 w-px bg-red-400/70 pointer-events-none"
              style={{ left: LANE_LABEL_WIDTH + nowLeft }}
            />
          </div>
        </div>
      </div>

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuItems}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  );
}
