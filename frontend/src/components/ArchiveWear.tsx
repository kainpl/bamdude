import { Wrench } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ArchiveWearFields } from '../utils/archiveWear';
import { formatMoney } from '../utils/currency';


/**
 * The wear charged to one print, as the server stamped it when the print ended — beside the
 * energy cost. The rate in the tooltip is the one that was applied, not the printer's current one.
 * `currency` is a code or an already resolved symbol; the formatter takes either.
 */
export function ArchiveWearFact({
  archive,
  currency,
  className,
  iconClassName,
}: {
  archive: ArchiveWearFields;
  currency: string;
  className: string;
  iconClassName: string;
}) {
  const { t } = useTranslation();
  if (archive.wear_cost == null) return null;
  const title =
    archive.wear_rate != null
      ? t('archives.wear.titleWithRate', { rate: formatMoney(archive.wear_rate, currency) })
      : t('archives.wear.title');
  return (
    <span className={className} title={title}>
      <Wrench className={iconClassName} />
      {formatMoney(archive.wear_cost, currency)}
    </span>
  );
}
