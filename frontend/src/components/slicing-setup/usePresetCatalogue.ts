// The preset catalogue of the slicing-setup family (spec slicing-setup R10):
// one key family for every consumer, carrying the authorisation generation.
// A forced refresh writes its answer only into the generation it started in;
// a declined one hands over to the ordinary read of that generation.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { api, ApiError, type UnifiedPreset, type UnifiedPresetsResponse } from '../../api/client';
import { isOwnBambuCloudId } from '../../utils/presetPickerUtils';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { presetContextGeneration } from '../../utils/presetContext';
import { usePresetAuthKey } from './authContext';
import { scopePresetCatalogue } from './presetScope';

export interface PresetCatalogue {
  data?: UnifiedPresetsResponse;
  /** Full answer for distinguishing a hidden preset from one that was deleted. */
  rawData?: UnifiedPresetsResponse;
  /** Which answer `data` is: the context key and the answer's own number.
   *  It changes with a new context AND with any different answer under the
   *  same key (a refresh), so a decision made on one answer can be told from
   *  the next — never a timestamp, which two writes may share. */
  revision: string | null;
  isLoading: boolean;
  isError: boolean;
  refresh: () => Promise<void>;
  refreshing: boolean;
}

const GENERATION_KEY = /^g\d+$/;

// Each distinct answer object gets its own number. TanStack hands back the
// SAME object for an equal answer (structural sharing) and a new one for any
// change, so the number tells answers apart where a timestamp may not: two
// writes can share a millisecond (review J-1).
const answerIds = new WeakMap<object, number>();
let lastAnswerId = 0;
function answerId(answer: object): number {
  let id = answerIds.get(answer);
  if (id === undefined) {
    lastAnswerId += 1;
    id = lastAnswerId;
    answerIds.set(answer, id);
  }
  return id;
}
const CLOUD_DETAIL_FAMILY = 'slicerPresetCloudCompatibility';
const CONTEXT_FAMILIES = new Set(['slicerPresets', 'slicerLoadedFilaments', CLOUD_DETAIL_FAMILY]);
let nextCloudDetailStart = 0;

function compatiblePrinterNames(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.filter((name): name is string => typeof name === 'string').map((name) => name.trim()).filter(Boolean);
  }
  if (typeof value !== 'string' || !value.trim()) return [];
  const text = value.trim();
  if (!/[;"]/.test(text)) return [text];
  // Bambu's detail API also uses the slicer's list encoding, e.g.
  // "Bambu Lab A1 mini (0.6mm)";"Bambu Lab A1 mini 0.6 nozzle".
  const entries = /\s*("(?:[^"\\]|\\.)*")\s*(;|$)/gy;
  const names: string[] = [];
  let end = 0;
  try {
    for (const entry of text.matchAll(entries)) {
      names.push((JSON.parse(entry[1]) as string).trim());
      end = entry.index + entry[0].length;
    }
  } catch {
    return [];
  }
  return end === text.length && !text.endsWith(';') ? names.filter(Boolean) : [];
}

