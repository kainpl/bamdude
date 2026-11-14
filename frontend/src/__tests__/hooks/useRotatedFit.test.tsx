import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  containLimits, fillStyle, fittedSize, isSideways, useRotatedBox, type Box,
} from '../../hooks/useRotatedFit';

const LANDSCAPE: Box = { width: 400, height: 260 };
const PORTRAIT: Box = { width: 260, height: 400 };
const ROTATIONS = [0, 90, 180, 270];

/** What `object-contain` draws of a picture of this aspect in a layout box. */
function contained(aspect: number, layout: Box): Box {
  const width = Math.min(layout.width, layout.height * aspect);
  return { width, height: width / aspect };
}

/** The room the drawn picture takes on screen once it is turned. */
function onScreen(drawn: Box, rotation: number): Box {
  return isSideways(rotation) ? { width: drawn.height, height: drawn.width } : drawn;
}

describe('isSideways', () => {
  it('is true for a quarter turn either way and nothing else', () => {
    expect([0, 90, 180, 270, -90, 360, 450].map(isSideways)).toEqual([false, true, false, true, true, false, true]);
    expect(isSideways(null)).toBe(false);
    expect(isSideways(undefined)).toBe(false);
  });
});

describe('fittedSize', () => {
  it('swaps the sides for a quarter turn and keeps them otherwise', () => {
    expect(fittedSize(LANDSCAPE, 0)).toEqual({ width: 400, height: 260 });
    expect(fittedSize(LANDSCAPE, 180)).toEqual({ width: 400, height: 260 });
    expect(fittedSize(LANDSCAPE, 90)).toEqual({ width: 260, height: 400 });
    expect(fittedSize(LANDSCAPE, 270)).toEqual({ width: 260, height: 400 });
    expect(fittedSize(PORTRAIT, 90)).toEqual({ width: 400, height: 260 });
    expect(fittedSize(PORTRAIT, 270)).toEqual({ width: 400, height: 260 });
  });

  // The property the whole thing exists for: laid out at the fitted size and
  // then turned, a 16:9 picture lies inside the box and touches two of its
  // sides — neither cut off nor smaller than it could be.
  it.each(
    [LANDSCAPE, PORTRAIT, { width: 1920, height: 1080 }, { width: 300, height: 300 }]
      .flatMap((box) => ROTATIONS.map((rotation) => [box.width, box.height, rotation] as const)),
  )('a 16:9 picture in a %ix%i box turned %i degrees fits and fills', (width, height, rotation) => {
    const box = { width, height };
    const seen = onScreen(contained(16 / 9, fittedSize(box, rotation)), rotation);

    expect(seen.width).toBeLessThanOrEqual(box.width + 1e-6);
    expect(seen.height).toBeLessThanOrEqual(box.height + 1e-6);
    const touchesWidth = Math.abs(seen.width - box.width) < 1e-6;
    const touchesHeight = Math.abs(seen.height - box.height) < 1e-6;
    expect(touchesWidth || touchesHeight).toBe(true);
  });

  it('is what the unswapped layout got wrong: a sideways picture overflowed a wide box', () => {
    const seen = onScreen(contained(16 / 9, LANDSCAPE), 90);
    expect(seen.height).toBeGreaterThan(LANDSCAPE.height);
  });
});

describe('fillStyle', () => {
  it('lays a sideways picture out centred at the swapped size, then turns it', () => {
    expect(fillStyle(LANDSCAPE, 90)).toEqual({
      position: 'absolute',
      left: '50%',
      top: '50%',
      width: 260,
      height: 400,
      maxWidth: 'none',
      transform: 'translate(-50%, -50%) rotate(90deg)',
    });
    expect(fillStyle(PORTRAIT, 270)).toMatchObject({
      width: 400, height: 260, transform: 'translate(-50%, -50%) rotate(270deg)',
    });
  });

  it('keeps a zoom between the centring and the turn', () => {
    expect(fillStyle(LANDSCAPE, 90, 'scale(2)').transform).toBe('translate(-50%, -50%) scale(2) rotate(90deg)');
    expect(fillStyle(LANDSCAPE, 180, 'scale(2)')).toEqual({ transform: 'scale(2) rotate(180deg)' });
  });

  it('only turns an upright or upside-down picture', () => {
    expect(fillStyle(LANDSCAPE, 0)).toEqual({});
    expect(fillStyle(LANDSCAPE, undefined)).toEqual({});
    expect(fillStyle(LANDSCAPE, 180)).toEqual({ transform: 'rotate(180deg)' });
  });

  it('only turns a sideways picture until its box is measured', () => {
    expect(fillStyle(null, 90)).toEqual({ transform: 'rotate(90deg)' });
  });
});

