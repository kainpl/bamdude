/**
 * What the print dialog knows about the spool bound to a slot (upstream
 * 5649f836d): the rate a print off it is charged and the grams left on it —
 * from the Spoolman spool the slot is bound to in Spoolman mode, never from
 * the built-in assignment rows.
 */
import { describe, it, expect } from 'vitest';
import { slotSpoolFacts } from '../../utils/slotSpoolNames';
import type { InventorySpool } from '../../api/client';

const spool = (over: Partial<InventorySpool>): InventorySpool =>
  ({ id: 1, material: 'PLA', label_weight: 1000, weight_used: 0, cost_per_kg: null, ...over }) as InventorySpool;

const spoolman = (spools: InventorySpool[]) =>
  slotSpoolFacts({
    spoolmanMode: true,
    spoolmanAssignments: [{ ams_id: 1, tray_id: 0, spoolman_spool_id: 42 }],
    spoolmanSpools: spools,
  });

describe('slotSpoolFacts', () => {
  it('prices a Spoolman slot at the rate its spool is charged', () => {
    const facts = spoolman([spool({ id: 42, cost_per_kg: 48, effective_cost_per_kg: 48 })]);
    expect(facts.get(4)?.costPerKg).toBe(48);
  });

  it("prices a Spoolman spool without a price of its own at its filament's", () => {
    const facts = spoolman([spool({ id: 42, cost_per_kg: null, effective_cost_per_kg: 25 })]);
    expect(facts.get(4)?.costPerKg).toBe(25);
  });

  it('leaves an unpriced Spoolman spool to the farm rate', () => {
    const facts = spoolman([spool({ id: 42, cost_per_kg: 0, effective_cost_per_kg: null })]);
    expect(facts.get(4)?.costPerKg).toBeNull();
  });

  it("reports the grams left on the bound Spoolman spool at the spool's own size", () => {
    const facts = spoolman([spool({ id: 42, label_weight: 250, weight_used: 62.6 })]);
    expect(facts.get(4)?.remainingGrams).toBe(187);
  });

  it('knows nothing about a slot whose Spoolman spool is not in the list', () => {
    expect(spoolman([spool({ id: 7 })]).has(4)).toBe(false);
  });

  it('ignores built-in rows in Spoolman mode', () => {
    const facts = slotSpoolFacts({
      spoolmanMode: true,
      assignments: [{ ams_id: 0, tray_id: 0, spool: spool({ cost_per_kg: 99 }) }],
      spoolmanAssignments: [],
      spoolmanSpools: [],
    });
    expect(facts.size).toBe(0);
  });

  it('keeps the built-in rule: the spool rate as stored, 0 included', () => {
    const facts = slotSpoolFacts({
      spoolmanMode: false,
      assignments: [
        { ams_id: 0, tray_id: 2, spool: spool({ cost_per_kg: 30, weight_used: 400 }) },
        { ams_id: 0, tray_id: 3, spool: spool({ cost_per_kg: 0 }) },
        { ams_id: 255, tray_id: 0, spool: spool({}) },
      ],
    });
    expect(facts.get(2)).toEqual({ costPerKg: 30, remainingGrams: 600 });
    expect(facts.get(3)?.costPerKg).toBe(0);
    expect(facts.get(254)).toEqual({ costPerKg: null, remainingGrams: 1000 });
  });
});
