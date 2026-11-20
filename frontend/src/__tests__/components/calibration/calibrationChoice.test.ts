/**
 * Review I-2 (second round): a page acts on the calibration section's choice
 * only when it was decided on the catalogue answer, nozzle and slot the page
 * sees in the same render — the section reports one commit late.
 */
import { describe, expect, it } from 'vitest';

import { EMPTY_CALIBRATION_CHOICE, choiceDecidedNow } from '../../../components/calibration/calibrationChoice';
import type { CalibrationPresetChoice } from '../../../components/calibration/CalibrationSlicingSection';

const ref = { source: 'standard' as const, id: 'x' };
const decided: CalibrationPresetChoice = {
  ...EMPTY_CALIBRATION_CHOICE,
  printer: ref,
  process: ref,
  filament: ref,
  startBlocked: false,
  basis: { catalogue: 'g1@100', nozzle: 0.4, slot: '0:0:0' },
};
const now = { catalogue: 'g1@100', nozzle: 0.4, slot: '0:0:0' };

describe('choiceDecidedNow', () => {
  it('holds when the verdict is clear and its basis is what the page sees', () => {
    expect(choiceDecidedNow(decided, now)).toBe(true);
  });

  it.each([
    ['a new context or a refresh', { ...now, catalogue: 'g2@200' }],
    ['no catalogue yet', { ...now, catalogue: null }],
    ['another nozzle', { ...now, nozzle: 0.6 }],
    ['another slot', { ...now, slot: '0:0:1' }],
  ])('refuses on %s', (_label, current) => {
    expect(choiceDecidedNow(decided, current)).toBe(false);
  });

  it('refuses whatever the basis while the verdict blocks, and before any decision', () => {
    expect(choiceDecidedNow({ ...decided, startBlocked: 'loading' }, now)).toBe(false);
    expect(choiceDecidedNow(EMPTY_CALIBRATION_CHOICE, { catalogue: null, nozzle: 0, slot: null })).toBe(false);
  });
});
