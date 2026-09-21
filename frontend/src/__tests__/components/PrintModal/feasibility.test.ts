/**
 * The feasibility verdict itself, away from the dialog that renders it.
 *
 * `PrintModalFeasibility.test.tsx` pins what the operator SEES; this pins the
 * decision behind it, where the two counter-examples that made the first design
 * wrong live: a channel's status word is not the verdict, and a counter of zero
 * is not a proof.
 *
 * ⚠️ `channelReason` only NAMES a refusal the mapping has already settled. A
 * wrong branch here is a misleading sentence, never a wrong button — which is
 * exactly why it needs tests of its own: nothing else in the suite would go red
 * if `nozzle_mismatch` started reading as «no compatible filament source».
 */

import { describe, it, expect } from 'vitest';
import {
  FEASIBLE,
  UNKNOWN,
  autoFeasibility,
  describeSource,
  printerFeasibility,
  worstVerdict,
  type FeasibilityVerdict,
} from '../../../components/PrintModal/feasibility';
import type { FilamentRequirement, LoadedFilament } from '../../../hooks/useFilamentMapping';
import type { RoutingPreview } from '../../../api/client';

const RED = '#FF0000';
const GREEN = '#00FF00';

const tray = (over: Partial<LoadedFilament> & { globalTrayId: number }): LoadedFilament => ({
  type: 'PETG',
  color: RED,
  colorName: '',
  amsId: 0,
  trayId: over.globalTrayId,
  isHt: false,
  isExternal: false,
  label: `A${over.globalTrayId + 1}`,
  trayInfoIdx: '',
  ...over,
});

const need = (over: Partial<FilamentRequirement> = {}): FilamentRequirement => ({
  slot_id: 1,
  type: 'PETG',
  color: RED,
  used_grams: 10,
  ...over,
});

/** One (plate, printer) pair, with the dialog's own mapping handed in. */
const judge = (
  requirements: FilamentRequirement[],
  mapping: number[] | undefined,
  loaded: LoadedFilament[],
  ftsActive = false,
) => printerFeasibility({ requirements, mapping, loaded, ftsActive });

describe('worstVerdict', () => {
  const blockedNow: FeasibilityVerdict = { state: 'blocked_now' };
  const blockedTarget: FeasibilityVerdict = { state: 'blocked_target' };

  it('ranks a wrong target above a plate that cannot print now', () => {
    expect(worstVerdict(blockedNow, blockedTarget)).toBe(blockedTarget);
    expect(worstVerdict(blockedTarget, blockedNow)).toBe(blockedTarget);
  });

  it('⚠️ does not let an unknown rescue a proven refusal', () => {
    // A submission writes one row per (plate, printer): one pair that cannot
    // print is a row that will sit there, whatever the machine beside it says.
    expect(worstVerdict(UNKNOWN, blockedNow)).toBe(blockedNow);
    expect(worstVerdict(blockedNow, UNKNOWN)).toBe(blockedNow);
  });

  it('counts not knowing as worse than fine', () => {
    expect(worstVerdict(FEASIBLE, UNKNOWN)).toBe(UNKNOWN);
    expect(worstVerdict(UNKNOWN, FEASIBLE)).toBe(UNKNOWN);
  });

  it('keeps the first argument when the two are equal', () => {
    expect(worstVerdict(FEASIBLE, FEASIBLE)).toBe(FEASIBLE);
  });
});

describe('describeSource', () => {
  it('names the variant beside the material and the colour by name', () => {
    expect(describeSource('PETG', 'GFG99', RED)).toMatch(/^PETG \(GFG99\) .+/);
  });

  it('drops the brackets when there is no variant', () => {
    expect(describeSource('PETG', undefined, undefined)).toBe('PETG');
  });

  it('says `?` rather than nothing when even the material is missing', () => {
    expect(describeSource(undefined, 'GFG99', undefined)).toBe('? (GFG99)');
  });
});

