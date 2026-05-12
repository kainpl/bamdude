import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import { api } from '../api/client';
import type { PrinterSettingsGetResponse, PrinterSettingsPostBody } from '../api/client';

const QK = (printerId: number) => ['printer-settings', printerId] as const;

/**
 * Printer Settings dialog data fetcher + mutator. Same shape as
 * useAmsSettings — per-flag 3 s client hold-timer mirrors the backend
 * hold so the UI doesn't blink between optimistic and confirmed values.
 */
export function usePrinterSettings(printerId: number, enabled: boolean = true) {
  const qc = useQueryClient();
  const holdsRef = useRef<Map<string, number>>(new Map());

  const query = useQuery<PrinterSettingsGetResponse>({
    queryKey: QK(printerId),
    queryFn: () => api.getPrinterSettings(printerId),
    enabled,
    staleTime: 5_000,
  });

  const mutation = useMutation({
    mutationFn: (body: PrinterSettingsPostBody) => api.postPrinterSettings(printerId, body),
    onSuccess: (_d, body) => {
      const now = Date.now();
      for (const flag of flagsForAction(body)) holdsRef.current.set(flag, now + 3_000);
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

function flagsForAction(body: PrinterSettingsPostBody): string[] {
  if (body.action === 'print_option_bool') return [body.key];
  if (body.action === 'print_option_int') return [body.key];
  if (body.action === 'xcam_control') return [body.module];
  if (body.action === 'camera_snapshot') return ['snapshot'];
  return [];
}