describe('containLimits', () => {
  it('gives a sideways picture the box sides swapped as its limits', () => {
    expect(containLimits(LANDSCAPE, 90)).toEqual({ maxWidth: 260, maxHeight: 400, flexShrink: 0 });
    expect(containLimits(PORTRAIT, 270)).toEqual({ maxWidth: 400, maxHeight: 260, flexShrink: 0 });
  });

  it('has nothing to say for an upright picture or an unmeasured box', () => {
    expect(containLimits(LANDSCAPE, 0)).toBeNull();
    expect(containLimits(LANDSCAPE, 180)).toBeNull();
    expect(containLimits(null, 90)).toBeNull();
  });
});

describe('useRotatedBox', () => {
  let size: Box = LANDSCAPE;
  let resized: (() => void) | undefined;
  const observed: Element[] = [];
  const disconnect = vi.fn();

  function stubLayout() {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return this.dataset.box ? size.width : 0;
    });
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.dataset.box ? size.height : 0;
    });
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { resized = callback; }
      observe = (element: Element) => { observed.push(element); };
      unobserve = vi.fn();
      disconnect = disconnect;
    });
  }

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    size = LANDSCAPE;
    resized = undefined;
    observed.length = 0;
    disconnect.mockClear();
  });

  function Probe({ rotation, shown = true }: { rotation: number; shown?: boolean }) {
    const { boxRef, box } = useRotatedBox(rotation);
    return shown
      ? <div ref={boxRef} data-box="1" data-testid="probe">{box ? `${box.width}x${box.height}` : 'none'}</div>
      : <p>hidden</p>;
  }

  it('measures the box of a sideways picture and follows a resize', () => {
    stubLayout();
    const view = render(<Probe rotation={90} />);
    expect(view.getByTestId('probe').textContent).toBe('400x260');

    size = PORTRAIT;
    act(() => resized?.());
    expect(view.getByTestId('probe').textContent).toBe('260x400');
  });

  it('measures nothing for an upright picture', () => {
    stubLayout();
    const view = render(<Probe rotation={180} />);
    expect(view.getByTestId('probe').textContent).toBe('none');
    expect(observed).toHaveLength(0);
  });

  it('picks up a box that appears later and lets go of one that is gone', () => {
    stubLayout();
    const view = render(<Probe rotation={270} shown={false} />);
    expect(observed).toHaveLength(0);

    view.rerender(<Probe rotation={270} />);
    expect(view.getByTestId('probe').textContent).toBe('400x260');

    view.rerender(<Probe rotation={270} shown={false} />);
    expect(disconnect).toHaveBeenCalled();
  });

  // The rotation is a printer setting: it changes under an open view.
  it('stops measuring when a sideways camera is set upright again', () => {
    stubLayout();
    const view = render(<Probe rotation={90} />);
    expect(view.getByTestId('probe').textContent).toBe('400x260');
    expect(observed).toHaveLength(1);

    view.rerender(<Probe rotation={0} />);

    expect(view.getByTestId('probe').textContent).toBe('none');
    expect(disconnect).toHaveBeenCalled();
    // A resize that still arrives measures nothing for an upright picture.
    size = PORTRAIT;
    act(() => resized?.());
    expect(view.getByTestId('probe').textContent).toBe('none');
  });

  it('keeps the measured box through a turn from one side to the other, and through every later turn', () => {
    stubLayout();
    const view = render(<Probe rotation={90} />);

    view.rerender(<Probe rotation={270} />);
    expect(view.getByTestId('probe').textContent).toBe('400x260');
    size = PORTRAIT;
    act(() => resized?.());
    expect(view.getByTestId('probe').textContent).toBe('260x400');

    view.rerender(<Probe rotation={180} />);
    expect(view.getByTestId('probe').textContent).toBe('none');
    view.rerender(<Probe rotation={90} />);
    // Measured afresh: the size the box has now, not the one from before.
    expect(view.getByTestId('probe').textContent).toBe('260x400');
  });

  it('starts measuring when the camera is turned sideways afterwards', () => {
    stubLayout();
    const view = render(<Probe rotation={0} />);
    expect(view.getByTestId('probe').textContent).toBe('none');

    view.rerender(<Probe rotation={90} />);
    expect(view.getByTestId('probe').textContent).toBe('400x260');
  });
});
