/**
 * The PDF viewer's arithmetic — no DOM, no PDF.js: which scale a zoom mode
 * means, where the pages lie in the scroll box, which of them are on screen,
 * and what a key does.
 *
 * Sizes are CSS pixels at scale 1 (a PDF point is drawn as one CSS pixel);
 * page numbers are 1-based, as the document counts them.
 */

export interface Size {
  width: number;
  height: number;
}

/** Fit the page's width, fit the whole page, or an explicit scale. */
export type ZoomMode = 'width' | 'page' | number;

export const MIN_SCALE = 0.25;
export const MAX_SCALE = 5;
export const ZOOM_STEP = 1.25;

const clampScale = (scale: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));

/** The scale `mode` means for a page of `page` in a stage of `stage` with `padding` on every side. */
export function scaleFor(mode: ZoomMode, page: Size, stage: Size, padding: number): number {
  if (typeof mode === 'number') return clampScale(mode);
  const roomX = stage.width - 2 * padding;
  const roomY = stage.height - 2 * padding;
  // A stage that has not been laid out yet (or a page with no size) fits nothing.
  if (roomX <= 0 || page.width <= 0 || page.height <= 0) return 1;
  const byWidth = roomX / page.width;
  if (mode === 'width' || roomY <= 0) return clampScale(byWidth);
  return clampScale(Math.min(byWidth, roomY / page.height));
}

/** One zoom step up (`1`) or down (`-1`) from `current`. */
export function zoomStep(current: number, direction: 1 | -1): number {
  return clampScale(direction > 0 ? current * ZOOM_STEP : current / ZOOM_STEP);
}

export function clampPage(page: number, pageCount: number): number {
  return Math.min(Math.max(1, pageCount), Math.max(1, Math.round(page)));
}

/** What the page box says: a whole number, clamped into the document — or `null` for anything else. */
export function parsePageInput(text: string, pageCount: number): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  return clampPage(Number(trimmed), pageCount);
}

export interface PageLayout {
  /** Each page's offset from the top of the first one. */
  tops: number[];
  widths: number[];
  heights: number[];
  /** The height of all pages and the gaps between them. */
  total: number;
}

/** The pages one under another at `scale`, `gap` pixels apart. */
export function layoutPages(sizes: Size[], scale: number, gap: number): PageLayout {
  const tops: number[] = [];
  const widths: number[] = [];
  const heights: number[] = [];
  let y = 0;
  for (const size of sizes) {
    const height = Math.round(size.height * scale);
    tops.push(y);
    widths.push(Math.round(size.width * scale));
    heights.push(height);
    y += height + gap;
  }
  return { tops, widths, heights, total: sizes.length ? y - gap : 0 };
}

/** The index of the last page that starts at or above `offset`. */
function pageIndexAt(tops: number[], offset: number): number {
  let low = 0;
  let high = tops.length - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (tops[middle] <= offset) low = middle;
    else high = middle - 1;
  }
  return low;
}

/** The pages a screen of `viewHeight` at `scrollTop` shows, widened by `overscan` pages each way. */
export function visibleRange(
  tops: number[],
  scrollTop: number,
  viewHeight: number,
  overscan: number,
): { first: number; last: number } {
  if (tops.length === 0) return { first: 1, last: 0 };
  const top = Math.max(0, scrollTop);
  const firstIndex = pageIndexAt(tops, top);
  let lastIndex = pageIndexAt(tops, top + Math.max(0, viewHeight));
  // A page that only starts exactly at the bottom edge is not on screen.
  if (lastIndex > firstIndex && tops[lastIndex] >= top + viewHeight) lastIndex -= 1;
  return {
    first: Math.max(0, firstIndex - overscan) + 1,
    last: Math.min(tops.length - 1, lastIndex + overscan) + 1,
  };
}

/** The page that holds the middle of the screen. */
export function currentPage(tops: number[], scrollTop: number, viewHeight: number): number {
  if (tops.length === 0) return 1;
  return pageIndexAt(tops, Math.max(0, scrollTop) + Math.max(0, viewHeight) / 2) + 1;
}

/**
 * How many canvas pixels per CSS pixel to draw a page of `cssSize` with: the
 * device's ratio, lowered when the canvas would pass `maxPixels` — a page
 * zoomed far in is megabytes of bitmap, and a tab has only so many.
 */
export function outputScale(cssSize: Size, devicePixelRatio: number, maxPixels: number): number {
  const ratio = devicePixelRatio > 0 ? devicePixelRatio : 1;
  const pixels = cssSize.width * ratio * cssSize.height * ratio;
  if (pixels <= maxPixels || pixels <= 0) return ratio;
  return ratio * Math.sqrt(maxPixels / pixels);
}

export type KeyAction = 'prev' | 'next' | 'first' | 'last' | 'scrollUp' | 'scrollDown' | 'zoomIn' | 'zoomOut' | 'fitWidth';

/** What a key, pressed with no modifier, does in the viewer. */
export function keyAction(key: string): KeyAction | null {
  switch (key) {
    case 'PageDown':
    case 'ArrowRight':
      return 'next';
    case 'PageUp':
    case 'ArrowLeft':
      return 'prev';
    case 'Home':
      return 'first';
    case 'End':
      return 'last';
    case 'ArrowDown':
      return 'scrollDown';
    case 'ArrowUp':
      return 'scrollUp';
    case '+':
    case '=':
      return 'zoomIn';
    case '-':
    case '_':
      return 'zoomOut';
    case '0':
      return 'fitWidth';
    default:
      return null;
  }
}
