// The filter controls of the slicing-setup family (spec slicing-setup r5, R2 /
// D4): the source segment (All / My presets / Built-in) and the farm switches,
// each part shown only where the place asks for it, plus the farm read's
// forbidden / unavailable / stale notes. The relaxation notes of a field are
// that field's own (PresetSelect).
import { useTranslation } from 'react-i18next';

import type { PresetSourceFilter } from '../../utils/presetPickerUtils';
import type { PresetFiltersState } from './usePresetFilters';

interface PresetFiltersProps {
  filters: PresetFiltersState;
  parts: { source?: boolean; farmModels?: boolean; loaded?: boolean };
  disabled?: boolean;
}

export function PresetFilters({ filters, parts, disabled }: PresetFiltersProps) {
  const { t } = useTranslation();
  const sourceOptions: { key: PresetSourceFilter; label: string }[] = [
    { key: 'all', label: t('profiles.cloudView.filters.all', 'All') },
    { key: 'mine', label: t('profiles.cloudView.filters.myPresets', 'My Presets') },
    { key: 'builtin', label: t('profiles.cloudView.filters.builtIn', 'Built-in') },
  ];
  // The farm switches need the farm read's rights; without them there are no
  // dead switches and no request.
  const showSwitches = filters.offered && (parts.farmModels || parts.loaded);
  const switchClass = 'flex items-center gap-2 text-sm text-bambu-gray cursor-pointer select-none';
  return (
    <div className="space-y-2">
      {parts.source && (
        <fieldset className="space-y-1">
          <legend className="text-xs text-bambu-gray mb-1">{t('slice.presetSource', 'Preset source')}</legend>
          <div className="inline-flex rounded-md border border-bambu-dark-tertiary overflow-hidden">
            {sourceOptions.map((opt) => {
              const selected = filters.source === opt.key;
              return (
                <button
                  key={opt.key}
                  type="button"
                  onClick={() => filters.setSource(opt.key)}
                  disabled={disabled}
                  aria-pressed={selected}
                  className={`px-3 py-1.5 text-xs transition-colors disabled:opacity-50 ${
                    selected ? 'bg-bambu-green/20 text-white' : 'bg-bambu-dark text-bambu-gray hover:text-white'
                  }`}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </fieldset>
      )}
      {showSwitches && (
        <div className="space-y-1.5">
          <div className="flex flex-wrap gap-x-5 gap-y-1.5">
            {parts.farmModels && (
              <label className={switchClass}>
                <input
                  type="checkbox"
                  checked={filters.onlyFarmModels}
                  onChange={(e) => filters.setOnlyFarmModels(e.target.checked)}
                  disabled={disabled}
                  className="accent-bambu-green cursor-pointer"
                />
                {t('slicingSetup.farmModels')}
              </label>
            )}
            {parts.loaded && (
              <label className={switchClass}>
                <input
                  type="checkbox"
                  checked={filters.onlyLoaded}
                  onChange={(e) => filters.setOnlyLoaded(e.target.checked)}
                  disabled={disabled}
                  className="accent-bambu-green cursor-pointer"
                />
                {t('slice.farmFilter.onlyLoaded')}
              </label>
            )}
          </div>
          {filters.farmStatus === 'forbidden' && (
            <p role="status" className="text-xs text-amber-700 dark:text-amber-400">
              {t('slice.farmFilter.forbidden')}
            </p>
          )}
          {(filters.farmStatus === 'unavailable' || filters.farmStatus === 'stale') && (
            <p role="status" className="text-xs text-amber-700 dark:text-amber-400">
              {filters.farmStatus === 'stale' ? t('slice.farmFilter.stale') : t('slice.farmFilter.unavailable')}{' '}
              <button
                type="button"
                onClick={filters.retry}
                className="underline underline-offset-2 hover:text-white transition-colors"
              >
                {t('common.retry')}
              </button>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
