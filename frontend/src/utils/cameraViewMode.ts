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

export type CameraViewMode = 'window' | 'embedded';

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
