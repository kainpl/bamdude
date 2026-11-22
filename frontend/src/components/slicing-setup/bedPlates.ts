// The bed plates of the slicing-setup family (spec slicing-setup r5, R8). The
// six values of BambuStudio's / OrcaSlicer's `curr_bed_type`, the one only
// OrcaSlicer has, the default, and the note a plate that was not applied gets.
import type { BedType } from '../../api/client';

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

/** The one remembered plate, shared by every place (the Slice dialog's existing key). */
export const BED_PLATE_MEMORY_KEY = 'bamdude:slice-modal:bed-type';

// A plate that was not applied, said rather than dropped: `unknown` / `slicer`
// — a saved set named a plate nobody knows, or one the slicer lacks (the
// current plate stays); `reset` — the current plate is one the slicer lacks,
// and was changed to the default.
export type BedTypeNote = { kind: 'unknown' | 'slicer' | 'reset'; bed: string };
