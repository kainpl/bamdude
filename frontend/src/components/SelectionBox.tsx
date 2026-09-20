import { Check } from 'lucide-react';

/**
 * The one selection box: a real-looking checkbox for rows and cards that
 * cannot use a native `<input type="checkbox">`.
 *
 * ⚠️ Not `CheckSquare`/`Square` from lucide, which several lists used to
 * reach for. Those draw an OUTLINE with the tick in the same colour as the
 * frame, so a selected row read as a tinted glyph rather than a ticked box —
 * visibly a different control from the native checkboxes elsewhere on the
 * same page.
 *
 * The shape here is what a browser paints for `accent-color`: the box fills
 * with the accent and the mark inside is dark. `text-black` is deliberate and
 * fixed — the box is always the accent, so a theme-following token would wash
 * the mark out in a light theme (see the note in the checkbox commit).
 *
 * Size comes from `className` (`w-4 h-4`, `w-5 h-5`, a CSS variable, …); the
 * tick is sized as a fraction so it follows whatever the caller asks for.
 */
export function SelectionBox({
  checked,
  className = 'w-4 h-4',
}: {
  checked: boolean;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      data-testid="selection-box"
      className={`inline-flex items-center justify-center shrink-0 rounded border transition-colors ${
        checked ? 'bg-bambu-green border-bambu-green' : 'border-bambu-gray/50'
      } ${className}`}
    >
      {checked && <Check className="w-3/4 h-3/4 text-black" strokeWidth={3} />}
    </span>
  );
}
