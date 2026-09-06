import type { CSSProperties } from 'react';

/**
 * The colours a printer tag may wear. Fixed, not free: each swatch is legible
 * as a tinted chip in both themes, and "Фаза 1 is yellow" is a choice the
 * operator makes once — not a hash that changes when a tag is renamed.
 * `nameKey` indexes `printers.tags.colors.*` in the locales.
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
