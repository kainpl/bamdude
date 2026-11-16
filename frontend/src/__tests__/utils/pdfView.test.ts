/**
 * The PDF viewer's arithmetic: which scale a zoom mode means, where the pages
 * lie, which of them are on screen, and what a key does.
 */
import { describe, it, expect } from 'vitest';
import {
  MAX_SCALE,
  MIN_SCALE,
  anchorAt,
  anchorOffset,
  clampPage,
  currentPage,
  keyAction,
  layoutPages,
  outputScale,
  parsePageInput,
  scaleFor,
  visibleRange,
  widestPage,
  zoomStep,
} from '../../utils/pdfView';

const A4 = { width: 595, height: 842 };

describe('scaleFor', () => {
  it('fits the page width to the stage, less the padding on both sides', () => {
    expect(scaleFor('width', A4, { width: 1190 + 32, height: 500 }, 16)).toBeCloseTo(2);
  });

  it('fits the whole page into the stage, whichever side binds', () => {
    // A tall stage: the width binds.
    expect(scaleFor('page', A4, { width: 595 + 32, height: 5000 }, 16)).toBeCloseTo(1);
    // A wide stage: the height binds.
    expect(scaleFor('page', A4, { width: 5000, height: 421 + 32 }, 16)).toBeCloseTo(0.5);
  });

  it('takes an explicit scale as given, within the limits', () => {
    expect(scaleFor(1.5, A4, { width: 100, height: 100 }, 16)).toBe(1.5);
    expect(scaleFor(100, A4, { width: 100, height: 100 }, 16)).toBe(MAX_SCALE);
    expect(scaleFor(0.001, A4, { width: 100, height: 100 }, 16)).toBe(MIN_SCALE);
  });

  it('answers 1 for a stage that has no size yet', () => {
    expect(scaleFor('width', A4, { width: 0, height: 0 }, 16)).toBe(1);
    expect(scaleFor('page', A4, { width: 0, height: 0 }, 16)).toBe(1);
  });
});

describe('zoomStep', () => {
  it('goes up and down by one step and stops at the limits', () => {
    expect(zoomStep(1, 1)).toBeCloseTo(1.25);
    expect(zoomStep(1.25, -1)).toBeCloseTo(1);
    expect(zoomStep(MAX_SCALE, 1)).toBe(MAX_SCALE);
    expect(zoomStep(MIN_SCALE, -1)).toBe(MIN_SCALE);
  });
});

describe('pages', () => {
  it('clamps a page number into the document', () => {
    expect(clampPage(0, 5)).toBe(1);
    expect(clampPage(9, 5)).toBe(5);
    expect(clampPage(3, 5)).toBe(3);
  });

  it('reads the page box: a whole number in range, nothing else', () => {
    expect(parsePageInput(' 3 ', 5)).toBe(3);
    expect(parsePageInput('99', 5)).toBe(5);
    expect(parsePageInput('0', 5)).toBe(1);
    expect(parsePageInput('', 5)).toBeNull();
    expect(parsePageInput('2.5', 5)).toBeNull();
    expect(parsePageInput('abc', 5)).toBeNull();
  });

  it('lays the pages out one under another with a gap', () => {
    const layout = layoutPages([A4, { width: 842, height: 595 }, A4], 2, 10);
    expect(layout.heights).toEqual([1684, 1190, 1684]);
    expect(layout.widths).toEqual([1190, 1684, 1190]);
    expect(layout.tops).toEqual([0, 1694, 2894]);
    expect(layout.total).toBe(4578);
  });

  it('names the pages on screen, with the neighbours on each side', () => {
    const { tops } = layoutPages(Array(10).fill(A4), 1, 10); // each 842 + 10
    // The top of the document, one screen tall: page 1 and part of 2, plus page 3 ahead.
    expect(visibleRange(tops, 0, 900, 1)).toEqual({ first: 1, last: 3 });
    // Scrolled into page 5.
    expect(visibleRange(tops, tops[4] + 5, 400, 1)).toEqual({ first: 4, last: 6 });
    // The end.
    expect(visibleRange(tops, tops[9], 900, 1)).toEqual({ first: 9, last: 10 });
    // No overscan, a stage with no height: the one page under the scroll position.
    expect(visibleRange(tops, tops[2] + 1, 0, 0)).toEqual({ first: 3, last: 3 });
  });

  it('calls current the page that holds the middle of the screen', () => {
    const { tops } = layoutPages(Array(4).fill(A4), 1, 10);
    expect(currentPage(tops, 0, 600)).toBe(1);
    // Page 2 starts at 852; with the screen's middle past it, page 2 is current.
    expect(currentPage(tops, 700, 600)).toBe(2);
    expect(currentPage(tops, 1e9, 600)).toBe(4);
    expect(currentPage([], 0, 600)).toBe(1);
  });
});

