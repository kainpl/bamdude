import { describe, it, expect } from 'vitest';
import { dryingBlockedKey } from '../../utils/dryingBlockers';

describe('dryingBlockedKey — one priority with the backend', () => {
  it('power outranks the rest', () => {
    expect(dryingBlockedKey([3, 8])).toBe('printers.drying.blockedPower');
    expect(dryingBlockedKey([1])).toBe('printers.drying.blockedPower');
  });
  it('then filament at the outlet', () => {
    expect(dryingBlockedKey([2, 3])).toBe('printers.drying.blockedRetract');
  });
  it('then anything else', () => {
    expect(dryingBlockedKey([0])).toBe('printers.drying.blockedOther');
  });
});
