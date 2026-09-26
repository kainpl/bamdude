/**
 * "Recalculate Costs" says how many costs it kept from Spoolman (upstream
 * 39835437, #2591).
 *
 * In Spoolman mode a print is priced at completion from the spools that fed it;
 * the recalculation leaves those alone rather than re-pricing them at the farm
 * rate, and the toast says so — otherwise "recalculated 3 of 40" reads as if
 * the other 37 were skipped for no reason.
 */
import { describe, expect, it } from 'vitest';

import page from '../../pages/StatsPage.tsx?raw';
import client from '../../api/client.ts?raw';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

describe('Stats — recalculate costs', () => {
  it('reads the kept count from the response', () => {
    expect(client).toContain("request<{ message: string; updated: number; preserved?: number }>('/statistics/recalculate-costs'");
    expect(page).toContain("t('stats.recalculatedCostsKept', { count: result.updated, kept: result.preserved })");
  });

  it('words it in both locales', () => {
    const stats = (locale: unknown) => (locale as { stats: Record<string, string> }).stats;
    expect(stats(en).recalculatedCostsKept).toContain('{{kept}}');
    expect(stats(uk).recalculatedCostsKept).toContain('{{kept}}');
  });
});
