// The bed plate of one form (spec slicing-setup r5, R8): the form keeps its
// own value; the one remembered plate is written only by a person's or a
// set's choice, never by the reset the form makes for Bambu Studio.
import { useCallback, useEffect, useState } from 'react';
import type { BedType } from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { BED_PLATE_MEMORY_KEY, bedPlateForSlicer, isBedType, rememberedBedPlate, type BedTypeNote } from './bedPlates';

function remember(bed: BedType): void {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(BED_PLATE_MEMORY_KEY, bed);
  } catch {
    /* private mode / quota — the form keeps its value */
  }
}

export type BedPlateApplication = { result: 'applied' | 'unchanged' } | { result: 'refused'; reason: 'unknown' | 'orcaOnly' };

export function useBedPlate(
  effective: SlicerKind | null,
  options?: { initial?: string | null; selection?: { value: BedType; onChange: (bed: BedType) => void } },
): {
  value: BedType;
  note: BedTypeNote | null;
  choose: (bed: BedType) => void;
  applyFromSet: (bed: string | null) => BedPlateApplication;
} {
  const [remembered] = useState(rememberedBedPlate);
  const [picked, setPicked] = useState<BedType | null>(null);
  const [refusal, setRefusal] = useState<BedTypeNote | null>(null);
  const { initial, selection } = options ?? {};
  const raw = selection?.value ?? picked ?? (isBedType(initial) ? initial : remembered);
  const value = bedPlateForSlicer(raw, effective);
  const note = refusal ?? (value !== raw ? { kind: 'reset' as const, bed: raw } : null);

  useEffect(() => {
    if (effective === 'orcaslicer') setRefusal((current) => current?.kind === 'slicer' ? null : current);
  }, [effective]);

  const setSelection = selection?.onChange ?? setPicked;
  const choose = useCallback((bed: BedType) => {
    setRefusal(null);
    setSelection(bed);
    remember(bed);
  }, [setSelection]);

  const applyFromSet = useCallback(
    (bed: string | null): BedPlateApplication => {
      if (!bed) {
        setRefusal(null);
        return { result: 'unchanged' };
      }
      if (!isBedType(bed)) {
        setRefusal({ kind: 'unknown', bed });
        return { result: 'refused', reason: 'unknown' };
      }
      if (bedPlateForSlicer(bed, effective) !== bed) {
        setRefusal({ kind: 'slicer', bed });
        return { result: 'refused', reason: 'orcaOnly' };
      }
      choose(bed);
      return bed === value ? { result: 'unchanged' } : { result: 'applied' };
    },
    [choose, effective, value],
  );

  return { value, note, choose, applyFromSet };
}
