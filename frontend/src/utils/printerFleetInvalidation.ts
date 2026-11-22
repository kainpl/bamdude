// A printer was created, changed, archived, unarchived or deleted: the farm
// read of the slicing-setup filters ("Only models of my farm", the loaded
// filaments) and the printer lists are out of date (spec slicing-setup §8.1).
//
// The farm read is cancelled BEFORE it is invalidated: a first read still in
// flight has no data, and TanStack joins a refetch onto such a fetch instead of
// cancelling it, so its old answer would be accepted after the change with no
// new request. Called explicitly from every printer mutation's success path,
// never from a QueryCache subscription, which notifies only on the first
// stale edge and would miss a second change.
import type { QueryClient } from '@tanstack/react-query';

export async function invalidatePrinterFleet(qc: QueryClient): Promise<void> {
  await qc.cancelQueries({ queryKey: ['slicerLoadedFilaments'] });
  void qc.invalidateQueries({ queryKey: ['slicerLoadedFilaments'] });
  void qc.invalidateQueries({ queryKey: ['printers'] });
}
