/** A drying timer the printer holds but does not run is not a drying cycle (upstream #2896 / #3096). */
import { describe, it, expect } from 'vitest';
import { dryStartVerdict, isDryingParked, isDryingRunning } from '../../utils/amsDrying';

const unit = (dry_time: number, dry_status = 0, dry_countdown_stalled = false) => ({ dry_time, dry_status, dry_countdown_stalled });

describe('amsDrying', () => {
  it('tells a parked timer from a running countdown', () => {
    expect(isDryingParked(unit(720, 0, true))).toBe(true);
    expect(isDryingRunning(unit(720, 0, true))).toBe(false);
    expect(isDryingRunning(unit(720))).toBe(true);
    expect(isDryingParked(unit(720))).toBe(false);
    // A flag left on a unit with no timer is nothing.
    expect(isDryingParked(unit(0, 0, true))).toBe(false);
    // A payload from before the flag existed reads as it always did.
    expect(isDryingRunning({ dry_time: 30, dry_status: 0 })).toBe(true);
  });

  describe('the verdict of the watch armed after a start', () => {
    it('a countdown that appears is a start', () => {
      expect(dryStartVerdict(unit(720), 0)).toBe('started');
    });
    it('a live phase is a start, whatever the countdown says', () => {
      expect(dryStartVerdict(unit(0, 1), 0)).toBe('started');
      expect(dryStartVerdict(unit(720, 2, true), 720)).toBe('started');
    });
    it('a parked timer is not a start', () => {
      expect(dryStartVerdict(unit(720, 0, true), 0)).toBeNull();
    });
    it('the timer that stood there when the command went out proves nothing', () => {
      // The start replaced a parked 720-minute timer; the same 720 is still there.
      expect(dryStartVerdict(unit(720), 720)).toBeNull();
      // The countdown moved: the new cycle is running.
      expect(dryStartVerdict(unit(719), 720)).toBe('started');
      expect(dryStartVerdict(unit(480), 720)).toBe('started');
    });
    it('an error is a failure', () => {
      expect(dryStartVerdict(unit(0, 5), 0)).toBe('failed');
    });
    it('nothing yet is no verdict', () => {
      expect(dryStartVerdict(unit(0), 0)).toBeNull();
    });
  });
});
