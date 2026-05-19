import { describe, it, expect } from 'vitest';
import { tempDefaultsForFilament } from '../../utils/calibrationTemp';

describe('tempDefaultsForFilament', () => {
  it('maps each BS Temp_Calibration_Dlg material preset', () => {
    expect(tempDefaultsForFilament('PLA')).toEqual({ start: 230, end: 190 });
    expect(tempDefaultsForFilament('PETG')).toEqual({ start: 250, end: 230 });
    expect(tempDefaultsForFilament('PCTG')).toEqual({ start: 280, end: 240 });
    expect(tempDefaultsForFilament('ABS')).toEqual({ start: 270, end: 230 });
    expect(tempDefaultsForFilament('ASA')).toEqual({ start: 270, end: 230 });
    expect(tempDefaultsForFilament('TPU')).toEqual({ start: 240, end: 210 });
    expect(tempDefaultsForFilament('PA-CF')).toEqual({ start: 320, end: 280 });
    expect(tempDefaultsForFilament('PET-CF')).toEqual({ start: 320, end: 280 });
  });

  it('is case-insensitive and matches within a full preset name', () => {
    expect(tempDefaultsForFilament('Bambu PLA Basic @BBL A1M')).toEqual({ start: 230, end: 190 });
    expect(tempDefaultsForFilament('generic petg hf')).toEqual({ start: 250, end: 230 });
  });

  it('checks the most specific type first', () => {
    // "PET-CF" contains "PET"/"CF" but must not resolve as PETG.
    expect(tempDefaultsForFilament('Bambu PET-CF')).toEqual({ start: 320, end: 280 });
    // "PCTG" must not resolve as PETG.
    expect(tempDefaultsForFilament('Generic PCTG')).toEqual({ start: 280, end: 240 });
  });

  it('falls back to the PLA range for unknown / custom filaments', () => {
    expect(tempDefaultsForFilament('')).toEqual({ start: 230, end: 190 });
    expect(tempDefaultsForFilament('Some Custom Resin')).toEqual({ start: 230, end: 190 });
  });
});
