/**
 * The one floating camera the Printers page keeps open, as the browser
 * remembers it — and whether a remembered one may be shown.
 *
 * Browser storage outlives what it describes: the user may have lost
 * `camera:view`, the printer may be deleted (and its id handed to the next
 * one), the camera switched off. A window for any of those shows "Camera
 * unavailable" on every visit, under a name that may no longer exist. So a
 * selection is shown only once the list that can vouch for it has answered.
 */

import type { CameraSourceKind } from './cameraSource';

export interface EmbeddedCameraSelection {
  kind: CameraSourceKind;
  id: number;
  name: string;
}

const STORAGE_KEY = 'openEmbeddedCameras';

/**
 * Reads the old array format but only its last valid entry, so upgrading
 * cannot reopen a fleet of live connections. An entry saved before a camera
 * could stand on its own has no kind and is a printer, which is what it was.
 */
export function readStoredEmbeddedCamera(): EmbeddedCameraSelection | null {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    if (!Array.isArray(saved)) return null;
    const last = saved.filter((camera) => camera && Number.isInteger(camera.id) && camera.id > 0
      && typeof camera.name === 'string').at(-1);
    if (!last) return null;
    return { kind: last.kind === 'camera' ? 'camera' : 'printer', id: last.id, name: last.name };
  } catch {
    return null;
  }
}

export function storeEmbeddedCamera(selection: EmbeddedCameraSelection | null): void {
  try {
    if (selection) localStorage.setItem(STORAGE_KEY, JSON.stringify([selection]));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // A private window or blocked storage: the window still works on this page.
  }
}

export interface EmbeddedCameraFacts {
  /** The user holds `camera:view`. */
  canView: boolean;
  /** The printers the user can see — `undefined` until fetched. */
  printers: readonly { id: number }[] | undefined;
  /** The standalone cameras — `undefined` until fetched. */
  cameras: readonly { id: number; enabled: boolean }[] | undefined;
}

/**
 * `show` — the source is there and may be watched; `wait` — its list has not
 * answered yet, so nothing is shown and nothing is forgotten; `drop` — forget
 * the selection.
 */
export function checkEmbeddedCamera(
  selection: EmbeddedCameraSelection,
  { canView, printers, cameras }: EmbeddedCameraFacts,
): 'show' | 'wait' | 'drop' {
  if (!canView) return 'drop';
  if (selection.kind === 'printer') {
    if (!printers) return 'wait';
    return printers.some((printer) => printer.id === selection.id) ? 'show' : 'drop';
  }
  if (!cameras) return 'wait';
  return cameras.some((camera) => camera.id === selection.id && camera.enabled) ? 'show' : 'drop';
}
