/**
 * Filament Track Switch inlet recommendation — a port of BambuStudio's print
 * dialog (SelectMachineDialog::get_filament_suggest_pos, is_at_suggested_pos,
 * get_filament_change_gap_time, update_save_time_hint) and of the channel
 * simulator behind it (MultiNozzleUtils::simulate_filament_change_time /
 * calc_filament_change_gap_for_assignment).
 *
 * The physics, from the simulator: an inlet is a shared channel
 * AMS → switch → hotend. Two filaments of one inlet cannot both be in it, so
 * feeding one drags the other all the way back to its AMS, even out of the
 * other hotend. With AMS preload (the printer's `ams_preload_version` >= 1) the
 * outgoing filament is parked at the switch and the next one pre-fed — but only
 * across the two inlets. The slicer already worked out which filaments belong
 * behind which inlet (`optimal_assignment`, read from the 3MF by the backend's
 * `services/track_switch_plan.py`); this module compares the live arrangement
 * with it and times the difference. Pure functions, no React.
 *
 * Divergence: BambuStudio shows this only for a plate it has just sliced
 * (FROM_NORMAL). We always print from a 3MF, and BambuStudio writes the same
 * data into it, so we read it from there.
 */

import type { TrackSwitchPlan } from '../api/client';

export type { TrackSwitchPlan };
export type Inlet = 'A' | 'B';

/** BambuStudio's grouping for the simulator: IN-A is group 0, IN-B group 1. */
const INLET_GROUP: Record<Inlet, number> = { A: 0, B: 1 };

type Location = 'ams' | 'selector' | 'extruder';

/**
 * simulate_filament_change_time with calc_sliced_time = true: the actual time
 * spent changing filament under this inlet arrangement, and the slicer's own
 * estimate of it (which knows nothing about the switch).
 */
