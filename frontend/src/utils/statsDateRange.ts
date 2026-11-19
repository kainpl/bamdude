import { localDateKey } from './date';

export type TimeframePreset =
  | 'today'
  | 'this-week'
  | 'this-month'
  | 'last-7'
  | 'last-30'
  | 'last-90'
  | 'this-year'
  | 'all-time'
  | 'custom';

/**
 * The calendar days a statistics preset covers, as `YYYY-MM-DD` — LOCAL days.
 *
 * ⚠️ The server reads these dates as calendar days in the zone this browser
 * sends with every request (`X-Client-Timezone`), so they must be built from
 * the local date, never from `getUTC*` / `toISOString()`: east of UTC the UTC
 * date is still yesterday for the first hours after midnight, and «Today»
 * then asked for — and got — the whole of yesterday; a week, a month and a
 * year opened on the wrong day the same way.
 *
 * `new Date(y, m, d ± n)` normalises across month and year ends in local time.
 */
export function computeDateRange(
  preset: TimeframePreset,
  now: Date = new Date(),
): { dateFrom?: string; dateTo?: string } {
  const y = now.getFullYear();
  const m = now.getMonth();
  const d = now.getDate();
  const day = (offset: number) => localDateKey(new Date(y, m, d + offset));
  const today = day(0);

  switch (preset) {
    case 'today':
      return { dateFrom: today, dateTo: today };
    case 'this-week': {
      // Monday first.
      const weekday = now.getDay();
      return { dateFrom: day(-(weekday === 0 ? 6 : weekday - 1)), dateTo: today };
    }
    case 'this-month':
      return { dateFrom: localDateKey(new Date(y, m, 1)), dateTo: today };
    case 'last-7':
      return { dateFrom: day(-6), dateTo: today };
    case 'last-30':
      return { dateFrom: day(-29), dateTo: today };
    case 'last-90':
      return { dateFrom: day(-89), dateTo: today };
    case 'this-year':
      return { dateFrom: localDateKey(new Date(y, 0, 1)), dateTo: today };
    case 'all-time':
      return { dateFrom: undefined, dateTo: undefined };
    case 'custom':
      return {};
  }
}

/** Today's local date — the last day a date input may offer. */
export function localToday(now: Date = new Date()): string {
  return localDateKey(now);
}
