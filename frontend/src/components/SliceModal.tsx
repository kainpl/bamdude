import { AlertTriangle, Check, Cloud, CloudOff, Cog, Loader2, RefreshCw, XCircle } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Modal } from './Modal';
import SlicerSettingsPanel from './SlicerSettingsPanel';
import type { SettingValue } from '../types/slicerSettings';
import {
  api,
  type BedType,
  type PresetRef,
  type SliceJobProgress,
  type SliceRequest,
  type SlicerCloudStatus,
  type SlicerPipeline,
  type UnifiedPreset,
  type UnifiedPresetsBySlot,
  type UnifiedPresetsResponse,
} from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useSliceJobTracker } from '../contexts/SliceJobTrackerContext';
import { useToast } from '../contexts/ToastContext';
import { SlicePlateSelector } from './SlicePlateSelector';
import type { DesignOverride, PlateFilament } from '../types/plates';
import {
  fromRefValue,
  resolvePresetName,
  toRefValue,
  type Slot,
} from '../utils/presetPickerUtils';
import { useSlicerHealth, type SlicerKind } from '../hooks/useSlicerHealth';
import { SlicerOutdatedNote } from './SlicerOutdatedNote';
import { useIsWideLayout } from '../hooks/useIsWideLayout';
import { defaultDesignKeys, designOverridesField } from '../lib/slicerSettings';
import {
  colourInputValue,
  filamentColoursPayload,
  resizeColourOverrides,
} from '../utils/sliceFilamentColours';
import { Select } from './Select';
import { SliceFarmFilterToggles } from './SliceFarmFilters';
import { useSliceFarmFilters, type PresetKeep } from '../hooks/useSliceFarmFilters';
import { buildPrinterParents } from '../utils/printerPresetParents';
import {
  EMPTY_COMPATIBILITY_INDEX,
  buildCompatibilityIndex,
  presetCompatibility,
  type PrinterCompatibilityIndex,
} from '../utils/slicerPrinterMatch';
import {
  SLICE_MODAL_TIER_ORDER,
  findPreset,
  findPresetByName,
  pickDefault,
  pickEmbeddedFilament,
  pickFilamentForSlot,
  pickProcessDefault,
  statesDifferentMaterial,
} from '../utils/slicePresetPicker';
import {
  groupPresetsForDisplay,
  presetGroupOf,
  sortPresetsForDisplay,
  type PresetGroup,
} from '../utils/presetDisplayOrder';

// What each group of a preset dropdown is called. The group IS the source, so
// two presets of one name in two sources can be told apart by it.
const PRESET_GROUP_LABELS: Record<PresetGroup, { key: string; fallback: string }> = {
  local: { key: 'slice.tier.local', fallback: 'Imported' },
  orca_cloud: { key: 'slice.tier.orcaCloud', fallback: 'Orca Cloud' },
  cloud_own: { key: 'slice.tier.cloudOwn', fallback: 'Bambu Cloud — my presets' },
  cloud: { key: 'slice.tier.cloud', fallback: 'Bambu Cloud' },
  standard: { key: 'slice.tier.standard', fallback: 'Standard' },
};

export type SliceSource =
  // `assembled`: the file was written by "Assemble project" (the list row's
  // `can_relayout`), so its assembly record can say which presets — source and
  // id, not just a name — it was built from.
  | { kind: 'libraryFile'; id: number; filename: string; assembled?: boolean }
  | { kind: 'archive'; id: number; filename: string };

interface SliceModalProps {
  source: SliceSource;
  onClose: () => void;
}

// The values the slice request's ``bed_type`` accepts (the backend's
// ``schemas/slicer.BedType``, itself the slicers' ``curr_bed_type`` enum) —
// same list ``BedTypePicker`` renders. Kept here as the validation whitelist
// for the localStorage read and for the bed type carried by a saved bundle, so
// a stale/foreign string can never reach the slicer CLI.
const ALLOWED_BED_TYPES: BedType[] = [
  'Cool Plate',
  'Engineering Plate',
  'High Temp Plate',
  'Textured PEI Plate',
  'Supertack Plate',
  'Textured Cool Plate',
];

// In OrcaSlicer's enum and not in Bambu Studio's, whose CLI does not reject a
// name it does not know but slices as for Cool Plate. Offered only while
// OrcaSlicer is the slicer; the backend refuses it for Bambu Studio (422).
const ORCA_ONLY_BED_TYPES: BedType[] = ['Textured Cool Plate'];

// The dialog's default plate, and what an OrcaSlicer-only pick falls back to.
const DEFAULT_BED_TYPE: BedType = 'Textured PEI Plate';

// A bed plate the dialog did not apply, to be SAID rather than dropped:
// `unknown` / `slicer` — saved settings named a plate nobody knows, or one the
// chosen slicer lacks (the pick was left alone); `reset` — the pick itself is
// one the slicer lacks, and was changed to the default.
type BedTypeNote = { kind: 'unknown' | 'slicer' | 'reset'; bed: string };

// Per-slot preset selection helpers (pickDefault / findPreset / findPresetByName
// / pickProcessDefault / pickFilamentForSlot) live in
// ``../utils/slicePresetPicker`` so this file only exports React components
// (react-refresh/only-export-components) and the picker logic — including the
// #1851 hard-skip-on-printer-mismatch contract — is unit-testable in isolation.

