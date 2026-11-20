// The calibration slicing section's answer before anything is chosen. Kept
// apart from the component file so that file exports components only.
import type { CalibrationPresetChoice } from './CalibrationSlicingSection';

export const EMPTY_CALIBRATION_CHOICE: CalibrationPresetChoice = {
  slicer: null,
  plate: 'Textured PEI Plate',
  printer: null,
  process: null,
  filament: null,
  startBlocked: 'empty',
  familyHint: null,
};
