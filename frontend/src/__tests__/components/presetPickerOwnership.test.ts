/**
 * The preset pickers belong to calibration, and the plain names are reserved.
 *
 * ⚠️ They were shared by the slice dialog and two calibration pages, which is
 * why the dialog could not simply inline them. Calibration now owns the
 * originals under its own prefix; the slice dialog builds its own. A file
 * reintroducing `components/preset-picker/` is the mistake this guards.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const SRC = join(process.cwd(), 'src');

describe('preset picker ownership', () => {
  it('has no shared preset-picker directory', () => {
    expect(existsSync(join(SRC, 'components/preset-picker'))).toBe(false);
  });

  it('keeps calibration’s copies under calibration', () => {
    const files = readdirSync(join(SRC, 'components/calibration/preset-picker'));
    expect(files.sort()).toEqual([
      'CalibrationBedTypePicker.tsx',
      'CalibrationPresetDropdown.tsx',
      'CalibrationSlicerPicker.tsx',
    ]);
  });
});
