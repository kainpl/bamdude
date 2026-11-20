// The filters of the slicing-setup family (spec slicing-setup r5, R2 / D4):
// the source filter (all / mine / built-in), "Only models of my farm" and
// "Only loaded filaments", all three remembered per browser and shared by
// every mounted consumer, and the one farm read both farm switches ask.
//
// A filter narrows what a list shows; it steers automatic picks only while its
// answer is current — a stale farm answer still narrows the list, but its
// `pickKeep` is null. A read that never answered narrows nothing.
import { useCallback, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ApiError, api, type PresetSource } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import type { LoadedFilamentsResponse } from '../../api/client';
import { matchesSourceFilter, type PresetSourceFilter, type Slot } from '../../utils/presetPickerUtils';
import type { PrinterParents } from '../../utils/printerPresetParents';
import { filamentPresetIsLoaded, loadedFilamentsFor, printerPresetFitsFarmPrinter } from '../../utils/sliceFarmFilter';
import { usePresetAuthKey } from './authContext';
import { FARM_MODELS_KEY, LEGACY_ONLINE_KEY, LOADED_KEY, SOURCE_FILTER_KEY, useSharedChoice, useSharedFlag } from './memory';
import type { SoftFilter, SoftFilterId } from './presetSets';

export interface PresetFiltersOptions {
  source?: boolean;
  farmModels?: boolean;
  loaded?: boolean;
  /** 'always' reads the farm with every switch off (calibration's family facts). */
  farmRead?: 'switches' | 'always';
}

export type FarmStatus = 'off' | 'pending' | 'ready' | 'stale' | 'unavailable' | 'forbidden';

export interface SoftFilterContext {
  slot: Slot;
  printerName: string | null;
  parentsBySource: Partial<Record<PresetSource, PrinterParents | null>>;
}

export interface PresetFiltersState {
  source: PresetSourceFilter;
  setSource: (f: PresetSourceFilter) => void;
  onlyFarmModels: boolean;
  setOnlyFarmModels: (on: boolean) => void;
  onlyLoaded: boolean;
  setOnlyLoaded: (on: boolean) => void;
  farm: LoadedFilamentsResponse | undefined;
  farmStatus: FarmStatus;
  retry: () => void;
  /** library:upload + printers:read — what the farm read asks. */
  offered: boolean;
  softFilters: (ctx: SoftFilterContext) => SoftFilter[];
  /** "Show all" in a relaxation note: turns that one filter off, for good. */
  turnOff: (id: SoftFilterId) => void;
}

const SOURCE_VALUES = ['all', 'mine', 'builtin'] as const;

export function usePresetFilters(opts: PresetFiltersOptions): PresetFiltersState {
  const { hasPermission } = useAuth();
  const offered = hasPermission('library:upload') && hasPermission('printers:read');
  const [source, setSource] = useSharedChoice<PresetSourceFilter>(SOURCE_FILTER_KEY, SOURCE_VALUES, 'mine');
  const [onlyFarmModels, setOnlyFarmModels] = useSharedFlag(FARM_MODELS_KEY, LEGACY_ONLINE_KEY);
  const [onlyLoaded, setOnlyLoaded] = useSharedFlag(LOADED_KEY);
  const key = usePresetAuthKey();

  const switchesOn = (Boolean(opts.farmModels) && onlyFarmModels) || (Boolean(opts.loaded) && onlyLoaded);
  const wanted = opts.farmRead === 'always' || switchesOn;
  const enabled = offered && key !== null && wanted;
  const farmQuery = useQuery({
    queryKey: ['slicerLoadedFilaments', key ?? 'pending'],
    queryFn: api.getSlicerLoadedFilaments,
    enabled,
    staleTime: 15_000,
  });

  // A refusal is an answer: whatever was read before is no longer this user's.
  const forbidden = farmQuery.error instanceof ApiError && farmQuery.error.status === 403;
  const farm = enabled && !forbidden ? farmQuery.data : undefined;
  const failed = farmQuery.isError && !forbidden;
  const farmStatus: FarmStatus = !enabled
    ? 'off'
    : forbidden
      ? 'forbidden'
      : farm && failed
        ? 'stale'
        : farm
          ? 'ready'
          : failed
            ? 'unavailable'
            : 'pending';

  const { refetch } = farmQuery;
  const retry = useCallback(() => {
    // An imperative refetch would start a query the null key disabled.
    if (key !== null && enabled) void refetch();
  }, [enabled, key, refetch]);

  const softFilters = useCallback(
    (ctx: SoftFilterContext): SoftFilter[] => {
      const out: SoftFilter[] = [];
      if (opts.source) {
        const keep = (p: Parameters<SoftFilter['keep']>[0]) => matchesSourceFilter(p, source);
        out.push({ id: 'source', keep, pickKeep: keep });
      }
      const steer = farmStatus === 'ready';
      if (ctx.slot === 'printer' && opts.farmModels && onlyFarmModels && farm?.farm_printers) {
        const machines = farm.farm_printers;
        const keep = (p: Parameters<SoftFilter['keep']>[0]) =>
          machines.some((m) => printerPresetFitsFarmPrinter(p, m, ctx.parentsBySource[p.source] ?? null));
        out.push({ id: 'farm', keep, pickKeep: steer ? keep : null });
      }
      if (ctx.slot === 'filament' && opts.loaded && onlyLoaded && farm) {
        const parents = Object.assign({}, ...Object.values(ctx.parentsBySource).filter(Boolean)) as PrinterParents;
        const loaded = loadedFilamentsFor(farm, ctx.printerName, parents);
        const keep = (p: Parameters<SoftFilter['keep']>[0]) => filamentPresetIsLoaded(p, loaded, farm.families);
        out.push({ id: 'loaded', keep, pickKeep: steer ? keep : null });
      }
      return out;
    },
    [farm, farmStatus, onlyFarmModels, onlyLoaded, opts.farmModels, opts.loaded, opts.source, source],
  );

  const turnOff = useCallback(
    (id: SoftFilterId) => {
      if (id === 'source') setSource('all');
      else if (id === 'farm') setOnlyFarmModels(false);
      else setOnlyLoaded(false);
    },
    [setSource, setOnlyFarmModels, setOnlyLoaded],
  );

  return useMemo(
    () => ({
      source,
      setSource,
      onlyFarmModels,
      setOnlyFarmModels,
      onlyLoaded,
      setOnlyLoaded,
      farm,
      farmStatus,
      retry,
      offered,
      softFilters,
      turnOff,
    }),
    [farm, farmStatus, offered, onlyFarmModels, onlyLoaded, retry, setOnlyFarmModels, setOnlyLoaded, setSource, softFilters, source, turnOff],
  );
}
