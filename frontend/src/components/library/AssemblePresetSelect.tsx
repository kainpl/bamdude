import { useId, useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import type { PresetRef, UnifiedPreset, UnifiedPresetsBySlot, UnifiedPresetsResponse } from '../../api/client';
import { fromRefValue, toRefValue, type Slot } from '../../utils/presetPickerUtils';
import {
  EMPTY_COMPATIBILITY_INDEX,
  presetCompatibility,
  type PrinterCompatibilityIndex,
} from '../../utils/slicerPrinterMatch';
import { Select } from '../Select';

interface AssemblePresetSelectProps {
  label: string;
  slot: Slot;
  data: UnifiedPresetsResponse;
  value: PresetRef | null;
  onChange: (ref: PresetRef | null) => void;
  /** For a process / filament slot: presets made for another printer move to a trailing group. */
  selectedPrinterName?: string | null;
  compatIndex?: PrinterCompatibilityIndex;
}

const TIERS: { key: keyof UnifiedPresetsResponse; label: string }[] = [
  { key: 'local', label: 'slice.tier.local' },
  { key: 'orca_cloud', label: 'slice.tier.orcaCloud' },
  { key: 'cloud', label: 'slice.tier.cloud' },
  { key: 'standard', label: 'slice.tier.standard' },
];

/**
 * The preset select of "Assemble project": the Slice dialog's tiers and its "other printers" rule, without
 * that dialog's colour control. This dialog's own on purpose — the Slice dialog keeps its pickers to itself
 * (SliceModalPickers.test.tsx), and the two share the matching functions, not the widget.
 */
export function AssemblePresetSelect({
  label,
  slot,
  data,
  value,
  onChange,
  selectedPrinterName,
  compatIndex,
}: AssemblePresetSelectProps) {
  const { t } = useTranslation();
  const selectId = useId();

  const { sections, others } = useMemo(() => {
    const all: { label: string; entries: UnifiedPreset[] }[] = [];
    const fitting: { label: string; entries: UnifiedPreset[] }[] = [];
    const other: UnifiedPreset[] = [];
    for (const tier of TIERS) {
      const entries = (data[tier.key] as UnifiedPresetsBySlot)[slot];
      if (entries.length === 0) continue;
      all.push({ label: t(tier.label), entries });
      if (slot === 'printer') continue;
      const keep = entries.filter((preset) => {
        const verdict = presetCompatibility(
          preset,
          slot,
          selectedPrinterName ?? null,
          compatIndex ?? EMPTY_COMPATIBILITY_INDEX,
        );
        if (verdict === 'mismatch') other.push(preset);
        return verdict !== 'mismatch';
      });
      if (keep.length > 0) fitting.push({ label: t(tier.label), entries: keep });
    }
    // a filter that leaves nothing says something about the matching, not about the presets: show them all
    if (slot === 'printer' || fitting.length === 0) return { sections: all, others: [] };
    return { sections: fitting, others: other };
  }, [data, slot, t, selectedPrinterName, compatIndex]);

  const total = sections.reduce((sum, section) => sum + section.entries.length, 0) + others.length;
  const option = (preset: UnifiedPreset) => (
    <option key={`${preset.source}:${preset.id}`} value={`${preset.source}:${preset.id}`}>
      {preset.name}
    </option>
  );

  return (
    <div className="block">
      <label htmlFor={selectId} className="block text-xs text-bambu-gray mb-1">
        {label}
      </label>
      <Select
        id={selectId}
        className="w-full"
        value={toRefValue(value)}
        onChange={(event) => onChange(fromRefValue(event.target.value))}
        disabled={total === 0}
      >
        <option value="">{total === 0 ? t('slice.noPresetsForSlot') : t('slice.selectPreset')}</option>
        {sections.map((section) => (
          <optgroup key={section.label} label={section.label}>
            {section.entries.map(option)}
          </optgroup>
        ))}
        {others.length > 0 && <optgroup label={t('slice.otherPrinters')}>{others.map(option)}</optgroup>}
      </Select>
    </div>
  );
}
