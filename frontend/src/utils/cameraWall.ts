/** Minimal printer status used to label a camera-wall tile.
 *
 * Keep this separate from the component so the pure label choice is testable
 * without making the React component a mixed export module.
 */
export interface CameraWallPrintStatus {
  subtask_name?: string | null;
  current_print?: string | null;
  gcode_file?: string | null;
}

/** Pick the first meaningful job label reported by the printer firmware.
 *
 * The G-code field sometimes includes a printer-side directory. A camera wall
 * is not a file browser, so show only its basename and never disclose that
 * path.
 */
export function cameraWallPrintName(status: CameraWallPrintStatus | undefined): string | null {
  for (const candidate of [status?.subtask_name, status?.current_print]) {
    const label = candidate?.trim();
    if (label) return label;
  }
  const gcodeFile = status?.gcode_file?.trim();
  if (gcodeFile) return gcodeFile.split(/[\\/]/).pop() || null;
  return null;
}
