// The bed plates of the slicing-setup family (spec slicing-setup r5, R8). The
// six values of BambuStudio's / OrcaSlicer's `curr_bed_type`, the one only
// OrcaSlicer has, the default, and the note a plate that was not applied gets.
import type { BedType } from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';

export const ALLOWED_BED_TYPES: BedType[] = [
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
export const ORCA_ONLY_BED_TYPES: BedType[] = ['Textured Cool Plate'];

export const DEFAULT_BED_TYPE: BedType = 'Textured PEI Plate';

export function isBedType(value: unknown): value is BedType {
  return (ALLOWED_BED_TYPES as readonly unknown[]).includes(value);
}

export function bedPlateForSlicer(value: BedType, slicer: SlicerKind | null): BedType {
  return slicer === 'bambu_studio' && ORCA_ONLY_BED_TYPES.includes(value) ? DEFAULT_BED_TYPE : value;
}

/** The one remembered plate, shared by every place (the Slice dialog's existing key). */
export const BED_PLATE_MEMORY_KEY = 'bamdude:slice-modal:bed-type';

export function rememberedBedPlate(): BedType {
  try {
    const stored = typeof localStorage === 'undefined' ? null : localStorage.getItem(BED_PLATE_MEMORY_KEY);
    return isBedType(stored) ? stored : DEFAULT_BED_TYPE;
  } catch {
    return DEFAULT_BED_TYPE;
  }
}

// A plate that was not applied, said rather than dropped: `unknown` / `slicer`
// — a saved set named a plate nobody knows, or one the slicer lacks (the
// current plate stays); `reset` — the current plate is one the slicer lacks,
// and was changed to the default.
export type BedTypeNote = { kind: 'unknown' | 'slicer' | 'reset'; bed: string };
