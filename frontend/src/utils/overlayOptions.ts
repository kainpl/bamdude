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