// The cloud listing can omit every base ID and printer list. Read the thin
// private entries through the existing authenticated detail endpoint. A single
// serial pass per catalogue read keeps Bambu's per-token detail limit in view.
async function readPresetCatalogue(
  queryClient: QueryClient,
  key: string,
  refresh: boolean,
  signal?: AbortSignal,
): Promise<UnifiedPresetsResponse> {
  const generation = presetContextGeneration();
  const sameContext = () => key === `g${generation}` && presetContextGeneration() === generation;
  const current = () => !signal?.aborted && sameContext();
  const catalogue = await (refresh ? api.getSlicerPresets({ refresh: true }) : api.getSlicerPresets());
  if (catalogue.cloud_status !== 'ok' || !current()) return catalogue;

  const cloud = { ...catalogue.cloud };
  for (const slot of ['printer', 'process', 'filament'] as const) {
    cloud[slot] = [...cloud[slot]];
    for (const [index, preset] of cloud[slot].entries()) {
      if (!current()) return catalogue;
      const hasDetails = slot === 'printer' ? preset.inherits : preset.compatible_printers?.length;
      if (!isOwnBambuCloudId(preset.id) || hasDetails) continue;
      try {
        const detail = await queryClient.fetchQuery({
          queryKey: [CLOUD_DETAIL_FAMILY, key, preset.id],
          queryFn: async () => {
            // Bambu limits details to about ten requests per second per token.
            // Pace starts even if a refresh overlaps an ordinary read.
            const now = performance.now();
            const start = Math.max(now, nextCloudDetailStart);
            nextCloudDetailStart = start + 125;
            if (start > now) await new Promise((resolve) => setTimeout(resolve, start - now));
            if (!sameContext()) throw new Error('Preset authorisation changed');
            const answer = await api.getCloudSettingDetail(preset.id);
            const inherits = answer.setting?.inherits;
            const baseId = answer.base_id && answer.base_id !== preset.id ? answer.base_id : null;
            const compatible = compatiblePrinterNames(answer.setting?.compatible_printers);
            const compatibility: Partial<UnifiedPreset> = {};
            if (typeof inherits === 'string' && inherits.trim() && (inherits.trim() !== answer.name || baseId)) {
              compatibility.inherits = inherits.trim();
              compatibility.inherits_id = baseId;
            }
            if (compatible.length) compatibility.compatible_printers = compatible;
            return { name: answer.name, compatibility };
          },
          staleTime: refresh ? 0 : 5 * 60_000,
          retry: false,
        });
        if (detail.name === preset.name) cloud[slot][index] = { ...preset, ...detail.compatibility };
      } catch (error) {
        // A deleted preset can race its listing. Connection, permission or
        // quota refusals leave the remaining entries on listing evidence.
        if (!(error instanceof ApiError && error.status === 404)) return { ...catalogue, cloud };
      }
    }
  }
  return current() ? { ...catalogue, cloud } : catalogue;
}

export function usePresetCatalogue(opts?: { enabled?: boolean; slicer?: SlicerKind | null }): PresetCatalogue {
  const key = usePresetAuthKey();
  const queryClient = useQueryClient();
  const enabled = (opts?.enabled ?? true) && key !== null;
  const query = useQuery({
    queryKey: ['slicerPresets', key ?? 'pending'],
    queryFn: ({ signal }) => readPresetCatalogue(queryClient, key ?? 'pending', false, signal),
    enabled,
    staleTime: 30_000,
  });

  // A new context drops what older ones read — only generation-keyed entries;
  // the bare keys of consumers not yet on this family are theirs.
  useEffect(() => {
    if (key === null) return;
    queryClient.removeQueries({
      predicate: (q) => {
        const [family, generation] = q.queryKey;
        return (
          typeof family === 'string' &&
          CONTEXT_FAMILIES.has(family) &&
          typeof generation === 'string' &&
          GENERATION_KEY.test(generation) &&
          generation !== key
        );
      },
    });
  }, [key, queryClient]);

  const [refreshing, setRefreshing] = useState(false);
  const refresh = useCallback(async () => {
    if (key === null) return;
    const started = presetContextGeneration();
    setRefreshing(true);
    try {
      const fresh = await readPresetCatalogue(queryClient, key, true);
      if (presetContextGeneration() === started) queryClient.setQueryData(['slicerPresets', key], fresh);
    } catch {
      // A declined forced read hands over to the ordinary query of the SAME,
      // still current key: it reads again, and shows its own error state when
      // that fails too (review I-5). A stale refresh touches no newer context.
      if (presetContextGeneration() === started) {
        await queryClient.invalidateQueries({ queryKey: ['slicerPresets', key] });
      }
    } finally {
      setRefreshing(false);
    }
  }, [key, queryClient]);

  const rawData = key === null ? undefined : query.data;
  const slicer = opts?.slicer;
  const data = useMemo(() => {
    if (!rawData || slicer === null) return undefined;
    return slicer === undefined ? rawData : scopePresetCatalogue(rawData, slicer);
  }, [rawData, slicer]);
  return {
    data,
    rawData,
    revision: data === undefined || rawData === undefined ? null : `${key}#${answerId(rawData)}${slicer ? `:${slicer}` : ''}`,
    isLoading: enabled && (query.isLoading || slicer === null),
    isError: query.isError,
    refresh,
    refreshing,
  };
}
