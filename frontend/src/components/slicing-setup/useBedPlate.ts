// The bed plate of one form (spec slicing-setup r5, R8): the form keeps its
// own value; the one remembered plate is written only by a person's or a
// set's choice, never by the reset the form makes for Bambu Studio.
import { useCallback, useEffect, useState } from 'react';
import type { BedType } from '../../api/client';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { ALLOWED_BED_TYPES, BED_PLATE_MEMORY_KEY, DEFAULT_BED_TYPE, ORCA_ONLY_BED_TYPES, type BedTypeNote } from './bedPlates';

function remembered(): BedType {
  try {
    const stored = typeof localStorage === 'undefined' ? null : localStorage.getItem(BED_PLATE_MEMORY_KEY);
    return (ALLOWED_BED_TYPES as string[]).includes(stored ?? '') ? (stored as BedType) : DEFAULT_BED_TYPE;
  } catch {
    return DEFAULT_BED_TYPE;
  }
}

function remember(bed: BedType): void {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(BED_PLATE_MEMORY_KEY, bed);
  } catch {
    /* private mode / quota — the form keeps its value */
  }
}

export type BedPlateApplication = { result: 'applied' | 'unchanged' } | { result: 'refused'; reason: 'unknown' | 'orcaOnly' };

export function useBedPlate(effective: SlicerKind | null): {
  value: BedType;
  note: BedTypeNote | null;
  choose: (bed: BedType) => void;
  applyFromSet: (bed: string | null) => BedPlateApplication;
} {
  const [value, setValue] = useState<BedType>(remembered);
  const [note, setNote] = useState<BedTypeNote | null>(null);

  // A plate only OrcaSlicer has cannot stay picked for Bambu Studio; it is
  // changed to the default and said so — and put back when OrcaSlicer returns.
  useEffect(() => {
    if (effective !== 'bambu_studio' || !ORCA_ONLY_BED_TYPES.includes(value)) return;
    setNote({ kind: 'reset', bed: value });
    setValue(DEFAULT_BED_TYPE);
  }, [effective, value]);
  useEffect(() => {
    if (effective !== 'orcaslicer' || note === null || note.kind === 'unknown') return;
    if (note.kind === 'reset' && (ALLOWED_BED_TYPES as string[]).includes(note.bed)) setValue(note.bed as BedType);
    setNote(null);
  }, [effective, note]);

  const choose = useCallback((bed: BedType) => {
    setNote(null);
    setValue(bed);
    remember(bed);
  }, []);

  const applyFromSet = useCallback(
    (bed: string | null): BedPlateApplication => {
      if (!bed) {
        setNote((current) => (current && current.kind !== 'reset' ? null : current));
        return { result: 'unchanged' };
      }
      if (!(ALLOWED_BED_TYPES as string[]).includes(bed)) {
        setNote({ kind: 'unknown', bed });
        return { result: 'refused', reason: 'unknown' };
      }
      if (effective === 'bambu_studio' && (ORCA_ONLY_BED_TYPES as string[]).includes(bed)) {
        setNote({ kind: 'slicer', bed });
        return { result: 'refused', reason: 'orcaOnly' };
      }
      setNote(null);
      setValue(bed as BedType);
      remember(bed as BedType);
      return bed === value ? { result: 'unchanged' } : { result: 'applied' };
    },
    [effective, value],
  );

  return { value, note, choose, applyFromSet };
}
