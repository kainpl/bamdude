/**
 * Millimetres in, millimetres out.
 *
 * ⚠️ **Pixels exist inside the canvas and nowhere else.** A template is stored
 * in millimetres because that is the only unit shared by a 203 dpi head, a
 * 300 dpi head and a sheet of paper; the moment a pixel escapes this boundary,
 * a design starts depending on how big somebody's browser window was.
 *
 * Everything here is pure arithmetic, which is why it is a module rather than
 * hooks inside the canvas — it is the part worth testing.
 */
import type { LabelTemplateElement } from '../../api/client';

/** Nothing may be smaller than this. Below it a box cannot be grabbed again. */
export const MIN_SIDE_MM = 1;

/** What an arrow key moves, and what shift-arrow moves. */
export const NUDGE_MM = 0.5;
export const NUDGE_COARSE_MM = 2;

/** How close a drag has to come before it sticks. In millimetres, so the feel
 *  is the same whatever the zoom — a pixel threshold would make snapping
 *  aggressive when zoomed out and unreachable when zoomed in. */
export const SNAP_TOLERANCE_MM = 0.6;

/** The background grid, and what dragging snaps to when nothing else is near. */
export const GRID_MM = 0.5;

export interface Box {
  x_mm: number;
  y_mm: number;
  w_mm: number;
  h_mm: number;
}

/** A line the editor draws while something is stuck to it. */
export interface Guide {
  axis: 'x' | 'y';
  /** Millimetres from the top-left of the label. */
  at_mm: number;
}

export const mmToPx = (mm: number, scale: number): number => mm * scale;
export const pxToMm = (px: number, scale: number): number => px / scale;

/** Round to a tenth of a millimetre — finer than any printer resolves, and it
 *  keeps floating-point drift out of what gets saved. */
export const roundMm = (mm: number): number => Math.round(mm * 10) / 10;

/**
 * Keep a box inside the label.
 *
 * ⚠️ Moves rather than shrinks. A box dragged past the edge should stop, not
 * quietly resize — the size is a decision somebody made and the drag was about
 * position.
 */
export function clampToLabel(box: Box, widthMm: number, heightMm: number): Box {
  return {
    ...box,
    x_mm: Math.min(Math.max(box.x_mm, 0), Math.max(0, widthMm - box.w_mm)),
    y_mm: Math.min(Math.max(box.y_mm, 0), Math.max(0, heightMm - box.h_mm)),
  };
}

/** Refuse a resize that would make a box ungrabbable or push it off the label. */
export function clampResize(box: Box, widthMm: number, heightMm: number): Box {
  const x = Math.max(0, Math.min(box.x_mm, widthMm - MIN_SIDE_MM));
  const y = Math.max(0, Math.min(box.y_mm, heightMm - MIN_SIDE_MM));
  return {
    x_mm: x,
    y_mm: y,
    w_mm: Math.max(MIN_SIDE_MM, Math.min(box.w_mm, widthMm - x)),
    h_mm: Math.max(MIN_SIDE_MM, Math.min(box.h_mm, heightMm - y)),
  };
}

const edgesOf = (box: Box) => ({
  x: [box.x_mm, box.x_mm + box.w_mm / 2, box.x_mm + box.w_mm],
  y: [box.y_mm, box.y_mm + box.h_mm / 2, box.y_mm + box.h_mm],
});

/**
 * Pull a dragged box onto whatever is nearest: another element's edges or
 * centre, the label's own edges and centre, or the grid.
 *
 * ⚠️ Neighbours beat the label, and the label beats the grid. Lining two boxes
 * up with each other is what somebody is actually trying to do; the grid is the
 * fallback that keeps free dragging tidy, and letting it win would fight them.
 *
 * Returns the box it landed on plus the guides to draw, so the caller does not
 * have to work out which rule fired.
 */
