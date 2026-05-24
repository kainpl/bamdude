import { Package } from 'lucide-react';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import type {
  PresetRef,
  SlicerBundle,
  UnifiedPreset,
  UnifiedPresetsBySlot,
  UnifiedPresetsResponse,
} from '../../api/client';
import {
  type OwnerFilter,
  type Slot,
  fromRefValue,
  matchesOwnerFilter,
  toRefValue,
} from './presetPickerUtils';
import {
  EMPTY_COMPATIBILITY_INDEX,
  presetCompatibility,
  type PrinterCompatibilityIndex,
} from '../../utils/slicerPrinterMatch';

interface PresetDropdownProps {
  label: string;
  slot: Slot;
  data: UnifiedPresetsResponse;
  value: PresetRef | null;
  onChange: (ref: PresetRef | null) => void;
  disabled?: boolean;
  // Optional colour swatch (multi-color plate filament slots).
  swatchColor?: string;
  // 3-state owner filter applied per-section: 'all' shows everything,
  // 'custom' keeps user-imported + user-cloud, 'builtin' keeps standard
  // + bundled cloud presets. Empty tiers collapse out.
  ownerFilter?: OwnerFilter;
  // Selected printer context (#1325). When provided for a process / filament
  // slot, presets that resolve to a *different* printer (per uploaded Slicer
  // Bundles + the @BBL name registry in compatIndex) move into a trailing
  // "Other printers" group instead of the main tier list. Compatibility-
  // unknown presets stay in their tier, so a custom / untagged preset is
  // never hidden. Omitted (or printer slot) ⇒ no compatibility partition.
  selectedPrinterName?: string | null;
  compatIndex?: PrinterCompatibilityIndex;
}

/** Cloud/local/standard preset dropdown with optgroup tiers. */
export function PresetDropdown({
  label,
  slot,
  data,
  value,
  onChange,
  disabled,
  swatchColor,
  ownerFilter = 'all',
  selectedPrinterName,
  compatIndex,
}: PresetDropdownProps) {
  const { t } = useTranslation();

  // Tier sections (imported → cloud → standard) after the owner filter, plus
  // — for a process / filament slot with a selected printer — a trailing
  // group of presets that resolve to a different printer (#1325). Empty
  // sections collapse out.
  const { sections, otherEntries } = useMemo(() => {
    const tiers: { key: keyof UnifiedPresetsResponse; label: string; fallback: string }[] = [
      { key: 'local', label: 'slice.tier.local', fallback: 'Imported' },
      { key: 'cloud', label: 'slice.tier.cloud', fallback: 'Cloud' },
      { key: 'standard', label: 'slice.tier.standard', fallback: 'Standard' },
    ];
    const filterByPrinter = slot !== 'printer';
    const compatSections: { tierLabel: string; entries: UnifiedPreset[] }[] = [];
    const other: UnifiedPreset[] = [];
    for (const { key, label: lk, fallback } of tiers) {
      const entries = (data[key] as UnifiedPresetsBySlot)[slot].filter((p) =>
        matchesOwnerFilter(p, ownerFilter),
      );
      if (!filterByPrinter) {
        if (entries.length > 0) compatSections.push({ tierLabel: t(lk, fallback), entries });
        continue;
      }
      const compatible: UnifiedPreset[] = [];
      for (const p of entries) {
        if (
          presetCompatibility(
            p,
            // filterByPrinter is true here, so slot is never 'printer'.
            slot as 'process' | 'filament',
            selectedPrinterName ?? null,
            compatIndex ?? EMPTY_COMPATIBILITY_INDEX,
          ) === 'mismatch'
        ) {
          other.push(p);
        } else {
          compatible.push(p);
        }
      }
      if (compatible.length > 0) {
        compatSections.push({ tierLabel: t(lk, fallback), entries: compatible });
      }
    }
    return { sections: compatSections, otherEntries: other };
  }, [data, slot, t, ownerFilter, selectedPrinterName, compatIndex]);

  const totalEntries =
    sections.reduce((sum, s) => sum + s.entries.length, 0) + otherEntries.length;

  return (
    <label className="block">
      <span className="flex items-center gap-2 text-xs text-bambu-gray mb-1">
        {swatchColor && (
          <span
            className="inline-block w-3 h-3 rounded-full border border-bambu-dark-tertiary"
            style={{ backgroundColor: swatchColor || 'transparent' }}
            aria-hidden
          />
        )}
        <span>{label}</span>
      </span>
      <select
        value={toRefValue(value)}
        onChange={(e) => onChange(fromRefValue(e.target.value))}
        disabled={disabled || totalEntries === 0}
        className="w-full px-3 py-2 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm focus:outline-none focus:border-bambu-gray disabled:opacity-50"
      >
        <option value="">
          {totalEntries === 0
            ? t('slice.noPresetsForSlot', 'No presets available')
            : t('slice.selectPreset', '— Select a preset —')}
        </option>
        {sections.map((section) => (
          <optgroup key={section.tierLabel} label={section.tierLabel}>
            {section.entries.map((p) => (
              <option key={`${p.source}:${p.id}`} value={`${p.source}:${p.id}`}>
                {p.name}
              </option>
            ))}
          </optgroup>
        ))}
        {otherEntries.length > 0 && (
          <optgroup label={t('slice.otherPrinters', 'Other printers')}>
            {otherEntries.map((p) => (
              <option key={`${p.source}:${p.id}`} value={`${p.source}:${p.id}`}>
                {p.name}
              </option>
            ))}
          </optgroup>
        )}
      </select>
    </label>
  );
}

