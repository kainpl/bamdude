/**
 * The dialog owns its pickers, and no longer offers the owner filter.
 *
 * ⚠️ All / My / Built-in was a leftover from the sidecar-bundle era — the
 * other half of that control was deleted with the bundles (#1712) and this
 * half stayed hanging. Source is chosen by the tier picker now.
 *
 * ⚠️ Dropped for the DIALOG only: calibration still shows it, and taking it
 * from there is a different decision about a different flow.
 */
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

  it('has no trace of the owner filter left', () => {
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
