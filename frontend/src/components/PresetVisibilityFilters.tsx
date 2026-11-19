import { useTranslation } from 'react-i18next';
import type { usePresetVisibility } from '../hooks/usePresetVisibility';

export function PresetVisibilityFilters({ filters, disabled = false }: {
  filters: ReturnType<typeof usePresetVisibility>; disabled?: boolean;
}) {
  const { t } = useTranslation();
  return <div className="rounded-lg border border-bambu-green/30 bg-bambu-green/5 p-3 space-y-2">
    <div className="flex flex-wrap gap-x-5 gap-y-2">
      {filters.canReadFarm && <label className="flex min-h-[44px] md:min-h-0 items-center gap-2 text-sm text-white cursor-pointer">
        <input type="checkbox" className="accent-bambu-green" checked={filters.onlyFarm}
          disabled={disabled} onChange={(e) => filters.setOnlyFarm(e.target.checked)} />
        {t('slice.visibility.onlyFarm')}
      </label>}
      <label className="flex min-h-[44px] md:min-h-0 items-center gap-2 text-sm text-white cursor-pointer">
        <input type="checkbox" className="accent-bambu-green" checked={filters.onlyMine}
          disabled={disabled} onChange={(e) => filters.setOnlyMine(e.target.checked)} />
        {t('slice.visibility.onlyMine')}
      </label>
    </div>
    <p className="text-xs text-bambu-gray">{t('slice.visibility.hint')}</p>
    {filters.onlyMine && filters.visible && !tally(filters.visible) && <p role="status" className="text-xs text-amber-400">
      {t('slice.visibility.noMine')}
      <button type="button" disabled={disabled} className="ml-1 underline" onClick={() => filters.setOnlyMine(false)}>{t('slice.visibility.showAll')}</button>
    </p>}
    {filters.unavailable && <p role="status" className="text-xs text-amber-400">{t('slice.visibility.unavailable')}</p>}
  </div>;
}

function tally(data: NonNullable<ReturnType<typeof usePresetVisibility>['visible']>) {
  return data.local.process.length + data.local.filament.length + data.orca_cloud.process.length
    + data.orca_cloud.filament.length + data.cloud.process.length + data.cloud.filament.length;
}
