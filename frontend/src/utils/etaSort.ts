import type { FarmForecast, PrinterForecast } from '../api/client';

/** The slice of a printer's live status the two ETA orders read. */
export interface EtaStatus {
  connected: boolean;
  state: string | null;
  remaining_time: number | null;
}

/** The slice of the server's per-printer forecast row the queue-aware order reads. */
export type FreeAtRow = Pick<PrinterForecast, 'free_seconds' | 'unknown_prints'>;

/**
 * Tier under «ETA (job)»: printing with a known remaining time (0,
 * soonest first) > printing with none yet (1) > idle / finished (2) > offline
 * (3). Printing machines lead because that is what the order is for — staging
 * the next job's filament on the printer finishing next; a machine with
 * nothing to finish is out of that race, and an offline one out of every race.
 * No status at all reads as offline, the same way the offline filter reads it.
 */
function currentJobTier(status: EtaStatus | undefined): number {
  if (!status?.connected) return 3;
  if (status.state === 'RUNNING' && status.remaining_time != null && status.remaining_time > 0) return 0;
  if (status.state === 'RUNNING') return 1;
  return 2;
}

/**
 * Tier under «ETA (queue)» — the same shape as the current-job order,
 * so the two read as a pair: owes timed work (0, free soonest first) > owes
 * work nobody has timed yet (1) > free now (2) > offline (3). The row comes
 * from the server's simulation of every queue (`GET /queue/forecast`), which
 * is arithmetic over the database, so an offline printer can still owe hours;
 * it sinks anyway — a machine that is not printing will not be free when the
 * arithmetic says. No row yet (the forecast still loading) reads as free now.
 */
function freeAtTier(row: FreeAtRow | undefined, status: EtaStatus | undefined): number {
  if (!status?.connected) return 3;
  if (row && row.free_seconds > 0) return 0;
  if (row && row.unknown_prints > 0) return 1;
  return 2;
}

/** 0 when the two are peers — the caller falls back to its own tie-break (the name). */
export function compareCurrentJobEta(a: EtaStatus | undefined, b: EtaStatus | undefined): number {
  const ta = currentJobTier(a);
  const tb = currentJobTier(b);
  if (ta !== tb) return ta - tb;
  if (ta === 0) return (a!.remaining_time ?? 0) - (b!.remaining_time ?? 0);
  return 0;
}

/** 0 when the two are peers — the caller falls back to its own tie-break (the name). */
export function compareFreeAt(
  rowA: FreeAtRow | undefined,
  rowB: FreeAtRow | undefined,
  statusA: EtaStatus | undefined,
  statusB: EtaStatus | undefined,
): number {
  const ta = freeAtTier(rowA, statusA);
  const tb = freeAtTier(rowB, statusB);
  if (ta !== tb) return ta - tb;
  if (ta === 0) return rowA!.free_seconds - rowB!.free_seconds;
  return 0;
}

/** The forecast's rows by printer id — an empty map while it has not loaded. */
export function forecastById(forecast: FarmForecast | undefined): Map<number, PrinterForecast> {
  return new Map((forecast?.printers ?? []).map((row) => [row.printer_id, row]));
}