describe('printerFeasibility, from the mapping and nothing else', () => {
  it('calls a plate with no used channel printable by definition', () => {
    expect(judge([need({ slot_id: 0 })], undefined, [])).toBe(FEASIBLE);
  });

  it('⚠️ answers unknown, not blocked, when the dialog ships no mapping at all', () => {
    // The fan-out case: the scheduler maps each plate against the printer it
    // picks, and this dialog has no answer to give.
    expect(judge([need()], undefined, [tray({ globalTrayId: 0 })])).toBe(UNKNOWN);
  });

  it('passes a channel that got a tray', () => {
    expect(judge([need()], [0], [tray({ globalTrayId: 0 })])).toBe(FEASIBLE);
  });
});

describe('channelReason names the refusal the mapping already settled', () => {
  const reasonFor = (
    requirements: FilamentRequirement[],
    mapping: number[],
    loaded: LoadedFilament[],
    ftsActive = false,
  ) => {
    const verdict = judge(requirements, mapping, loaded, ftsActive);
    expect(verdict.state).toBe('blocked_now');
    return verdict.reason!;
  };

  it('says the material is missing when nothing is loaded at all', () => {
    const reason = reasonFor([need()], [-1], []);
    expect(reason.code).toBe('material_mismatch');
    expect(reason.slot).toBe(1);
    // ⚠️ `''`, not absent: an empty AMS is an answer, not an unasked question.
    expect(reason.loaded).toBe('');
  });

  it('says the material is missing when the trays hold another material', () => {
    const reason = reasonFor([need()], [-1], [tray({ globalTrayId: 0, type: 'ABS' })]);
    expect(reason.code).toBe('material_mismatch');
    expect(reason.loaded).toContain('ABS');
  });

  it('⚠️ says the neighbour took it, not that nothing fits, when the only tray is spoken for', () => {
    // Assignment is stateful: a channel can be starved by its neighbour while
    // the tray it wants is in plain sight.
    const reason = reasonFor(
      [need({ slot_id: 1 }), need({ slot_id: 2 })],
      [0, -1],
      [tray({ globalTrayId: 0 })],
    );
    expect(reason.code).toBe('distinct_sources_required');
    expect(reason.slot).toBe(2);
  });

  it('says the tray is on the wrong nozzle when the material is there but the extruder is not', () => {
    const reason = reasonFor([need({ nozzle_id: 1 })], [-1], [tray({ globalTrayId: 0, extruderId: 0 })]);
    expect(reason.code).toBe('nozzle_mismatch');
  });

  it('⚠️ stops calling it a nozzle refusal when an FTS is installed', () => {
    // An FTS routes any slot to either extruder, so the hard filter is lifted
    // entirely and the colour is what is left to have refused the tray.
    const reason = reasonFor(
      [need({ nozzle_id: 1, color: GREEN })],
      [-1],
      [tray({ globalTrayId: 0, extruderId: 0, color: RED })],
      true,
    );
    expect(reason.code).toBe('color_mismatch');
  });

  it('says the colour when material and profile both agree', () => {
    const reason = reasonFor([need({ color: GREEN })], [-1], [tray({ globalTrayId: 0, color: RED })]);
    expect(reason.code).toBe('color_mismatch');
  });

  it('says the variant when the material is in the tray under another profile', () => {
    const reason = reasonFor(
      [need({ tray_info_idx: 'Pa240002', strict_profile_match: true })],
      [-1],
      [tray({ globalTrayId: 0, trayInfoIdx: 'GFG99' })],
    );
    expect(reason.code).toBe('variant_mismatch');
    expect(reason.wanted).toContain('Pa240002');
    expect(reason.loaded).toContain('GFG99');
  });

  it('⚠️ leaves the profile out of what the channel wanted when it may be ignored', () => {
    // Naming the id in a refusal that was never about the id points the
    // operator at the wrong thing.
    const reason = reasonFor(
      [need({ type: 'ABS', tray_info_idx: 'Pa240002', ignore_profile: true })],
      [-1],
      [tray({ globalTrayId: 0, type: 'PETG' })],
    );
    expect(reason.wanted).not.toContain('Pa240002');
  });

  it('stops naming the trays one by one past the fourth and counts the rest', () => {
    const loaded = [0, 1, 2, 3, 4, 5].map((id) => tray({ globalTrayId: id, type: 'ABS' }));
    const reason = reasonFor([need()], [-1], loaded);
    expect(reason.loaded).toMatch(/, \+2$/);
    expect(reason.loaded!.match(/ABS/g) ?? []).toHaveLength(4);
  });

  it('names every tray while they fit', () => {
    const loaded = [0, 1, 2, 3].map((id) => tray({ globalTrayId: id, type: 'ABS' }));
    const reason = reasonFor([need()], [-1], loaded);
    expect(reason.loaded).not.toContain('+');
    expect(reason.loaded!.match(/ABS/g) ?? []).toHaveLength(4);
  });
});

