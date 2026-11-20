// The preset catalogue of the slicing-setup family (spec slicing-setup R10):
// one key family for every consumer, carrying the authorisation generation.
// A forced refresh writes its answer only into the generation it started in;
// a declined one hands over to the ordinary read of that generation.
import { useCallback, useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type UnifiedPresetsResponse } from '../../api/client';
import { presetContextGeneration } from '../../utils/presetContext';
import { usePresetAuthKey } from './authContext';

export interface PresetCatalogue {
  data?: UnifiedPresetsResponse;
  /** Which answer `data` is: the context key and the moment it was written.
   *  It changes with a new context AND with a refresh written under the same
   *  key, so a decision made on one answer can be told from the next. */
  revision: string | null;
  isLoading: boolean;
  isError: boolean;
  refresh: () => Promise<void>;
  refreshing: boolean;
}

const GENERATION_KEY = /^g\d+$/;
const CONTEXT_FAMILIES = new Set(['slicerPresets', 'slicerLoadedFilaments']);

export function usePresetCatalogue(opts?: { enabled?: boolean }): PresetCatalogue {
  const key = usePresetAuthKey();
  const queryClient = useQueryClient();
  const enabled = (opts?.enabled ?? true) && key !== null;
  const query = useQuery({
    queryKey: ['slicerPresets', key ?? 'pending'],
    queryFn: () => api.getSlicerPresets(),
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
      const fresh = await api.getSlicerPresets({ refresh: true });
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

  const data = key === null ? undefined : query.data;
  return {
    data,
    revision: data === undefined ? null : `${key}@${query.dataUpdatedAt}`,
    isLoading: enabled && query.isLoading,
    isError: query.isError,
    refresh,
    refreshing,
  };
}
