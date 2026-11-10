import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { slotSpoolNames } from '../utils/slotSpoolNames';

/**
 * `{global tray id → assigned spool's name}` for one printer (upstream
 * d5c70477) — see `utils/slotSpoolNames`. Same query keys as the printer card,
 * so an open Printers page and the print dialog share the requests. The
 * printer's own assignments are re-read when the dialog mounts: they name the
 * slots NOW, and a spool assigned a moment ago must not keep its old name.
 */
export function useSlotSpoolNames(printerId: number | null | undefined): Map<number, string> {
  const { data: settings } = useQuery({ queryKey: ['ui-preferences'], queryFn: api.getUiPreferences });
  const { data: spoolmanStatus } = useQuery({
    queryKey: ['spoolman-status'],
    queryFn: api.getSpoolmanStatus,
    staleTime: 60 * 1000,
  });
  const spoolmanMode = !!(spoolmanStatus?.enabled && spoolmanStatus?.connected);
  const enabled = !!printerId;

  const { data: assignments } = useQuery({
    queryKey: ['spool-assignments', printerId],
    queryFn: () => api.getAssignments(printerId!),
    enabled: enabled && !spoolmanMode,
    refetchOnMount: 'always',
  });
  const { data: spoolmanAssignments } = useQuery({
    queryKey: ['spoolman-slot-assignments'],
    queryFn: () => api.getSpoolmanSlotAssignments(),
    enabled: enabled && spoolmanMode,
    refetchOnMount: 'always',
  });
  const { data: spoolmanSpools } = useQuery({
    queryKey: ['spoolman-inventory-spools'],
    queryFn: () => api.getSpoolmanInventorySpools(false),
    enabled: enabled && spoolmanMode,
    staleTime: 30 * 1000,
  });

  return useMemo(
    () =>
      slotSpoolNames({
        spoolmanMode,
        template: settings?.spool_display_template,
        assignments,
        spoolmanAssignments: spoolmanAssignments?.filter((row) => row.printer_id === printerId),
        spoolmanSpools,
      }),
    [spoolmanMode, settings?.spool_display_template, assignments, spoolmanAssignments, spoolmanSpools, printerId],
  );
}
