/**
 * Which side badge an AMS card header gets (upstream 7a42e0a7). A switch inlet
 * binding outranks everything; a real extruder id outranks the unit-id guess;
 * and with a switch fitted the unit-id guess is never made — every unit then
 * reports extruder 0xE, so the guess would badge AMS 0 "R" and AMS 1 "L" from
 * nothing but their numbers.
 */
import { describe, it, expect } from 'vitest';
import { amsSideBadge, FTS_INLET_SIDE } from '../../utils/amsHelpers';

describe('amsSideBadge', () => {
  it('names the inlet when the AMS is bound to one', () => {
    expect(amsSideBadge(1, {}, { '1': 'B' }, true)).toEqual({ kind: 'inlet', inlet: 'B' });
  });

  it('prefers a real extruder id over the unit-id guess', () => {
    expect(amsSideBadge(0, { '0': 1 }, {}, false)).toEqual({ kind: 'nozzle', side: 'L' });
  });

  it('falls back to the unit id only without a switch', () => {
    expect(amsSideBadge(128, {}, {}, false)).toEqual({ kind: 'nozzle', side: 'R' });
    expect(amsSideBadge(1, {}, {}, true)).toBeNull();
  });

  it('letters In-A as L and In-B as R', () => {
    expect(FTS_INLET_SIDE).toEqual({ A: 'L', B: 'R' });
  });
});
