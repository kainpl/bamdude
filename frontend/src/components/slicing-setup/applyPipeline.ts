// A saved set's application to one place (spec slicing-setup r5, R11): which
// fields it fills there, each as a ref, an unresolved entry (named, the current
// value stays) or untouched. Positional for filaments and bounded by the
// target's slots; the plate is checked against the slicer in effect (R8).
// Pure — the place turns the result into choice events and notes.
import type { BedType, PresetRef, SlicerPipeline, UnifiedPresetsResponse } from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { resolvePresetName, type Slot } from '../../utils/presetPickerUtils';
import { ALLOWED_BED_TYPES, ORCA_ONLY_BED_TYPES } from './bedPlates';

export type PipelineFields = { printer: boolean; process: boolean; plate: boolean; filaments: 'all' | 'first' | 'none' };

type Entry = { ref: PresetRef } | { unresolved: true };

export interface PipelineApplication {
  printer: Entry | null;
  process: Entry | null;
  /** Exactly `targetSlots` entries, by slot index; null = untouched. */
  filaments: (Entry | null)[];
  plate: { value: BedType } | { refused: 'unknown' | 'orcaOnly' } | null;
}

function entry(listing: UnifiedPresetsResponse, ref: PresetRef, slot: Slot): Entry {
  return resolvePresetName(listing, ref, slot) !== null ? { ref } : { unresolved: true };
}

export function applyPipeline(
  set: SlicerPipeline,
  listing: UnifiedPresetsResponse,
  fields: PipelineFields,
  slicer: SlicerKind | null,
  targetSlots: number,
): PipelineApplication {
  const filled = fields.filaments === 'none' ? 0 : fields.filaments === 'first' ? 1 : set.filament_presets.length;
  const filaments = Array.from({ length: targetSlots }, (_, i) =>
    i < filled && i < set.filament_presets.length ? entry(listing, set.filament_presets[i], 'filament') : null,
  );
  let plate: PipelineApplication['plate'] = null;
  if (fields.plate && set.bed_type) {
    if (!(ALLOWED_BED_TYPES as string[]).includes(set.bed_type)) plate = { refused: 'unknown' };
    else if (slicer === 'bambu_studio' && (ORCA_ONLY_BED_TYPES as string[]).includes(set.bed_type)) plate = { refused: 'orcaOnly' };
    else plate = { value: set.bed_type as BedType };
  }
  return {
    printer: fields.printer ? entry(listing, set.printer_preset, 'printer') : null,
    process: fields.process ? entry(listing, set.process_preset, 'process') : null,
    filaments,
    plate,
  };
}