describe('autoFeasibility reads the preview three-valued', () => {
  const group = (over: Partial<RoutingPreview['plates'][number]['groups'][number]> = {}) => ({
    key: 'X1C/1/present',
    model: 'X1C',
    nozzles: 1,
    ams: 'present' as const,
    total: 1,
    compatible: 0,
    unknown: 0,
    incompatible: 0,
    ready: 0,
    reasons: [],
    ...over,
  });

  const preview = (
    groups: ReturnType<typeof group>[],
    over: { advisoryUnavailable?: boolean; status?: 'ok' | 'unavailable' } = {},
  ): RoutingPreview => ({
    advisory_unavailable: over.advisoryUnavailable ?? false,
    plates: [
      {
        requested_plate_id: 1,
        plate_id: 1,
        status: over.status ?? 'ok',
        reason: null,
        model: 'X1C',
        filaments: [],
        groups,
      },
    ],
  });

  const refused = [
    { code: 'material_mismatch', message: 'No compatible filament source is available.', count: 1 },
  ];

  it('answers unknown with no preview in hand', () => {
    expect(autoFeasibility(undefined)).toBe(UNKNOWN);
  });

  it('blocks only on all four conjuncts: incompatible, nothing compatible, nothing unknown, nothing skipped', () => {
    const verdict = autoFeasibility(preview([group({ incompatible: 1, reasons: refused })]));
    expect(verdict.state).toBe('blocked_now');
    expect(verdict.reason?.message).toBe('No compatible filament source is available.');
  });

  it('⚠️ does not block when a printer was skipped entirely (advisory_unavailable)', () => {
    // Counters that look complete while the evaluation is not: without the
    // fourth conjunct one incompatible beside one skipped printer would block.
    const verdict = autoFeasibility(
      preview([group({ incompatible: 1, reasons: refused })], { advisoryUnavailable: true }),
    );
    expect(verdict.state).toBe('unknown');
  });

  it('⚠️ does not block when something is merely unknown — zero compatible is not proof', () => {
    const verdict = autoFeasibility(
      preview([group({ incompatible: 1, unknown: 1, total: 2, reasons: refused })]),
    );
    expect(verdict.state).toBe('unknown');
  });

  it('is fine with a farm that is compatible but busy', () => {
    // Zero READY with something compatible is an ordinary busy farm: the job
    // waits, which is what a queue is for.
    expect(autoFeasibility(preview([group({ compatible: 2, total: 2, ready: 0 })])).state).toBe('ok');
  });

  it('⚠️ answers unknown, not ok, when no plate could be evaluated', () => {
    // An accumulator seeded `FEASIBLE` reported "everything is fine" for a
    // preview in which nothing at all had been judged.
    expect(
      autoFeasibility(preview([group({ compatible: 1 })], { status: 'unavailable' })).state,
    ).toBe('unknown');
    expect(autoFeasibility({ advisory_unavailable: false, plates: [] }).state).toBe('unknown');
  });

  it('takes the refusal the most printers agreed on', () => {
    const verdict = autoFeasibility(
      preview([
        group({
          incompatible: 3,
          reasons: [
            { code: 'material_mismatch', message: 'No compatible filament source is available.', count: 1 },
            { code: 'color_mismatch', message: 'The required color is not loaded.', count: 2 },
          ],
        }),
      ]),
    );
    expect(verdict.reason?.message).toBe('The required color is not loaded.');
  });
});
