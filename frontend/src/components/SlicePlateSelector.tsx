import { useEffect, useState } from 'react';
import { Layers, Check, Box } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { PlateMetadata } from '../types/plates';

interface SlicePlateSelectorProps {
  plates: PlateMetadata[];
  selectedPlate: number | null;
  onSelect: (plateIndex: number) => void;
  disabled?: boolean;
}

/**
 * Single-select plate picker for the SliceModal — visually mirrors
 * ``PrintModal/PlateSelector`` (vertical paginator on the left + big
 * details card on the right) but with two differences:
 *
 *   1. **Single-select.** A slice job operates on exactly one plate, so
 *      the UI tracks one ``selectedPlate`` int instead of a Set; the
 *      paginator just navigates the visible card, the card body is the
 *      "pick this plate" affordance.
 *   2. **No print-time / weight / per-plate filament breakdown.** Those
 *      stats only exist for already-sliced files; the SliceModal mostly
 *      runs against unsliced project 3MFs where the values are null.
 *      Keep it simple — thumbnail, plate name, object count + names.
 *
 * Activation rule (PrintModal pattern preserved): ``activeIdx`` is the
 * paginator's currently-visible plate, decoupled from ``selectedPlate``
 * so the user can flip through plate previews without committing. The
 * card click is what moves ``selectedPlate``.
 */
export function SlicePlateSelector({
  plates,
  selectedPlate,
  onSelect,
  disabled,
}: SlicePlateSelectorProps) {
  const { t } = useTranslation();

  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  useEffect(() => {
    if (plates.length === 0) return;
    if (activeIdx !== null && plates.find((p) => p.index === activeIdx)) return;
    // Default the visible card to the selected plate so reopening the
    // modal mid-flow lands on the user's existing pick; otherwise show
    // plate 1 first.
    const fallback =
      selectedPlate != null && plates.find((p) => p.index === selectedPlate)
        ? selectedPlate
        : plates[0].index;
    setActiveIdx(fallback);
  }, [plates, selectedPlate, activeIdx]);

  if (plates.length <= 1) return null;
  const active = plates.find((p) => p.index === activeIdx);

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <Layers className="w-4 h-4 text-bambu-gray" />
        <span className="text-sm text-bambu-gray">
          {t('slice.platePicker.label', 'Pick a plate to slice')}
        </span>
      </div>

      <div className="flex gap-2 items-stretch">
        {/* Vertical paginator — one button per plate. Click switches the
            active card; the dot in the corner mirrors the committed pick
            so the user can browse without losing track of their choice. */}
        <div className="flex flex-col gap-1 flex-shrink-0">
          {plates.map((p) => {
            const isActive = p.index === activeIdx;
            const isSelected = p.index === selectedPlate;
            return (
              <button
                key={p.index}
                type="button"
                onClick={() => setActiveIdx(p.index)}
                disabled={disabled}
                className={`relative w-10 h-10 rounded-lg border text-xs font-medium transition-colors disabled:opacity-50 ${
                  isActive
                    ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                    : 'border-bambu-dark-tertiary bg-bambu-dark text-bambu-gray hover:border-bambu-gray'
                }`}
                title={p.name || t('archives.platePicker.plateLabel', { index: p.index })}
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

        {/* Active-plate details card. Clicking commits the pick; the
            current selection gets the green border + check icon. */}
        {active && (
          <button
            type="button"
            onClick={() => onSelect(active.index)}
            disabled={disabled}
            className={`flex-1 flex flex-col gap-3 p-3 rounded-lg border transition-colors text-left disabled:opacity-50 ${
              selectedPlate === active.index
                ? 'border-bambu-green bg-bambu-green/10'
                : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-gray'
            }`}
          >
            <div className="flex items-start gap-3 h-32">
              {active.has_thumbnail && active.thumbnail_url != null ? (
                <img
                  src={active.thumbnail_url}
                  alt={t('archives.platePicker.plateLabel', { index: active.index })}
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
                    {active.name || t('archives.platePicker.plateLabel', { index: active.index })}
                  </p>
                  {selectedPlate === active.index && (
                    <Check className="w-4 h-4 text-bambu-green flex-shrink-0" />
                  )}
                </div>

                {(active.object_count ?? active.objects.length) > 0 && (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-bambu-gray">
                    <span className="flex items-center gap-1">
                      <Box className="w-3 h-3" />
                      {active.object_count ?? active.objects.length}
                    </span>
                  </div>
                )}

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
          </button>
        )}
      </div>
    </div>
  );
}
