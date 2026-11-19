/** The dialog owns its pickers. Its visible catalogue preferences are separate
 * from calibration's legacy three-state owner control. */
import { describe, it, expect } from 'vitest';

import modalSource from '../../components/SliceModal.tsx?raw';
import calibrationSource from '../../components/calibration/CalibrationPresetPage.tsx?raw';

describe('SliceModal pickers', () => {
  it('declares its pickers inline rather than importing calibration’s', () => {
    // The whole point of the rework: the dialog composes, it does not host.
    expect(modalSource).not.toContain("from './calibration/preset-picker/");
    expect(modalSource).toContain('function PresetDropdown(');
    expect(modalSource).toContain('function BedTypePicker(');
    expect(modalSource).toContain('function SlicerPicker(');
  });

  it('does not keep those pickers exported', () => {
    // They belong to this dialog. An export invites the next consumer, which
    // is how the shared family happened the first time.
    expect(modalSource).not.toContain('export function PresetDropdown');
    expect(modalSource).not.toContain('export function BedTypePicker');
    expect(modalSource).not.toContain('export function SlicerPicker');
  });

  it('does not restore calibration’s legacy owner control', () => {
    // ⚠️ Hiding the control while still filtering by it would look the same
    // on screen and quietly drop presets from the dropdowns.
    expect(modalSource).not.toContain('matchesOwnerFilter');
    expect(modalSource).not.toContain('ownerFilter');
    expect(modalSource).not.toContain('filterOwner');
    // ⚠️ Asserted on USE, not on the word: the comment explaining why the
    // control is absent is the line most worth keeping in that file.
    expect(modalSource).not.toContain('<PresetSourceControl');
    expect(modalSource).not.toContain('import { CalibrationPresetSourceControl');
  });

  it('leaves calibration’s copy of the filter alone', () => {
    // ⚠️ The other half of this task's contract. Whether calibration should
    // still have it is a separate question about a separate flow.
    expect(calibrationSource).toContain('CalibrationPresetSourceControl');
  });
});
