import { useTranslation } from 'react-i18next';

/** A slicer estimate for one pending print, never an ETA or a batch total. */
export function QueueDuration({ seconds, group = false }: { seconds?: number | null; group?: boolean }) {
  const { t } = useTranslation();
  const valid = typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0;
  let text = '—';
  if (valid) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    text = seconds < 60 ? t('queueOperator.lessThanMinute') : hours
      ? t('queueOperator.hoursMinutes', { hours, minutes }) : t('queueOperator.minutes', { minutes });
  }
  const title = t(valid ? group ? 'queueOperator.slicerPerPrint' : 'queueOperator.slicerTime'
    : group ? 'queueOperator.mixedTime' : 'queueOperator.unknownTime');
  return <span className="shrink-0 whitespace-nowrap text-xs tabular-nums text-bambu-gray" title={title} aria-label={`${title}: ${text}`}>{text}</span>;
}
