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
  trayType?: string | null;
  isEmpty: boolean;
  slotNumber: number | string;
  emptyKind?: 'physical' | 'reset' | null;
}

function isLightFilamentColor(hex: string): boolean {
  if (!hex || hex.length < 6) return false;
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6;
}

export function FilamentSlotCircle({ trayColor, trayType, isEmpty, slotNumber, emptyKind }: FilamentSlotCircleProps) {
  // A spool physically loaded but not yet configured ('reset') gets a solid
  // amber border so it stands out from a truly empty slot's dashed grey (#1694).
  const isUnconfigured = isEmpty && emptyKind === 'reset';
  return (
    <div
      className="w-3.5 h-3.5 rounded-full mx-auto mb-0.5 border-2 flex items-center justify-center"
      style={{
        backgroundColor: trayColor ? `#${trayColor}` : (trayType ? '#333' : 'transparent'),
        borderColor: isUnconfigured ? '#f59e0b' : (isEmpty ? '#666' : 'rgba(255,255,255,0.1)'),
        borderStyle: isEmpty && !isUnconfigured ? 'dashed' : 'solid',
      }}
    >
      <span
        className="text-[6px] font-bold leading-none select-none"
        style={{ color: trayColor && isLightFilamentColor(trayColor) ? '#000' : '#fff' }}
      >
        {slotNumber}
      </span>
    </div>
  );
}