function simulate(
  plan: TrackSwitchPlan,
  logicalFilaments: number[],
  groupOfFilament: number[],
  preloadEnabled: boolean[],
): { actual: number; sliced: number } {
  const standardLoad = plan.load_time;
  const standardUnload = plan.unload_time;
  // The slicer's machine times cover AMS → switch → hotend; BS takes half of
  // each as the short switch → hotend leg (GCode.cpp / get_filament_change_gap_time).
  const selectorLoad = standardLoad / 2;
  const selectorUnload = standardUnload / 2;
  const loadAmsToSelector = standardLoad - selectorLoad;
  const unloadAmsToSelector = standardUnload - selectorUnload;
  const loadSelectorToExt = selectorLoad;
  const unloadExtToSelector = selectorUnload;

  const nozzleToExtruder = new Map(plan.nozzles.map((n) => [n.id, n.extruder_id]));
  const filamentToGroup = new Map(logicalFilaments.map((f, i) => [f, groupOfFilament[i]]));
  const groupOf = (f: number) => filamentToGroup.get(f) ?? -1;
  const preloadOn = (group: number) => group >= 0 && group < preloadEnabled.length && preloadEnabled[group];

  const location = new Map<number, Location>(logicalFilaments.map((f) => [f, 'ams']));
  // std::unordered_map::operator[] default-constructs IN_AMS for a filament it
  // has not seen; read the same way so an unlisted id behaves identically.
  const loc = (f: number): Location => location.get(f) ?? 'ams';
  const filamentExtruder = new Map<number, number>();
  const extruderFilament = new Map<number, number>();
  const groupOccupied = new Map<number, Set<number>>();
  const occupied = (group: number) => {
    let set = groupOccupied.get(group);
    if (!set) {
      set = new Set();
      groupOccupied.set(group, set);
    }
    return set;
  };

  // NozzleStatusRecorder for the sliced estimate.
  const nozzleFilament = new Map<number, number>();
  const extruderNozzle = new Map<number, number>();

  const length = Math.min(plan.filament_sequence.length, plan.nozzle_sequence.length);
  let actual = 0;
  let sliced = 0;

  for (let i = 0; i < length; i++) {
    const B = plan.filament_sequence[i];
    const nozzleId = plan.nozzle_sequence[i];
    const E = nozzleToExtruder.get(nozzleId);
    if (E === undefined) continue;

    // Step 0: the slicer's view — a nozzle or filament change costs a full
    // unload (when something was loaded) plus a full load.
    const oldNozzleInE = extruderNozzle.get(E) ?? -1;
    const oldFilamentInNozzle = nozzleFilament.get(nozzleId) ?? -1;
    const oldFilamentInExt = nozzleFilament.get(oldNozzleInE) ?? -1;
    if (oldNozzleInE !== nozzleId || oldFilamentInNozzle !== B) {
      if (oldFilamentInExt !== -1) sliced += standardUnload;
      sliced += standardLoad;
    }
    nozzleFilament.set(nozzleId, B);
    extruderNozzle.set(E, nozzleId);

    // Step 1: what hotend E holds now.
    const A = extruderFilament.get(E) ?? -1;
    const groupB = groupOf(B);
    const groupA = A !== -1 ? groupOf(A) : -1;

    // Step 2: clear B's inlet — any other filament of it goes back to its AMS.
    const sameInlet = groupOccupied.get(groupB);
    if (sameInlet) {
      for (const X of sameInlet) {
        if (X === B) continue;
        const locX = loc(X);
        if (locX === 'extruder') {
          actual += unloadExtToSelector + unloadAmsToSelector;
          const E2 = filamentExtruder.get(X);
          if (E2 !== undefined) extruderFilament.delete(E2);
          filamentExtruder.delete(X);
        } else if (locX === 'selector') {
          actual += unloadAmsToSelector;
        }
        location.set(X, 'ams');
      }
      sameInlet.clear();
    }

    // Step 3: A leaves E; Step 3.5: B is pre-fed in parallel.
    let step3Executed = false;
    let step3 = 0;
    if (A !== -1 && A !== B && loc(A) === 'extruder') {
      if (preloadOn(groupA) && groupA !== groupB) {
        step3 = unloadExtToSelector;
        location.set(A, 'selector');
      } else {
        step3 = unloadExtToSelector + unloadAmsToSelector;
        location.set(A, 'ams');
        occupied(groupA).delete(A);
      }
      extruderFilament.delete(E);
      filamentExtruder.delete(A);
      step3Executed = true;
    }
    let step35 = 0;
    if (step3Executed && loc(B) === 'ams' && groupA !== groupB && preloadOn(groupB)) {
      step35 = loadAmsToSelector;
      location.set(B, 'selector');
      occupied(groupB).add(B);
    }
    actual += Math.max(step3, step35);

    // Step 4: B into E; Step 6: the next filament pre-fed in parallel.
    let step4 = 0;
    const locB = loc(B);
    if (locB === 'ams') step4 = loadAmsToSelector + loadSelectorToExt;
    else if (locB === 'selector') step4 = loadSelectorToExt;

    extruderFilament.set(E, B);
    location.set(B, 'extruder');
    filamentExtruder.set(B, E);
    occupied(groupB).add(B);

    let step6 = 0;
    if (i + 1 < length) {
      const C = plan.filament_sequence[i + 1];
      const groupC = groupOf(C);
      if (loc(C) === 'ams' && groupC !== groupB && preloadOn(groupC) && occupied(groupC).size === 0) {
        step6 = loadAmsToSelector;
        location.set(C, 'selector');
        occupied(groupC).add(C);
      }
    }
    actual += Math.max(step4, step6);
  }

  return { actual, sliced };
}

/**
 * calc_filament_change_gap_for_assignment: how much longer than the slicer's
 * estimate the filament changes take under this inlet grouping. Negative when
 * preload makes the arrangement faster than the estimate.
 */
export function filamentChangeGap(
  plan: TrackSwitchPlan,
  logicalFilaments: number[],
  groupOfFilament: number[],
  preloadVersion: number | null | undefined,
): number {
  const groupCount = groupOfFilament.length ? Math.max(...groupOfFilament) + 1 : 0;
  const preload = Array.from({ length: groupCount }, () => (preloadVersion ?? 0) >= 1);
  const { actual, sliced } = simulate(plan, logicalFilaments, groupOfFilament, preload);
  return actual - sliced;
}

