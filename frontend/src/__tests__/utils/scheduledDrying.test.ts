import { describe, it, expect } from 'vitest';
import { computeStartAfter, WEEKDAY_BITS } from '../../utils/scheduledDrying';

const NOW = new Date('2026-09-25T10:00:00Z');

describe('computeStartAfter', () => {
  it('delay adds hours', () => {
    expect(computeStartAfter('delay', { delayHours: 3 }, NOW)).toBe('2026-09-25T13:00:00.000Z');
  });
  it('at is a local datetime-local value converted to UTC', () => {
    const local = new Date(2026, 8, 26, 1, 0); // 01:00 in the test runner's zone
    const value = `2026-09-26T01:00`;
    expect(computeStartAfter('at', { at: value }, NOW)).toBe(local.toISOString());
  });
  it('when_free sends null; now and repeat send nothing', () => {
    expect(computeStartAfter('when_free', {}, NOW)).toBeNull();
    expect(computeStartAfter('now', {}, NOW)).toBeUndefined();
    expect(computeStartAfter('repeat', {}, NOW)).toBeUndefined();
  });
  it('Monday is bit 0, Sunday bit 6', () => {
    expect(WEEKDAY_BITS).toEqual([1, 2, 4, 8, 16, 32, 64]);
  });
});
