import { Package } from 'lucide-react';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import type {
  PresetRef,
  PresetSource,
  SlicerBundle,
  UnifiedPreset,
  UnifiedPresetsBySlot,
  UnifiedPresetsResponse,
} from '../../api/client';

// Slot is one of the three preset categories the slicer takes: machine,
// process, filament. Shared with SliceModal so both sites use the same
// vocabulary.
export type Slot = 'printer' | 'process' | 'filament';

// Manual mode tier display order — local imports first (operator's
// curated picks), then cloud (per-user), then standard bundled
// fallbacks. Identical to the SliceModal ordering.
export const TIER_ORDER = ['local', 'cloud', 'standard'] as const;

export type OwnerFilter = 'all' | 'custom' | 'builtin';

// Cloud setting_id prefixes Bambu uses for user-created presets — kept
// in sync with ``pages/ProfilesPage.tsx::isUserPreset`` and SliceModal.
// Local presets are user-imported by definition; standard presets are
// always built-in. Cloud splits by id prefix.
const _USER_CLOUD_PRESET_RE = /^(P[FPM]US|PF\d|PP\d)/;

export function isCustomPreset(p: UnifiedPreset): boolean {
  if (p.source === 'local') return true;
  if (p.source === 'standard') return false;
  return _USER_CLOUD_PRESET_RE.test(p.id);
}

export function matchesOwnerFilter(p: UnifiedPreset, filter: OwnerFilter): boolean {
  if (filter === 'all') return true;
  const custom = isCustomPreset(p);
  return filter === 'custom' ? custom : !custom;
}

export function toRefValue(ref: PresetRef | null): string {
  return ref ? `${ref.source}:${ref.id}` : '';
}

export function fromRefValue(raw: string): PresetRef | null {
  if (!raw) return null;
  const idx = raw.indexOf(':');
  if (idx < 0) return null;
  const source = raw.slice(0, idx) as PresetSource;
  const id = raw.slice(idx + 1);
  if (source !== 'cloud' && source !== 'local' && source !== 'standard') return null;
  return { source, id };
}

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
}: PresetDropdownProps) {
  const { t } = useTranslation();

  const sections: { tierLabel: string; entries: UnifiedPreset[] }[] = useMemo(() => {
    const tiers: { key: keyof UnifiedPresetsResponse; label: string; fallback: string }[] = [
      { key: 'local', label: 'slice.tier.local', fallback: 'Imported' },
      { key: 'cloud', label: 'slice.tier.cloud', fallback: 'Cloud' },
      { key: 'standard', label: 'slice.tier.standard', fallback: 'Standard' },
    ];
    return tiers
      .map(({ key, label: lk, fallback }) => ({
        tierLabel: t(lk, fallback),
        entries: (data[key] as UnifiedPresetsBySlot)[slot].filter((p) =>
          matchesOwnerFilter(p, ownerFilter),
        ),
      }))
      .filter((s) => s.entries.length > 0);
  }, [data, slot, t, ownerFilter]);

  const totalEntries = sections.reduce((sum, s) => sum + s.entries.length, 0);

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
