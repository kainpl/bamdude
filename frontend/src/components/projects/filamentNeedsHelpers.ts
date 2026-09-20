/**
 * Pure (non-component) export shared by `<FilamentNeeds>` and any future
 * farm-wide counterpart that needs the identical testid shape.
 *
 * Lives in its own file so `FilamentNeeds.tsx` stays component-only and
 * satisfies the `react-refresh/only-export-components` ESLint rule — the same
 * split already used by `filamentSwatchHelpers.ts` / `plateDialogLayout.ts` /
 * `staggerGroupIds.ts` (spec 2026-09-07).
 *
 * The `prefix` is a PARAMETER because the strip's ids are the table's under
 * another name: rebuilding one from the other with `.replace()` made the
 * shape depend on a substring that also occurs inside a material name.
 */
export function needTestId(material: string, colour: string | null, prefix = 'filament-need-'): string {
  return colour ? `${prefix}${material}-${colour}` : `${prefix}${material}`;
}
