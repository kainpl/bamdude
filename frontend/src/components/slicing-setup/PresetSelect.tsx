// The preset select of the slicing-setup family (spec slicing-setup r5, R2 /
// R3 / R9 / R12, §12). It draws a field built by `buildPresetField`: the
// source groups, the trailing "Other printers" group (each entry naming its
// source), search, the optional colour control beside it, and every note of
// the field — other printer, gone, relaxed filters with "Show all", nothing
// fits, not proven, another model — each tied to the select by
// aria-describedby. The markup is the Slice dialog's PresetDropdown.
import { useId } from 'react';
import { useTranslation } from 'react-i18next';

import type { PresetRef, UnifiedPreset } from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { presetGroupOf, type PresetGroup } from '../../utils/presetDisplayOrder';
import { fromRefValue, toRefValue } from '../../utils/presetPickerUtils';
import { colourInputValue } from '../../utils/sliceFilamentColours';
import { Select } from '../Select';
import { presetSourceFitsSlicer } from './presetScope';
import type { PresetField, SoftFilterId } from './presetSets';

const GROUP_LABELS: Record<PresetGroup, { key: string; fallback: string }> = {
  local: { key: 'slice.tier.local', fallback: 'Imported' },
  orca_cloud: { key: 'slice.tier.orcaCloud', fallback: 'Orca Cloud' },
  cloud_own: { key: 'slice.tier.cloudOwn', fallback: 'Bambu Cloud — my presets' },
  cloud: { key: 'slice.tier.cloud', fallback: 'Bambu Cloud' },
  standard: { key: 'slice.tier.standard', fallback: 'Standard' },
};

interface PresetSelectProps {
  label: string;
  field: PresetField;
  value: PresetRef | null;
  slicer?: SlicerKind | null;
  onChange: (ref: PresetRef | null) => void;
  disabled?: boolean;
  /** The chosen ref left the listing and blocks the action until a person picks. */
  gone?: boolean;
  /** Calibration preset page: the chosen printer is not proven for the physical printer (D7). */
  notProven?: boolean;
  /** Calibration verify page: the chosen printer is of another model; the file is for it. */
  otherModel?: boolean;
  /** Turns off, for good, the filter a relaxation note names. */
  onShowAll?: (id: SoftFilterId) => void;
  colour?: { value: string | null | undefined; onChange: (hex: string) => void; label: string };
}

type Note = { key: string; text: string; action?: { label: string; onClick: () => void } };

export function PresetSelect({
  label,
  field,
  value,
  slicer,
  onChange,
  disabled,
  gone,
  notProven,
  otherModel,
  onShowAll,
  colour,
}: PresetSelectProps) {
  const { t } = useTranslation();
  const selectId = useId();
  const colourId = `${selectId}-colour`;
  const groupLabel = (group: PresetGroup) => t(GROUP_LABELS[group].key, GROUP_LABELS[group].fallback);
  const entryCount = field.sections.reduce((sum, s) => sum + s.entries.length, 0) + field.others.length;
  const blocked = disabled || (field.boundEmpty && !field.boundFallback) || entryCount === 0;

  const notes: Note[] = [];
  const outsideSlicer = !!(slicer && value && !presetSourceFitsSlicer(value.source, slicer));
  if (outsideSlicer) notes.push({ key: 'slicer', text: t('slicingSetup.otherSlicer') });
  if (field.pinnedOutside === 'otherPrinter') {
    notes.push({ key: 'other', text: t('slice.presetForOtherPrinter', 'This profile is listed for another printer than the selected one.') });
  }
  if (gone) notes.push({ key: 'gone', text: t('slicingSetup.gone') });
  if (notProven) notes.push({ key: 'notProven', text: t('slicingSetup.notProven') });
  if (otherModel) notes.push({ key: 'otherModel', text: t('slicingSetup.otherModel') });
  if (field.noFit) notes.push({ key: 'noFit', text: t('slicingSetup.noFit') });
  for (const id of field.relaxed) {
    notes.push({
      key: `relaxed-${id}`,
      text: t(`slicingSetup.relaxed.${id}`),
      action: onShowAll ? { label: t('slicingSetup.showAll'), onClick: () => onShowAll(id) } : undefined,
    });
  }
  const noteId = (note: Note) => `${selectId}-note-${note.key}`;

  const option = (preset: UnifiedPreset, suffix?: string) => (
    <option key={`${preset.source}:${preset.id}`} value={`${preset.source}:${preset.id}`}>
      {suffix ? `${preset.name} · ${suffix}` : preset.name}
    </option>
  );

  return (
    <div className="block">
      <label htmlFor={selectId} className="flex items-center gap-2 text-xs text-bambu-gray mb-1">
        <span>{label}</span>
      </label>
      <div className="flex items-stretch gap-2">
        <Select
          id={selectId}
          className="flex-1 min-w-0"
          value={outsideSlicer ? '' : toRefValue(value)}
          onChange={(e) => onChange(fromRefValue(e.target.value))}
          disabled={blocked}
          aria-describedby={notes.length > 0 ? notes.map(noteId).join(' ') : undefined}
          search={{
            label: t('slice.searchPresets', { list: label, defaultValue: 'Search: {{list}}' }),
            placeholder: t('slice.searchPresetsPlaceholder', 'Search presets…'),
            noMatches: t('slice.noMatchingPresets', 'No matching presets.'),
          }}
        >
          <option value="">
            {entryCount === 0 ? t('slice.noPresetsForSlot', 'No presets available') : t('slice.selectPreset', '— Select a preset —')}
          </option>
          {field.sections.map((section) => (
            <optgroup key={section.group} label={groupLabel(section.group)}>
              {section.entries.map((preset) => option(preset))}
            </optgroup>
          ))}
          {field.others.length > 0 && (
            <optgroup label={t('slice.otherPrinters', 'Other printers')}>
              {field.others.map((preset) => option(preset, groupLabel(presetGroupOf(preset))))}
            </optgroup>
          )}
        </Select>
        {colour && (
          <label
            htmlFor={colourId}
            title={colour.label}
            // Pinned to the select's own row and height: with the search box
            // open the group beside it is two rows tall.
            className={`self-end h-9 min-h-[44px] md:min-h-0 flex items-center gap-2 px-2.5 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-sm ${
              disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer hover:border-bambu-gray transition-colors'
            }`}
          >
            <input
              id={colourId}
              type="color"
              value={colourInputValue(colour.value)}
              onChange={(e) => colour.onChange(e.target.value.toUpperCase())}
              disabled={disabled}
              aria-label={colour.label}
              style={{ backgroundColor: colourInputValue(colour.value) }}
              className="slice-colour-swatch w-4 h-4 shrink-0 rounded-full border border-bambu-dark-tertiary p-0 disabled:cursor-not-allowed enabled:cursor-pointer"
            />
            <span className="font-mono text-xs text-white tracking-tight">{colourInputValue(colour.value)}</span>
          </label>
        )}
      </div>
      {notes.map((note) => (
        <p key={note.key} id={noteId(note)} role="status" className="mt-1 text-xs text-amber-700 dark:text-amber-400">
          {note.text}
          {note.action && (
            <>
              {' '}
              <button
                type="button"
                onClick={note.action.onClick}
                className="underline underline-offset-2 hover:text-white transition-colors"
              >
                {note.action.label}
              </button>
            </>
          )}
        </p>
      ))}
    </div>
  );
}
