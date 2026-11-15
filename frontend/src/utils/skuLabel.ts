/**
 * How a forecast SKU is named on screen: brand, material, subtype, colour.
 *
 * A colour that only repeats the subtype is named once. In Spoolman mode a
 * filament without a stored colour name takes its subtype as the colour, so
 * "Bambu Lab PLA Basic Black" came out with "Basic Black" twice (upstream
 * #3400); an operator who typed the same text in both fields gets the same
 * cure. Display only — the SKU key, its settings and the shopping list keep
 * the colour.
 */
export function skuLabel(sku: {
  material: string | null;
  subtype: string | null;
  brand: string | null;
  color_name: string | null;
}): string {
  const fold = (value: string | null) => (value ?? '').trim().toLowerCase();
  const colour = fold(sku.color_name) === fold(sku.subtype) ? null : sku.color_name;
  return [sku.brand, sku.material, sku.subtype, colour].filter(Boolean).join(' ');
}
