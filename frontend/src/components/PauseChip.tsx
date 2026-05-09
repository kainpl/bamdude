import { useEffect, useState } from 'react';
import { Pause } from 'lucide-react';
import { useTranslation } from 'react-i18next';

/**
 * Inline chip rendered in printer-card headers when a printer is in the
 * PAUSE state. Shows the pause cause + a live-ticking elapsed counter so
 * an operator scanning a 20-printer grid can see "Bambu X1C — paused
 * 14m, door open" at a glance instead of having to open the HMS modal.
 *
 * Counter source-of-truth is ``status.pause_started_at`` (epoch seconds,
 * stamped server-side in ``main._handle_pause_edge``). Frontend ticks every
 * second locally; on F5 / page navigation the snapshot still carries the
 * original timestamp so the counter resumes from the correct value.
 *
 * Used in two view modes (compact + expanded) — same component, different
 * spacing controlled by parent. Hides itself entirely when not paused or
 * when the snapshot lacks a start timestamp (printer was already paused
 * when BamDude restarted; the running snapshot only knows ``state="PAUSE"``
 * but no edge ever fired so we have no anchor for the counter — render the
 * static "Paused" chip with reason but no minutes).
 */
interface PauseChipProps {
  state: string | null | undefined;
  pauseReasonLabel: string | null | undefined;
  pauseStartedAt: number | null | undefined;
  size?: 'sm' | 'xs';
}

export function PauseChip({ state, pauseReasonLabel, pauseStartedAt, size = 'sm' }: PauseChipProps) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    if (state !== 'PAUSE') return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [state]);

  if (state !== 'PAUSE') return null;

  const elapsed = pauseStartedAt ? Math.max(0, Math.floor(now - pauseStartedAt)) : null;
  const elapsedLabel = elapsed === null
    ? null
    : elapsed < 60
      ? `${elapsed}s`
      : elapsed < 3600
        ? `${Math.floor(elapsed / 60)}m`
        : `${Math.floor(elapsed / 3600)}h ${Math.floor((elapsed % 3600) / 60)}m`;

  const reason = pauseReasonLabel || t('printers.status.paused', 'Paused');
  const padding = size === 'xs' ? 'px-1.5 py-0.5' : 'px-2 py-0.5';
  const text = size === 'xs' ? 'text-[10px]' : 'text-xs';
  const icon = size === 'xs' ? 'w-2.5 h-2.5' : 'w-3 h-3';

  return (
    <span
      className={`inline-flex items-center gap-1 ${padding} bg-status-warning/20 text-status-warning rounded-full ${text} font-medium whitespace-nowrap`}
      title={reason}
    >
      <Pause className={icon} />
      <span className="truncate max-w-[180px]">{reason}</span>
      {elapsedLabel && <span className="opacity-70">· {elapsedLabel}</span>}
    </span>
  );
}
