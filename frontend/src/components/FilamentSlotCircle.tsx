/**
 * FilamentSlotCircle renders a small color circle with the 1-based slot
 * number centered inside, matching the style used on AMS cards in PrintersPage.
 *
 * Props:
 *   trayColor  - 6-char hex color string WITHOUT leading '#' (e.g. "FF0000").
 *                Pass undefined / empty string when the slot is empty.
 *   trayType   - Filament material string (e.g. "PLA").  Used to decide the
 *                fallback background when there is no color but a type is known.
 *   isEmpty    - Whether the slot contains no filament.
 *   slotNumber - 1-based slot number to display inside the circle.
 *   emptyKind  - Distinguishes a firmware-confirmed empty slot ('physical')
 *                from one that has a spool loaded but no filament type set
 *                ('reset'). A 'reset' slot gets a solid amber border so it
 *                reads as "needs attention" rather than an empty dashed slot
 *                (#1694). null / undefined ⇒ configured (or caller doesn't know).
 */

interface FilamentSlotCircleProps {
  trayColor?: string | null;
  /**
   * Every colour the spool carries, hex without '#'. Optional: the fifteen
   * existing call sites keep working unchanged, and a one-item list behaves
   * exactly like `trayColor` alone.
   */
  trayColors?: string[] | null;
  /**
   * BambuStudio's DevFilaColorType. ⚠️ 0 = MULTI, 1 = GRADIENT, 2 = SINGLE, and
   * the drawing rule inverts the names: MULTI blends, everything else bands.
   */
  ctype?: number | null;
  trayType?: string | null;
  isEmpty: boolean;
  slotNumber: number | string;
  emptyKind?: 'physical' | 'reset' | null;
}

/**
 * The fill for a spool that carries more than one colour.
 *
 * ⚠️ Follows BambuStudio's `AMSItem.cpp` rather than the constant names, which
 * point the other way: `CTYPE_MULTI` (0) draws a smooth blend from the first
 * colour to the last, and every other type with more than one colour draws
 * equal bands. Copied as found — it is the reference behaviour.
 *
 * Bands are equal sectors of the circle, which is the shape a 14 px dot can
 * actually show; BS draws vertical stripes because its swatch is a rectangle.
 */
function multiColourFill(colours: string[], ctype: number | null | undefined): string {
  if (colours.length < 2) return '';
  const hexes = colours.map((c) => `#${c}`);
  if (ctype === 0) {
    return `linear-gradient(90deg, ${hexes[0]}, ${hexes[hexes.length - 1]})`;
  }
  const step = 360 / hexes.length;
  const stops = hexes.map((h, i) => `${h} ${i * step}deg ${(i + 1) * step}deg`).join(', ');
  return `conic-gradient(${stops})`;
}

/** The mean of several hex colours, for a contrast decision that has to cover
 *  all of them. Falls back to the single colour when there is only one. */
function averageHex(colours: string[], fallback?: string | null): string {
  const list = colours.length ? colours : fallback ? [fallback] : [];
  if (!list.length) return '';
  let r = 0;
  let g = 0;
  let b = 0;
  for (const c of list) {
    r += parseInt(c.slice(0, 2), 16) || 0;
    g += parseInt(c.slice(2, 4), 16) || 0;
    b += parseInt(c.slice(4, 6), 16) || 0;
  }
  const n = list.length;
  return [r / n, g / n, b / n].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
}

function isLightFilamentColor(hex: string): boolean {
  if (!hex || hex.length < 6) return false;
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6;
}

export function FilamentSlotCircle({
  trayColor,
  trayColors,
  ctype,
  trayType,
  isEmpty,
  slotNumber,
  emptyKind,
}: FilamentSlotCircleProps) {
  const colours = (trayColors ?? []).filter(Boolean);
  const fill = isEmpty ? '' : multiColourFill(colours, ctype);
  // A spool physically loaded but not yet configured ('reset') gets a solid
  // amber border so it stands out from a truly empty slot's dashed grey (#1694).
  const isUnconfigured = isEmpty && emptyKind === 'reset';
  return (
    <div
      className="w-3.5 h-3.5 rounded-full mx-auto mb-0.5 border-2 flex items-center justify-center"
      style={{
        backgroundColor: trayColor ? `#${trayColor}` : (trayType ? '#333' : 'transparent'),
        backgroundImage: fill || undefined,
        borderColor: isUnconfigured ? '#f59e0b' : (isEmpty ? '#666' : 'rgba(255,255,255,0.1)'),
        borderStyle: isEmpty && !isUnconfigured ? 'dashed' : 'solid',
      }}
    >
      <span
        className="text-[6px] font-bold leading-none select-none"
        // ⚠️ Contrast is judged on the AVERAGE of the colours, not the first
        // one. On a banded dot the digit sits over all of them, and picking by
        // the first makes a black-and-white spool read as one or the other at
        // random depending on which colour firmware happened to list first.
        style={{ color: isLightFilamentColor(averageHex(colours, trayColor)) ? '#000' : '#fff' }}
      >
        {slotNumber}
      </span>
    </div>
  );
}
