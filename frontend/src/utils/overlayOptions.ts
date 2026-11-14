/**
 * The stream overlay's optional URL parameters, read in ONE place.
 *
 * The overlay page reads them off its own address; the builder in Settings
 * reads them off a pasted address and off the choices it remembers. All three
 * go through the validators here, so a value the page would ignore is never
 * one the builder accepts.
 *
 * ⚠️ The overlay is opened by OBS with a token and no session, from an address
 * somebody typed or pasted. A value that is not understood is IGNORED — never
 * an error, and never passed on to a style.
 */

/**
 * A colour as `#rrggbb`, or null.
 *
 * Six hex digits and nothing else, with or without the hash (a bare `#` in an
 * address starts the fragment, so hand-written addresses leave it out). The
 * result goes into a style, which is why no other CSS colour form is accepted.
 */
export function overlayColour(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const match = /^#?([0-9a-f]{6})$/i.exec(raw);
  // `$` also matches before a trailing line break: compare the lengths.
  if (!match || match[0].length !== raw.length) return null;
  return `#${match[1].toLowerCase()}`;
}

/**
 * The fill of the progress bar for `progressFrom` / `progressTo`: a gradient
 * from left to right, one colour when only one of the two is valid, undefined
 * when neither is — the overlay then keeps its own colour.
 */
export function progressFill(
  from: string | null | undefined,
  to: string | null | undefined,
): string | undefined {
  const start = overlayColour(from);
  const end = overlayColour(to);
  if (!start && !end) return undefined;
  return `linear-gradient(to right, ${start ?? end}, ${end ?? start})`;
}

export type OverlayDesign = 'classic' | 'v2';

/** `design=2` selects Version 2; absent or anything else is Classic. */
export function overlayDesign(raw: string | null | undefined): OverlayDesign {
  return raw === '2' ? 'v2' : 'classic';
}

/**
 * `backgroundTransparency`: a whole number of percent, 0 (solid) … 100.
 * Anything else — a sign, a fraction, an exponent, a number out of range — is
 * ignored and the ground stays solid.
 */
export function overlayTransparency(raw: string | null | undefined): number {
  if (!raw || raw.length > 3) return 0;
  for (const char of raw) {
    if (char < '0' || char > '9') return 0;
  }
  const value = Number(raw);
  return value <= 100 ? value : 0;
}

export type OverlayLayout = 'landscape' | 'portrait';

/** The size each layout is drawn at before it is scaled to the browser source. */
export const OVERLAY_CANVAS: Record<OverlayLayout, { width: number; height: number }> = {
  landscape: { width: 1920, height: 1080 },
  portrait: { width: 1080, height: 1920 },
};

/**
 * `layout=landscape|portrait` draws the overlay on a fixed canvas. Null — no
 * parameter, or anything else — keeps the overlay sized by its window, as
 * every address from before the parameter expects.
 */
export function overlayLayout(raw: string | null | undefined): OverlayLayout | null {
  return raw === 'landscape' || raw === 'portrait' ? raw : null;
}
