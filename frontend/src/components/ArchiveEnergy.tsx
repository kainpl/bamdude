import { Zap } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ArchiveEnergyFields } from '../utils/archiveEnergy';

const KNOWN_REASONS = new Set([
  'insufficient_history',
  'unknown_end',
  'source_changed',
  'clock_before_source',
  'insufficient_budget',
]);

/**
 * An archive's energy, as the server says it was obtained (spec plug-meter-watchdog W11) — never
 * read off the number: an estimate and a recovered window carry «≈», a figure the server could
 * not trust says so instead of reading as a zero.
 */
export function ArchiveEnergyFact({
  archive,
  currency,
  className,
  iconClassName,
}: {
  archive: ArchiveEnergyFields;
  currency: string;
  className: string;
  iconClassName: string;
}) {
  const { t } = useTranslation();
  if (archive.energy_status === 'unavailable') {
    const reason = archive.energy_reason && KNOWN_REASONS.has(archive.energy_reason) ? archive.energy_reason : 'other';
    return (
      <span className={`${className} text-status-warning`} title={t(`archives.energy.reasons.${reason}`)}>
        <Zap className={iconClassName} />
        {t('archives.energy.unknown')}
      </span>
    );
  }
  if (archive.energy_cost == null) return null;
  const kwh = archive.energy_kwh?.toFixed(3) ?? 'N/A';
  const approximate = archive.energy_status === 'estimated' || archive.energy_status === 'approximate';
  const title =
    archive.energy_status === 'estimated'
      ? t('archives.energy.estimated', { kwh, coverage: Math.round((archive.energy_coverage ?? 0) * 100) })
      : archive.energy_status === 'approximate'
        ? t('archives.energy.approximate', { kwh })
        : `${t('stats.energyUsed')}: ${kwh} kWh`;
  return (
    <span className={className} title={title}>
      <Zap className={iconClassName} />
      {approximate ? '≈ ' : ''}
      {currency}
      {archive.energy_cost.toFixed(2)}
    </span>
  );
}
