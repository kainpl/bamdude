// The Slice dialog's two farm filters: "only printers that are online" and
// "only filaments that are loaded" (owner's decision 7; upstream 7f00816f7 /
// 8a95eb9f7 as the behaviour reference). The rules are in
// `utils/sliceFarmFilter`; this hook holds the two switches, their memory and
// the one read they share (`components/SliceFarmFilters` draws them).
//
// ⚠️ Display only. A filter narrows what a list OFFERS; it never picks,
// replaces or clears a preset — the dropdown keeps the chosen one listed even
// when the filter would hide it — and the dialog's auto-picks do not read it.
// A filter that cannot be answered (the read failed, or has not arrived) hides
// nothing. One that WAS answered and cannot be answered again keeps narrowing
// by the last good answer and says that it may be out of date (`stale`) —
// never "every profile is shown" beside a list that is still narrowed.
//
// The answer is read once per dialog; `useWebSocket` marks it stale when a
// printer connects or disconnects and when a spool is assigned or unassigned,
// which refetches it while a switch is on.
import { useCallback, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api, type UnifiedPreset, type UnifiedPresetsBySlot, type UnifiedPresetsResponse } from '../api/client';
import type { PrinterParents } from '../utils/printerPresetParents';
import { filamentPresetIsLoaded, printerPresetIsOnline, printersCountedFor } from '../utils/sliceFarmFilter';

const STORAGE_KEYS = {
  online: 'bamdude:slice-modal:only-online-printers',
  loaded: 'bamdude:slice-modal:only-loaded-filaments',
} as const;

const TIERS = ['local', 'orca_cloud', 'cloud', 'standard'] as const;

function readFlag(key: string): boolean {
  if (typeof localStorage === 'undefined') return false;
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function writeFlag(key: string, value: boolean): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(key, value ? '1' : '0');
  } catch {
    /* private mode / quota — the switch still works for this dialog */
  }
}

/** A switch remembered per browser; off until the user turns it on. */
function useStoredFlag(key: string): [boolean, (next: boolean) => void] {
  const [value, setValue] = useState<boolean>(() => readFlag(key));
  const set = useCallback(
    (next: boolean) => {
      setValue(next);
      writeFlag(key, next);
    },
    [key],
  );
  return [value, set];
}

export type PresetKeep = (preset: UnifiedPreset) => boolean;

export interface SliceFarmFilters {
  onlyOnline: boolean;
  setOnlyOnline: (next: boolean) => void;
  onlyLoaded: boolean;
  setOnlyLoaded: (next: boolean) => void;
  /** Which printer presets the list offers; undefined = no filter applied. */
  keepPrinter: PresetKeep | undefined;
  /** Which filament presets the lists offer; undefined = no filter applied. */
  keepFilament: PresetKeep | undefined;
  /** Why the printer list is empty under the filter, when it is. */
  printerNote: string | null;
  /** Why the filament lists are empty under the filter, when they are. */
  filamentNote: string | null;
  /** The farm could not be read: nothing is filtered. */
  unavailable: boolean;
  /** The farm could not be read AGAIN: the lists are narrowed by the last good answer. */
  stale: boolean;
  /** Read the farm again. */
  retry: () => void;
}

export function useSliceFarmFilters(
  presets: UnifiedPresetsResponse | undefined,
  selectedPrinterName: string | null,
  printerParents: PrinterParents,
): SliceFarmFilters {
  const { t } = useTranslation();
  const [onlyOnline, setOnlyOnline] = useStoredFlag(STORAGE_KEYS.online);
  const [onlyLoaded, setOnlyLoaded] = useStoredFlag(STORAGE_KEYS.loaded);

  // One read for both switches, and none while both are off.
  const farmQuery = useQuery({
    queryKey: ['slicerLoadedFilaments'],
    queryFn: api.getSlicerLoadedFilaments,
    enabled: onlyOnline || onlyLoaded,
    staleTime: 15_000,
  });
  const farm = farmQuery.data;
  const { refetch } = farmQuery;
  const retry = useCallback(() => {
    void refetch();
  }, [refetch]);

  return useMemo<SliceFarmFilters>(() => {
    const all = (slot: keyof UnifiedPresetsBySlot): UnifiedPreset[] =>
      presets ? TIERS.flatMap((tier) => presets[tier][slot]) : [];
    let keepPrinter: PresetKeep | undefined;
    let keepFilament: PresetKeep | undefined;
    let printerNote: string | null = null;
    let filamentNote: string | null = null;

    if (farm && onlyOnline) {
      keepPrinter = (preset) => printerPresetIsOnline(preset, farm, printerParents);
      if (presets && !all('printer').some(keepPrinter)) {
        printerNote = farm.printers.length === 0 ? t('slice.farmFilter.noOnlinePrinters') : t('slice.farmFilter.noOnlinePresets');
      }
    }
    if (farm && onlyLoaded) {
      const counted = printersCountedFor(farm, selectedPrinterName, printerParents);
      const loaded = counted.printers.flatMap((p) => p.filaments);
      keepFilament = (preset) => filamentPresetIsLoaded(preset, loaded, farm.families);
      if (presets && !all('filament').some(keepFilament)) {
        // Three different reasons, three sentences: nobody of the chosen model
        // is online; they are online and empty; something is loaded that no
        // listed profile matches.
        filamentNote =
          loaded.length > 0
            ? t('slice.farmFilter.noLoadedPresets')
            : !counted.byModel
              ? t('slice.farmFilter.nothingLoaded')
              : counted.printers.length === 0
                ? t('slice.farmFilter.noOnlineOfModel')
                : t('slice.farmFilter.nothingLoadedOnModel');
      }
    }
    return {
      onlyOnline,
      setOnlyOnline,
      onlyLoaded,
      setOnlyLoaded,
      keepPrinter,
      keepFilament,
      printerNote,
      filamentNote,
      unavailable: (onlyOnline || onlyLoaded) && farmQuery.isError && !farm,
      stale: (onlyOnline || onlyLoaded) && farmQuery.isError && Boolean(farm),
      retry,
    };
  }, [
    farm,
    farmQuery.isError,
    onlyOnline,
    onlyLoaded,
    presets,
    printerParents,
    retry,
    selectedPrinterName,
    setOnlyOnline,
    setOnlyLoaded,
    t,
  ]);
}
