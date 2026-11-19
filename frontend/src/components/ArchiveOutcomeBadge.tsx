import { useTranslation } from 'react-i18next';
import { Eye } from 'lucide-react';

/**
 * «Not checked» — a completed print whose result nobody has looked at yet.
 *
 * Not a verdict: whether the parts are good is the defect count. The server
 * decides which prints are unchecked (`outcome_unchecked`); this only shows it.
 * Amber and outlined, so it never reads as the red of a failed print — and a
 * completed print has no status badge of its own, so it takes that place.
 */
export function ArchiveOutcomeBadge({
  unchecked,
  variant,
}: {
  unchecked: boolean | undefined;
  variant: 'card' | 'row';
}) {
  const { t } = useTranslation();
  if (!unchecked) return null;
  const shape =
    variant === 'card'
      ? 'absolute top-2 left-12 px-2 py-1 rounded text-xs bg-black/60'
      : 'px-1.5 py-0.5 rounded text-[10px] leading-tight flex-shrink-0';
  return (
    <span
      data-testid="archive-outcome-unchecked"
      className={`${shape} flex items-center gap-1 border border-amber-400/60 text-amber-300`}
      title={t('archives.outcome.uncheckedTitle')}
    >
      <Eye className={variant === 'card' ? 'w-3 h-3' : 'w-2.5 h-2.5'} />
      {t('archives.outcome.unchecked')}
    </span>
  );
}
