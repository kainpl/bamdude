/** Plate dialog geometry and marker placement — shared, component-free.
 *
 * Split out of PlateObjectMarkers.tsx because that file exports a component and
 * `react-refresh/only-export-components` needs component files to export only
 * components for HMR to work. Same reason filamentSwatchHelpers.ts and
 * presetPickerUtils.ts exist.
 *
 * Both plate dialogs — SkipObjectsModal and the read-only
 * PlateObjectsPreviewModal — import from here, so their geometry and their
 * marker maths cannot drift. The position function was already extracted once
 * for that reason: the inline preview and the lightbox had two verbatim copies.
 *
 * Tailwind scans this file as raw text: a class reaches the CSS only if it
 * appears as a whole literal. A template-built class generates nothing,
 * silently, with no build error — so every Tailwind knob below stays a complete
 * class string, and anything arithmetic is a number applied through inline style.
 */

/* ── Layout knobs ───────────────────────────────────────────────────────────
 * Every dimension worth tuning by eye lives here rather than being buried in a
 * className soup. These are whole Tailwind class strings on purpose (see the
 * scanner note above).
 *
 * DIALOG_FRAME    — the height, as a percentage of the viewport, plus two max-*
 *                   caps that only bite on a small window. The overlay is
 *                   `fixed inset-0`, so a percentage here is a share of the
 *                   client area — `vh` would include the scrollbar gutter.
 *                   Width is absent on purpose — see DIALOG_WIDTH_PX.
 * PLATE_IMAGE_PX  — rendered edge of the square plate image inside the dialog.
 *                   Deliberately fixed and deliberately NOT tied to the dialog
 *                   width: the markers are a fixed size, so growing the plate is
 *                   what spreads them apart. A plain number applied as an inline
 *                   style rather than a Tailwind class, because the column width
 *                   and the full-screen size are both derived from it and a
 *                   literal class string cannot express arithmetic.
 * LIGHTBOX_SCALE  — how much bigger the full-screen plate is than the dialog's.
 *                   The source PNG is 512px (`Metadata/top_N.png`, served
 *                   straight out of the 3MF with no resizing), so past ~1.45x the
 *                   photo softens. The markers do not: they are DOM nodes placed
 *                   by percentage, so they stay sharp and simply spread further
 *                   apart — which is the entire reason the view exists.
 * LIST_COLUMN     — width of ONE object column as a share of the list viewport.
 *                   100% ⇒ a single column visible; 50% ⇒ two; 33.333% ⇒ three.
 *                   Anything past the visible count scrolls horizontally. This
 *                   is a share of the list viewport, whose own width is
 *                   LIST_WIDTH_SCALE — the two are independent knobs.
 * LIST_ROW_HEIGHT — fixed row height driving how many objects stack before the
 *                   list wraps into the next column. Must stay ≥ the tallest row
 *                   (48px ID badge + 24px padding = 72px = 4.5rem), or content
 *                   clips; 5rem leaves a little air.
 */
export const DIALOG_FRAME = 'h-[60%] max-w-[calc(100vw-2rem)] max-h-[calc(100vh-2rem)]';
export const PLATE_IMAGE_PX = 352;
export const PLATE_GUTTER_PX = 32; // the column's p-4, both sides
export const LIGHTBOX_SCALE = 1.5;
export const LIST_COLUMN = 'auto-cols-[100%]';
export const LIST_ROW_HEIGHT = 'grid-rows-[repeat(auto-fill,5rem)]';

/** The plate column: the image plus its gutters. */
export const COLUMN_PX = PLATE_IMAGE_PX + PLATE_GUTTER_PX;

/** The object list, as a multiple of the plate column.
 *
 * Its own knob rather than a shared width: object names are long and the row
 * carries an ID badge and a Skip button besides, so the list wants more room
 * than the square plate does. 1 pins the two columns equal.
 */
export const LIST_WIDTH_SCALE = 1.25;
export const LIST_COLUMN_PX = Math.round(COLUMN_PX * LIST_WIDTH_SCALE);

/** Dialog width, cut to its content rather than to a share of the screen.
 *
 * Both columns are a fixed size, so a percentage width would only ever add
 * empty background to the right of the list. Derived instead of hard-coded so
 * that tuning PLATE_IMAGE_PX still leaves the dialog flush.
 *
 * The `+ 2` is the 1px border on each side. Tailwind sets `box-sizing:
 * border-box` globally, so the border eats into this number — without it the
 * content is 2px wider than its own container and `overflow-hidden` shaves a
 * sliver off the list's right edge.
 *
 * NOT computed with `w-fit`: the info banner's paragraphs would then set the
 * width, making the dialog as wide as the longest translated string.
 */
export const DIALOG_WIDTH_PX = COLUMN_PX + LIST_COLUMN_PX + 2;

export type PlateObject = {
  id: number;
  name: string;
  x: number | null;
  y: number | null;
  norm?: boolean;
  skipped: boolean;
};

/** Where a marker sits on the plate image, as percentages of the image box.
 *
 * Four sources in descending order of trust; the first that has usable data
 * wins. Kept as a plain function so the inline preview and the enlarged
 * lightbox cannot drift apart — they used to carry two verbatim copies of this.
 */
export function markerPosition(
  obj: PlateObject,
  idx: number,
  total: number,
  bboxAll: number[] | null | undefined,
): { x: number; y: number } {
  // 1. Normalised pick-PNG centroid — matches what the printer's own screen shows.
  if (obj.norm && obj.x != null && obj.y != null) {
    return {
      x: Math.max(2, Math.min(98, obj.x * 100)),
      y: Math.max(2, Math.min(98, obj.y * 100)),
    };
  }
  // 2. Millimetre coords mapped through the bbox the top view was rendered from.
  if (obj.x != null && obj.y != null && bboxAll) {
    const [xMin, yMin, xMax, yMax] = bboxAll;
    const padding = 8; // the top_N.png render leaves roughly this much margin
    const contentArea = 100 - padding * 2;
    return {
      x: Math.max(5, Math.min(95, padding + ((obj.x - xMin) / (xMax - xMin)) * contentArea)),
      // Image Y grows downward, 3D Y grows toward the back of the plate.
      y: Math.max(5, Math.min(95, padding + ((yMax - obj.y) / (yMax - yMin)) * contentArea)),
    };
  }
  // 3. No bbox — assume a full 256mm plate.
  if (obj.x != null && obj.y != null) {
    const buildPlate = 256;
    return {
      x: Math.max(5, Math.min(95, (obj.x / buildPlate) * 100)),
      y: Math.max(5, Math.min(95, 100 - (obj.y / buildPlate) * 100)),
    };
  }
  // 4. No coordinates at all — lay them out in a grid so every object is still
  //    reachable. Positions are meaningless here; the list is the real UI.
  const cols = Math.ceil(Math.sqrt(total));
  const rows = Math.ceil(total / cols);
  return {
    x: 15 + (idx % cols) * (70 / cols) + 35 / cols,
    y: 15 + Math.floor(idx / cols) * (70 / rows) + 35 / rows,
  };
}
