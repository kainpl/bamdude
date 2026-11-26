import type { TFunction } from 'i18next';
import { ApiError } from '../api/client';

/**
 * The text of a refused change of the Spoolman source (C047 S43), or null when the
 * error is not one.
 *
 * `spoolman_print_in_progress` carries a `reason`: `charge_window` — a print is
 * being charged right now, a moment — `slot_writes` — spools are being assigned
 * to slots while the mode changes, a moment (C047 A08) — or `print_accounting` —
 * a print accounts in Spoolman, so its address or database waits until the print
 * ends. The code and the reason decide; the server's sentence is never matched.
 */
export function spoolmanSourceRefusalText(error: unknown, t: TFunction): string | null {
  if (!(error instanceof ApiError) || error.code !== 'spoolman_print_in_progress') return null;
  if (error.reason === 'charge_window') return t('settings.spoolmanSourceRefused.charge_window');
  if (error.reason === 'slot_writes') return t('settings.spoolmanSourceRefused.slot_writes');
  return t('settings.spoolmanSourceRefused.print_accounting');
}
