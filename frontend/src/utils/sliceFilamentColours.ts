// The colour each filament slot is sliced in (upstream 4f10d155, #2977).
//
// Neither Bambu Studio nor OrcaSlicer stores a colour on a filament preset —
// it is a per-project property their GUIs set from the plate — so a CLI slice
// that is given none records the slicer's compiled-in #00AE42 for every slot.
// The slice dialog therefore sends a colour per slot, and the backend writes it
// onto the resolved filament profile as `filament_colour`.
//
// ⚠️ A recorded colour is not a requirement. Dispatch ranks by colour and
// refuses nothing unless the job forces colour matching, so printing a slice
// in another colour stays an ordinary choice.

/** The slicer's own default when nothing supplies a colour — Bambu green. */
export const SLICER_DEFAULT_COLOUR = '#00AE42';

/**
 * What a native `<input type="color">` can show for a slot's colour.
 *
 * The input accepts `#RRGGBB` only, while source colours arrive in that form
 * and in the 8-digit `#RRGGBBAA` the AMS reports, so the alpha byte is trimmed
 * for DISPLAY only — an untouched slot still submits the original string. A
 * slot with no colour shows the slicer's default, because that is what the
 * slice will record for it.
 */
export function colourInputValue(raw: string | null | undefined): string {
  const value = (raw || '').trim();
  return /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(value)
    ? value.slice(0, 7).toUpperCase()
    : SLICER_DEFAULT_COLOUR;
}

/**
 * The request's `filament_colours`, one per slot in plate order: the user's
 * pick, else the colour the source plate was designed with, else an EMPTY
 * string.
 *
 * Empty rather than the swatch's displayed default: a sent colour outranks the
 * preset's own `default_filament_colour` on the backend, so pinning the
 * placeholder would silently discard the real colour of an imported OrcaSlicer
 * profile that carries one.
 */
export function filamentColoursPayload(
  slots: readonly { color?: string | null }[],
  overrides: readonly (string | null)[],
): string[] {
  return slots.map((slot, i) => overrides[i] ?? slot.color ?? '');
}

/**
 * Per-slot colour picks after the slot list changed.
 *
 * A plate switch renumbers the slots, so index-keyed picks would paint slot 2's
 * colour onto whatever the new plate calls slot 2. The same slot count keeps
 * them: that is a re-pick of presets, not a different plate layout. Returns the
 * SAME array when nothing changes, so a state setter bails out.
 */
export function resizeColourOverrides(current: (string | null)[], count: number): (string | null)[] {
  return current.length === count ? current : Array.from({ length: count }, () => null);
}
