import { useTranslation } from 'react-i18next';

interface Props {
  /** ISO timestamp of when the last auto-tick finished, or null if never. */
  lastRunAt: string | null;
  /** Optional one-line summary (e.g. "cleared 5 archive(s), freed 12 MB"). */
  lastRunSummary?: string | null;
  /** ISO timestamp of when the next auto-tick is expected, or null when off. */
  nextRunAt: string | null;
  /** Hint shown under the "Next run" card (e.g. cron cadence). */
  nextRunHint?: string | null;
  /**
   * Whether the auto-mode toggle is currently on. When false, "Next run"
   * shows "auto-mode is off" instead of the bare "—" placeholder, so the
   * empty value is self-explanatory.
   */
  autoEnabled?: boolean;
}

function formatRelativeFromNow(iso: string | null, t: (k: string) => string): string {
  if (!iso) return t('settings.archiveCleanup.never');
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return t('settings.archiveCleanup.never');
  const diffMin = Math.round((Date.now() - ts) / 60000);
  if (diffMin < 1) return t('settings.archiveCleanup.justNow');
  if (diffMin < 60) return `${diffMin} ${t('settings.archiveCleanup.minutesAgo')}`;
  const diffH = Math.round(diffMin / 60);
  if (diffH < 48) return `${diffH} ${t('settings.archiveCleanup.hoursAgo')}`;
  const diffD = Math.round(diffH / 24);
  return `${diffD} ${t('settings.archiveCleanup.daysAgo')}`;
}

function formatRelativeUntil(iso: string | null, t: (k: string) => string): string {
  if (!iso) return '—';
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return '—';
  const diffMin = Math.max(0, Math.round((ts - Date.now()) / 60000));
  if (diffMin < 60) return `${t('settings.archiveCleanup.in')} ${diffMin} ${t('settings.archiveCleanup.minutes')}`;
  const diffH = Math.round(diffMin / 60);
  return `${t('settings.archiveCleanup.in')} ${diffH} ${t('settings.archiveCleanup.hours')}`;
}

/**
 * Pair of "Last run | Next run" cards. Shared between the archive 3MF
 * cleanup block and the library auto-purge block in Settings so the two
 * bins read identically.
 */
// Loop tick used by both auto-cleanups (matches `TICK_INTERVAL_SECONDS` /
// `_check_interval` in the backend services). When the toggle is on but the
// status query hasn't refetched yet (settings save → invalidate → fetch =
// ~700 ms-1 s), we use this to show an optimistic "in ~15 min" instead of
// the dash, which would have made the just-flipped toggle look broken.
const FIRST_TICK_OPTIMISTIC_MIN = 15;

export function LastNextRunCards({ lastRunAt, lastRunSummary, nextRunAt, nextRunHint, autoEnabled }: Props) {
  const { t } = useTranslation();
  let nextRunDisplay: string;
  if (nextRunAt !== null) {
    nextRunDisplay = formatRelativeUntil(nextRunAt, t);
  } else if (autoEnabled === false) {
    nextRunDisplay = t('settings.archiveCleanup.autoModeOff');
  } else if (autoEnabled === true) {
    // Toggle just flipped on — settings save races the status refetch.
    // Show the same "~15 min" the backend will return on the next poll.
    nextRunDisplay = `${t('settings.archiveCleanup.in')} ~${FIRST_TICK_OPTIMISTIC_MIN} ${t('settings.archiveCleanup.minutes')}`;
  } else {
    nextRunDisplay = formatRelativeUntil(nextRunAt, t);
  }
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm">
      <div className="p-3 bg-bambu-dark rounded-lg">
        <div className="text-bambu-gray text-xs mb-1">{t('settings.archiveCleanup.lastRun')}</div>
        <div className="text-white">{formatRelativeFromNow(lastRunAt, t)}</div>
        {lastRunSummary && <div className="text-xs text-bambu-gray mt-1">{lastRunSummary}</div>}
      </div>
      <div className="p-3 bg-bambu-dark rounded-lg">
        <div className="text-bambu-gray text-xs mb-1">{t('settings.archiveCleanup.nextRun')}</div>
        <div className="text-white">{nextRunDisplay}</div>
        {nextRunHint && <div className="text-xs text-bambu-gray mt-1">{nextRunHint}</div>}
      </div>
    </div>
  );
}
