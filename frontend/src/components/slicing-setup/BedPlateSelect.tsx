// The bed-plate select of the slicing-setup family (spec slicing-setup r5, R8):
// one dropdown over the six `curr_bed_type` values, the OrcaSlicer-only plate
// listed while OrcaSlicer is the slicer (or while it is the value, so the field
// never shows a value it has no option for), and the note of a plate that was
// not applied, outside the label so it is not part of the field's name.
import { useId } from 'react';
import { AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { BedType } from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { Select } from '../Select';
import { DEFAULT_BED_TYPE, ORCA_ONLY_BED_TYPES, type BedTypeNote } from './bedPlates';

interface BedPlateSelectProps {
  value: BedType;
  onChange: (next: BedType) => void;
  /** The slicer in effect, once known: decides whether the OrcaSlicer-only plate is offered. */
  slicer: SlicerKind | null;
  note: BedTypeNote | null;
  disabled?: boolean;
}

const PLATES: { value: BedType; labelKey: string; fallback: string }[] = [
  { value: 'Cool Plate', labelKey: 'slice.bedType.coolPlate', fallback: 'Cool Plate' },
  { value: 'Engineering Plate', labelKey: 'slice.bedType.engineeringPlate', fallback: 'Engineering Plate' },
  { value: 'High Temp Plate', labelKey: 'slice.bedType.highTempPlate', fallback: 'Smooth PEI / High Temp Plate' },
  { value: 'Textured PEI Plate', labelKey: 'slice.bedType.texturedPeiPlate', fallback: 'Textured PEI Plate' },
  { value: 'Supertack Plate', labelKey: 'slice.bedType.supertackPlate', fallback: 'Cool Plate SuperTack' },
  { value: 'Textured Cool Plate', labelKey: 'slice.bedType.texturedCoolPlate', fallback: 'Textured Cool Plate' },
];

export function BedPlateSelect({ value, onChange, slicer, note, disabled }: BedPlateSelectProps) {
  const { t } = useTranslation();
  const selectId = useId();
  const options = PLATES.filter(
    (opt) => !ORCA_ONLY_BED_TYPES.includes(opt.value) || slicer === 'orcaslicer' || opt.value === value,
  );
  const labelOf = (bed: string) => {
    const known = PLATES.find((opt) => opt.value === bed);
    return known ? t(known.labelKey, known.fallback) : bed;
  };
  const noteText =
    note == null
      ? null
      : note.kind === 'unknown'
        ? t('slice.bedType.ignoredUnknown', {
            bed: note.bed,
            defaultValue:
              'The saved settings name a bed plate that is not known here ("{{bed}}"). The bed plate was left as it is.',
          })
        : note.kind === 'slicer'
          ? t('slice.bedType.ignoredForSlicer', {
              bed: labelOf(note.bed),
              defaultValue:
                'The saved settings name {{bed}}, which is available only with OrcaSlicer. The bed plate was left as it is.',
            })
          : t('slice.bedType.resetForSlicer', {
              bed: labelOf(note.bed),
              fallback: labelOf(DEFAULT_BED_TYPE),
              defaultValue: '{{bed}} is available only with OrcaSlicer. The bed plate was changed to {{fallback}}.',
            });
  return (
    <div>
      <label htmlFor={selectId} className="text-xs text-bambu-gray mb-1 block">
        {t('slice.bedType.label', 'Bed plate')}
      </label>
      <Select
        id={selectId}
        className="w-full"
        value={value}
        onChange={(e) => onChange(e.target.value as BedType)}
        disabled={disabled}
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {t(opt.labelKey, opt.fallback)}
          </option>
        ))}
      </Select>
      {noteText && (
        <div
          className="flex items-start gap-2 text-xs rounded-md border p-2 mt-2 border-amber-300 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200"
          role="status"
          aria-label={t('slice.bedType.label', 'Bed plate')}
        >
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span>{noteText}</span>
        </div>
      )}
    </div>
  );
}
