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
  basis: { catalogue: null, nozzle: 0, slot: null },
};

/**
 * May a page act on the section's choice in THIS render? Only when nothing
 * blocks it and it was decided on exactly the catalogue answer, nozzle and slot
 * the page sees now: the section reports a commit late, and on the commit of a
 * new context (or a refresh, a new nozzle, another slot) its last verdict is
 * about something that is no longer on screen (review I-2).
 */
export function choiceDecidedNow(
  choice: CalibrationPresetChoice,
  now: { catalogue: string | null; nozzle: number; slot: string | null },
): boolean {
  return (
    choice.startBlocked === false &&
    choice.basis.catalogue !== null &&
    choice.basis.catalogue === now.catalogue &&
    choice.basis.nozzle === now.nozzle &&
    choice.basis.slot === now.slot
  );
}
