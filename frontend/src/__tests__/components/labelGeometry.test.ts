/**
 * The arithmetic between a mouse and a millimetre.
 *
 * ⚠️ This is the part of the editor worth testing. Everything visible is a
 * frame over a picture the server drew; what can silently be wrong is the
 * conversion, the clamp and the snap — and being wrong there means a design
 * that looks right on screen and prints off the edge.
 */
import { describe, expect, it } from 'vitest';
import {
  alignBox,
  boxOf,
  clampResize,
  clampToLabel,
  GRID_MM,
  MIN_SIDE_MM,
  mmToPx,
  newElement,
  pxToMm,
  roundMm,
  snapBox,
  SNAP_TOLERANCE_MM,
} from '../../components/labels/labelGeometry';

const box = (x: number, y: number, w = 10, h = 5) => ({ x_mm: x, y_mm: y, w_mm: w, h_mm: h });

describe('millimetres and pixels', () => {
  it('round-trips through the scale', () => {
    expect(pxToMm(mmToPx(12.5, 8), 8)).toBeCloseTo(12.5);
  });

  it('rounds to a tenth of a millimetre', () => {
    // Finer than any printer resolves, and it keeps floating-point drift out
    // of what gets saved.
    expect(roundMm(3.7214)).toBe(3.7);
    expect(roundMm(0.04)).toBe(0);
  });
});

describe('staying on the label', () => {
  it('stops a box at the edge rather than shrinking it', () => {
    // ⚠️ The size is a decision somebody made; the drag was about position.
    const result = clampToLabel(box(38, 1), 40, 20);
    expect(result.x_mm).toBe(30);
    expect(result.w_mm).toBe(10);
  });

  it('pulls a negative position back to zero', () => {
    expect(clampToLabel(box(-5, -2), 40, 20)).toMatchObject({ x_mm: 0, y_mm: 0 });
  });

  it('refuses a resize that would make a box ungrabbable', () => {
    // A box below the minimum cannot be clicked again, which is a trap.
    const result = clampResize({ x_mm: 1, y_mm: 1, w_mm: 0.01, h_mm: 0.01 }, 40, 20);
    expect(result.w_mm).toBeGreaterThanOrEqual(MIN_SIDE_MM);
    expect(result.h_mm).toBeGreaterThanOrEqual(MIN_SIDE_MM);
  });

  it('trims a resize that would run off the label', () => {
    const result = clampResize({ x_mm: 35, y_mm: 1, w_mm: 20, h_mm: 5 }, 40, 20);
    expect(result.x_mm + result.w_mm).toBeLessThanOrEqual(40);
  });
});

describe('snapping', () => {
  it('lines a box up with a neighbour and says which line it used', () => {
    const { box: snapped, guides } = snapBox(box(10.3, 5), [box(10, 12)], 40, 20);
    expect(snapped.x_mm).toBe(10);
    expect(guides).toContainEqual({ axis: 'x', at_mm: 10 });
  });

  it('leaves a box alone when nothing is near, beyond the grid', () => {
    // ⚠️ The fallback is the grid, not "wherever the mouse was" — a free drag
    // must not land on 3.7214 mm.
    const { box: snapped, guides } = snapBox(box(17.3, 9.1), [], 40, 20);
    expect(snapped.x_mm % GRID_MM).toBeCloseTo(0);
    expect(guides).toHaveLength(0);
  });

  it('does not reach across the label for something far away', () => {
    const far = SNAP_TOLERANCE_MM * 4;
    const { guides } = snapBox(box(10 + far, 5), [box(10, 12)], 40, 20);
    expect(guides.some((guide) => guide.axis === 'x' && guide.at_mm === 10)).toBe(false);
  });

  it('snaps a centre to a centre, not only an edge to an edge', () => {
    // Two boxes of different widths line up on their middles; matching only
    // edges would make centring by hand impossible.
    const { box: snapped } = snapBox(box(14.8, 5, 10), [box(10, 12, 20)], 40, 20);
    expect(snapped.x_mm + 5).toBeCloseTo(20, 1);
  });

  it('snaps to the label itself when there are no neighbours', () => {
    const { box: snapped, guides } = snapBox(box(0.3, 5), [], 40, 20);
    expect(snapped.x_mm).toBe(0);
    expect(guides).toContainEqual({ axis: 'x', at_mm: 0 });
  });

  it('never leaves the label, even snapping', () => {
    const { box: snapped } = snapBox(box(39.8, 19.8), [], 40, 20);
    expect(snapped.x_mm + snapped.w_mm).toBeLessThanOrEqual(40);
    expect(snapped.y_mm + snapped.h_mm).toBeLessThanOrEqual(20);
  });
});

describe('aligning', () => {
  it('puts a box against each edge and in the middle', () => {
    expect(alignBox(box(5, 5), 'left', 40, 20).x_mm).toBe(0);
    expect(alignBox(box(5, 5), 'right', 40, 20).x_mm).toBe(30);
    expect(alignBox(box(5, 5), 'hcenter', 40, 20).x_mm).toBe(15);
    expect(alignBox(box(5, 5), 'bottom', 40, 20).y_mm).toBe(15);
    expect(alignBox(box(5, 5), 'vcenter', 40, 20).y_mm).toBe(7.5);
  });

  it('does not change the size', () => {
    const before = box(5, 5);
    expect(alignBox(before, 'right', 40, 20)).toMatchObject({ w_mm: before.w_mm, h_mm: before.h_mm });
  });
});

describe('new elements', () => {
  it('sizes itself to the label rather than to a constant', () => {
    // ⚠️ A 12 mm QR is most of a 20 mm label and a corner of a 75 mm one.
    const small = newElement('qr', 40, 20);
    const large = newElement('qr', 75, 55);
    expect(small.w_mm).toBeLessThan(large.w_mm);
  });

  it('makes a QR square', () => {
    const element = newElement('qr', 50, 30);
    expect(element.w_mm).toBe(element.h_mm);
  });

  it('lands inside the label it was made for', () => {
    for (const type of ['text', 'qr', 'barcode', 'swatch'] as const) {
      const element = newElement(type, 40, 20);
      expect(element.x_mm + element.w_mm).toBeLessThanOrEqual(40);
      expect(element.y_mm + element.h_mm).toBeLessThanOrEqual(20);
    }
  });

  it('starts with a field rather than empty text', () => {
    // An empty element resolves to nothing and is skipped by the renderer, so
    // a new box would be invisible and read as broken.
    expect(newElement('text', 40, 20).content).toContain('{');
  });

  it('boxOf keeps only the geometry', () => {
    expect(Object.keys(boxOf(newElement('text', 40, 20))).sort()).toEqual(['h_mm', 'w_mm', 'x_mm', 'y_mm']);
  });
});
