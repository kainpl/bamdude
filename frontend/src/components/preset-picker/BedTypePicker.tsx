import { useTranslation } from 'react-i18next';

import type { BedType } from '../../api/client';

interface BedTypePickerProps {
  value: BedType;
  onChange: (next: BedType) => void;
  disabled?: boolean;
}

/**
 * Bed plate picker — single-select dropdown over the five values
 * BambuStudio's ``curr_bed_type`` enum accepts
 * (``libslic3r/PrintConfig.cpp:1069+``). Forwarded as ``bed_type`` on
 * the slice / calibration request body, becomes ``--curr-bed-type`` on
 * the slicer CLI. Compact dropdown rather than a 5-tile grid because
 * four of the five values are uncommon; a wide tile row would dominate
 * a modal for a setting most users flip once.
 */
export function BedTypePicker({ value, onChange, disabled }: BedTypePickerProps) {
  const { t } = useTranslation();
  const options: { value: BedType; labelKey: string; fallback: string }[] = [
    { value: 'Cool Plate', labelKey: 'slice.bedType.coolPlate', fallback: 'Cool Plate' },
    { value: 'Engineering Plate', labelKey: 'slice.bedType.engineeringPlate', fallback: 'Engineering Plate' },
    { value: 'High Temp Plate', labelKey: 'slice.bedType.highTempPlate', fallback: 'Smooth PEI / High Temp Plate' },
    { value: 'Textured PEI Plate', labelKey: 'slice.bedType.texturedPeiPlate', fallback: 'Textured PEI Plate' },
    { value: 'Supertack Plate', labelKey: 'slice.bedType.supertackPlate', fallback: 'Cool Plate SuperTack' },
  ];
  return (
    <label className="block">
      <span className="text-xs text-bambu-gray mb-1 block">
        {t('slice.bedType.label', 'Bed plate')}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as BedType)}
        disabled={disabled}
        className="w-full px-3 py-2 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm focus:outline-none focus:border-bambu-gray disabled:opacity-50"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {t(opt.labelKey, opt.fallback)}
          </option>
        ))}
      </select>
    </label>
  );
}
