import { useEffect, useState } from 'react';
import { Layers, Check, AlertTriangle, Square, CheckSquare, Clock, Weight, Box } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { PlateSelectorProps } from './types';
import { formatDuration } from '../../utils/date';
import { resolveSpoolColorName } from '../../utils/colors';

/**
 * Plate selection for multi-plate 3MF files.
 *
 * Layout: a tall paginator strip on the LEFT (vertical column of plate-index
 * buttons, each showing the selected-state dot) + one big card on the RIGHT
 * with the active plate's thumbnail, name, full per-plate stats (print time,
 * total weight, instance count from ``object_count``) and per-filament
 * breakdown (color swatch + type + grams).
 *
 * Selection state lives outside this component (in PrintModal) so navigating
 * between plates with the paginator preserves which plates are selected for
 * dispatch. Navigation and selection are deliberately decoupled — clicking a
 * paginator button switches the visible plate, while toggling selection is
 * done from the big card body (checkbox in multi-select mode, click-to-select
 * in single-select mode).
 */
export function PlateSelector({
  plates,
  isMultiPlate,
  selectedPlates,
  onToggle,
  onSelectAll,
  onDeselectAll,
  multiSelect,
}: PlateSelectorProps) {
  const { t } = useTranslation();

  // Currently displayed plate index. Default to the first selected plate so
  // when the user opens the modal mid-flow they see the relevant card right
  // away; falls back to plate 1 if none selected yet.
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  useEffect(() => {
    if (plates.length === 0) return;
    if (activeIdx !== null && plates.find((p) => p.index === activeIdx)) return;
    const firstSelected = plates.find((p) => selectedPlates.has(p.index));
    setActiveIdx((firstSelected ?? plates[0]).index);
  }, [plates, selectedPlates, activeIdx]);

  if (!isMultiPlate || plates.length <= 1) return null;

  const allSelected = selectedPlates.size === plates.length;
  const active = plates.find((p) => p.index === activeIdx);

  return (
    <div className="mb-4">
      <div className="flex items-center gap-2 mb-2">
        <Layers className="w-4 h-4 text-bambu-gray" />
        <span className="text-sm text-bambu-gray">
          {multiSelect ? t('printModal.selectPlatesToPrint') : t('printModal.selectPlateToPrint')}
        </span>
        {selectedPlates.size === 0 && (
          <span className="text-xs text-orange-400 flex items-center gap-1">
            <AlertTriangle className="w-3 h-3" />
            {t('printModal.selectionRequired')}
          </span>
        )}
        {multiSelect && onSelectAll && onDeselectAll && (
          <button
            type="button"
            onClick={allSelected ? onDeselectAll : onSelectAll}
            className={`ml-auto text-xs px-2 py-0.5 rounded-full border transition-colors ${
              allSelected
                ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                : 'border-bambu-dark-tertiary text-bambu-gray hover:border-bambu-gray'
            }`}
          >
            {allSelected
              ? t('queue.deselectAll')
              : t('queue.selectAllPlates', { count: plates.length })}
          </button>
        )}
      </div>

      <div className="flex gap-2 items-stretch">
        {/* Vertical paginator strip — one button per plate, clicking
            switches the visible card. The little dot/check on each button
            mirrors selection state so the user can see at a glance which
            plates are queued for dispatch. */}
        <div className="flex flex-col gap-1 flex-shrink-0">
          {plates.map((p) => {
            const isActive = p.index === activeIdx;
            const isSelected = selectedPlates.has(p.index);
            return (
              <button
                key={p.index}
                type="button"
                onClick={() => setActiveIdx(p.index)}
                className={`relative w-10 h-10 rounded-lg border text-xs font-medium transition-colors ${
                  isActive
                    ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                    : 'border-bambu-dark-tertiary bg-bambu-dark text-bambu-gray hover:border-bambu-gray'
                }`}
                title={p.name || t('printModal.plateNFallback', { index: p.index })}
              >
                {p.index}
                {isSelected && (
                  <span
                    className="absolute top-0.5 right-0.5 w-1.5 h-1.5 rounded-full bg-bambu-green"
                    aria-hidden="true"
                  />
                )}
              </button>
            );
          })}
        </div>

        {/* Active-plate card — full remaining width. Click selects (single
            mode) or the checkbox in the header toggles (multi mode). */}
        {active && (
          <button
            type="button"
            onClick={() => onToggle(active.index)}
            className={`flex-1 flex flex-col gap-3 p-3 rounded-lg border transition-colors text-left ${
              selectedPlates.has(active.index)
                ? 'border-bambu-green bg-bambu-green/10'
                : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-gray'
            }`}
          >
            <div className="flex items-start gap-3 h-32">
              {active.has_thumbnail && active.thumbnail_url != null ? (
                <img
                  src={active.thumbnail_url}
                  alt={t('printModal.plateNFallback', { index: active.index })}
                  className="w-32 h-32 rounded object-contain bg-bambu-dark-tertiary flex-shrink-0"
                />
              ) : (
                <div className="w-32 h-32 rounded bg-bambu-dark-tertiary flex items-center justify-center flex-shrink-0">
                  <Layers className="w-10 h-10 text-bambu-gray" />
                </div>
              )}

              <div className="min-w-0 flex-1 flex flex-col h-full min-h-0">
                <div className="flex items-start justify-between gap-2 mb-1.5">
                  <p className="text-sm text-white font-medium truncate">
                    {active.name || t('printModal.plateNFallback', { index: active.index })}
                  </p>
                  {multiSelect ? (
                    selectedPlates.has(active.index) ? (
                      <CheckSquare className="w-4 h-4 text-bambu-green flex-shrink-0" />
                    ) : (
                      <Square className="w-4 h-4 text-bambu-gray flex-shrink-0" />
                    )
                  ) : selectedPlates.has(active.index) ? (
                    <Check className="w-4 h-4 text-bambu-green flex-shrink-0" />
                  ) : null}
                </div>

                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-bambu-gray">
                  {active.print_time_seconds != null && (
                    <span className="flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {formatDuration(active.print_time_seconds)}
                    </span>
                  )}
                  {active.filament_used_grams != null && (
                    <span className="flex items-center gap-1">
                      <Weight className="w-3 h-3" />
                      {active.filament_used_grams.toFixed(1)} {t('common.gramShort')}
                    </span>
                  )}
                  {(active.object_count ?? active.objects.length) > 0 && (
                    <span className="flex items-center gap-1">
                      <Box className="w-3 h-3" />
                      {active.object_count ?? active.objects.length}
                    </span>
                  )}
                </div>

                {active.objects.length > 0 && (
                  <ul className="mt-1.5 text-xs text-bambu-gray/70 space-y-0.5 list-disc list-inside flex-1 min-h-0 overflow-y-auto">
                    {active.objects.map((name, i) => (
                      <li key={`${name}-${i}`} className="truncate">
                        {name}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            {active.filaments.length > 0 && (
              <div className="border-t border-bambu-dark-tertiary pt-2 space-y-1">
                {active.filaments.map((f, i) => (
                  <div key={f.slot_id ?? i} className="flex items-center gap-2 text-xs">
                    <span
                      className="inline-block w-3 h-3 rounded-sm border border-bambu-dark-tertiary flex-shrink-0"
                      style={{ backgroundColor: f.color || '#888' }}
                    />
                    {f.slot_id != null && (
                      <span className="text-white">
                        {t('fileManager.plateSlot')} {f.slot_id}
                      </span>
                    )}
                    <span className="text-bambu-gray">{f.type}</span>
                    <span className="text-bambu-gray">{resolveSpoolColorName(null, f.color) || f.color}</span>
                    {f.used_grams != null && (
                      <span className="ml-auto text-bambu-gray">
                        {f.used_grams.toFixed(1)} {t('common.gramShort')}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </button>
        )}
      </div>
    </div>
  );
}
