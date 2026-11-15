import type { InventorySpool } from '../api/client';

/**
 * The cost per kg to show for a spool: the rate prints off it are charged
 * (upstream 2e30470df). A Spoolman spool's own price wins when it is above 0;
 * otherwise prints pay its filament's catalogue price
 * (`effective_cost_per_kg`), because the charge reads a missing or 0 spool
 * price as "not set". Without one the spool's own value stands, so a built-in
 * spool priced 0 still shows 0.
 *
 * The spool form keeps editing `cost_per_kg` alone — the fallback never goes
 * into it, or the first unrelated save would copy the catalogue price onto
 * the spool.
 */
export function shownCostPerKg(
  spool: Pick<InventorySpool, 'cost_per_kg' | 'effective_cost_per_kg'>,
): { rate: number | null; fromFilament: boolean } {
  const own = spool.cost_per_kg;
  if (own != null && own > 0) return { rate: own, fromFilament: false };
  const charged = spool.effective_cost_per_kg;
  if (charged != null) return { rate: charged, fromFilament: true };
  return { rate: own ?? null, fromFilament: false };
}

/**
 * A whole spool's price ↔ its cost per kg, through the spool's net weight in
 * grams — the one rule the server converts by (`cost_per_kg_to_spoolman_price`
 * and its inverse), so the form's "price per spool" helper and what Spoolman
 * stores are the same arithmetic. `null` when there is no rate or no weight.
 */
export function spoolPriceFromCostPerKg(costPerKg: number | null | undefined, netWeightGrams: number): number | null {
  if (costPerKg == null || !Number.isFinite(costPerKg) || !(netWeightGrams > 0)) return null;
  return (costPerKg * netWeightGrams) / 1000;
}

export function costPerKgFromSpoolPrice(spoolPrice: number, netWeightGrams: number): number | null {
  if (!Number.isFinite(spoolPrice) || !(netWeightGrams > 0)) return null;
  return (spoolPrice * 1000) / netWeightGrams;
}
