import { describe, expect, it } from 'vitest';
import { cameraWallPrintName } from '../../utils/cameraWall';

describe('cameraWallPrintName', () => {
  it('uses the first non-empty status label supplied by the printer firmware', () => {
    expect(cameraWallPrintName({
      subtask_name: '  ',
      current_print: 'Functional bracket',
      gcode_file: 'fallback.gcode',
    })).toBe('Functional bracket');
  });

  it('falls through to the uploaded filename without exposing its path', () => {
    expect(cameraWallPrintName({ gcode_file: '/sdcard/jobs/plate_04.gcode' })).toBe('plate_04.gcode');
    expect(cameraWallPrintName({ gcode_file: 'C:\\prints\\plate_05.gcode' })).toBe('plate_05.gcode');
    expect(cameraWallPrintName({ subtask_name: '   ', current_print: null })).toBeNull();
  });
});
