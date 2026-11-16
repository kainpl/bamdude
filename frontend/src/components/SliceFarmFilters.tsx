// The two switches of the Slice dialog's farm filters, drawn: "only printers
// that are online" and "only filaments that are loaded". State, memory and the
// rules live in `hooks/useSliceFarmFilters`.
import { useTranslation } from 'react-i18next';

import type { SliceFarmFilters } from '../hooks/useSliceFarmFilters';

interface TogglesProps {
  filters: SliceFarmFilters;
  disabled?: boolean;
}

/** The two switches, and — when a filter leaves a list empty — why, with a way back. */
export function SliceFarmFilterToggles({ filters, disabled }: TogglesProps) {
  const { t } = useTranslation();
  const note = (text: string, showAllLabel: string, onShowAll: () => void) => (
    <p role="status" className="text-xs text-amber-700 dark:text-amber-400">
      {text}{' '}
      <button
        type="button"
        onClick={onShowAll}
        aria-label={showAllLabel}
        className="underline underline-offset-2 hover:text-white transition-colors"
      >
        {t('slice.farmFilter.showAll')}
      </button>
    </p>
  );
  // Not for someone who may not read printers: no dead switches, no request.
  if (!filters.offered) return null;
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap gap-x-5 gap-y-1.5">
        <label className="flex items-center gap-2 text-sm text-bambu-gray cursor-pointer select-none">
          <input
            type="checkbox"
            checked={filters.onlyOnline}
            onChange={(e) => filters.setOnlyOnline(e.target.checked)}
            disabled={disabled}
            className="accent-bambu-green cursor-pointer"
          />
          {t('slice.farmFilter.onlyOnline')}
        </label>
        <label className="flex items-center gap-2 text-sm text-bambu-gray cursor-pointer select-none">
          <input
            type="checkbox"
            checked={filters.onlyLoaded}
            onChange={(e) => filters.setOnlyLoaded(e.target.checked)}
            disabled={disabled}
            className="accent-bambu-green cursor-pointer"
          />
          {t('slice.farmFilter.onlyLoaded')}
        </label>
      </div>
      {filters.forbidden && (
        <p role="status" className="text-xs text-amber-700 dark:text-amber-400">
          {t('slice.farmFilter.forbidden')}
        </p>
      )}
      {(filters.unavailable || filters.stale) && (
        <p role="status" className="text-xs text-amber-700 dark:text-amber-400">
          {filters.stale ? t('slice.farmFilter.stale') : t('slice.farmFilter.unavailable')}{' '}
          <button
            type="button"
            onClick={filters.retry}
            className="underline underline-offset-2 hover:text-white transition-colors"
          >
            {t('common.retry')}
          </button>
        </p>
      )}
      {filters.printerNote
        && note(filters.printerNote, t('slice.farmFilter.showAllPrinters'), () => filters.setOnlyOnline(false))}
      {filters.filamentNote
        && note(filters.filamentNote, t('slice.farmFilter.showAllFilaments'), () => filters.setOnlyLoaded(false))}
    </div>
  );
}
