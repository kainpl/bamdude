import { useTranslation } from 'react-i18next';
import { Pause } from 'lucide-react';
import type { PrinterStatus } from '../api/client';

/**
 * The next pause the slicer put into the running print, as the server says it
 * (`next_pause`). Both card layouts draw this one element. The server decides
 * everything: an absent field draws nothing, and a time is shown only when the
 * server gave one (Standard speed, a file with time markers) — otherwise the
 * layer alone. The server counts whole minutes and answers 0 for the last one
 * (and for a printer running behind the slicer's estimate): that is "less than
 * a minute", never "in ~0 min".
 *
 * ⚠️ The text is clipped (`truncate`), so its line box must be taller than
 * 1 em: `leading-none` cuts the tails of "р", "у" and the brackets. The
 * caller sets the size; the line height is this component's.
 */
export function NextPauseLine({ nextPause, className = '' }: {
  nextPause: PrinterStatus['next_pause'];
  className?: string;
}) {
  const { t } = useTranslation();
  if (!nextPause) return null;
  const { layer, minutes } = nextPause;
  return (
    <span
      data-testid="next-pause"
      className={`flex min-w-0 items-center gap-1 leading-snug text-status-warning ${className}`}
      title={t('printers.nextPause.title', { layer })}
    >
      <Pause className="w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] shrink-0" />
      <span className="truncate">
        {minutes == null
          ? t('printers.nextPause.atLayer', { layer })
          : minutes < 1
            ? t('printers.nextPause.underMinute', { layer })
            : t('printers.nextPause.inMinutes', { minutes, layer })}
      </span>
    </span>
  );
}
