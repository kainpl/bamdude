/** A run can show one-print duration only when every copy has that same estimate. */
export function sharedSlicerTime(times: (number | null | undefined)[]): number | null {
  const first = times[0];
  return typeof first === 'number' && Number.isFinite(first) && first > 0 && times.every(value => value === first) ? first : null;
}