// Inline spinner for the filament-requirements query. The backend runs a
// preview slice on first open of an unsliced project file (cached after);
// on a complex multi-color model that's a real slice — multi-second to
// multi-minute. The spinner shows elapsed seconds, polls the sidecar's
// --pipe progress (via /slicer/preview-progress proxy) for live stage +
// percent, and after ~5s surfaces a "this is a one-time slice — repeat
// opens are instant" note so users don't worry it'll be slow forever.
//
// requestId: a UUID generated by the modal when the filament-requirements
// fetch starts. Forwarded to the sidecar via the API call AND used here
// to poll the matching progress snapshot. Same id, two consumers.
function FilamentAnalysisSpinner({
  requestId,
  sourceName,
}: {
  requestId: string;
  sourceName: string;
}) {
  const { t } = useTranslation();
  const { showPersistentToast, dismissToast } = useToast();
  const [elapsed, setElapsed] = useState(0);
  const [progress, setProgress] = useState<SliceJobProgress | null>(null);
  // Defensive decode — see prettifyFilename comment in SliceJobTrackerContext.
  let prettyName = sourceName;
  try {
    prettyName = decodeURIComponent(sourceName);
  } catch {
    /* keep raw on malformed encoding */
  }

  // Elapsed-time tick.
  useEffect(() => {
    const startedAt = Date.now();
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, []);

  // Progress polling — once per second while the spinner is mounted.
  // Mirrors the slice-job tracker's cadence. Sidecar 404s during the
  // race window between fetch start and progressStore.start() are
  // swallowed by the API method (returns null) so we keep polling.
  useEffect(() => {
    let cancelled = false;
    const id = setInterval(async () => {
      if (cancelled) return;
      const snap = await api.getPreviewSliceProgress(requestId);
      if (!cancelled && snap) setProgress(snap);
    }, 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [requestId]);

  // Mirror the spinner's contents into a persistent toast so the user
  // sees activity even when their cursor is elsewhere on the page.
  // Dismissed in the parent's effect when the requirements arrive.
  const toastId = `slice-preview-${requestId}`;
  useEffect(() => {
    const hasUseful = progress && progress.stage && progress.total_percent > 0;
    const elapsedStr = formatElapsed(elapsed);
    if (hasUseful) {
      showPersistentToast(
        toastId,
        t(
          'slice.previewWithProgress',
          'Analyzing {{name}} — {{stage}} ({{percent}}%) — {{elapsed}}',
          {
            name: prettyName,
            stage: progress!.stage,
            percent: Math.min(100, Math.max(0, Math.round(progress!.total_percent))),
            elapsed: elapsedStr,
          },
        ),
        'loading',
      );
    } else {
      showPersistentToast(
        toastId,
        t('slice.previewToast', 'Analyzing {{name}} — {{elapsed}}', {
          name: prettyName,
          elapsed: elapsedStr,
        }),
        'loading',
      );
    }
    return () => {
      dismissToast(toastId);
    };
  }, [elapsed, progress, prettyName, showPersistentToast, dismissToast, t, toastId]);

  const stage = progress?.stage;
  const percent = progress?.total_percent;
  const inlineLabel =
    stage && typeof percent === 'number' && percent > 0
      ? `${stage} (${Math.min(100, Math.max(0, Math.round(percent)))}%)`
      : t('slice.analyzingPlateFilaments', 'Analyzing plate filaments…');
  return (
    <div className="flex flex-col gap-1 text-bambu-gray text-sm py-2">
      <div className="flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        {inlineLabel}
        <span className="text-xs tabular-nums">{elapsed}s</span>
      </div>
      {elapsed >= 5 && (
        <div className="text-xs text-bambu-gray/70 pl-6">
          {t(
            'slice.analyzingPlateFilamentsHint',
            'Running a preview slice to discover which AMS slots this plate uses. Cached after — re-opening is instant.',
          )}
        </div>
      )}
    </div>
  );
}

// Render a slicer parameter value for the design-settings list. Bambu's process
// schema stores everything as strings or arrays of strings, so this only has to
// flatten arrays and keep scalars readable — no unit or type interpretation,
// which would rot against every slicer release.
function formatDesignValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(', ');
  if (value == null) return '';
  return String(value);
}

function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) return `${m}m ${remS}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return `${h}h ${remM}m`;
}

// ---------------------------------------------------------------------------
// The dialog's own pickers.
//
// ⚠️ Inline, and not exported, on purpose. These lived in a shared
// `components/preset-picker/` folder that the slice dialog and two calibration
// pages all drew from, which made this dialog a HOST for widgets rather than a
// composition — every ported feature bolted on at the side instead of joining
// the layout. Calibration keeps its own copy under
// `calibration/preset-picker/`; these belong here and change with the dialog.
//
// ⚠️ `PresetSourceControl` (All / My / Built-in) is deliberately absent. It was
// a leftover from the sidecar-bundle era — the other half of that control was
// deleted with the bundles (#1712) and this half stayed hanging — and source is
// chosen by the tier grouping below. It still exists for calibration, which is
// a separate question about a separate flow.
// ---------------------------------------------------------------------------

interface PresetDropdownProps {
  label: string;
  slot: Slot;
  data: UnifiedPresetsResponse;
  value: PresetRef | null;
  onChange: (ref: PresetRef | null) => void;
  disabled?: boolean;
  // Optional colour swatch (multi-color plate filament slots).
  swatchColor?: string;
  // When set, the swatch becomes an editable colour control for this slot
  // (upstream 4f10d155, #2977). Filament presets carry no colour of their own,
  // so for an STL — or any source whose plate was never given one — this is
  // the only place the colour recorded in the sliced file can come from.
  onSwatchColorChange?: (colour: string) => void;
  // Accessible name for that control. Required alongside the handler because
  // the visible label belongs to the preset select, not to the swatch.
  swatchColorLabel?: string;
  // 3-state owner filter applied per-section: 'all' shows everything,
  // 'custom' keeps user-imported + user-cloud, 'builtin' keeps standard
  // + bundled cloud presets. Empty tiers collapse out.
  // Selected printer context (#1325). When provided for a process / filament
  // slot, presets that resolve to a *different* printer (per the @BBL name
  // registry in compatIndex, plus the slicer's own compatible_printers list)
  // move into a trailing "Other printers" group instead of the main tier
  // list. Compatibility-unknown presets stay in their tier, so a custom /
  // untagged preset is never hidden. Omitted (or printer slot) ⇒ no
  // compatibility partition.
  selectedPrinterName?: string | null;
  compatIndex?: PrinterCompatibilityIndex;
  // A farm filter ("only online printers" / "only loaded filaments"): the
  // presets the list offers. The chosen preset stays listed whatever it says —
  // a filter never replaces a choice. Omitted ⇒ everything is offered.
  keep?: PresetKeep;
}

/** Cloud/local/standard preset dropdown with optgroup tiers. */
function PresetDropdown({
  label,
  slot,
  data,
  value,
  onChange,
  disabled,
  swatchColor,
  onSwatchColorChange,
  swatchColorLabel,
  selectedPrinterName,
  compatIndex,
  keep,
}: PresetDropdownProps) {
  const { t } = useTranslation();
  // A `<div>` with explicit `htmlFor` labels, not one wrapping `<label>`: the
  // colour control is a label of its own, and labels do not nest.
  const selectId = useId();
  const colourId = `${selectId}-colour`;
  const noteId = `${selectId}-note`;

  // Tier sections (imported → cloud → standard) after the owner filter, plus
  // — for a process / filament slot with a selected printer — a trailing
  // group of presets that resolve to a different printer (#1325). Empty
  // sections collapse out.
  //
  // A group is a SOURCE, the user's own first, and each is sorted for reading
  // (`presetDisplayOrder`, upstream a9ffe5b7c). ⚠️ Display only: the auto-picks
  // read `data` in the order it arrived, so sorting never changes what is
  // preselected. ⚠️ A stock preset that Bambu Cloud and the bundle both carry
  // stays two entries — a name is not an identity — told apart by their group.
  const { sections, otherEntries } = useMemo(() => {
    const groupLabel = (group: PresetGroup) => t(PRESET_GROUP_LABELS[group].key, PRESET_GROUP_LABELS[group].fallback);
    const toSections = (entries: UnifiedPreset[]) =>
      groupPresetsForDisplay(entries, slot).map((g) => ({ tierLabel: groupLabel(g.group), entries: g.entries }));
    const listed = SLICE_MODAL_TIER_ORDER.flatMap((tier) => (data[tier] as UnifiedPresetsBySlot)[slot]);
    const isChosen = (p: UnifiedPreset) => value !== null && p.source === value.source && p.id === value.id;
    const all = keep ? listed.filter((p) => keep(p) || isChosen(p)) : listed;
    // The same sections with the printer filter never applied, for the
    // all-filtered-out case below.
    const unfiltered = toSections(all);
    if (slot === 'printer') return { sections: unfiltered, otherEntries: [] };
    const compatible: UnifiedPreset[] = [];
    const other: UnifiedPreset[] = [];
    for (const p of all) {
      const verdict = presetCompatibility(p, slot, selectedPrinterName ?? null, compatIndex ?? EMPTY_COMPATIBILITY_INDEX);
      (verdict === 'mismatch' ? other : compatible).push(p);
    }
    const compatSections = toSections(compatible);
    // A filter that leaves nothing at all is a statement about our matching,
    // not about the presets (upstream e9daa212, #2982): a P1S has ten usable
    // process presets and read as having none, because each is named for an
    // X1C and says "P1S" only in a `compatible_printers` list an older sidecar
    // does not report. So an emptied list is shown unfiltered — a visible
    // preset for the wrong printer can be changed, an empty dropdown cannot.
    if (compatSections.length === 0 && other.length > 0) {
      return { sections: unfiltered, otherEntries: [] };
    }
    // The tail mixes sources under one heading, so each entry names its own.
    return {
      sections: compatSections,
      otherEntries: sortPresetsForDisplay(other, slot).map((preset) => ({
        preset,
        sourceLabel: groupLabel(presetGroupOf(preset)),
      })),
    };
  }, [data, slot, t, selectedPrinterName, compatIndex, keep, value]);

  const totalEntries =
    sections.reduce((sum, s) => sum + s.entries.length, 0) + otherEntries.length;
  // The chosen preset sits in "Other printers": it stays the choice — nothing
  // here replaces one — and the field says so, because the closed select shows
  // a name only.
  const chosenForOtherPrinter =
    value !== null && otherEntries.some(({ preset: p }) => p.source === value.source && p.id === value.id);

  return (
    <div className="block">
      <label htmlFor={selectId} className="flex items-center gap-2 text-xs text-bambu-gray mb-1">
        {/* Read-only dot for a row with no editable colour; filament rows carry
            a real control beside the dropdown instead. */}
        {!onSwatchColorChange && swatchColor && (
          <span
            className="inline-block w-3 h-3 rounded-full border border-bambu-dark-tertiary"
            style={{ backgroundColor: swatchColor }}
            aria-hidden
          />
        )}
        <span>{label}</span>
      </label>
      <div className="flex items-stretch gap-2">
      <Select
        id={selectId}
        className="flex-1 min-w-0"
        value={toRefValue(value)}
        onChange={(e) => onChange(fromRefValue(e.target.value))}
        disabled={disabled || totalEntries === 0}
        aria-describedby={chosenForOtherPrinter ? noteId : undefined}
        // Thousands of stock filaments are no list to scroll: the words typed
        // narrow these same options, other printers included; the chosen
        // preset always stays listed.
        search={{
          label: t('slice.searchPresets', { list: label, defaultValue: 'Search: {{list}}' }),
          placeholder: t('slice.searchPresetsPlaceholder', 'Search presets…'),
          noMatches: t('slice.noMatchingPresets', 'No matching presets.'),
        }}
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
            {otherEntries.map(({ preset: p, sourceLabel }) => (
              <option key={`${p.source}:${p.id}`} value={`${p.source}:${p.id}`}>
                {`${p.name} · ${sourceLabel}`}
              </option>
            ))}
          </optgroup>
        )}
      </Select>
      {/* The colour control sits beside the dropdown, styled like it and the
          same height, because that is what makes it read as a control: a bare
          swatch in the label row looks exactly like the read-only dot above.
          The whole label opens the picker, so the hex is a hit target and not
          a caption. */}
      {onSwatchColorChange && (
        <label
          htmlFor={colourId}
          title={swatchColorLabel}
          // Pinned to the select's own row and height: with the search box
          // open the group beside it is two rows tall.
          className={`self-end h-9 min-h-[44px] md:min-h-0 flex items-center gap-2 px-2.5 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-sm ${
            disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer hover:border-bambu-gray transition-colors'
          }`}
        >
          <input
            id={colourId}
            type="color"
            value={colourInputValue(swatchColor)}
            onChange={(e) => onSwatchColorChange(e.target.value.toUpperCase())}
            disabled={disabled}
            aria-label={swatchColorLabel}
            // Painted explicitly as well as through the native swatch, so an
            // engine that did not fill the swatch still shows the colour.
            style={{ backgroundColor: colourInputValue(swatchColor) }}
            className="slice-colour-swatch w-4 h-4 shrink-0 rounded-full border border-bambu-dark-tertiary p-0 disabled:cursor-not-allowed enabled:cursor-pointer"
          />
          <span className="font-mono text-xs text-white tracking-tight">{colourInputValue(swatchColor)}</span>
        </label>
      )}
      </div>
      {chosenForOtherPrinter && (
        <p id={noteId} role="status" className="mt-1 text-xs text-amber-700 dark:text-amber-400">
          {t('slice.presetForOtherPrinter', 'This profile is listed for another printer than the selected one.')}
        </p>
      )}
    </div>
  );
}

interface BedTypePickerProps {
  value: BedType;
  onChange: (next: BedType) => void;
  disabled?: boolean;
  // The slicer this slice will run on, once known. Decides whether the
  // OrcaSlicer-only plates are offered.
  slicer: SlicerKind | null;
  note: BedTypeNote | null;
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
function BedTypePicker({ value, onChange, disabled, slicer, note }: BedTypePickerProps) {
  const { t } = useTranslation();
  const all: { value: BedType; labelKey: string; fallback: string }[] = [
    { value: 'Cool Plate', labelKey: 'slice.bedType.coolPlate', fallback: 'Cool Plate' },
    { value: 'Engineering Plate', labelKey: 'slice.bedType.engineeringPlate', fallback: 'Engineering Plate' },
    { value: 'High Temp Plate', labelKey: 'slice.bedType.highTempPlate', fallback: 'Smooth PEI / High Temp Plate' },
    { value: 'Textured PEI Plate', labelKey: 'slice.bedType.texturedPeiPlate', fallback: 'Textured PEI Plate' },
    { value: 'Supertack Plate', labelKey: 'slice.bedType.supertackPlate', fallback: 'Cool Plate SuperTack' },
    { value: 'Textured Cool Plate', labelKey: 'slice.bedType.texturedCoolPlate', fallback: 'Textured Cool Plate' },
  ];
  // An OrcaSlicer-only plate is listed while OrcaSlicer is the slicer — and
  // while it is the current value, so the field never shows a value it has no
  // option for in the moment before the slicer is known.
  const options = all.filter(
    (opt) => !ORCA_ONLY_BED_TYPES.includes(opt.value) || slicer === 'orcaslicer' || opt.value === value,
  );
  const labelOf = (bed: string) => {
    const known = all.find((opt) => opt.value === bed);
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
      <label className="block">
        <span className="text-xs text-bambu-gray mb-1 block">
          {t('slice.bedType.label', 'Bed plate')}
        </span>
        <Select
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
      </label>
      {/* Outside the label, or its text would become part of the field's
          name. Same amber shape as the "a saved preset is gone" banner. */}
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

interface SlicerPickerProps {
  value: SlicerKind | null;
  onChange: (next: SlicerKind) => void;
  disabled?: boolean;
}

/**
 * Per-job slicer picker, card-style — mirrors the "Filament Tracking"
 * section in Settings. Two big card-buttons, each carrying a live
 * reachability badge (version / offline / checking) so the user sees
 * what's available WITHOUT a separate status strip cluttering the modal.
 *
 * Disabled cards (sidecar offline) cannot be picked. When only one is
 * healthy the calling form still works — pre-select whatever is
 * reachable; if neither, the backend will surface the real error at
 * submit time.
 */
function SlicerPicker({ value, onChange, disabled }: SlicerPickerProps) {
  const { t } = useTranslation();
  const options: { kind: SlicerKind; label: string }[] = [
    { kind: 'orcaslicer', label: 'OrcaSlicer' },
    { kind: 'bambu_studio', label: 'BambuStudio' },
  ];
  return (
    <fieldset>
      <legend className="text-xs text-bambu-gray mb-2">
        {t('slice.sidecarPicker', 'Slice with')}
      </legend>
      <div className="grid grid-cols-2 gap-3">
        {options.map((opt) => (
          <SlicerPickerCard
            key={opt.kind}
            kind={opt.kind}
            label={opt.label}
            selected={value === opt.kind}
            outerDisabled={!!disabled}
            onSelect={() => onChange(opt.kind)}
          />
        ))}
      </div>
      <SlicerOutdatedNote kind={value} />
    </fieldset>
  );
}

interface SlicerPickerCardProps {
  kind: SlicerKind;
  label: string;
  selected: boolean;
  outerDisabled: boolean;
  onSelect: () => void;
}

function SlicerPickerCard({
  kind,
  label,
  selected,
  outerDisabled,
  onSelect,
}: SlicerPickerCardProps) {
  const { t } = useTranslation();
  const { data, isLoading } = useSlicerHealth(kind);
  const healthy = data?.healthy === true;
  const version = data?.version ?? null;
  const error = data?.error ?? null;
  // Disable when the outer form is busy OR the sidecar isn't reachable —
  // clicking an offline card can't lead to a successful slice.
  const isDisabled = outerDisabled || isLoading || !healthy;

  const statusLabel = isLoading
    ? t('slicerHealth.statusChecking', 'checking…')
    : healthy
      ? t('slicerHealth.cardReady', { version: version ?? '?', defaultValue: 'Ready · v{{version}}' })
      : t('slicerHealth.cardOffline', 'Offline');

  const baseClass = selected
    ? 'border-bambu-green bg-bambu-green/10'
    : isDisabled
      ? 'border-bambu-dark-tertiary bg-bambu-dark/50 opacity-60 cursor-not-allowed'
      : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-gray/50';
  const iconClass = selected ? 'text-bambu-green' : healthy ? 'text-bambu-gray' : 'text-red-700 dark:text-red-400';
  const titleClass = selected ? 'text-white' : 'text-bambu-gray';
  const descClass = selected ? 'text-bambu-gray' : 'text-bambu-gray/60';

  return (
    <button
      type="button"
      onClick={() => {
        if (!isDisabled) onSelect();
      }}
      disabled={isDisabled}
      className={`p-3 rounded-lg border-2 text-left transition-colors ${baseClass}`}
    >
      <div className="flex items-center gap-2 mb-1.5">
        {isLoading ? (
          <Loader2 className={`w-4 h-4 animate-spin ${iconClass}`} />
        ) : healthy ? (
          <Cog className={`w-4 h-4 ${iconClass}`} />
        ) : (
          <XCircle className={`w-4 h-4 ${iconClass}`} />
        )}
        <span className={`text-sm font-medium ${titleClass}`}>{label}</span>
      </div>
      <p className={`text-xs ${descClass}`}>{statusLabel}</p>
      {!isLoading && !healthy && error && (
        <p className="text-xs text-red-400/80 mt-1 break-words">{error}</p>
      )}
      {selected && healthy && (
        <div className="flex items-center gap-1 mt-2">
          <Check className="w-3 h-3 text-bambu-green" />
          <span className="text-xs text-bambu-green">{t('slicerHealth.active', 'active')}</span>
        </div>
      )}
    </button>
  );
}

export function SliceModal({ source, onClose }: SliceModalProps) {
  const { t } = useTranslation();
  const headingId = useId();
  const { trackJob } = useSliceJobTracker();

  const [printerPreset, setPrinterPreset] = useState<PresetRef | null>(null);
  const [processPreset, setProcessPreset] = useState<PresetRef | null>(null);
  // Process settings the user edited by hand in the settings panel. TWO shapes
  // are kept: the panel's editing values, and the same set serialised into the
  // string forms a process preset stores. The panel owns the option schema, so
  // it hands back both rather than making this component re-derive the second.
  const [processOverrides, setProcessOverrides] = useState<Record<string, SettingValue>>({});
  const [serializedProcessOverrides, setSerializedProcessOverrides] = useState<Record<string, string | string[]>>({});
  const [settingsExpanded, setSettingsExpanded] = useState(false);
  const isWideLayout = useIsWideLayout();
  // ⚠️ The panel is permanently open once it owns a column — there is nothing
  // to collapse it out of the way of. The disclosure exists only because the
  // single-column stack below `lg` cannot afford 348 options unfolded.
  const panelOpen = isWideLayout || settingsExpanded;
  // One filament ref per plate slot, in plate order. For STL / single-plate /
  // single-color sources this is a one-element array; multi-color 3MFs get one
  // entry per AMS slot the plate uses. Pre-pick (effect below) initialises
  // each slot from the source plate's required (type, colour).
  const [filamentPresets, setFilamentPresets] = useState<(PresetRef | null)[]>([]);
  // Per-slot colour picks, plate-slot-ordered alongside `filamentPresets`
  // (upstream 4f10d155, #2977). `null` means "not picked", not "no colour": the
  // slot then falls through to the source plate's colour and, past that, to the
  // backend's own fallbacks. Storing a colour for every slot up front would
  // defeat that chain — a sent colour outranks the preset's own default.
  const [filamentColours, setFilamentColours] = useState<(string | null)[]>([]);
  // Slots the user chose a filament for by hand, or by applying a pipeline
  // (upstream e9daa212, #2982). The pre-pick re-picks a slot whose preset
  // states the wrong material, and without this it would do so to a deliberate
  // choice too — printing PETG on a plate a designer labelled PLA is a thing
  // people do on purpose. A ref, not state: it must not re-trigger the pre-pick.
  //
  // ⚠️ An explicit choice is replaced by NO automatic pass, whatever the
  // matcher thinks of it: not for its material and not for the printer it was
  // made for. It stays while the listing has it — a choice that does not fit
  // the printer is marked beside its field, never swapped — and when the
  // listing drops it the dialog falls back and says so (`goneChoices`). Only
  // the user, or saved settings the user applies, replace it. Each entry keeps
  // the chosen ref, so "gone from the listing" can be told without reading
  // state. The process has the same record.
  const explicitFilamentSlots = useRef<Map<number, PresetRef>>(new Map());
  const explicitProcess = useRef<PresetRef | null>(null);
  // Fields whose hand-made choice left the listing and was replaced by an
  // automatic pick: 'process', or a filament slot's index.
  const [goneChoices, setGoneChoices] = useState<('process' | number)[]>([]);
  const filamentSlotCount = useRef(0);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Bed plate override sent to the slicer as ``--curr-bed-type``. Default
  // ``Textured PEI Plate`` matches the factory plate shipped with X1C / P1S
  // / H2D — the modern Bambu lineup the majority of our users own. A1 /
  // A1-mini owners who run Smooth PEI (SuperTack) will flip this once and
  // localStorage persists the pick, so subsequent slices land on the right
  // adhesion temps without re-clicking. Without this override the slicer
  // CLI defaults to "Cool Plate", which produces the wrong first-layer bed
  // temperature for any X1/A1 user with a default plate.
  const [bedType, setBedType] = useState<BedType>(() => {
    if (typeof localStorage === 'undefined') return DEFAULT_BED_TYPE;
    try {
      const stored = localStorage.getItem('bamdude:slice-modal:bed-type');
      return (ALLOWED_BED_TYPES as string[]).includes(stored ?? '')
        ? (stored as BedType)
        : DEFAULT_BED_TYPE;
    } catch {
      return DEFAULT_BED_TYPE;
    }
  });
  const [bedTypeNote, setBedTypeNote] = useState<BedTypeNote | null>(null);
  useEffect(() => {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem('bamdude:slice-modal:bed-type', bedType);
    } catch {
      /* private mode / quota — silently skip */
    }
  }, [bedType]);
  // When the owner filter narrows the visible set, any current selection
  // that no longer matches must drop to null — otherwise the dropdown
  // would render an out-of-section value the user can no longer see in
  // the list, and clicking Slice would submit a preset they didn't
  // (re-)pick under the new filter. Pre-pick effect won't re-fire because
  // its dep is presetsQuery.data only.
  // null = plate not yet picked (or single-plate / non-3MF — picker is skipped
  // and we'll backfill 1 at submit time). Set to a 1-indexed plate number once
  // the user picks one (or implicitly for single-plate sources).
  const [selectedPlate, setSelectedPlate] = useState<number | null>(null);
  // "Slice all plates": sends ``plate=0`` so the backend slices every plate
  // (one archive, one multi-plate 3MF) instead of a single picked plate.
  // Filament selection then covers every project slot (used_in_plate forced
  // true below) since across the whole project every defined slot is used.
  const [sliceAllPlates, setSliceAllPlates] = useState(false);

  // Per-job slicer picker (B.4 follow-up). Visible only when both sidecars
  // are reachable — otherwise the global preferred_slicer setting is the
  // sole sensible target and the picker would just be misleading. The pick
  // is persisted per (source kind, source id) in localStorage so re-slicing
  // the same file defaults to the user's last choice; first-time defaults
  // to the global preferred_slicer.
  const settingsQuery = useQuery({
    queryKey: ['ui-preferences'],
    queryFn: api.getUiPreferences,
    staleTime: 60_000,
  });
  // Backend caches the health response per (kind, url) for 30 s — these
  // two queries hit the network at most once per modal open, even if the
  // SliceModal re-mounts a few times during the user's flow.
  // The picked process preset's effective values, flattened by the sidecar.
  // ⚠️ Without this the settings panel shows OrcaSlicer's compiled-in defaults —
  // a preset with a 0.42 mm line width would read 0, which is the C++ default
  // meaning "derive from the nozzle", not zero. Keyed on the preset so
  // switching presets re-baselines the panel.
  const presetValuesQuery = useQuery({
    queryKey: ['slicer-preset-values', processPreset?.source, processPreset?.id],
    queryFn: () => api.getSlicerPresetValues(processPreset as PresetRef),
    enabled: processPreset != null,
    // Preset contents only change when the user edits them in the slicer, and
    // the dialog is short-lived; no need to re-fetch while it is open.
    staleTime: 5 * 60_000,
  });

  // A failed fetch is not an error the user must act on — the panel falls back
  // to schema defaults and says so — so "no data yet" counts as unresolved
  // rather than blocking the panel on it.
  const presetValues = presetValuesQuery.data?.values as Record<string, SettingValue> | undefined;
  const presetValuesResolved = presetValuesQuery.data?.resolved ?? presetValuesQuery.isLoading;
  // A failed REQUEST (rather than a `resolved: false` answer) means we never
  // reached the backend, which is the same situation as an unreachable sidecar
  // as far as the user is concerned.
  const presetValuesReason =
    presetValuesQuery.data?.reason ?? (presetValuesQuery.isError ? 'sidecar_unavailable' : undefined);

  const orcaHealthQuery = useQuery({
    queryKey: ['slicerHealth', 'orcaslicer'],
    queryFn: () => api.getSlicerHealth('orcaslicer'),
    staleTime: 30_000,
    retry: false,
  });
  const bambuHealthQuery = useQuery({
    queryKey: ['slicerHealth', 'bambu_studio'],
    queryFn: () => api.getSlicerHealth('bambu_studio'),
    staleTime: 30_000,
    retry: false,
  });
  const orcaHealthy = orcaHealthQuery.data?.healthy === true;
  const bambuHealthy = bambuHealthQuery.data?.healthy === true;
  const showSlicerPicker = orcaHealthy && bambuHealthy;

  const slicerPickStorageKey = `bamdude:slicer-pick:${source.kind}:${source.id}`;
  const [pickedSlicer, setPickedSlicer] = useState<SlicerKind | null>(() => {
    if (typeof localStorage === 'undefined') return null;
    try {
      const stored = localStorage.getItem(slicerPickStorageKey);
      return stored === 'orcaslicer' || stored === 'bambu_studio' ? stored : null;
    } catch {
      return null;
    }
  });
  // Auto-select / default pick logic. Two cases beyond "user already picked":
  //
  //   1. Only one sidecar is healthy → lock to that one. Stops the modal
  //      from defaulting to a global preferred_slicer that's offline,
  //      and visually reflects "you have one option" in the picker cards.
  //
  //   2. Both healthy → first-time default to the global preferred_slicer
  //      (same as before). Subsequent renders keep whatever the user picked
  //      (persisted via localStorage on a separate effect).
  //
  // Also: if the user previously picked a sidecar that is now offline,
  // override to the healthy one. Otherwise the Slice button would silently
  // submit an offline-targeted request and fail at backend.
  useEffect(() => {
    if (orcaHealthQuery.isLoading || bambuHealthQuery.isLoading) return;
    const onlyOrca = orcaHealthy && !bambuHealthy;
    const onlyBambu = bambuHealthy && !orcaHealthy;
    if (onlyOrca) {
      if (pickedSlicer !== 'orcaslicer') setPickedSlicer('orcaslicer');
      return;
    }
    if (onlyBambu) {
      if (pickedSlicer !== 'bambu_studio') setPickedSlicer('bambu_studio');
      return;
    }
    if (!showSlicerPicker) return; // neither healthy — leave as-is
    if (pickedSlicer != null) return;
    const preferred = settingsQuery.data?.preferred_slicer;
    if (preferred === 'orcaslicer' || preferred === 'bambu_studio') {
      setPickedSlicer(preferred);
    } else {
      setPickedSlicer('bambu_studio');
    }
  }, [
    showSlicerPicker,
    orcaHealthy,
    bambuHealthy,
    orcaHealthQuery.isLoading,
    bambuHealthQuery.isLoading,
    pickedSlicer,
    settingsQuery.data?.preferred_slicer,
  ]);
  // The slicer this slice will run on: the only reachable one when exactly
  // one is, else the user's pick, else the global preference the backend falls
  // back to. `null` while the health checks are still out — nothing is decided
  // about the bed plate until it is known.
  const preferredSlicer = settingsQuery.data?.preferred_slicer;
  const sliceSlicer: SlicerKind | null =
    orcaHealthQuery.isLoading || bambuHealthQuery.isLoading
      ? null
      : orcaHealthy && !bambuHealthy
        ? 'orcaslicer'
        : bambuHealthy && !orcaHealthy
          ? 'bambu_studio'
          : (pickedSlicer ?? (preferredSlicer === 'orcaslicer' || preferredSlicer === 'bambu_studio' ? preferredSlicer : null));
  // A plate only OrcaSlicer has cannot stay picked for Bambu Studio — its CLI
  // would slice as for Cool Plate without a word. Changed to the default, and
  // said so beside the field.
  useEffect(() => {
    if (sliceSlicer !== 'bambu_studio' || !ORCA_ONLY_BED_TYPES.includes(bedType)) return;
    setBedTypeNote({ kind: 'reset', bed: bedType });
    setBedType(DEFAULT_BED_TYPE);
  }, [sliceSlicer, bedType]);
  // Persist user's pick across modal opens.
  useEffect(() => {
    if (pickedSlicer == null || typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(slicerPickStorageKey, pickedSlicer);
    } catch {
      /* private mode / quota — silently skip */
    }
  }, [pickedSlicer, slicerPickStorageKey]);

  const platesQuery = useQuery({
    queryKey: ['slicePlates', source.kind, source.id],
    queryFn: async () => {
      if (source.kind === 'libraryFile') {
        return api.getLibraryFilePlates(source.id);
      }
      return api.getArchivePlates(source.id);
    },
    staleTime: 60_000,
  });

  const isMultiPlate =
    !!platesQuery.data?.is_multi_plate && (platesQuery.data?.plates?.length ?? 0) > 1;
  // Single-plate / non-3MF / fetch failure: skip the picker, default to plate 1
  // at submit time so the backend's existing default behaviour is preserved.
  // Multi-plate: auto-pick the first plate on load so the filament-reqs +
  // presets queries fire immediately (the inline ``<SlicePlateSelector>``
  // lets the user reassign without blocking the rest of the modal).
  const needsPlatePicker = isMultiPlate && selectedPlate == null;
  useEffect(() => {
    if (!isMultiPlate || selectedPlate != null) return;
    const first = platesQuery.data?.plates?.[0]?.index;
    if (first != null) setSelectedPlate(first);
  }, [isMultiPlate, selectedPlate, platesQuery.data]);

  // Per-plate filament requirements via the same endpoint the print/schedule
  // modal uses. Reusing it here keeps the SliceModal honest with whatever
  // logic that endpoint applies (slice_info parsing, preview-slice fallback,
  // future enhancements for unsliced project files, dual-nozzle fields, etc.)
  // instead of duplicating extraction. plate_id is always sent: single-plate
  // falls through to plate 1 server-side; multi-plate uses the user's pick.
  const effectivePlateId = selectedPlate ?? 1;
  // Generate a request_id per (source, plate) pair so the backend's
  // preview-slice and the FilamentAnalysisSpinner's progress poll share the
  // same id. useMemo keeps it stable across renders within the same pair;
  // switching plates regenerates so a stale poll doesn't bleed progress
  // between plates.
  const previewRequestId = useMemo(() => {
    const random =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    // Tag the id with the (source, plate) so logs/Network panel show which
    // pair owns the poll. Also lets the lint rule see the deps in use.
    return `${source.kind}-${source.id}-p${effectivePlateId}-${random}`;
  }, [source.kind, source.id, effectivePlateId]);
  const filamentReqsQuery = useQuery({
    queryKey: ['sliceFilamentReqs', source.kind, source.id, effectivePlateId],
    queryFn: async () => {
      // `fullSlots`: one row per project slot, not only the ones this plate
      // prints with. The list below is positional all the way to the CLI's
      // filament_N.json parts, so a source whose only used slot is 4 has to
      // present four rows — otherwise the single pick binds to slot 1 and slot
      // 4 slices with whatever the source had baked in (#2712). The unused rows
      // stay disabled exactly as before. PrintModal deliberately does NOT pass
      // it: AMS matching must ask for the spools the job actually needs.
      if (source.kind === 'libraryFile') {
        return api.getLibraryFileFilamentRequirements(source.id, effectivePlateId, previewRequestId, true);
      }
      return api.getArchiveFilamentRequirements(source.id, effectivePlateId, previewRequestId, true);
    },
    enabled: !needsPlatePicker,
    staleTime: 60_000,
  });

  // Filament slot list for the active plate. Falls back to one synthetic slot
  // for STL/STEP and any "no metadata available" case so the modal still
  // works (single dropdown, mono-color slice).
  const filamentSlots = useMemo<PlateFilament[]>(() => {
    const reqs = filamentReqsQuery.data?.filaments ?? [];
    const base: PlateFilament[] =
      reqs.length > 0 ? (reqs as PlateFilament[]) : [{ slot_id: 1, type: '', color: '', used_grams: 0, used_meters: 0 }];
    // In slice-all mode every defined slot is used by at least one plate, so
    // drop the per-plate "not used" gating that disables the dropdowns.
    if (sliceAllPlates) {
      return base.map((slot) => ({ ...slot, used_in_plate: true }));
    }
    return base;
  }, [sliceAllPlates, filamentReqsQuery.data]);

  const queryClient = useQueryClient();
  // The presets an assembled project was built from, by source and id. The
  // file itself names them by name only, and a name can exist in two sources
  // (Bambu Cloud lists the stock presets under the bundle's names) — the
  // record is what tells a Standard preset from its Bambu Cloud namesake. Read
  // only for a file known to be assembled; a failed read falls back to names.
  const isAssembled = source.kind === 'libraryFile' && source.assembled === true;
  const assemblyQuery = useQuery({
    queryKey: ['assemblyRecord', source.id],
    queryFn: () => api.getAssemblyRecord(source.id),
    enabled: isAssembled,
    retry: false,
    staleTime: 60_000,
  });
  const builtFrom = isAssembled ? assemblyQuery.data?.printer : undefined;
  const presetsQuery = useQuery({
    queryKey: ['slicerPresets'],
    queryFn: () => api.getSlicerPresets(),
    staleTime: 60_000,
    // Don't fetch presets while the plate picker is on screen — saves a
    // round-trip if the user cancels out of the plate step. And not before the
    // assembly record has answered: the pre-picks below run when the listing
    // arrives, and must already know what the project was built from.
    enabled: !platesQuery.isLoading && !needsPlatePicker && !(isAssembled && assemblyQuery.isLoading),
  });

  // Manual refresh — bypasses the backend's cloud + bundled preset caches for
  // one call so a user who deleted a preset in Bambu Studio / Handy sees the
  // change immediately (#1581). The cache write inside the backend fetchers
  // refills with the fresh result, so later normal callers stay cached.
  const [isRefreshing, setIsRefreshing] = useState(false);
  const handleRefreshPresets = async () => {
    if (isRefreshing) return;
    setIsRefreshing(true);
    try {
      const fresh = await api.getSlicerPresets({ refresh: true });
      queryClient.setQueryData(['slicerPresets'], fresh);
    } catch {
      // Fall through to invalidate so React Query retries on next render,
      // surfacing the failure through the existing presetsQuery error path.
      queryClient.invalidateQueries({ queryKey: ['slicerPresets'] });
    } finally {
      setIsRefreshing(false);
    }
  };

  // Canonical Bambu printer-model registry — drives the @BBL name fallback in
  // the compatibility matcher for cloud / standard presets (#1325). Long
  // staleTime: the registry only changes across backend releases.
  const printerModelsQuery = useQuery({
    queryKey: ['slicerPrinterModels'],
    queryFn: api.getPrinterModels,
    staleTime: Infinity,
  });

  // Selected-printer context for the process / filament filter (#1325).
  const selectedPrinterName = useMemo<string | null>(() => {
    if (!presetsQuery.data || !printerPreset) return null;
    return findPreset(presetsQuery.data, printerPreset, 'printer')?.name ?? null;
  }, [presetsQuery.data, printerPreset]);
  // Compatibility ground truth: the slicer's own `compatible_printers` list
  // on local-imported presets, plus the @BBL <code> name fallback for cloud /
  // standard presets via the backend Bambu printer-model registry. Plus, for a
  // printer preset saved under a name of its own, the preset it was saved from
  // (upstream 3acc52d93): its model, nozzle and compatible presets are the
  // parent's. The ref sent to the slicer stays the picked preset's own.
  const printerParents = useMemo(() => buildPrinterParents(presetsQuery.data), [presetsQuery.data]);
  const compatIndex = useMemo<PrinterCompatibilityIndex>(
    () => buildCompatibilityIndex(printerModelsQuery.data ?? {}, printerParents),
    [printerModelsQuery.data, printerParents],
  );
  // "Only online printers" / "only loaded filaments": what the lists OFFER.
  // Never read by the pre-pick effects below — a filter replaces no choice.
  const farmFilters = useSliceFarmFilters(presetsQuery.data, selectedPrinterName, printerParents);

  // Printer / process preset names the source 3MF was prepared with. The
  // plates query resolves before the presets query (the latter is gated on
  // it), so these are known by the time the pre-pick effects run.
  const embeddedPrinter = platesQuery.data?.embedded_printer ?? null;
  const embeddedProcess = platesQuery.data?.embedded_process ?? null;
  const embeddedFilaments = platesQuery.data?.embedded_filaments;
  const printerSource = printerPreset?.source ?? null;

  // "Slice as designed" (upstream #2611). When on, the backend honours the
  // source 3MF's embedded project_settings.config (the designer's own wall
  // count, infill, …) instead of the picked process/filament profiles. Only
  // offered when the picked printer matches the design's target model — see
  // canUseEmbedded below.
  const [useEmbedded, setUseEmbedded] = useState(false);
  // Per-slice layout passes, both off by default. They act on the loaded
  // geometry rather than on the print config, so they apply on every slice
  // path — picked presets, embedded settings and the crash fallback alike.
  const [autoArrange, setAutoArrange] = useState(false);
  const [autoOrient, setAutoOrient] = useState(false);

  // Process settings the designer changed away from the stock preset (#2622),
  // carried onto the picked process profile so a cross-printer re-slice keeps
  // the model's intended wall count / infill / first layer instead of losing
  // them to --load-settings. Keys the file flags as machine-coupled — speeds,
  // accelerations, prime-tower geometry — are listed but start unticked: those
  // were tuned for the designer's printer and can be plain wrong on another, or
  // outside the range its profile accepts, which fails the slice outright.
  const [designKeys, setDesignKeys] = useState<Set<string>>(new Set());
  const [designExpanded, setDesignExpanded] = useState(false);

  const designOverrides = useMemo<DesignOverride[]>(
    () => platesQuery.data?.design_overrides ?? [],
    [platesQuery.data],
  );
  // Pre-tick design intent once the source's list arrives; machine-coupled keys
  // wait for the user to opt in.
  useEffect(() => {
    setDesignKeys(defaultDesignKeys(designOverrides));
  }, [designOverrides]);

  // The toggle is offered only when the source carries embedded settings (a
  // real project 3MF, not an STL) AND the picked printer matches the design's
  // target model. The match gate is load-bearing: honouring embedded settings
  // for a different model would place the model on the wrong bed. Names come
  // from the same preset namespace, so a normalised (strip "# " prefix,
  // case-fold) equality is enough.
  const canUseEmbedded = useMemo<boolean>(() => {
    if (!embeddedPrinter || !embeddedProcess || !selectedPrinterName) return false;
    const norm = (s: string) => s.replace(/^#\s*/, '').trim().toLowerCase();
    return norm(selectedPrinterName) === norm(embeddedPrinter);
  }, [embeddedPrinter, embeddedProcess, selectedPrinterName]);

  // Drop back to profile slicing whenever the toggle stops being offered (e.g.
  // the user switches to a printer that doesn't match the design).
  useEffect(() => {
    if (!canUseEmbedded) setUseEmbedded(false);
  }, [canUseEmbedded]);

  // Printer pre-pick: defaults to the printer the 3MF was prepared for when
  // that preset is available, else the first listed printer (see
  // SLICE_MODAL_TIER_ORDER in slicePresetPicker). Runs once when presets first
  // arrive; later re-renders preserve any manual choice (#1325).
  useEffect(() => {
    const data = presetsQuery.data;
    if (!data) return;
    if (printerPreset == null) {
      // The preset an assembled project was built from, by identity, before
      // any lookup by name.
      const built = builtFrom && findPreset(data, builtFrom.printer, 'printer') ? builtFrom.printer : null;
      setPrinterPreset(
        built ?? findPresetByName(data, 'printer', embeddedPrinter) ?? pickDefault(data, 'printer'),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presetsQuery.data, embeddedPrinter, builtFrom]);

  // Process pre-pick / re-pick (#1325): defaults to a process compatible with
  // the selected printer (honouring the 3MF's embedded process first), and
  // re-defaults when a printer change leaves the current process incompatible.
  // A choice made by hand is kept whatever the matcher says of it, for as long
  // as the listing has it; one that is gone falls back and is named on screen.
  useEffect(() => {
    const data = presetsQuery.data;
    if (!data) return;
    if (explicitProcess.current && !findPreset(data, explicitProcess.current, 'process')) {
      explicitProcess.current = null;
      setGoneChoices((gone) => (gone.includes('process') ? gone : [...gone, 'process']));
    }
    setProcessPreset((current) => {
      if (current) {
        const p = findPreset(data, current, 'process');
        if (
          p
          && (explicitProcess.current !== null
            || presetCompatibility(p, 'process', selectedPrinterName, compatIndex) !== 'mismatch')
        ) {
          return current;
        }
      }
      // An assembled project's own process, by identity, while it is listed
      // and the selected printer can take it.
      const built = findPreset(data, builtFrom?.process ?? null, 'process');
      if (built && presetCompatibility(built, 'process', selectedPrinterName, compatIndex) !== 'mismatch') {
        return { source: built.source, id: built.id };
      }
      return pickProcessDefault(data, selectedPrinterName, compatIndex, embeddedProcess);
    });
  }, [presetsQuery.data, selectedPrinterName, compatIndex, embeddedProcess, builtFrom]);

  // A plate switch renumbers the slots, so the colour picks are dropped when
  // the slot count changes (see `resizeColourOverrides`).
  useEffect(() => {
    if (filamentSlotCount.current !== filamentSlots.length) {
      // A renumbered slot list invalidates the record of which slots the
      // user chose for the same reason: slot 2 of the new plate is not slot 2
      // of the old one. Done here and not inside a state updater: the filament
      // pre-pick below must already see the record empty.
      filamentSlotCount.current = filamentSlots.length;
      explicitFilamentSlots.current = new Map();
      setGoneChoices((gone) => (gone.some((g) => g !== 'process') ? gone.filter((g) => g === 'process') : gone));
    }
    setFilamentColours((current) => resizeColourOverrides(current, filamentSlots.length));
  }, [filamentSlots]);

  // Filament pre-pick: re-runs when the active filament-slot count changes
  // (plate selection, single-plate metadata arriving) or the selected printer
  // changes. Each slot scores every available filament preset against the
  // slot's required (type, colour); an automatic pick is kept as long as it's
  // still compatible with the selected printer, while null slots and
  // printer-incompatible automatic picks are re-picked (#1325). A slot chosen
  // by hand is kept while the listing has its preset.
  useEffect(() => {
    const data = presetsQuery.data;
    if (!data) return;
    const gone: number[] = [];
    for (const [i, ref] of explicitFilamentSlots.current) {
      if (!findPreset(data, ref, 'filament')) gone.push(i);
    }
    if (gone.length > 0) {
      for (const i of gone) explicitFilamentSlots.current.delete(i);
      setGoneChoices((known) => [...known, ...gone.filter((i) => !known.includes(i))]);
    }
    setFilamentPresets((current) => {
      return filamentSlots.map((slot, i) => {
        const cur = current[i] ?? null;
        if (cur) {
          const p = findPreset(data, cur, 'filament');
          if (p && explicitFilamentSlots.current.has(i)) return cur;
          // Compatible with the printer is not on its own a reason to keep a
          // pick: a preset stating another base material than the plate asks
          // for is the wrong preset however well it fits the machine, and
          // holding onto one is how a PETG profile survived on a PLA plate
          // through every re-pick (#2982). An explicit choice is exempt.
          const wrongMaterial = p && statesDifferentMaterial(p, slot.type);
          if (p && !wrongMaterial && presetCompatibility(p, 'filament', selectedPrinterName, compatIndex) !== 'mismatch') {
            return cur;
          }
        }
        // The preset the project itself names for this slot comes first (a
        // file from "Assemble project" names the one chosen there), when it is
        // listed and fits the printer and the plate's material. A name that
        // several sources hold is told apart by what the project was built
        // from, else by the printer preset's source — never by tier order.
        return (
          pickEmbeddedFilament(
            data,
            embeddedFilaments?.[slot.slot_id - 1],
            { type: slot.type },
            selectedPrinterName,
            compatIndex,
            { built: builtFrom?.filament, printerSource },
          )
          ?? pickFilamentForSlot(
            data,
            { type: slot.type, color: slot.color },
            selectedPrinterName,
            compatIndex,
          )
        );
      });
    });
  }, [presetsQuery.data, filamentSlots, selectedPrinterName, compatIndex, embeddedFilaments, builtFrom, printerSource]);


  const enqueueMutation = useMutation({
    mutationFn: async () => {
      if (
        !printerPreset ||
        !processPreset ||
        filamentPresets.length === 0 ||
        filamentPresets.some((r) => r == null)
      ) {
        throw new Error(t('slice.allPresetsRequired', 'All presets must be selected'));
      }
      const body: SliceRequest = {
        printer_preset: printerPreset,
        process_preset: processPreset,
        // The first slot also goes into the legacy singular field so the
        // backend's older callers / clients keep behaving the same — the
        // backend validator prefers `filament_presets` when both are set.
        filament_preset: filamentPresets[0] as PresetRef,
        filament_presets: filamentPresets as PresetRef[],
        // The colour each slot is printed in, so the sliced file stops recording
        // the CLI's #00AE42 whatever was picked (#2977). An untouched slot sends
        // the source plate's colour, or "" when it has none.
        filament_colours: filamentColoursPayload(filamentSlots, filamentColours),
        // Always send a concrete plate number when the source is multi-plate;
        // omit otherwise so the backend default applies for STL / single-plate
        // 3MF sources where the concept doesn't apply.
        ...(sliceAllPlates ? { plate: 0 } : selectedPlate != null ? { plate: selectedPlate } : {}),
        // Per-job slicer override. Only sent when the picker is actually
        // visible to the user (both sidecars healthy) AND the user picked
        // something — otherwise the global preferred_slicer setting decides.
        ...(pickedSlicer != null ? { slicer: pickedSlicer } : {}),
        // Bed plate override → sidecar's ``bedType`` form field →
        // ``--curr-bed-type`` CLI flag.
        bed_type: bedType,
        // The preset refs above are still sent (the backend validator requires
        // them) but go unused when this flag is set — the slicer falls back on
        // the file's embedded project_settings.config instead.
        // Sent only when on: the backend omits the sidecar form field entirely
        // for an off flag, because a literal "false" is truthy over multipart.
        ...(autoArrange ? { auto_arrange: true } : {}),
        ...(autoOrient ? { auto_orient: true } : {}),
        ...(useEmbedded && canUseEmbedded ? { use_embedded_settings: true } : {}),
        // Carried design settings are patched onto the resolved process JSON,
        // which the embedded-settings path never sends — so the two are
        // mutually exclusive by construction (#2622).
        ...designOverridesField(useEmbedded, designOverrides, designKeys),
        // The user's own edits from the settings panel. Like design_overrides
        // these patch the resolved process JSON, so the embedded-settings path
        // — which sends no process JSON at all — cannot carry them.
        ...(!useEmbedded && Object.keys(processOverrides).length > 0
          ? { process_overrides: serializedProcessOverrides }
          : {}),
      };
      if (source.kind === 'libraryFile') {
        return api.sliceLibraryFile(source.id, body);
      }
      return api.sliceArchive(source.id, body);
    },
    onSuccess: (enqueue) => {
      trackJob(enqueue.job_id, source.kind, source.filename);
      onClose();
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(msg);
    },
  });

  // Cross-printer re-slicing is supported: the slicer CLI re-slices a 3MF
  // for whatever printer the chosen bundle/preset names — bed, kinematics,
  // nozzle count and start-gcode all come from the target. No source/target
  // model gate (#1325 cross-printer; upstream Bambuddy ed27b27a).

  // Slice button gates only on a complete picker selection. Multi-plate
  // sources also need an explicit plate pick so the slicer (and the
  // filament-reqs query that feeds the dropdowns) target the right plate.
  // Also gates on the preview-slice / embedded-metadata read having
  // succeeded (``filamentReqsQuery.isSuccess``) — until then, the synthetic
  // single-slot fallback would have auto-enabled the button on opaque
  // defaults before the slicer returned the real slot map.
  // Upstream Bambuddy commit a64cfbbd.
  const isReady =
    printerPreset != null &&
    processPreset != null &&
    filamentReqsQuery.isSuccess &&
    filamentPresets.length > 0 &&
    filamentPresets.every((r) => r != null) &&
    (!isMultiPlate || sliceAllPlates || selectedPlate != null);
  const isEnqueuing = enqueueMutation.isPending;

  // Saved preset bundles (#1425) — load a saved printer / process / filament /
  // bed selection into this dialog with one pick, or save the current one for
  // next time. Managed in Settings → Pipelines. Read + write are separately
  // permissioned, so a viewer can load bundles without being able to add any.
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const canReadPipelines = hasPermission('pipelines:read');
  const canWritePipelines = hasPermission('pipelines:write');
  const pipelinesQuery = useQuery({
    queryKey: ['slicer-pipelines'],
    queryFn: () => api.listSlicerPipelines(),
    staleTime: 60_000,
    enabled: canReadPipelines,
  });
  const [savePipelineOpen, setSavePipelineOpen] = useState(false);
  const [pipelineDraftName, setPipelineDraftName] = useState('');
  // Slot labels from the last loaded bundle whose saved PresetRef is no longer
  // in the catalogue (preset deleted in Bambu Studio / Orca since the bundle
  // was saved). Those slots keep their current pick rather than being set to a
  // ref the dropdown can't render — and the banner below names them, because
  // a silently half-applied bundle would slice with the wrong profile.
  const [unresolvedBundleSlots, setUnresolvedBundleSlots] = useState<string[] | null>(null);
  // The last applied set and the fields it changed — the note beside the
  // picker. Retired, like the banner above, by the next pick made by hand.
  const [appliedSet, setAppliedSet] = useState<{ name: string; changed: string[] } | null>(null);
  const retireBundleNotes = () => {
    setUnresolvedBundleSlots(null);
    setAppliedSet(null);
  };
  const createPipelineMutation = useMutation({
    mutationFn: (body: {
      name: string;
      printer_preset: PresetRef;
      process_preset: PresetRef;
      filament_presets: PresetRef[];
      bed_type: string | null;
    }) => api.createSlicerPipeline(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slicer-pipelines'] });
      showToast(t('slice.pipelines.toast.saved', 'Pipeline saved'), 'success');
      setSavePipelineOpen(false);
      setPipelineDraftName('');
    },
    onError: (err: Error) => {
      showToast(err.message || t('slice.pipelines.toast.saveFailed', 'Save failed'), 'error');
    },
  });

  // Load a saved bundle into the dialog's four selections. Every saved
  // PresetRef is resolved against the loaded catalogue first: a ref whose
  // preset has since been deleted is left out (the slot keeps its current
  // auto-pick) and its label is collected for the banner. The filament list is
  // walked per slot so a bundle with fewer entries than this source's slot
  // count leaves the tail untouched.
  const applyPipeline = (picked: SlicerPipeline) => {
    const data = presetsQuery.data;
    if (!data) return;
    const unresolved: string[] = [];
    // The fields this set gave another value than they had — said beside the
    // picker, because applying a set is the one action besides the user's own
    // pick that replaces a choice made by hand.
    const changed: string[] = [];
    const resolved = (ref: PresetRef, slot: Slot) => resolvePresetName(data, ref, slot) !== null;
    const differs = (a: PresetRef | null, b: PresetRef) => a == null || a.source !== b.source || a.id !== b.id;

    if (resolved(picked.printer_preset, 'printer')) {
      if (differs(printerPreset, picked.printer_preset)) changed.push(t('slice.printer', 'Printer profile'));
      setPrinterPreset(picked.printer_preset);
    } else {
      unresolved.push(t('slice.printer', 'Printer profile'));
    }
    if (resolved(picked.process_preset, 'process')) {
      if (differs(processPreset, picked.process_preset)) changed.push(t('slice.process', 'Process profile'));
      setProcessPreset(picked.process_preset);
      // As deliberate as a pick from the dropdown: it stays even when the
      // matcher calls it a mismatch for the set's printer.
      explicitProcess.current = picked.process_preset;
    } else {
      unresolved.push(t('slice.process', 'Process profile'));
    }
    // A saved bed plate that is not applied is said, never skipped in
    // silence: a set saved before the server checked the name can carry
    // anything, and an OrcaSlicer-only plate means nothing to Bambu Studio.
    if (picked.bed_type) {
      if (!(ALLOWED_BED_TYPES as string[]).includes(picked.bed_type)) {
        setBedTypeNote({ kind: 'unknown', bed: picked.bed_type });
      } else if (sliceSlicer === 'bambu_studio' && (ORCA_ONLY_BED_TYPES as string[]).includes(picked.bed_type)) {
        setBedTypeNote({ kind: 'slicer', bed: picked.bed_type });
      } else {
        if (picked.bed_type !== bedType) changed.push(t('slice.bedType.label', 'Bed plate'));
        setBedType(picked.bed_type as BedType);
        setBedTypeNote(null);
      }
    }
    // Computed from current state rather than inside a setState updater —
    // the updater can run twice (StrictMode) and runs after this function
    // returns, so collecting ``unresolved`` in there would double-count and
    // read empty below.
    const nextFilaments =
      filamentPresets.length > 0
        ? [...filamentPresets]
        : picked.filament_presets.map(() => null as PresetRef | null);
    for (let i = 0; i < nextFilaments.length && i < picked.filament_presets.length; i++) {
      const ref = picked.filament_presets[i];
      const slotLabel =
        nextFilaments.length > 1
          ? t('slice.filamentSlot', {
              index: i + 1,
              type: filamentSlots[i]?.type ?? '',
              defaultValue: `Filament ${i + 1} (${filamentSlots[i]?.type ?? ''})`,
            })
          : t('slice.filament', 'Filament profile');
      if (resolved(ref, 'filament')) {
        if (differs(nextFilaments[i], ref)) changed.push(slotLabel);
        nextFilaments[i] = ref;
        // Applying a pipeline is as deliberate as picking from the dropdown,
        // so the slots it fills are exempt from every automatic re-pick too.
        explicitFilamentSlots.current.set(i, ref);
      } else {
        unresolved.push(slotLabel);
      }
    }
    setFilamentPresets(nextFilaments);
    // What the set filled is no longer "replaced because it was gone".
    setGoneChoices([]);
    setAppliedSet({ name: picked.name, changed });

    setUnresolvedBundleSlots(unresolved.length > 0 ? unresolved : null);
    if (unresolved.length === 0) {
      showToast(
        t('slice.pipelines.toast.applied', 'Applied "{{name}}"', { name: picked.name }),
        'success',
      );
    }
  };

  // Single-screen layout: preset picker
  // picker. While the plates query is in-flight we still render the shell
  // because the presets query is gated on it; the loader covers both.
  return (
    <Modal
      onClose={onClose}
      closeDisabled={isEnqueuing}
      labelledBy={headingId}
      size="xl"
      panelClassName="lg:max-w-5xl xl:max-w-7xl 2xl:max-w-8xl"
      bodyClassName="flex flex-col"
      header={
        <div className="min-w-0">
          <h3 id={headingId} className="text-white font-medium flex items-center gap-2">
            <Cog className="w-4 h-4" />
            {t('slice.title', 'Slice model')}
          </h3>
          <p className="text-xs text-bambu-gray mt-1 truncate" title={source.filename}>
            {source.filename}
            {selectedPlate != null
              ? ` • ${t('archives.platePicker.plateLabel', { index: selectedPlate })}`
              : ''}
          </p>
        </div>
      }
    >
      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Inline plate picker for multi-plate sources. Visually mirrors
            ``PrintModal/PlateSelector`` so the slice + print flows feel
            consistent (vertical paginator strip + big details card).
            Renders above the other sections so the user sees their
            plate context first; switching plates re-keys the
            filament-reqs query so the dropdowns below realign to the
            new plate's required (type, color) automatically. */}
        {isMultiPlate && platesQuery.data && (
          <>
            <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
              <input
                type="checkbox"
                checked={sliceAllPlates}
                onChange={(e) => setSliceAllPlates(e.target.checked)}
                disabled={isEnqueuing}
                className="accent-bambu-green"
              />
              {t('slice.allPlates', 'Slice all plates')}
            </label>
            {!sliceAllPlates && (
              <SlicePlateSelector
                plates={platesQuery.data.plates}
                selectedPlate={selectedPlate}
                onSelect={setSelectedPlate}
                disabled={isEnqueuing}
              />
            )}
          </>
        )}
        {/* Preset listing loader — printer/process dropdowns can't render
            without it. Plate query reuses the same spinner since it's
            also blocking. */}
        {(platesQuery.isLoading || presetsQuery.isLoading) && (
          <div className="flex items-center gap-2 text-bambu-gray text-sm">
            <Loader2 className="w-4 h-4 animate-spin" />
            {t('slice.loadingPresets', 'Loading presets…')}
          </div>
        )}

        {presetsQuery.isError && (
          <div className="text-sm text-red-700 dark:text-red-400" role="alert">
            {t(
              'slice.presetsLoadFailed',
              'Failed to load presets. Open Settings → Profiles to import them, or sign in to Bambu Cloud.',
            )}
          </div>
        )}

        {presetsQuery.data && (
          <>
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1 space-y-2">
                <CloudStatusBanner status={presetsQuery.data.cloud_status} cloudName="bambu" />
                <CloudStatusBanner status={presetsQuery.data.orca_cloud_status} cloudName="orca" />
              </div>
              <button
                type="button"
                onClick={handleRefreshPresets}
                disabled={isRefreshing || isEnqueuing}
                className="flex-shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary/40 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                title={t('slice.refreshPresetsTitle')}
                aria-label={t('slice.refreshPresets')}
              >
                <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
                {t('slice.refreshPresets')}
              </button>
            </div>
            {/* Two columns once there is room for them. The left keeps
                the "what am I slicing with" decisions together; the right
                gives the process-settings panel a column of its own, which
                is the only way 348 options are comfortable to work through.
                ⚠️ Below lg both collapse back into the single stack this
                dialog used to be, so nothing on a phone changes.
                The left column grows with the frame (upstream bda14f753):
                fixed at 20rem it cut preset names off and wrapped every
                filament label while the settings panel sat half empty. 20rem
                is its floor now, and the frame keeps widening past lg in
                steps of the modal's own size table. */}
            <div className="lg:grid lg:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] lg:gap-4 lg:items-start">
              <div className="space-y-4 min-w-0">
            {/* Slicer picker — two big card-buttons matching the
                "Filament Tracking" pattern in Settings. Each card carries
                its own live health status (version / offline / checking)
                pulled from the same shared React Query cache. Always
                renders so the user sees what's available even when only
                one sidecar is up; offline ones are disabled and can't
                be picked. */}
            <SlicerPicker
              value={pickedSlicer}
              onChange={setPickedSlicer}
              disabled={isEnqueuing}
            />
            {/* Saved preset bundles (#1425). Same label + full-width select
                shape as the preset dropdowns below, so loading a bundle
                reads as one more picker rather than a separate widget. The
                select is an action list (value is always ""), which is why
                re-picking the same bundle re-applies it. Bundles are
                managed in Settings → Pipelines. */}
            {canReadPipelines && (
              <div>
                <div className="flex items-end justify-between gap-2 mb-1">
                  <label htmlFor="slice-pipeline-picker" className="text-xs text-bambu-gray">
                    {t('slice.pipelines.label', 'Pipeline')}
                  </label>
                  {canWritePipelines && !savePipelineOpen && (
                    <button
                      type="button"
                      onClick={() => {
                        setPipelineDraftName('');
                        setSavePipelineOpen(true);
                      }}
                      disabled={
                        isEnqueuing ||
                        useEmbedded ||
                        !printerPreset ||
                        !processPreset ||
                        filamentPresets.length === 0 ||
                        filamentPresets.some((f) => f === null)
                      }
                      className="text-xs text-bambu-green hover:text-bambu-green/80 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                      title={t(
                        'slice.pipelines.saveTitle',
                        'Save the current four-slot selection as a reusable pipeline',
                      )}
                    >
                      {t('slice.pipelines.saveButton', 'Save as pipeline')}
                    </button>
                  )}
                </div>
                <Select
                  className="w-full"
                  id="slice-pipeline-picker"
                  value=""
                  disabled={
                    isEnqueuing || useEmbedded || (pipelinesQuery.data?.pipelines.length ?? 0) === 0
                  }
                  onChange={(e) => {
                    const id = parseInt(e.target.value, 10);
                    // Reset immediately so re-picking the same bundle fires
                    // onChange again (a select doesn't when the DOM value
                    // already equals the option).
                    e.target.value = '';
                    if (Number.isNaN(id)) return;
                    const picked = pipelinesQuery.data?.pipelines.find((p) => p.id === id);
                    if (picked) applyPipeline(picked);
                  }}
                >
                  <option value="">
                    {(pipelinesQuery.data?.pipelines.length ?? 0) === 0
                      ? t('slice.pipelines.empty', 'No saved pipelines')
                      : t('slice.pipelines.applyPrompt', 'Apply pipeline…')}
                  </option>
                  {pipelinesQuery.data?.pipelines.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </Select>
                {savePipelineOpen && (
                  <div className="flex items-center gap-2 mt-2">
                    <input
                      autoFocus
                      value={pipelineDraftName}
                      onChange={(e) => setPipelineDraftName(e.target.value)}
                      placeholder={t('slice.pipelines.namePlaceholder', 'Pipeline name')}
                      aria-label={t('slice.pipelines.nameAria', 'New pipeline name')}
                      className="flex-1 min-w-0 px-3 py-2 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm focus:outline-none focus:border-bambu-gray"
                    />
                    <button
                      type="button"
                      onClick={() => {
                        const trimmed = pipelineDraftName.trim();
                        if (!trimmed || !printerPreset || !processPreset) return;
                        const nonNull = filamentPresets.filter((f): f is PresetRef => f !== null);
                        if (nonNull.length === 0) return;
                        createPipelineMutation.mutate({
                          name: trimmed,
                          printer_preset: printerPreset,
                          process_preset: processPreset,
                          filament_presets: nonNull,
                          bed_type: bedType,
                        });
                      }}
                      disabled={createPipelineMutation.isPending || !pipelineDraftName.trim()}
                      className="flex-shrink-0 px-3 py-1.5 text-sm rounded-md bg-bambu-green hover:bg-bambu-green/90 text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                    >
                      {createPipelineMutation.isPending && (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      )}
                      {t('common.save', 'Save')}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSavePipelineOpen(false);
                        setPipelineDraftName('');
                      }}
                      className="flex-shrink-0 px-3 py-1.5 text-sm rounded-md border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray transition-colors"
                    >
                      {t('common.cancel', 'Cancel')}
                    </button>
                  </div>
                )}
                {/* A saved ref whose preset is gone is never applied
                    silently — same amber banner shape the cloud-status
                    warnings use, naming the slots left on their current
                    pick so the user can re-choose or re-save the bundle. */}
                {unresolvedBundleSlots && unresolvedBundleSlots.length > 0 && (
                  <div
                    className="flex items-start gap-2 text-xs rounded-md border p-2 mt-2 border-amber-300 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200"
                    role="status"
                  >
                    <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                    <span>
                      {t(
                        'slice.pipelines.staleWarning',
                        'Some presets saved in this pipeline no longer exist. These stayed on their current selection: {{slots}}',
                        { slots: unresolvedBundleSlots.join(', ') },
                      )}
                    </span>
                  </div>
                )}
                {appliedSet && (
                  <p role="status" className="mt-2 text-xs text-bambu-gray">
                    {appliedSet.changed.length > 0
                      ? t('slice.pipelines.appliedChanged', {
                          name: appliedSet.name,
                          fields: appliedSet.changed.join(', '),
                          defaultValue: 'Applied "{{name}}". Changed: {{fields}}',
                        })
                      : t('slice.pipelines.appliedUnchanged', {
                          name: appliedSet.name,
                          defaultValue: 'Applied "{{name}}". Nothing changed — these were already the selection.',
                        })}
                  </p>
                )}
              </div>
            )}
            {/* Bed plate picker — five values from BambuStudio's
                ``curr_bed_type`` enum. Always sent on slice (the slicer
                CLI's silent fallback to "Cool Plate" is the bug we're
                fixing for STL / pure-3MF inputs). Default Textured PEI
                matches the factory plate on X1C / P1S / H2D; A1 owners
                flip to SuperTack once and localStorage persists. */}
            {/* Bed-type patches curr_bed_type onto the resolved process JSON,
                which the embedded-settings path never sends — so it has no
                effect there and is disabled rather than implying it does. */}
            <BedTypePicker
              value={bedType}
              onChange={(next) => {
                setBedTypeNote(null);
                setBedType(next);
              }}
              disabled={isEnqueuing || useEmbedded}
              slicer={sliceSlicer}
              note={bedTypeNote}
            />
            {/* "Slice as designed" (upstream #2611): honour the file's embedded
                settings instead of the picked process/filament. Offered only
                when the picked printer matches the design's target. */}
            {/* Layout passes the slicer runs on the geometry before slicing.
                Off by default: both move the user's objects, and arrange in
                particular is applied automatically on a cross-nozzle-class
                re-slice, so the box unions with that rather than governing
                it. */}
            <label className="flex items-start gap-2 text-sm text-bambu-gray cursor-pointer select-none">
              <input
                type="checkbox"
                checked={autoArrange}
                onChange={(e) => setAutoArrange(e.target.checked)}
                disabled={isEnqueuing}
                className="accent-bambu-green mt-0.5 cursor-pointer"
              />
              <span>
                {t('slice.autoArrange')}
                <span className="block text-xs text-bambu-gray/70">{t('slice.autoArrangeHint')}</span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm text-bambu-gray cursor-pointer select-none">
              <input
                type="checkbox"
                checked={autoOrient}
                onChange={(e) => setAutoOrient(e.target.checked)}
                disabled={isEnqueuing}
                className="accent-bambu-green mt-0.5 cursor-pointer"
              />
              <span>
                {t('slice.autoOrient')}
                <span className="block text-xs text-bambu-gray/70">{t('slice.autoOrientHint')}</span>
              </span>
            </label>
            {canUseEmbedded && (
              <label className="flex items-start gap-2 text-sm text-bambu-gray cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={useEmbedded}
                  onChange={(e) => setUseEmbedded(e.target.checked)}
                  disabled={isEnqueuing}
                  className="accent-bambu-green mt-0.5 cursor-pointer"
                />
                <span>
                  {t('slice.useEmbedded')}
                  <span className="block text-xs text-bambu-gray/70">
                    {t('slice.useEmbeddedHint')}
                  </span>
                </span>
              </label>
            )}
            {/* The designer's own process tweaks (#2622). BambuStudio records
                which keys deviate from the stock preset in the 3MF itself, so
                a re-slice for another printer can carry them instead of
                flattening them under --load-settings. Hidden entirely when
                the source lists none, and disabled in embedded mode, where
                the process JSON these patch is never sent at all. */}
            {designOverrides.length > 0 && (
              <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark/40 p-3">
                <button
                  type="button"
                  onClick={() => setDesignExpanded((v) => !v)}
                  className="flex w-full items-center justify-between gap-2 text-left"
                >
                  <span className="text-sm text-white">
                    {t('slice.designSettings')}
                    <span className="block text-xs text-bambu-gray/70">
                      {t('slice.designSettingsHint', { count: designOverrides.length })}
                    </span>
                  </span>
                  <span className="shrink-0 text-xs text-bambu-gray">
                    {t('slice.designSettingsSelected', { selected: designKeys.size, total: designOverrides.length })}
                  </span>
                </button>
                {designExpanded && (
                  <div className="mt-3 space-y-1.5 border-t border-bambu-dark-tertiary pt-3">
                    {designOverrides.map((o) => (
                      <label
                        key={o.key}
                        className={`flex items-start gap-2 text-xs ${useEmbedded ? 'opacity-50' : 'cursor-pointer'}`}
                      >
                        <input
                          type="checkbox"
                          checked={designKeys.has(o.key)}
                          disabled={isEnqueuing || useEmbedded}
                          onChange={(e) => {
                            setDesignKeys((prev) => {
                              const next = new Set(prev);
                              if (e.target.checked) next.add(o.key);
                              else next.delete(o.key);
                              return next;
                            });
                          }}
                          className="accent-bambu-green mt-0.5 shrink-0 cursor-pointer"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="font-mono text-bambu-gray">{o.key}</span>
                          <span className="ml-1.5 break-all text-white">{formatDesignValue(o.value)}</span>
                          {o.printer_coupled && (
                            <span
                              className="ml-1.5 rounded bg-amber-100 px-1 py-0.5 text-[10px] text-amber-700 dark:bg-amber-500/20 dark:text-amber-400"
                              title={t('slice.designSettingsPrinterCoupledHint')}
                            >
                              {t('slice.designSettingsPrinterCoupled')}
                            </span>
                          )}
                          {!o.printer_coupled && o.preset_defining && (
                            <span
                              className="ml-1.5 rounded bg-amber-100 px-1 py-0.5 text-[10px] text-amber-700 dark:bg-amber-500/20 dark:text-amber-400"
                              title={t('slice.designSettingsOverridesPresetHint')}
                            >
                              {t('slice.designSettingsOverridesPreset')}
                            </span>
                          )}
                        </span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            )}
            {/* Any manual re-pick retires the "bundle had missing presets"
                banner — it describes the state right after a load, and the
                user is now driving the slots by hand. */}
            <SliceFarmFilterToggles filters={farmFilters} disabled={isEnqueuing || useEmbedded} />
            {/* A choice made by hand that the listing no longer has was
                replaced by an automatic pick — said, never done in silence. */}
            {goneChoices.length > 0 && (
              <div
                className="flex items-start gap-2 text-xs rounded-md border p-2 border-amber-300 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200"
                role="status"
              >
                <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                <span>
                  {t('slice.chosenPresetGone', {
                    fields: goneChoices
                      .map((field) =>
                        field === 'process'
                          ? t('slice.process', 'Process profile')
                          : filamentSlots.length > 1
                            ? t('slice.filamentSlot', {
                                index: field + 1,
                                type: filamentSlots[field]?.type ?? '',
                                defaultValue: `Filament ${field + 1} (${filamentSlots[field]?.type ?? ''})`,
                              })
                            : t('slice.filament', 'Filament profile'),
                      )
                      .join(', '),
                    defaultValue:
                      'A profile you chose is no longer in the list, so another one was selected. Check: {{fields}}',
                  })}
                </span>
              </div>
            )}
            {/* Changing the printer by hand replaces no choice made by hand:
                a process or filament chosen for the old printer stays and is
                marked beside its field; only automatic picks follow. */}
            <PresetDropdown
              label={t('slice.printer', 'Printer profile')}
              slot="printer"
              data={presetsQuery.data}
              value={printerPreset}
              keep={farmFilters.keepPrinter}
              onChange={(ref) => {
                retireBundleNotes();
                setPrinterPreset(ref);
              }}
              // Locked in embedded mode too: the picked printer is unused on
              // the embedded-settings path, and changing it away from the
              // design's target would drop canUseEmbedded and yank the toggle
              // out from under the user.
              disabled={isEnqueuing || useEmbedded}
            />
            <PresetDropdown
              label={t('slice.process', 'Process profile')}
              slot="process"
              data={presetsQuery.data}
              value={processPreset}
              onChange={(ref) => {
                retireBundleNotes();
                explicitProcess.current = ref;
                setGoneChoices((gone) => gone.filter((g) => g !== 'process'));
                setProcessPreset(ref);
              }}
              disabled={isEnqueuing || useEmbedded}
              selectedPrinterName={selectedPrinterName}
              compatIndex={compatIndex}
            />
            {/* Filament reqs may need a server-side preview-slice for
                unsliced project files (single-pass, then cached). Show a
                scoped spinner so the user sees the printer/process
                dropdowns instead of an opaque "Loading presets…" wait. */}
            {filamentReqsQuery.isLoading ? (
              <FilamentAnalysisSpinner
                requestId={previewRequestId}
                sourceName={source.filename}
              />
            ) : (
              filamentSlots.map((slot, idx) => {
                // Slots flagged by the backend as not used by the
                // picked plate are auto-picked from project metadata
                // and disabled — the slicer CLI still needs a
                // profile per project slot, but the user shouldn't
                // have to think about slots their plate doesn't
                // paint with. used_in_plate defaults to true when
                // missing (sliced 3MFs and the no-flag legacy path).
                const isUsed = slot.used_in_plate !== false;
                const baseLabel =
                  filamentSlots.length > 1
                    ? t('slice.filamentSlot', {
                        index: idx + 1,
                        type: slot.type,
                        defaultValue: `Filament ${idx + 1} (${slot.type || ''})`,
                      })
                    : t('slice.filament', 'Filament profile');
                const label = isUsed
                  ? baseLabel
                  : `${baseLabel} ${t('slice.notUsedByPlate', '— not used by this plate')}`;
                return (
                  <PresetDropdown
                    key={`filament-${idx}`}
                    label={label}
                    slot="filament"
                    data={presetsQuery.data}
                    value={filamentPresets[idx] ?? null}
                    keep={farmFilters.keepFilament}
                    onChange={(ref) => {
                      if (ref) explicitFilamentSlots.current.set(idx, ref);
                      else explicitFilamentSlots.current.delete(idx);
                      setGoneChoices((gone) => gone.filter((g) => g !== idx));
                      retireBundleNotes();
                      setFilamentPresets((current) => {
                        const next = current.length === filamentSlots.length
                          ? [...current]
                          : filamentSlots.map((_, i) => current[i] ?? null);
                        next[idx] = ref;
                        return next;
                      });
                    }}
                    disabled={isEnqueuing || !isUsed || useEmbedded}
                    // Every filament row, single-slot ones included: an STL
                    // is exactly the source with no colour anywhere else to
                    // inherit.
                    swatchColor={filamentColours[idx] ?? slot.color}
                    swatchColorLabel={t('slice.filamentColour')}
                    onSwatchColorChange={(colour) =>
                      setFilamentColours((current) => {
                        const next = resizeColourOverrides([...current], filamentSlots.length);
                        next[idx] = colour;
                        return next;
                      })
                    }
                    selectedPrinterName={selectedPrinterName}
                    compatIndex={compatIndex}
                  />
                );
              })
            )}
              </div>

              {/* Right column: the settings panel. ⚠️ Kept on screen in
                  embedded mode but DISABLED rather than removed — nothing
                  here is sent on that path (the file's own settings drive
                  the slice), and dropping the column outright made the
                  dialog look like it had lost a feature whenever the
                  toggle was flipped. */}
              <div className="mt-4 lg:mt-0 min-w-0">
            {/* Process settings, mirroring OrcaSlicer's own Print Settings
                tabs. Collapsed by default: the common case is slicing with a
                preset as-is, and the full option set unfolded would bury the
                preset pickers above. Hidden entirely in embedded mode, where
                no process JSON is sent for these to patch. */}
            {(
              <div
                className={`rounded border border-bambu-dark-tertiary p-3 ${useEmbedded ? 'opacity-60' : ''}`}
              >
                <button
                  type="button"
                  onClick={() => setSettingsExpanded((v) => !v)}
                  aria-expanded={panelOpen}
                  disabled={isWideLayout}
                  className="flex w-full items-center justify-between gap-2 text-left lg:cursor-default"
                >
                  <span className="text-sm text-white">
                    {t('slice.processSettings')}
                    <span className="block text-xs text-bambu-gray/70">{t('slice.processSettingsHint')}</span>
                  </span>
                  <span className="shrink-0 text-xs text-bambu-gray">
                    {Object.keys(serializedProcessOverrides).length > 0
                      ? t('slice.processSettingsChanged', {
                          count: Object.keys(serializedProcessOverrides).length,
                        })
                      : t('slice.processSettingsUnchanged')}
                  </span>
                </button>
                {panelOpen && !useEmbedded && (
                  <div className="mt-3 border-t border-bambu-dark-tertiary pt-3">
                    <SlicerSettingsPanel
                      values={processOverrides}
                      onChange={(values, serialized) => {
                        setProcessOverrides(values);
                        setSerializedProcessOverrides(serialized);
                      }}
                      presetValues={presetValues}
                      presetValuesResolved={presetValuesResolved}
                      presetValuesReason={presetValuesReason}
                      disabled={isEnqueuing}
                      // The designer's settings and which of them are on — the
                      // same state as the list above, so a tick in either place
                      // is one tick. The panel's greying rules need it: a file
                      // value that is on is one the slice runs with (#2942).
                      sourceOverrides={designOverrides}
                      sourceSelected={designKeys}
                      onToggleSource={(key, on) =>
                        setDesignKeys((prev) => {
                          const next = new Set(prev);
                          if (on) next.add(key);
                          else next.delete(key);
                          return next;
                        })
                      }
                    />
                  </div>
                )}
              </div>
            )}
              </div>
            </div>
          </>
        )}

        {errorMessage && (
          <div className="text-sm text-red-700 dark:text-red-400 bg-red-900/20 border border-red-900/40 rounded p-2" role="alert">
            {errorMessage}
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex-shrink-0 flex justify-end gap-2 px-4 py-3 border-t border-bambu-dark-tertiary/40">
        <button
          type="button"
          onClick={onClose}
          disabled={isEnqueuing}
          className="px-3 py-1.5 text-sm rounded-md border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray transition-colors disabled:opacity-50"
        >
          {t('common.cancel', 'Cancel')}
        </button>
        <button
          type="button"
          onClick={() => {
            setErrorMessage(null);
            enqueueMutation.mutate();
          }}
          disabled={!isReady || isEnqueuing}
          className="px-3 py-1.5 text-sm rounded-md bg-bambu-green hover:bg-bambu-green/90 text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
        >
          {isEnqueuing ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('slice.enqueuing', 'Submitting slice job…')}
            </>
          ) : (
            t('slice.action', 'Slice')
          )}
        </button>
      </div>
    </Modal>
  );
}

function CloudStatusBanner({
  status,
  cloudName = 'bambu',
}: {
  status: SlicerCloudStatus;
  cloudName?: 'bambu' | 'orca';
}) {
  const { t } = useTranslation();
  // `ok` is the happy path. `not_authenticated` is silenced too: a user who
  // hasn't signed in (or has explicitly logged out — #1712) doesn't need a
  // permanent nag at the top of the modal; sign-in lives on the Profiles
  // page if they want it. Only `expired` and `unreachable` surface — those
  // are real breakage states a previously-signed-in user needs to see.
  if (status === 'ok' || status === 'not_authenticated') return null;

  // Same status vocabulary for both Bambu and Orca Cloud — only the
  // user-facing text varies. The fallbacks below name each cloud explicitly
  // so the banner makes sense without translation when i18n hasn't been
  // updated for a new locale.
  const messages =
    cloudName === 'orca'
      ? {
          expired: {
            key: 'slice.orcaCloud.expired',
            fallback: 'Orca Cloud session expired — sign in again to refresh your Orca presets.',
          },
          unreachable: {
            key: 'slice.orcaCloud.unreachable',
            fallback: 'Orca Cloud is unreachable right now. Other presets still work.',
          },
        }
      : {
          expired: {
            key: 'slice.cloud.expired',
            fallback: 'Bambu Cloud session expired — sign in again to refresh your cloud presets.',
          },
          unreachable: {
            key: 'slice.cloud.unreachable',
            fallback: 'Bambu Cloud is unreachable right now. Local and standard presets still work.',
          },
        };

  const tones: Record<'expired' | 'unreachable', { tone: string; icon: typeof Cloud }> = {
    expired: {
      tone: 'border-amber-300 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200',
      icon: CloudOff,
    },
    unreachable: {
      tone: 'border-bambu-dark-tertiary/40 bg-bambu-dark text-bambu-gray',
      icon: CloudOff,
    },
  };
  const { tone, icon: Icon } = tones[status];
  const { key, fallback } = messages[status];
  return (
    <div className={`flex items-start gap-2 text-xs rounded-md border p-2 ${tone}`} role="status">
      <Icon className="w-4 h-4 flex-shrink-0 mt-0.5" />
      <span>{t(key, fallback)}</span>
    </div>
  );
}
