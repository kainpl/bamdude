import { useTranslation } from 'react-i18next';

interface Props {
  ticks: number[];
  windowStartMs: number;
  windowDurationMs: number;
  nowMs: number;
  /** Width of the label column on the left in px. */
  laneLabelWidth: number;
  /** Full track width in px (not including label column). */
  trackWidth: number;
}

function formatTick(ms: number): string {
  const d = new Date(ms);
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function formatDateLabel(ms: number): string {
  const d = new Date(ms);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function TimelineAxis({ ticks, windowStartMs, windowDurationMs, nowMs, laneLabelWidth, trackWidth }: Props) {
  const { t } = useTranslation();
  const pxPerMs = trackWidth / windowDurationMs;

  // Show day labels once per rendered midnight that falls in the window.
  const dayLabels: { ms: number; label: string }[] = [];
  const startDay = new Date(windowStartMs);
  startDay.setHours(0, 0, 0, 0);
  for (let t0 = startDay.getTime(); t0 <= windowStartMs + windowDurationMs; t0 += 24 * 60 * 60 * 1000) {
    if (t0 >= windowStartMs) dayLabels.push({ ms: t0, label: formatDateLabel(t0) });
  }

  return (
    <div
      className="relative border-b border-bambu-dark-tertiary"
      style={{ height: 40, paddingLeft: laneLabelWidth }}
    >
      <div className="relative h-full" style={{ width: trackWidth }}>
        {/* Day labels */}
        {dayLabels.map(({ ms, label }) => {
          const left = (ms - windowStartMs) * pxPerMs;
          return (
            <div
              key={`day-${ms}`}
              className="absolute top-0 text-[10px] text-bambu-gray-light uppercase tracking-wide"
              style={{ left: Math.max(0, left) + 4 }}
            >
              {label}
            </div>
          );
        })}

        {/* Tick marks */}
        {ticks.map(tick => {
          const left = (tick - windowStartMs) * pxPerMs;
          return (
            <div key={tick} className="absolute top-0 h-full flex flex-col items-start" style={{ left }}>
              <div className="h-2" />
              <div className="flex-1 w-px bg-bambu-dark-tertiary/60" />
              <div className="absolute bottom-1 left-1 text-[10px] text-bambu-gray">{formatTick(tick)}</div>
            </div>
          );
        })}

        {/* Now marker label */}
        <div
          className="absolute top-0 h-full"
          style={{ left: (nowMs - windowStartMs) * pxPerMs }}
        >
          <div className="absolute -top-0.5 left-0 -translate-x-1/2 text-[9px] font-semibold text-red-400 bg-bambu-dark px-1 rounded">
            {t('queue.timeline.now')}
          </div>
        </div>
      </div>
    </div>
  );
}
