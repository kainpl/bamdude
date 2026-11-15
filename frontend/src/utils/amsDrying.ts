// A drying timer the printer holds but does not run is not a drying cycle
// (upstream #2896 / #3096): it took the command, keeps the countdown and is not
// heating — its power budget is spent, a print is running. The backend marks it
// (`dry_countdown_stalled`); these are the card's only readers of "is it drying".
interface DryState {
  dry_time: number;
  dry_status?: number;
  dry_countdown_stalled?: boolean;
}

// AMSUnit.dry_status values that prove a cycle really began: 1=Checking, 2=Drying.
// Deliberately NOT 3=Cooling / 4=Stopping — those are leftovers from a previous
// cycle and would false-clear a watch armed right after one ended — and not
// 5=Error, which is the failure the watch exists to report, not a success.
const DRY_STATUS_STARTED = new Set([1, 2]);
const DRY_STATUS_ERROR = 5;

/** The unit holds a timer with no cycle behind it. */
export const isDryingParked = (unit: DryState): boolean => unit.dry_time > 0 && !!unit.dry_countdown_stalled;

/** The unit's countdown is a cycle in progress. */
export const isDryingRunning = (unit: DryState): boolean => unit.dry_time > 0 && !unit.dry_countdown_stalled;

/**
 * What the unit says about a start that was just acknowledged (#2533).
 * `standing` is the countdown the unit showed when the command went out: a start
 * may replace a parked timer, and that same value still standing proves nothing.
 */
export function dryStartVerdict(unit: DryState, standing: number): 'started' | 'failed' | null {
  if (unit.dry_status !== undefined && DRY_STATUS_STARTED.has(unit.dry_status)) return 'started';
  if (isDryingRunning(unit) && unit.dry_time !== standing) return 'started';
  if (unit.dry_status === DRY_STATUS_ERROR) return 'failed';
  return null;
}
