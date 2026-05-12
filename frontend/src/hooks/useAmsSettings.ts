import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import { api } from '../api/client';
import type { AmsSettingsGetResponse, AmsSettingsPostBody } from '../api/client';

const QK = (printerId: number) => ['ams-settings', printerId] as const;

/**
 * AMS Settings dialog data fetcher + mutator.
 *
 * Client-side hold-timer: when we POST a change, we record which flag was
 * touched and for how long (3 s). The modal reads this map and prefers its
 * own optimistic value over WS-driven refetches during the hold window —
 * mirrors the backend hold so the UI doesn't blink.
 */
export function useAmsSettings(printerId: number, enabled: boolean = true) {
  const qc = useQueryClient();
  const holdsRef = useRef<Map<string, number>>(new Map());

  const query = useQuery<AmsSettingsGetResponse>({
    queryKey: QK(printerId),
    queryFn: () => api.getAmsSettings(printerId),
    enabled,
    staleTime: 5_000,
  });

  const mutation = useMutation({
    mutationFn: (body: AmsSettingsPostBody) => api.postAmsSettings(printerId, body),
    onSuccess: (_data, body) => {
      const now = Date.now();
      for (const flag of flagsForAction(body)) {
        holdsRef.current.set(flag, now + 3_000);
      }
      qc.invalidateQueries({ queryKey: QK(printerId) });
    },
  });

  const isHeld = useCallback((flag: string) => {
    const deadline = holdsRef.current.get(flag);
    return deadline != null && deadline > Date.now();
  }, []);

  return {
    data: query.data,
    isLoading: query.isLoading,
    error: query.error,
    refetch: query.refetch,
    mutate: mutation.mutateAsync,
    isMutating: mutation.isPending,
    isHeld,
  };
}

function flagsForAction(body: AmsSettingsPostBody): string[] {
  switch (body.action) {
    case 'user_setting':
      return ['insertion_update', 'power_on_update', 'remain_capacity'];
    case 'auto_switch_filament':
      return ['auto_switch_filament'];
    case 'air_print_detect':
      return ['air_print_detect'];
    default:
      return [];
  }
}
