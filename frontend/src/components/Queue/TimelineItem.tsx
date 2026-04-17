import { useTranslation } from 'react-i18next';
import { AlertTriangle, Hand, HelpCircle } from 'lucide-react';
import type { TimelineSlot } from '../../hooks/useQueueTimeline';

interface Props {
  slot: TimelineSlot;
  left: number;
  width: number;
  /** Index of this slot among items in the same batch. 0 if solo. */
  batchIndex: number;
  batchTotal: number;
  onClick: (e: React.MouseEvent) => void;
  onContextMenu: (e: React.MouseEvent) => void;
}

function formatRange(startMs: number, endMs: number): string {
  const fmt: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' };
  return `${new Date(startMs).toLocaleTimeString(undefined, fmt)} – ${new Date(endMs).toLocaleTimeString(undefined, fmt)}`;
}

function formatDuration(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  if (h > 0 && m > 0) return `${h}h ${m}m`;
  if (h > 0) return `${h}h`;
  return `${m}m`;
}

export function TimelineItem({
  slot,
  left,
  width,
  batchIndex,
  batchTotal,
  onClick,
  onContextMenu,
}: Props) {
  const { t } = useTranslation();
  const { item, active, estimated, scheduledPin, startMs, endMs, durationSec } = slot;

  const title = item.archive_name || item.library_file_name || `#${item.id}`;
  const filamentColor = item.filament_color || null;

  const bg = active
    ? 'bg-bambu-green/25 border-bambu-green'
    : estimated
      ? 'bg-bambu-dark-tertiary/60 border-dashed border-bambu-dark-tertiary'
      : 'bg-bambu-dark border-bambu-dark-tertiary hover:border-bambu-green';

  const tooltipLines = [
    title,
    item.printer_name || '',
    formatRange(startMs, endMs),
    `${t('queue.timeline.estimatedDuration')}: ${formatDuration(durationSec)}${estimated ? ` (${t('queue.timeline.noDuration')})` : ''}`,
  ];
  if (item.waiting_reason) tooltipLines.push(`⏳ ${item.waiting_reason}`);
  if (scheduledPin) tooltipLines.push(`📌 ${new Date(startMs).toLocaleString()}`);
  if (batchTotal > 1) tooltipLines.push(`${t('queue.timeline.batchGroupOf', { count: batchTotal })} · ${batchIndex + 1}/${batchTotal}`);

  const clampedWidth = Math.max(6, width);
  // Slots that began before the visible window render with negative `left`.
  // Without compensation, the label sits off-screen at the slot's hard-left
  // edge.  Shifting the label right by the off-screen amount keeps the
  // filename visible near the window-left edge; the label still truncates
  // against the slot's right edge so it can't escape the slot bounds.
  const hiddenLeft = Math.max(0, -left);
  const labelOffset = hiddenLeft + 10; // 10px default gutter after the colour stripe
  const labelWidth = clampedWidth - labelOffset - 4; // 4px right gutter
  const showLabel = labelWidth >= 60;

  return (
    <button
      type="button"
      onClick={onClick}
      onContextMenu={onContextMenu}
      title={tooltipLines.join('\n')}
      className={`absolute top-1 bottom-1 rounded border ${bg} text-left overflow-hidden transition-colors`}
      style={{ left, width: clampedWidth }}
    >
      {/* Filament color stripe */}
      {filamentColor && (
        <div
          className="absolute left-0 top-0 bottom-0 w-1.5"
          style={{ backgroundColor: filamentColor }}
        />
      )}

      {showLabel && (
        <div
          className="absolute top-0 bottom-0 flex items-center gap-1 min-w-0"
          style={{ left: labelOffset, width: labelWidth }}
        >
          {estimated && <HelpCircle className="w-3 h-3 text-bambu-gray shrink-0" />}
          {item.manual_start && <Hand className="w-3 h-3 text-yellow-400 shrink-0" />}
          {item.waiting_reason && <AlertTriangle className="w-3 h-3 text-yellow-400 shrink-0" />}
          <span className="truncate text-xs text-white min-w-0">{title}</span>
          {batchTotal > 1 && (
            <span className="ml-auto shrink-0 text-[10px] text-bambu-gray-light bg-bambu-dark/60 px-1 rounded">
              {batchIndex + 1}/{batchTotal}
            </span>
          )}
        </div>
      )}

      {/* Progress stripe for active print */}
      {active && item.print_time_seconds && item.started_at && (
        <ActiveProgress slot={slot} />
      )}
    </button>
  );
}

function ActiveProgress({ slot }: { slot: TimelineSlot }) {
  const startedAt = slot.item.started_at ? Date.parse(slot.item.started_at) : null;
  if (!startedAt || !slot.item.print_time_seconds) return null;
  const elapsedRatio = Math.max(
    0,
    Math.min(1, (Date.now() - startedAt) / (slot.item.print_time_seconds * 1000)),
  );
  return (
    <div
      className="absolute bottom-0 left-0 h-0.5 bg-bambu-green"
      style={{ width: `${elapsedRatio * 100}%` }}
    />
  );
}
