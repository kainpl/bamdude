/** `#RRGGBB` for a colour, with the alpha pair only when it is not opaque. */
export function displayHex(color: string | null | undefined): string {
  const clean = (color ?? '').replace(/^#/, '').toUpperCase();
  if (!/^[0-9A-F]{6}([0-9A-F]{2})?$/.test(clean)) return '';
  return `#${clean.length === 8 && clean.slice(6) !== 'FF' ? clean : clean.slice(0, 6)}`;
}