interface BundleStringDropdownProps {
  label: string;
  options: string[];
  value: string | null;
  onChange: (next: string | null) => void;
  disabled?: boolean;
  swatchColor?: string;
}

/** Plain-string dropdown for bundle-mode process / filament selectors. */
export function BundleStringDropdown({
  label,
  options,
  value,
  onChange,
  disabled,
  swatchColor,
}: BundleStringDropdownProps) {
  const { t } = useTranslation();
  return (
    <label className="block">
      <span className="block text-sm text-bambu-gray mb-1 inline-flex items-center gap-1.5">
        {swatchColor && (
          <span
            className="inline-block w-3 h-3 rounded-sm border border-black/20"
            style={{ backgroundColor: swatchColor || 'transparent' }}
            aria-hidden
          />
        )}
        <span>{label}</span>
      </span>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        disabled={disabled || options.length === 0}
        className="w-full px-3 py-2 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm focus:outline-none focus:border-bambu-gray disabled:opacity-50"
      >
        <option value="">
          {options.length === 0
            ? t('slice.noPresetsForSlot', 'No presets available')
            : t('slice.selectPreset', '— Select a preset —')}
        </option>
        {options.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </label>
  );
}

interface PresetSourceControlProps {
  mode: 'manual' | 'bundle';
  onModeChange: (next: 'manual' | 'bundle') => void;
  ownerFilter: OwnerFilter;
  onOwnerFilterChange: (next: OwnerFilter) => void;
  bundles: SlicerBundle[];
  selectedBundleId: string | null;
  onBundleChange: (id: string | null) => void;
  disabled?: boolean;
}

/**
 * Top-level preset-source control — segmented Manual / Bundle picker
 * (only shown when bundles exist) plus mode-aware sub-control:
 * - Manual → 3-state owner segmented (All / My Presets / Built-in)
 * - Bundle → bundle dropdown
 */
export function PresetSourceControl({
  mode,
  onModeChange,
  ownerFilter,
  onOwnerFilterChange,
  bundles,
  selectedBundleId,
  onBundleChange,
  disabled,
}: PresetSourceControlProps) {
  const { t } = useTranslation();
  const hasBundles = bundles.length > 0;
  const modeOptions: { key: 'manual' | 'bundle'; label: string; hint: string }[] = [
    {
      key: 'manual',
      label: t('slice.presetSourceManual', 'Manual'),
      hint: t(
        'slice.presetSourceManualHint',
        'Pick printer / process / filament from cloud, local imports, or built-in presets.',
      ),
    },
    {
      key: 'bundle',
      label: t('slice.presetSourceBundle', 'Bundle'),
      hint: t(
        'slice.presetSourceBundleHint',
        'Slice using a previously imported BambuStudio Printer Preset Bundle (.bbscfg).',
      ),
    },
  ];
  const ownerOptions: { key: OwnerFilter; label: string }[] = [
    { key: 'all', label: t('profiles.cloudView.filters.all', 'All') },
    { key: 'custom', label: t('profiles.cloudView.filters.myPresets', 'My Presets') },
    { key: 'builtin', label: t('profiles.cloudView.filters.builtIn', 'Built-in') },
  ];
  const segmentedClass = (selected: boolean) =>
    selected ? 'bg-bambu-green/20 text-white' : 'bg-bambu-dark text-bambu-gray hover:text-white';
  return (
    <fieldset className="space-y-2">
      <legend className="text-xs text-bambu-gray mb-1">
        {t('slice.presetSource', 'Preset source')}
      </legend>
      {hasBundles && (
        <div className="inline-flex rounded-md border border-bambu-dark-tertiary overflow-hidden">
          {modeOptions.map((opt) => {
            const selected = mode === opt.key;
            return (
              <button
                key={opt.key}
                type="button"
                onClick={() => onModeChange(opt.key)}
                disabled={disabled}
                title={opt.hint}
                className={`px-3 py-1.5 text-xs transition-colors disabled:opacity-50 ${segmentedClass(selected)}`}
                aria-pressed={selected}
              >
                {opt.key === 'bundle' ? (
                  <span className="inline-flex items-center gap-1.5">
                    <Package className="w-3.5 h-3.5" />
                    {opt.label}
                  </span>
                ) : (
                  opt.label
                )}
              </button>
            );
          })}
        </div>
      )}
      {mode === 'manual' || !hasBundles ? (
        <div className="inline-flex rounded-md border border-bambu-dark-tertiary overflow-hidden">
          {ownerOptions.map((opt) => {
            const selected = ownerFilter === opt.key;
            return (
              <button
                key={opt.key}
                type="button"
                onClick={() => onOwnerFilterChange(opt.key)}
                disabled={disabled}
                className={`px-3 py-1.5 text-xs transition-colors disabled:opacity-50 ${segmentedClass(selected)}`}
                aria-pressed={selected}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      ) : (
        <select
          value={selectedBundleId ?? ''}
          onChange={(e) => onBundleChange(e.target.value || null)}
          disabled={disabled || !hasBundles}
          className="w-full px-3 py-2 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm focus:outline-none focus:border-bambu-gray disabled:opacity-50"
        >
          {bundles.map((b) => (
            <option key={b.id} value={b.id}>
              {b.printer_preset_name}
            </option>
          ))}
        </select>
      )}
    </fieldset>
  );
}