/**
 * get_filament_suggest_pos: the inlet each mapped filament should sit behind.
 * `mapping` is 0-based filament id → the inlet of the AMS it is mapped to
 * (null when that AMS has no inlet — then nothing is suggested, as in BS).
 */
export function suggestInletPositions(
  optimalAssignment: number[],
  mapping: Map<number, Inlet | null>,
): Map<number, Inlet> {
  const suggestion = new Map<number, Inlet>();

  const posToGroup = new Map<number, Set<number>>();
  optimalAssignment.forEach((group, filamentId) => {
    if (!mapping.has(filamentId)) return;
    if (!posToGroup.has(group)) posToGroup.set(group, new Set());
    posToGroup.get(group)!.add(filamentId);
  });

  const onA = new Set<number>();
  const onB = new Set<number>();
  for (const [filamentId, inlet] of mapping) {
    if (inlet === 'A') onA.add(filamentId);
    else if (inlet === 'B') onB.add(filamentId);
    else return suggestion;
  }

  if (posToGroup.size === 1) {
    const inlet: Inlet = onA.size >= onB.size ? 'A' : 'B';
    for (const filamentId of mapping.keys()) suggestion.set(filamentId, inlet);
  } else if (posToGroup.size === 2) {
    // std::map order: the lower group key first.
    const [group1, group2] = [...posToGroup.entries()].sort(([a], [b]) => a - b).map(([, set]) => set);
    let offset1 = 0; // group 1 on IN-A, group 2 on IN-B
    for (const f of group1) if (!onA.has(f)) offset1++;
    for (const f of group2) if (!onB.has(f)) offset1++;
    let offset2 = 0; // group 1 on IN-B, group 2 on IN-A
    for (const f of group1) if (!onB.has(f)) offset2++;
    for (const f of group2) if (!onA.has(f)) offset2++;
    const [first, second]: [Inlet, Inlet] = offset1 <= offset2 ? ['A', 'B'] : ['B', 'A'];
    for (const f of group1) suggestion.set(f, first);
    for (const f of group2) suggestion.set(f, second);
  }
  return suggestion;
}

export interface ArrangementAdvice {
  /** Seconds the recommended arrangement saves over the current one. */
  saveSeconds: number;
  /** Filaments not at their suggested inlet, and where they belong. */
  moves: { filamentId: number; inlet: Inlet }[];
}

/**
 * update_save_time_hint: advice only when a move saves at least a second and
 * something is not where the slicer suggests. `mapping` covers every filament
 * the plate prints (0-based id → inlet of its mapped AMS, null when unknown).
 */
export function arrangementAdvice(
  plan: TrackSwitchPlan,
  mapping: Map<number, Inlet | null>,
  preloadVersion: number | null | undefined,
): ArrangementAdvice | null {
  if (mapping.size === 0) return null;
  // BS gives up when a filament the plate changes to has no mapping; timing a
  // sequence against a partial arrangement would invent a saving.
  if (plan.filament_sequence.some((f) => !mapping.has(f))) return null;
  const logical = [...mapping.keys()].sort((a, b) => a - b);
  const groups: number[] = [];
  for (const filamentId of logical) {
    const inlet = mapping.get(filamentId);
    if (!inlet) return null;
    groups.push(INLET_GROUP[inlet]);
  }

  const saveSeconds = filamentChangeGap(plan, logical, groups, preloadVersion);
  if (saveSeconds < 1) return null;

  const suggestion = suggestInletPositions(plan.optimal_assignment, mapping);
  const moves = logical
    .filter((f) => suggestion.has(f) && suggestion.get(f) !== mapping.get(f))
    .map((f) => ({ filamentId: f, inlet: suggestion.get(f)! }));
  return moves.length ? { saveSeconds, moves } : null;
}
