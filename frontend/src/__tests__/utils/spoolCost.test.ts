/**
 * The Filaments page shows the rate a print off the spool is charged
 * (upstream 2e30470df): a Spoolman spool's own price when it has one, else
 * its filament's catalogue price, marked as such.
 */
import { describe, expect, it } from 'vitest';
import { shownCostPerKg } from '../../utils/spoolCost';

describe('shownCostPerKg', () => {
  it("shows the spool's own rate when it has one", () => {
    expect(shownCostPerKg({ cost_per_kg: 48, effective_cost_per_kg: 48 })).toEqual({ rate: 48, fromFilament: false });
  });

  it('falls back to the filament price for a spool without one, and says so', () => {
    expect(shownCostPerKg({ cost_per_kg: null, effective_cost_per_kg: 25 })).toEqual({ rate: 25, fromFilament: true });
  });

  it('treats a spool-level 0 as not set, as the charge does', () => {
    expect(shownCostPerKg({ cost_per_kg: 0, effective_cost_per_kg: 25 })).toEqual({ rate: 25, fromFilament: true });
  });

  it('shows nothing when no price exists anywhere', () => {
    expect(shownCostPerKg({ cost_per_kg: null, effective_cost_per_kg: null })).toEqual({ rate: null, fromFilament: false });
  });

  it('leaves a built-in spool priced 0 at 0', () => {
    expect(shownCostPerKg({ cost_per_kg: 0 })).toEqual({ rate: 0, fromFilament: false });
  });
});

describe('the Spoolman list sorts by the shown rate', () => {
  it('orders a filament-priced spool by the rate it is charged', async () => {
    const { compareByOrder } = await import('../../utils/spoolSort');
    const spools = [
      { id: 1, cost_per_kg: 40, effective_cost_per_kg: 40 },
      { id: 2, cost_per_kg: null, effective_cost_per_kg: 25 },
      { id: 3, cost_per_kg: 0, effective_cost_per_kg: 60 },
    ] as unknown as import('../../api/client').InventorySpool[];
    const sorted = [...spools].sort(compareByOrder([{ field: 'cost_per_kg', direction: 'asc' }]));
    expect(sorted.map((s) => s.id)).toEqual([2, 1, 3]);
  });
});
