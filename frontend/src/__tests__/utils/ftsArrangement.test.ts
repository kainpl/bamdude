/**
 * The Filament Track Switch inlet recommendation, ported from BambuStudio
 * (MultiNozzleUtils::simulate_filament_change_time, SelectMachineDialog::
 * get_filament_suggest_pos / get_filament_change_gap_time / is_at_suggested_pos).
 *
 * An inlet is a shared channel AMS → switch → hotend: two filaments of one
 * inlet cannot both be in it, so feeding one drags the other all the way back
 * to its AMS. With AMS preload the firmware parks the outgoing filament at the
 * switch and pre-feeds the next one — but only across the two inlets. The
 * expected numbers below are worked by hand from BS's code with load 29 s,
 * unload 28 s (selector half = 14.5 / 14).
 */
import { describe, it, expect } from 'vitest';
import {
  arrangementAdvice,
  filamentChangeGap,
  suggestInletPositions,
  type TrackSwitchPlan,
} from '../../utils/ftsArrangement';

const TIMES = { load_time: 29, unload_time: 28 };
const TWO_HOTENDS = [
  { id: 0, extruder_id: 0 },
  { id: 1, extruder_id: 1 },
];

describe('filamentChangeGap — the channel simulator', () => {
  it('costs a full retract when the other hotend holds a filament of the same inlet', () => {
    const plan: TrackSwitchPlan = {
      ...TIMES,
      optimal_assignment: [0, 1],
      filament_sequence: [0, 1],
      nozzle_sequence: [0, 1],
      nozzles: TWO_HOTENDS,
    };
    // Sliced 58 s (two loads); actual 86 s — filament 0 is pulled back from
    // hotend 0 to its AMS (14 + 14) before filament 1 can use the shared inlet.
    expect(filamentChangeGap(plan, [0, 1], [0, 0], 0)).toBe(28);
    // Across the two inlets nothing is in the way.
    expect(filamentChangeGap(plan, [0, 1], [0, 1], 0)).toBe(0);
  });

  it('gains nothing from two inlets on one hotend without preload', () => {
    const plan: TrackSwitchPlan = {
      ...TIMES,
      optimal_assignment: [0, 0],
      filament_sequence: [0, 1, 0],
      nozzle_sequence: [0, 0, 0],
      nozzles: [{ id: 0, extruder_id: 0 }],
    };
    expect(filamentChangeGap(plan, [0, 1], [0, 0], 0)).toBe(0);
    expect(filamentChangeGap(plan, [0, 1], [0, 1], 0)).toBe(0);
  });

  it('with preload, a split across the inlets beats the slicer estimate', () => {
    const plan: TrackSwitchPlan = {
      ...TIMES,
      optimal_assignment: [0, 0],
      filament_sequence: [0, 1, 0],
      nozzle_sequence: [0, 0, 0],
      nozzles: [{ id: 0, extruder_id: 0 }],
    };
    // Sliced 143 s; actual 86 s — the outgoing filament stops at the switch and
    // the next one is already waiting there.
    expect(filamentChangeGap(plan, [0, 1], [0, 1], 1)).toBe(-57);
  });
});

describe('suggestInletPositions — get_filament_suggest_pos', () => {
  it('puts the two slicer groups on the two inlets, moving as few filaments as it can', () => {
    const suggestion = suggestInletPositions([0, 0, 1], new Map([[0, 'A'], [1, 'B'], [2, 'B']]));
    expect(Object.fromEntries(suggestion)).toEqual({ 0: 'A', 1: 'A', 2: 'B' });
  });

  it('puts a single group behind the inlet most of it already uses, IN-A on a tie', () => {
    expect(Object.fromEntries(suggestInletPositions([0, 0, 0], new Map([[0, 'B'], [1, 'B'], [2, 'A']]))))
      .toEqual({ 0: 'B', 1: 'B', 2: 'B' });
    expect(Object.fromEntries(suggestInletPositions([0, 0], new Map([[0, 'A'], [1, 'B']]))))
      .toEqual({ 0: 'A', 1: 'A' });
  });

  it('suggests nothing while any filament sits in an AMS with no inlet', () => {
    expect(suggestInletPositions([0, 1], new Map([[0, 'A'], [1, null]])).size).toBe(0);
  });
});

describe('arrangementAdvice — what the print dialog says', () => {
  const plan: TrackSwitchPlan = {
    ...TIMES,
    optimal_assignment: [0, 1],
    filament_sequence: [0, 1],
    nozzle_sequence: [0, 1],
    nozzles: TWO_HOTENDS,
  };

  it('names the filament to move and the time it saves', () => {
    const advice = arrangementAdvice(plan, new Map([[0, 'A'], [1, 'A']]), 0);
    expect(advice).toEqual({ saveSeconds: 28, moves: [{ filamentId: 1, inlet: 'B' }] });
  });

  it('is silent when the filaments already sit where the slicer suggests', () => {
    expect(arrangementAdvice(plan, new Map([[0, 'A'], [1, 'B']]), 0)).toBeNull();
  });

  it('is silent when a move would save less than a second', () => {
    // One hotend, no preload: the slicer suggests a single inlet and a split
    // costs nothing — BambuStudio stays quiet, and so do we.
    const oneHotend: TrackSwitchPlan = {
      ...TIMES,
      optimal_assignment: [0, 0],
      filament_sequence: [0, 1, 0],
      nozzle_sequence: [0, 0, 0],
      nozzles: [{ id: 0, extruder_id: 0 }],
    };
    expect(arrangementAdvice(oneHotend, new Map([[0, 'A'], [1, 'B']]), 0)).toBeNull();
  });

  it('is silent while any mapped filament has no inlet', () => {
    expect(arrangementAdvice(plan, new Map([[0, 'A'], [1, null]]), 0)).toBeNull();
  });
});

describe('arrangementAdvice — an incomplete mapping', () => {
  it('says nothing when a filament the plate changes to is not mapped', () => {
    const plan: TrackSwitchPlan = {
      ...TIMES,
      optimal_assignment: [0, 1, 1],
      filament_sequence: [0, 1, 2],
      nozzle_sequence: [0, 1, 1],
      nozzles: TWO_HOTENDS,
    };
    expect(arrangementAdvice(plan, new Map([[0, 'A'], [1, 'A']]), 0)).toBeNull();
  });
});
