import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

export type SlicerKind = 'orcaslicer' | 'bambu_studio';

// Same queryKey shape used by the SliceModal's slicer-picker cards and
// the SlicerHealthIndicator visual component, so the React Query cache
// is shared across SettingsPage / SliceModal / SystemInfoPage. Backend
// caches the underlying /health probe for 30 s anyway — this just
// stops the surfaces flickering against each other on first render.
export function useSlicerHealth(slicer: SlicerKind, pollMs?: number) {
  return useQuery({
    queryKey: ['slicerHealth', slicer],
    queryFn: () => api.getSlicerHealth(slicer),
    staleTime: 30_000,
    refetchInterval: pollMs,
    retry: false,
  });
}
