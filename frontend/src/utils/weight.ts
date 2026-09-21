export function formatWeight(grams: number): string {
  if (grams >= 1_000_000) {
    const tonnes = grams / 1_000_000;
    return `${tonnes % 1 === 0 ? tonnes.toFixed(0) : tonnes.toFixed(1)}t`;
  }
  if (grams >= 1000) {
    const kg = grams / 1000;
    return `${kg % 1 === 0 ? kg.toFixed(0) : kg.toFixed(1)}kg`;
  }
  return `${Math.round(grams)}g`;
}

/**
 * Compact inventory weight used beside spool fill bars.
 *
 * Individual spools stay in grams. Aggregates switch to kilograms only once
 * five-digit gram values become harder to scan; precision then steps down as
 * the total grows. Keep forecast stock on this formatter too: both surfaces
 * describe the same physical inventory.
 */
export function formatInventoryWeight(grams: number, useKg = false): string {
  if (useKg && grams >= 1000) return `${(grams / 1000).toFixed(1)}kg`;
  if (grams >= 100_000) return `${(grams / 1000).toFixed(1)}kg`;
  if (grams >= 10_000) return `${(grams / 1000).toFixed(2)}kg`;
  if (grams >= 5_000) return `${(grams / 1000).toFixed(3)}kg`;
  return `${Math.round(grams)}g`;
}
