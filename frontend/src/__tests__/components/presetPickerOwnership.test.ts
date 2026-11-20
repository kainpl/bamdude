/**
 * The preset controls have one home (spec slicing-setup r5 G1): the
 * slicing-setup family. Neither the old shared `components/preset-picker/`
 * folder nor calibration's copies under `calibration/preset-picker/` may come
 * back — a second copy is exactly what the family replaced.
 */
import { describe, it, expect } from 'vitest';
import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const SRC = join(process.cwd(), 'src');

describe('preset picker ownership', () => {
  it('has no shared preset-picker directory', () => {
    expect(existsSync(join(SRC, 'components/preset-picker'))).toBe(false);
  });

  it('has no calibration copy of the pickers', () => {
    expect(existsSync(join(SRC, 'components/calibration/preset-picker'))).toBe(false);
  });

  it('keeps the controls in the slicing-setup family', () => {
    const files = readdirSync(join(SRC, 'components/slicing-setup'));
    for (const part of ['SlicerChoice.tsx', 'BedPlateSelect.tsx', 'PresetSelect.tsx', 'PresetFilters.tsx']) {
      expect(files).toContain(part);
    }
  });
});
