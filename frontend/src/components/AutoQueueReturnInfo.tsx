import { useTranslation } from 'react-i18next';

export function AutoQueueReturnInfo({ returned, ordinal, count = 0, reason }: {
  returned?: boolean;
  ordinal?: number | null;
  count?: number;
  reason?: string | null;
}) {
  const { t } = useTranslation();
  if (!returned && count === 0 && !reason) return null;
  return <span className="inline-flex flex-wrap items-center gap-x-2 text-xs text-bambu-gray max-w-full">
    {returned && <a href="#auto-queue" className="inline-flex items-center min-h-11 underline text-bambu-green focus-visible:outline focus-visible:outline-2">
      {t('autoQueue.failedReturn.returned')}{ordinal ? ` · ${ordinal}/2` : ''}
    </a>}
    {!returned && count > 0 && <span>{t('autoQueue.failedReturn.count', { count })}</span>}
    {reason && <span>{t('autoQueue.failedReturn.deferred', {
      reason: t(`autoQueue.failedReturn.reasons.${reason}`, { defaultValue: t('autoQueue.failedReturn.reasons.temporarily_unavailable') }),
    })}</span>}
  </span>;
}
