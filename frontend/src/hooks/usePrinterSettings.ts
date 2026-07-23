import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef, useState } from 'react';

import { api } from '../api/client';
import type { PrinterSettingsGetResponse, PrinterSettingsPostBody } from '../api/client';

const QK = (printerId: number) => ['printer-settings', printerId] as const;

// How long a control stays frozen + shows the "waiting for confirmation" spinner
// after a toggle. Matches the backend/optimistic 3 s hold, with a little slack for
// the MQTT round-trip so the confirmed value has landed by the time we release.
const PENDING_MS = 3_500;

/**
 * Printer Settings dialog data fetcher + mutator. Same shape as
 * useAmsSettings — per-flag 3 s client hold-timer mirrors the backend
 * hold so the UI doesn't blink between optimistic and confirmed values.
 * `submit` freezes the touched control(s) until the printer confirms
 * (surfaced via `isPending`); the dialog refetches in place, never reopens.
 */
export function usePrinterSettings(printerId: number, enabled: boolean = true) {
  const qc = useQueryClient();
  const holdsRef = useRef<Map<string, number>>(new Map());
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const query = useQuery<PrinterSettingsGetResponse>({
    queryKey: QK(printerId),
    queryFn: () => api.getPrinterSettings(printerId),
    enabled,
    staleTime: 5_000,
  });

  const mutation = useMutation({
    mutationFn: (body: PrinterSettingsPostBody) => api.postPrinterSettings(printerId, body),
  });

  const clearPending = useCallback((flags: string[]) => {
    setPending((p) => {
      const n = { ...p };
      for (const f of flags) delete n[f];
      return n;
    });
  }, []);

  // Freeze the touched control(s), send the command, then keep the spinner for the
  // hold window while the printer confirms — refetching in place at the end.
  const submit = useCallback(
    async (body: PrinterSettingsPostBody) => {
      const flags = flagsForAction(body);
      const now = Date.now();
      for (const f of flags) holdsRef.current.set(f, now + 3_000);
      setPending((p) => {
        const n = { ...p };
        for (const f of flags) n[f] = true;
        return n;
      });
      try {
        await mutation.mutateAsync(body);
      } catch (e) {
        clearPending(flags);
        throw e;
      }
      // Refetch mid-way (catch a fast confirm) and again when the freeze ends.
      window.setTimeout(() => qc.invalidateQueries({ queryKey: QK(printerId) }), PENDING_MS / 2);
      window.setTimeout(() => {
        qc.invalidateQueries({ queryKey: QK(printerId) });
        clearPending(flags);
      }, PENDING_MS);
    },
    [mutation, qc, printerId, clearPending],
  );

  const isHeld = useCallback((flag: string) => {
    const deadline = holdsRef.current.get(flag);
    return deadline != null && deadline > Date.now();
  }, []);

  const isPending = useCallback((flag: string) => pending[flag] === true, [pending]);

  return {
    data: query.data,
    isLoading: query.isLoading,
    error: query.error,
    refetch: query.refetch,
    mutate: mutation.mutateAsync,
    submit,
    isMutating: mutation.isPending,
    isHeld,
    isPending,
  };
}

function flagsForAction(body: PrinterSettingsPostBody): string[] {
  if (body.action === 'print_option_bool') return [body.key];
  if (body.action === 'print_option_int') return [body.key];
  if (body.action === 'xcam_control') return [body.module];
  if (body.action === 'camera_snapshot') return ['snapshot'];
  if (body.action === 'safety_open_door') return ['open_door'];
  if (body.action === 'safety_idle_heating') return ['idle_heating'];
  return [];
}
