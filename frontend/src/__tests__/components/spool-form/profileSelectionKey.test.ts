/**
 * A PA-tab selection names one profile the way BambuStudio does.
 *
 * BS identifies a pressure-advance profile by filament + extruder + nozzle
 * diameter + nozzle flow type (`CalibrationWizardSavePage.cpp`, and
 * `AMSMaterialsSetting.cpp` filters a slot's candidates by the same fields).
 * The printer numbers its table per nozzle diameter, so index 3 on the 0.4 mm
 * table and index 3 on the 0.6 mm table are two different profiles. A key
 * without the diameter made them one checkbox, and the save picked whichever of
 * the two it met first.
 */

import { describe, it, expect } from 'vitest';
import {
  nozzleFlowFromId,
  parseProfileSelectionKey,
  profileSelectionGroup,
  profileSelectionKey,
} from '../../../components/spool-form/utils';

describe('profileSelectionKey', () => {
  it('tells two nozzles apart under the same index and extruder', () => {
    expect(profileSelectionKey(1, 3, 0, '0.4', 'standard')).not.toBe(profileSelectionKey(1, 3, 0, '0.6', 'standard'));
  });

  it('spells one diameter one way, whichever way the source spelled it', () => {
    // The printer reports "0.40" on some firmware; a saved link reads back "0.4".
    expect(profileSelectionKey(1, 3, 0, '0.40', 'standard')).toBe(profileSelectionKey(1, 3, 0, '0.4', 'standard'));
  });

  it('reads an unknown flow as Standard, as BambuStudio does', () => {
    expect(profileSelectionKey(1, 3, 0, '0.4', undefined)).toBe(profileSelectionKey(1, 3, 0, '0.4', 'standard'));
  });

  it('keeps an unknown extruder distinct from extruder 0', () => {
    expect(profileSelectionKey(1, 3, null, '0.4', 'standard')).not.toBe(profileSelectionKey(1, 3, 0, '0.4', 'standard'));
  });

  it('round-trips through the parser', () => {
    expect(parseProfileSelectionKey(profileSelectionKey(7, 12, 1, '0.6', 'high_flow'))).toEqual({
      printerId: 7,
      caliIdx: 12,
      extruder: '1',
      nozzle: '0.6',
      flow: 'high_flow',
    });
  });

  it('parses a profile the printer reported no diameter for', () => {
    expect(parseProfileSelectionKey(profileSelectionKey(7, 12, null, undefined, undefined))).toEqual({
      printerId: 7,
      caliIdx: 12,
      extruder: 'null',
      nozzle: '',
      flow: 'standard',
    });
  });
});

describe('profileSelectionGroup', () => {
  it('is one slot per printer, extruder, nozzle diameter and flow type', () => {
    const group = (idx: number, d: string, flow: string) => profileSelectionGroup(profileSelectionKey(1, idx, 0, d, flow));

    // Two profiles for the same nozzle compete for one slot…
    expect(group(3, '0.4', 'standard')).toBe(group(5, '0.4', 'standard'));
    // …but a different diameter or a different flow type is a slot of its own.
    expect(group(3, '0.4', 'standard')).not.toBe(group(3, '0.6', 'standard'));
    expect(group(3, '0.4', 'standard')).not.toBe(group(4, '0.4', 'high_flow'));
  });
});

describe('nozzleFlowFromId', () => {
  it.each([
    ['HS00-0.4', 'standard'],
    ['HH00-0.4', 'high_flow'],
    ['HU00-0.4', 'tpu_high_flow'],
    ['HB00-0.4', 'e3d_high_flow'],
    ['HY00-0.4', 'hybrid'],
  ])('reads %s as %s', (nozzleId, flow) => {
    expect(nozzleFlowFromId(nozzleId)).toBe(flow);
  });

  it('reads a missing or unknown nozzle id as Standard', () => {
    expect(nozzleFlowFromId(undefined)).toBe('standard');
    expect(nozzleFlowFromId('')).toBe('standard');
    expect(nozzleFlowFromId('XX00-0.4')).toBe('standard');
  });
});
