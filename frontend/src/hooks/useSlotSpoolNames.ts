import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { slotSpoolFacts, slotSpoolNames, type SlotSpoolFacts } from '../utils/slotSpoolNames';

/**
 * The spools bound to one printer's slots, from the ACTIVE inventory: the
 * built-in assignment rows, or in Spoolman mode the slot assignments plus the
 * Spoolman spool list. Same query keys as the printer card, so an open
 * Printers page and the print dialog share the requests. The printer's own
 * assignments are re-read when the dialog mounts: they describe the slots
 * NOW, and a spool assigned a moment ago must not keep its old name or price.
 */
function useSlotSpoolSources(printerId: number | null | undefined) {
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
    () => ({
      spoolmanMode,
      assignments,
      spoolmanAssignments: spoolmanAssignments?.filter((row) => row.printer_id === printerId),
      spoolmanSpools,
    }),
    [spoolmanMode, assignments, spoolmanAssignments, spoolmanSpools, printerId],
  );
}

/**
 * `{global tray id → assigned spool's name}` for one printer (upstream
 * d5c70477) — see `utils/slotSpoolNames`.
 */
export function useSlotSpoolNames(printerId: number | null | undefined): Map<number, string> {
  const { data: settings } = useQuery({ queryKey: ['ui-preferences'], queryFn: api.getUiPreferences });
  const sources = useSlotSpoolSources(printerId);
  return useMemo(
    () => slotSpoolNames({ ...sources, template: settings?.spool_display_template }),
    [sources, settings?.spool_display_template],
  );
}

/**
 * `{global tray id → charged rate and grams left}` of the spool bound to each
 * slot of one printer (upstream 5649f836d) — see `slotSpoolFacts`.
 */
export function useSlotSpoolFacts(printerId: number | null | undefined): Map<number, SlotSpoolFacts> {
  const sources = useSlotSpoolSources(printerId);
  return useMemo(() => slotSpoolFacts(sources), [sources]);
}