export function snapBox(
  box: Box,
  others: Box[],
  widthMm: number,
  heightMm: number,
): { box: Box; guides: Guide[] } {
  const guides: Guide[] = [];
  const moving = edgesOf(box);

  const candidatesFor = (axis: 'x' | 'y'): number[] => {
    const fromOthers = others.flatMap((other) => edgesOf(other)[axis]);
    const size = axis === 'x' ? widthMm : heightMm;
    return [...fromOthers, 0, size / 2, size];
  };

  const settle = (axis: 'x' | 'y'): number => {
    const origin = axis === 'x' ? box.x_mm : box.y_mm;
    const extent = axis === 'x' ? box.w_mm : box.h_mm;
    let best: { delta: number; at: number } | null = null;

    for (const candidate of candidatesFor(axis)) {
      for (const edge of moving[axis]) {
        const delta = candidate - edge;
        if (Math.abs(delta) > SNAP_TOLERANCE_MM) continue;
        if (best === null || Math.abs(delta) < Math.abs(best.delta)) {
          best = { delta, at: candidate };
        }
      }
    }

    if (best !== null) {
      guides.push({ axis, at_mm: best.at });
      return origin + best.delta;
    }
    // Nothing to line up with: fall back to the grid, which is what keeps a
    // free drag from landing on 3.7214 mm.
    void extent;
    return Math.round(origin / GRID_MM) * GRID_MM;
  };

  return {
    box: clampToLabel({ ...box, x_mm: roundMm(settle('x')), y_mm: roundMm(settle('y')) }, widthMm, heightMm),
    guides,
  };
}

export type Alignment = 'left' | 'hcenter' | 'right' | 'top' | 'vcenter' | 'bottom';

/** Put one box against an edge or the middle of the label. */
export function alignBox(box: Box, how: Alignment, widthMm: number, heightMm: number): Box {
  switch (how) {
    case 'left':
      return { ...box, x_mm: 0 };
    case 'hcenter':
      return { ...box, x_mm: roundMm((widthMm - box.w_mm) / 2) };
    case 'right':
      return { ...box, x_mm: roundMm(widthMm - box.w_mm) };
    case 'top':
      return { ...box, y_mm: 0 };
    case 'vcenter':
      return { ...box, y_mm: roundMm((heightMm - box.h_mm) / 2) };
    case 'bottom':
      return { ...box, y_mm: roundMm(heightMm - box.h_mm) };
  }
}

/** The box of an element, without the rest of it. */
export const boxOf = (element: LabelTemplateElement): Box => ({
  x_mm: element.x_mm,
  y_mm: element.y_mm,
  w_mm: element.w_mm,
  h_mm: element.h_mm,
});

/**
 * A new element, sized to something usable on this label rather than to a
 * constant — a 12 mm QR is most of a 20 mm label and a corner of a 75 mm one.
 */
export function newElement(
  type: LabelTemplateElement['type'],
  widthMm: number,
  heightMm: number,
): LabelTemplateElement {
  const shortest = Math.min(widthMm, heightMm);
  const box = {
    x_mm: roundMm(widthMm * 0.1),
    y_mm: roundMm(heightMm * 0.1),
    w_mm: roundMm(widthMm * 0.5),
    h_mm: roundMm(Math.max(MIN_SIDE_MM, shortest * 0.18)),
  };

  switch (type) {
    case 'qr': {
      const side = roundMm(Math.min(shortest * 0.5, 16));
      return { type: 'qr', ...box, w_mm: side, h_mm: side, content: '{deeplink}' };
    }
    case 'barcode':
      return {
        type: 'barcode',
        ...box,
        w_mm: roundMm(widthMm * 0.7),
        h_mm: roundMm(Math.max(MIN_SIDE_MM, heightMm * 0.25)),
        content: '{ean}',
        symbology: 'ean13',
      };
    case 'swatch':
      return { type: 'swatch', ...box, w_mm: roundMm(shortest * 0.2), content: '{color_hex_all}', shape: 'rect' };
    case 'text':
    default:
      return {
        type: 'text',
        ...box,
        content: '{display_name}',
        size_mm: box.h_mm,
        bold: false,
        italic: false,
        align: 'left',
        valign: 'top',
        fit: 'shrink',
      };
  }
}
