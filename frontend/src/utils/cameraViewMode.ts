/**
 * How this browser opens a camera: its own window, or the floating overlay
 * (audit D9 b, upstream 61a6ed1f).
 *
 * The choice is made on a printer's camera button and lives in the browser, so
 * two people watching one farm each keep the view they want. The Settings value
 * (`camera_view_mode`) is only the default for a browser that has never chosen
 * — and it is never written from here: an operator's pick must not change what
 * every other browser opens with.
 */

import { isHomeScreenApp } from './standaloneApp';

export type CameraViewMode = 'window' | 'embedded';

/**
 * The modes this browser can offer. A web app started from the iOS home
 * screen has no second window — `window.open` loads the camera page into the
 * app itself, with nothing to come back by — so there a camera is the overlay.
 */
export function cameraViewModes(): CameraViewMode[] {
  return isHomeScreenApp() ? ['embedded'] : ['window', 'embedded'];
}

/** The mode a camera button opens with: this browser's pick, else the farm
 *  default — and always the overlay where no window can open. */
export function resolveCameraViewMode(
  picked: CameraViewMode | null,
  farmDefault: CameraViewMode | null | undefined,
): CameraViewMode {
  if (isHomeScreenApp()) return 'embedded';
  return picked ?? farmDefault ?? 'window';
}

const STORAGE_KEY = 'cameraViewMode';

/** The mode this browser picked, or `null` when it never did (or storage is unavailable). */
export function storedCameraViewMode(): CameraViewMode | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === 'window' || value === 'embedded' ? value : null;
  } catch {
    return null;
  }
}

export function rememberCameraViewMode(mode: CameraViewMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // A private window or blocked storage: the pick still applies to this page.
  }
}
