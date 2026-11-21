import { useTranslation } from 'react-i18next';

export function AutoQueueReturnInfo({ returned, ordinal, count = 0, reason, kind, autoItemId, unsentCount = 0, printer }: {
  returned?: boolean;
  ordinal?: number | null;
  count?: number;
  reason?: string | null;
  kind?: 'failed_print' | 'unsent' | null;
  autoItemId?: number | null;
  unsentCount?: number;
  printer?: string | null;
}) {
  const { t } = useTranslation();
  if (!returned && count === 0 && unsentCount === 0 && !reason) return null;
  const href = autoItemId == null ? '#auto-queue' : `/queue#auto-job-${autoItemId}`;
  return <span className="inline-flex flex-wrap items-center gap-x-2 text-xs text-bambu-gray max-w-full">
    {returned && <a href={href} className="inline-flex items-center min-h-11 underline text-bambu-green focus-visible:outline focus-visible:outline-2">
      {t(kind === 'unsent' ? 'autoQueue.failedReturn.unsent' : 'autoQueue.failedReturn.returned')}
      {kind !== 'unsent' && ordinal ? ` · ${ordinal}/2` : ''}
    </a>}
    {!returned && count > 0 && <span>{t('autoQueue.failedReturn.count', { count })}</span>}
    {unsentCount > 0 && <span className="break-words min-w-0">{t('autoQueue.failedReturn.unsentCount', {
      count: unsentCount, printer: printer || t('autoQueue.failedReturn.unknownPrinter'),
    })}</span>}
    {reason && <span>{t('autoQueue.failedReturn.deferred', {
      reason: t(`autoQueue.failedReturn.reasons.${reason}`, { defaultValue: t('autoQueue.failedReturn.reasons.temporarily_unavailable') }),
    })}</span>}
  </span>;
}
