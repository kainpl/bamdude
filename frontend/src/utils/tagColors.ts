import type { CSSProperties } from 'react';

/**
 * The colours a printer tag may wear. Fixed, not free: ten swatches mean the
 * same tag looks the same everywhere it is drawn, and "Фаза 1 is yellow" is a
 * choice the operator makes once — not a hash that changes when a tag is
 * renamed. `nameKey` indexes `printers.tags.colors.*` in the locales.
 *
 * ⚠️ This is NOT a promise of contrast. The tint formula below paints
 * full-strength swatch text on a 15 % fill of the same hue, which on a light
 * background leaves the pale swatches (amber, lime, cyan) near 2:1 — under
 * WCAG AA. The formula is what the spec asked for and the chip is decoration
 * beside a name that is always spelled out, so the shortfall is accepted, not
 * unnoticed. Fix it by darkening the text per swatch, never by silently
 * dropping a colour operators have already assigned.
 */
export const TAG_PALETTE: ReadonlyArray<{ hex: string; nameKey: string }> = [
  { hex: '#f59e0b', nameKey: 'amber' },
  { hex: '#f97316', nameKey: 'orange' },
  { hex: '#ef4444', nameKey: 'red' },
  { hex: '#ec4899', nameKey: 'rose' },
  { hex: '#8b5cf6', nameKey: 'violet' },
  { hex: '#3b82f6', nameKey: 'blue' },
  { hex: '#06b6d4', nameKey: 'cyan' },
  { hex: '#22c55e', nameKey: 'green' },
  { hex: '#84cc16', nameKey: 'lime' },
  { hex: '#64748b', nameKey: 'slate' },
];

/** Inline style for a tinted chip — 15 % fill, 35 % border, full-strength text. Undefined keeps the neutral classes. */
export function tagChipStyle(color: string | null | undefined): CSSProperties | undefined {
  if (!color) return undefined;
  return { backgroundColor: `${color}26`, color, borderColor: `${color}59` };
}
