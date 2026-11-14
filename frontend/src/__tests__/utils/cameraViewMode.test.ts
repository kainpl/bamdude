import { afterEach, describe, expect, it } from 'vitest';
import { cameraViewModes, resolveCameraViewMode } from '../../utils/cameraViewMode';
import { isHomeScreenApp } from '../../utils/standaloneApp';

function setHomeScreenApp(value: boolean | undefined) {
  Object.defineProperty(window.navigator, 'standalone', { configurable: true, value });
}

afterEach(() => {
  Reflect.deleteProperty(window.navigator, 'standalone');
});

describe('isHomeScreenApp', () => {
  it('is true only where the browser says it runs from the home screen', () => {
    expect(isHomeScreenApp()).toBe(false);
    setHomeScreenApp(false);
    expect(isHomeScreenApp()).toBe(false);
    setHomeScreenApp(true);
    expect(isHomeScreenApp()).toBe(true);
  });
});

describe('resolveCameraViewMode', () => {
  it('is the browser pick, else the farm default, else a window', () => {
    expect(resolveCameraViewMode('embedded', 'window')).toBe('embedded');
    expect(resolveCameraViewMode('window', 'embedded')).toBe('window');
    expect(resolveCameraViewMode(null, 'embedded')).toBe('embedded');
    expect(resolveCameraViewMode(null, undefined)).toBe('window');
  });

  // A home-screen web app has no second window: `window.open` loads the camera
  // page into the app itself, with no browser chrome to come back by.
  it('is always the overlay in a home-screen web app', () => {
    setHomeScreenApp(true);
    expect(resolveCameraViewMode('window', 'window')).toBe('embedded');
    expect(resolveCameraViewMode(null, undefined)).toBe('embedded');
  });
});

describe('cameraViewModes', () => {
  it('offers both modes in a browser and only the overlay in a home-screen web app', () => {
    expect(cameraViewModes()).toEqual(['window', 'embedded']);
    setHomeScreenApp(true);
    expect(cameraViewModes()).toEqual(['embedded']);
  });
});
