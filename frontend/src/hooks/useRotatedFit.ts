import { useCallback, useState, type CSSProperties } from 'react';

/**
 * Fitting a camera picture that is turned with CSS `rotate()` into its box.
 *
 * `rotate()` turns the pixels and leaves layout alone, so a picture turned a
 * quarter is still laid out as if it were upright: fitted to the box, then
 * turned, it is cut off along one side and boxed in along the other. The cure
 * is to lay it out in the box's sides SWAPPED and then turn it.
 *
 * One home for that, used by every surface that shows a camera — the floating
 * window, the camera page, the Cam Wall tile (live and snapshot) and the
 * stream overlay.
 *
 * ⚠️ Measured, not written as container units (`100cqh`): an OBS browser
 * source and a wall kiosk may run a Chromium older than those units, and
 * viewport units are wrong for any box smaller than the window.
 */

export interface Box {
  width: number;
  height: number;
}

/** Whether a rotation turns the picture on its side. */
export function isSideways(rotation: number | null | undefined): boolean {
  return Math.abs((rotation ?? 0) % 180) === 90;
}

/** The size the picture is laid out at, before it is turned, so that the
 *  turned picture occupies exactly `box`. */
export function fittedSize(box: Box, rotation: number | null | undefined): Box {
  return isSideways(rotation) ? { width: box.height, height: box.width } : box;
}

/**
 * Style of a picture that FILLS its box (`h-full w-full object-contain`): the
 * tile, the snapshot, the overlay. `before` is any transform applied around
 * the turn (a zoom).
 *
 * Until the box is measured, and for an upright picture, this is the plain
 * turn the surfaces always had.
 */
export function fillStyle(box: Box | null, rotation: number | null | undefined, before = ''): CSSProperties {
  const turn = rotation ? `rotate(${rotation}deg)` : '';
  if (!box || !isSideways(rotation)) {
    const transform = [before, turn].filter(Boolean).join(' ');
    return transform ? { transform } : {};
  }
  const size = fittedSize(box, rotation);
  return {
    // Centred by position, not by the box's flow: the swapped size is larger
    // than the box along one side.
    position: 'absolute',
    left: '50%',
    top: '50%',
    width: size.width,
    height: size.height,
    // The base stylesheet caps every image at its container's width.
    maxWidth: 'none',
    transform: ['translate(-50%, -50%)', before, turn].filter(Boolean).join(' '),
  };
}

/**
 * Limits of a picture that keeps its own size up to the box (`max-w-full
 * max-h-full` in a centring flex box): the floating window and the camera
 * page, whose zoom and pan own the transform. `null` when there is nothing to
 * swap — an upright picture, or a box not measured yet.
 */
export function containLimits(box: Box | null, rotation: number | null | undefined): CSSProperties | null {
  if (!box || !isSideways(rotation)) return null;
  const size = fittedSize(box, rotation);
  // No shrinking: the limit along the box's short side is longer than the box.
  return { maxWidth: size.width, maxHeight: size.height, flexShrink: 0 };
}

/**
 * The box a camera picture is shown in, measured only while the picture is
 * turned sideways and followed through every resize. Attach `boxRef` to the
 * element the picture must fit; an element that mounts later (a restored
 * window, a page past its loading state) is measured when it appears.
 *
 * ⚠️ The ref itself measures, and only for a sideways picture: an upright
 * camera costs its surface no state and no extra render.
 */
export function useRotatedBox(rotation: number | null | undefined): {
  boxRef: (element: HTMLElement | null) => void | (() => void);
  box: Box | null;
} {
  const sideways = isSideways(rotation);
  const [box, setBox] = useState<Box | null>(null);

  // A ref callback runs in the commit, before paint, so a sideways camera
  // never shows a frame at the unswapped size.
  const boxRef = useCallback((element: HTMLElement | null) => {
    if (!element || !sideways) return;
    const measure = () => {
      // Layout size, so a transform on an ancestor is left out.
      const width = element.clientWidth;
      const height = element.clientHeight;
      if (width > 0 && height > 0) {
        setBox((prev) => (prev && prev.width === width && prev.height === height ? prev : { width, height }));
      }
    };
    measure();
    let stop: () => void;
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      stop = () => window.removeEventListener('resize', measure);
    } else {
      const observer = new ResizeObserver(measure);
      observer.observe(element);
      stop = () => observer.disconnect();
    }
    return () => {
      stop();
      // The next box is not this one's size.
      setBox(null);
    };
  }, [sideways]);

  return { boxRef, box: sideways ? box : null };
}
