/**
 * Where the floating camera window may stand.
 *
 * The window is `position: fixed`, so its coordinates are the viewport's. Two
 * things it must never do: start (or be restored) off the screen — a phone is
 * narrower than the desktop default — and lie under the fixed compact header,
 * whose height `Layout` publishes as `--app-top` on `<html>`. Under the header
 * its title bar, the only handle it is dragged and closed by, cannot be reached.
 */

export interface CameraWindowState {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Viewport {
  width: number;
  height: number;
  /** Height of the fixed compact header in px; 0 when there is none. */
  top: number;
}

const DEFAULT_WIDTH = 400;
const DEFAULT_HEIGHT = 300;
const MARGIN = 20;
const MIN_WIDTH = 200;
const MIN_HEIGHT = 150;
/** How much of a restored window must stay on screen to be grabbed. */
const GRIP = 100;

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, max));

/** `--app-top` in px. It is written in `rem` (or `0px`), and a custom property
 *  is not resolved for us, so it is converted against the root font size. */
export function appTopPx(): number {
  const root = document.documentElement;
  const raw = root.style.getPropertyValue('--app-top') || getComputedStyle(root).getPropertyValue('--app-top');
  const value = parseFloat(raw);
  if (!Number.isFinite(value) || value <= 0) return 0;
  if (raw.trim().endsWith('rem')) {
    return value * (parseFloat(getComputedStyle(root).fontSize) || 16);
  }
  return value;
}

export function currentViewport(): Viewport {
  return { width: window.innerWidth, height: window.innerHeight, top: appTopPx() };
}

/** The size a window may have in this viewport. */
function fitSize(width: number, height: number, viewport: Viewport) {
  return {
    width: clamp(width, Math.min(MIN_WIDTH, viewport.width), Math.max(MIN_WIDTH, viewport.width - MARGIN)),
    height: clamp(height, MIN_HEIGHT, Math.max(MIN_HEIGHT, viewport.height - viewport.top - MARGIN)),
  };
}

/** A first-opened window: top right, whole on screen, below the header. */
export function defaultCameraWindow(viewport: Viewport): CameraWindowState {
  const { width, height } = fitSize(DEFAULT_WIDTH, DEFAULT_HEIGHT, viewport);
  return {
    x: Math.max(0, viewport.width - width - MARGIN),
    y: viewport.top + MARGIN,
    width,
    height,
  };
}

/** A saved window, brought back to where it can be seen and grabbed. */
export function restoredCameraWindow(saved: CameraWindowState, viewport: Viewport): CameraWindowState {
  return {
    x: clamp(saved.x, 0, Math.max(0, viewport.width - GRIP)),
    y: clamp(saved.y, viewport.top, Math.max(viewport.top, viewport.height - GRIP)),
    ...fitSize(saved.width, saved.height, viewport),
  };
}

/** Where a drag may put a window of this size: whole on screen, below the header. */
export function draggedCameraWindow(
  x: number, y: number, size: { width: number; height: number }, viewport: Viewport,
): { x: number; y: number } {
  return {
    x: clamp(x, 0, Math.max(0, viewport.width - size.width)),
    y: clamp(y, viewport.top, Math.max(viewport.top, viewport.height - size.height)),
  };
}
