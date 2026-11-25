// The slicer a place slices with (spec slicing-setup r5, R7): the only
// healthy one; else the person's remembered choice for this place; else the
// preferred slicer; unknown while health is still being read. Only a person's
// choice is remembered — a slicer forced because the other was offline is not
// (§4.4.6).
import { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSlicerHealth, type SlicerKind } from '../../hooks/useSlicerHealth';

const KINDS: SlicerKind[] = ['orcaslicer', 'bambu_studio'];
const asKind = (raw: unknown): SlicerKind | null => (KINDS.includes(raw as SlicerKind) ? (raw as SlicerKind) : null);

function read(key: string): SlicerKind | null {
  try {
    return typeof localStorage === 'undefined' ? null : asKind(localStorage.getItem(key));
  } catch {
    return null;
  }
}

export function useSlicerChoice(memoryKey: string | null, options?: { catalogueOnly?: boolean; enabled?: boolean }): {
  effective: SlicerKind | null;
  forPresets: SlicerKind | null;
  picked: SlicerKind | null;
  choose: (k: SlicerKind) => void;
} {
  const [picked, setPicked] = useState<SlicerKind | null>(() => memoryKey === null ? null : read(memoryKey));
  const enabled = options?.enabled ?? true;
  const catalogueOnly = options?.catalogueOnly ?? false;
  const orca = useSlicerHealth('orcaslicer', undefined, enabled && !catalogueOnly);
  const bambu = useSlicerHealth('bambu_studio', undefined, enabled && !catalogueOnly);
  const settings = useQuery({ queryKey: ['ui-preferences'], queryFn: api.getUiPreferences, staleTime: 60_000, enabled });
  const preferred = asKind(settings.data?.preferred_slicer);

  const choose = useCallback(
    (kind: SlicerKind) => {
      setPicked(kind);
      try {
        if (memoryKey !== null && typeof localStorage !== 'undefined') localStorage.setItem(memoryKey, kind);
      } catch {
        /* private mode / quota — the choice lives for this form */
      }
    },
    [memoryKey],
  );

  const preferredOrDefault = picked ?? preferred ?? (settings.isPending ? null : 'bambu_studio');
  let effective: SlicerKind | null = null;
  let forPresets: SlicerKind | null = null;
  if (enabled && catalogueOnly) {
    effective = forPresets = preferredOrDefault;
  } else if (enabled && !orca.isLoading && !bambu.isLoading) {
    const orcaUp = orca.data?.healthy === true;
    const bambuUp = bambu.data?.healthy === true;
    if (orcaUp && !bambuUp) effective = 'orcaslicer';
    else if (bambuUp && !orcaUp) effective = 'bambu_studio';
    else if (orcaUp && bambuUp) effective = preferredOrDefault;
    else effective = picked ?? preferred;
    forPresets = effective ?? preferredOrDefault;
  }
  return { effective, forPresets, picked, choose };
}
