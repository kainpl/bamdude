export type DryingStartMode = 'now' | 'delay' | 'at' | 'when_free' | 'repeat';

// One fleet-wide query each: every printer card reads the same lists.
export const SCHEDULED_DRYINGS_KEY = ['scheduled-dryings'] as const;
export const DRYING_SCHEDULES_KEY = ['drying-schedules'] as const;

// Monday = bit 0 … Sunday = bit 6, as the backend stores ``weekdays``.
export const WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64];
export const ALL_WEEKDAYS = 127;
export const WEEKDAY_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const;

/**
 * The ``start_after`` a one-shot run is created with. ``undefined`` — the mode
 * does not create a one-shot run (``now`` uses the immediate endpoint, ``repeat``
 * creates a rule); ``null`` — as soon as the printer is free.
 */
export function computeStartAfter(
  mode: DryingStartMode,
  opts: { delayHours?: number; at?: string },
  now: Date,
): string | null | undefined {
  if (mode === 'delay') return new Date(now.getTime() + (opts.delayHours ?? 1) * 3_600_000).toISOString();
  if (mode === 'at') return opts.at ? new Date(opts.at).toISOString() : undefined;
  if (mode === 'when_free') return null;
  return undefined;
}

function canonicalZone(tz: string): string {
  // Through the same Intl both sides: Europe/Kiev and Europe/Kyiv are one zone.
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: tz }).resolvedOptions().timeZone;
  } catch {
    return tz;
  }
}

/** Does the browser sit in another zone than the farm whose clock the schedules follow? */
export function farmZoneDiffers(serverTz?: string | null): boolean {
  if (!serverTz) return false;
  return canonicalZone(serverTz) !== canonicalZone(Intl.DateTimeFormat().resolvedOptions().timeZone);
}

/** "AMS-A" for AMS 0, "HT-A" for AMS-HT 128 — the label the printer card uses. */
export function amsLabel(amsId: number): string {
  return amsId >= 128 ? `HT-${String.fromCharCode(65 + amsId - 128)}` : `AMS-${String.fromCharCode(65 + amsId)}`;
}

export function weekdaysLabel(mask: number, t: (key: string) => string): string {
  if (mask === ALL_WEEKDAYS) return t('printers.drying.everyDay');
  return WEEKDAY_BITS.filter((bit) => mask & bit)
    .map((bit) => t(`printers.drying.weekdayShort.${WEEKDAY_NAMES[WEEKDAY_BITS.indexOf(bit)]}`))
    .join(' ');
}
