// When a Cam Wall tile shows its controls (upstream #3479): on hover (and focus), so
// the picture stays clear, or always. Touch screens have no hover and always show
// them. Stored per browser, like the wall's other settings.
export type CamWallControlsMode = 'hover' | 'always';

export const CAM_WALL_CONTROLS_KEY = 'camWallControls';

export function savedCamWallControlsMode(): CamWallControlsMode {
  try {
    return localStorage.getItem(CAM_WALL_CONTROLS_KEY) === 'always' ? 'always' : 'hover';
  } catch {
    // Storage unavailable (a private window, blocked site data): the default.
    return 'hover';
  }
}

export function saveCamWallControlsMode(mode: CamWallControlsMode): void {
  try {
    localStorage.setItem(CAM_WALL_CONTROLS_KEY, mode);
  } catch {
    // Not remembered; the wall keeps the choice for this visit.
  }
}
