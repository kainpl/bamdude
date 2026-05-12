import { describe, expect, it } from 'vitest';

import { computeNextStep } from '../../hooks/useFilamentCalibration';

describe('computeNextStep', () => {
  it('start → preset', () => {
    expect(computeNextStep('start', { cali_mode: 'pa_line', method: 'manual' })).toBe('preset');
  });

  it('preset → running when sessionStarted', () => {
    expect(
      computeNextStep('preset', {
        cali_mode: 'pa_line',
        method: 'manual',
        sessionStarted: true,
      }),
    ).toBe('running');
  });

  it('preset stays put when not started', () => {
    expect(computeNextStep('preset', { cali_mode: 'pa_line', method: 'manual' })).toBe('preset');
  });

  it('running PA → manualSave on awaiting_user_input', () => {
    expect(
      computeNextStep('running', {
        cali_mode: 'pa_line',
        method: 'manual',
        sessionStatus: 'awaiting_user_input',
      }),
    ).toBe('manualSave');
  });

  it('running Flow Rate stage 1 → coarseSave', () => {
    expect(
      computeNextStep('running', {
        cali_mode: 'flow_rate',
        method: 'manual',
        stage: 1,
        sessionStatus: 'awaiting_user_input',
      }),
    ).toBe('coarseSave');
  });

  it('running Flow Rate stage 2 → fineSave', () => {
    expect(
      computeNextStep('running', {
        cali_mode: 'flow_rate',
        method: 'manual',
        stage: 2,
        sessionStatus: 'awaiting_user_input',
      }),
    ).toBe('fineSave');
  });

  it('coarseSave with skip_fine → finish after save', () => {
    expect(computeNextStep('coarseSave', { skipFine: true, savedRows: 1 })).toBe('finish');
  });

  it('coarseSave continue → running stage 2', () => {
    expect(computeNextStep('coarseSave', { skipFine: false, nextSessionId: 99 })).toBe('running');
  });

  it('manualSave → finish on saved rows', () => {
    expect(computeNextStep('manualSave', { savedRows: 1 })).toBe('finish');
  });

  it('fineSave → finish on saved rows', () => {
    expect(computeNextStep('fineSave', { savedRows: 1 })).toBe('finish');
  });
});
