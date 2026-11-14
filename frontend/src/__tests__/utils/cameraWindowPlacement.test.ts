import { afterEach, describe, expect, it } from 'vitest';
import {
  appTopPx, defaultCameraWindow, draggedCameraWindow, resizedCameraWindow, restoredCameraWindow,
} from '../../utils/cameraWindowPlacement';

const DESKTOP = { width: 1440, height: 900, top: 0 };
const PHONE = { width: 375, height: 667, top: 56 };

describe('defaultCameraWindow', () => {
  it('stands top right on a desktop, as it always did', () => {
    expect(defaultCameraWindow(DESKTOP)).toEqual({ x: 1020, y: 20, width: 400, height: 300 });
  });

  it('lies whole inside a phone screen and below the compact header', () => {
    const state = defaultCameraWindow(PHONE);
    expect(state.x).toBeGreaterThanOrEqual(0);
    expect(state.x + state.width).toBeLessThanOrEqual(PHONE.width);
    expect(state.y).toBeGreaterThanOrEqual(PHONE.top);
    expect(state.y + state.height).toBeLessThanOrEqual(PHONE.height);
  });

  it('stays below the header on a narrow desktop window too', () => {
    expect(defaultCameraWindow({ width: 1000, height: 700, top: 56 }).y).toBe(76);
  });
});

describe('restoredCameraWindow', () => {
  it('keeps a saved window that is already in place', () => {
    const saved = { x: 300, y: 200, width: 500, height: 350 };
    expect(restoredCameraWindow(saved, DESKTOP)).toEqual(saved);
  });

  it('brings a window saved on a wide screen back onto a phone', () => {
    const state = restoredCameraWindow({ x: 1500, y: 800, width: 900, height: 700 }, PHONE);
    expect(state.x).toBeLessThanOrEqual(PHONE.width - 100);
    expect(state.y).toBeLessThanOrEqual(PHONE.height - 100);
    expect(state.width).toBeLessThanOrEqual(PHONE.width);
    expect(state.height).toBeLessThanOrEqual(PHONE.height - PHONE.top);
  });

  it('moves a window saved above the header line down to it', () => {
    expect(restoredCameraWindow({ x: 10, y: 5, width: 300, height: 200 }, PHONE).y).toBe(56);
    expect(restoredCameraWindow({ x: -40, y: -10, width: 300, height: 200 }, DESKTOP)).toMatchObject({ x: 0, y: 0 });
  });
});

// The resize handle used arithmetic of its own, against the raw window size:
// it knew nothing of the header line or of the limits a restored window obeys.
describe('resizedCameraWindow', () => {
  const state = { x: 100, y: 200, width: 300, height: 200 };

  it('follows the pointer from the window corner', () => {
    expect(resizedCameraWindow(state, { x: 600, y: 650 }, DESKTOP)).toEqual({ x: 100, y: 200, width: 500, height: 450 });
  });

  it('stops at the edges of the screen', () => {
    expect(resizedCameraWindow(state, { x: 9999, y: 9999 }, DESKTOP)).toEqual({ x: 100, y: 200, width: 1340, height: 700 });
  });

  it('keeps the smallest size a restored window may have', () => {
    expect(resizedCameraWindow(state, { x: -50, y: -50 }, DESKTOP)).toMatchObject({ width: 200, height: 150 });
  });

  it('never leaves the window under the header, wherever it stood when the drag began', () => {
    const under = resizedCameraWindow({ x: 10, y: 5, width: 300, height: 200 }, { x: 9999, y: 9999 }, PHONE);
    expect(under.y).toBe(56);
    expect(under.y + under.height).toBeLessThanOrEqual(PHONE.height);
    expect(under.x + under.width).toBeLessThanOrEqual(PHONE.width);
  });

  it('gives back what the restore rule would keep', () => {
    for (const pointer of [{ x: 9999, y: 9999 }, { x: 250, y: 400 }, { x: -9, y: -9 }]) {
      const resized = resizedCameraWindow({ x: 20, y: 80, width: 300, height: 200 }, pointer, PHONE);
      expect(restoredCameraWindow(resized, PHONE)).toEqual(resized);
    }
  });
});

describe('draggedCameraWindow', () => {
  const size = { width: 300, height: 200 };

  it('stops a drag at the header line, not at the top of the screen', () => {
    expect(draggedCameraWindow(20, 0, size, PHONE)).toEqual({ x: 20, y: 56 });
    expect(draggedCameraWindow(20, 0, size, DESKTOP)).toEqual({ x: 20, y: 0 });
  });

  it('keeps the window whole on screen', () => {
    expect(draggedCameraWindow(999, 999, size, PHONE)).toEqual({ x: 75, y: 467 });
    expect(draggedCameraWindow(-5, 300, size, PHONE)).toEqual({ x: 0, y: 300 });
  });
});

describe('appTopPx', () => {
  afterEach(() => {
    document.documentElement.style.removeProperty('--app-top');
    document.documentElement.style.removeProperty('font-size');
  });

  it('is 0 when no header is announced', () => {
    expect(appTopPx()).toBe(0);
    document.documentElement.style.setProperty('--app-top', '0px');
    expect(appTopPx()).toBe(0);
  });

  it('converts the rem height Layout announces', () => {
    document.documentElement.style.setProperty('--app-top', '3.5rem');
    expect(appTopPx()).toBe(56);
    document.documentElement.style.setProperty('font-size', '20px');
    expect(appTopPx()).toBe(70);
  });

  it('reads a height given in px as it is', () => {
    document.documentElement.style.setProperty('--app-top', '48px');
    expect(appTopPx()).toBe(48);
  });
});