describe('outputScale', () => {
  it('draws at the device pixel ratio', () => {
    expect(outputScale({ width: 100, height: 100 }, 2, 1e9)).toBe(2);
  });

  it('draws coarser rather than allocate more pixels than the cap', () => {
    // 4000 × 4000 CSS px at dpr 2 would be 64 MP; the cap is 16 MP → ratio 1.
    expect(outputScale({ width: 4000, height: 4000 }, 2, 16_000_000)).toBeCloseTo(1);
  });
});

describe('keyAction', () => {
  it('maps the navigation and zoom keys', () => {
    expect(keyAction('PageDown')).toBe('next');
    expect(keyAction('ArrowRight')).toBe('next');
    expect(keyAction('PageUp')).toBe('prev');
    expect(keyAction('ArrowLeft')).toBe('prev');
    expect(keyAction('Home')).toBe('first');
    expect(keyAction('End')).toBe('last');
    expect(keyAction('ArrowDown')).toBe('scrollDown');
    expect(keyAction('ArrowUp')).toBe('scrollUp');
    expect(keyAction('+')).toBe('zoomIn');
    expect(keyAction('=')).toBe('zoomIn');
    expect(keyAction('-')).toBe('zoomOut');
    expect(keyAction('0')).toBe('fitWidth');
    expect(keyAction('a')).toBeNull();
  });
});

describe('widestPage', () => {
  it('is the page a fit to the width must fit in a document of mixed sizes', () => {
    expect(widestPage([A4, { width: 842, height: 595 }, A4])).toEqual({ width: 842, height: 595 });
    expect(widestPage([])).toEqual({ width: 0, height: 0 });
  });
});

describe('a scroll anchor', () => {
  const sizes = [
    { width: 600, height: 800 },
    { width: 600, height: 800 },
    { width: 600, height: 800 },
  ];

  it('names the page under the top edge and how far down it the edge is', () => {
    const layout = layoutPages(sizes, 1, 12);
    expect(anchorAt(layout, 0, 12)).toEqual({ page: 1, fraction: 0 });
    expect(anchorAt(layout, 812 + 406, 12)).toEqual({ page: 2, fraction: 0.5 });
  });

  it('is found again at the same place of the same page after the pages change size', () => {
    const before = layoutPages(sizes, 1, 12);
    const anchor = anchorAt(before, 812 + 406, 12);

    // Twice the scale: the same half-way point of page 2.
    const zoomed = layoutPages(sizes, 2, 12);
    expect(anchorOffset(zoomed, anchor, 12)).toBe(1612 + 806);

    // Page 1 turns out to be half as tall: page 2 starts 400 px higher.
    const measured = layoutPages([{ width: 600, height: 400 }, sizes[1], sizes[2]], 1, 12);
    expect(anchorOffset(measured, anchor, 12)).toBe(412 + 406);
  });

  it('is the top of the document when there are no pages', () => {
    expect(anchorOffset(layoutPages([], 1, 12), { page: 3, fraction: 0.5 }, 12)).toBe(0);
  });
});
