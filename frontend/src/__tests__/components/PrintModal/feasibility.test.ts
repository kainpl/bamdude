/**
 * The feasibility verdict itself, away from the dialog that renders it.
 *
 * `PrintModalFeasibility.test.tsx` pins what the operator SEES; this pins the
 * decision behind it, where the two counter-examples that made the first design
 * wrong live: a channel's status word is not the verdict, and a counter of zero
 * is not a proof.
 *
 * Specific targets carry the full resolver verdict and its own refusal sentence;
 * this adapter must not reconstruct either from the channel display status.
 */

import { describe, it, expect } from 'vitest';
import {
  FEASIBLE,
  UNKNOWN,
  autoFeasibility,
  targetFeasibility,
  worstVerdict,
  type FeasibilityVerdict,
} from '../../../components/PrintModal/feasibility';
import type { RoutingPreview } from '../../../api/client';


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

describe('targetFeasibility uses the backend verdict', () => {
  it('does not turn a populated mapping into permission', () => {
    expect(targetFeasibility({ printer_id: 1, plate_id: 1, status: 'incompatible', mapping: [0],
      reason: { code: 'variant_mismatch', message: 'Profile differs' } }))
      .toEqual({ state: 'blocked_now', reason: { code: 'variant_mismatch', message: 'Profile differs' } });
  });
  it('unknown is not evidence of incompatibility', () => {
    expect(targetFeasibility(undefined)).toBe(UNKNOWN);
    expect(targetFeasibility({ printer_id: 1, plate_id: 1, status: 'unknown', mapping: null, reason: null })).toBe(UNKNOWN);
  });
  it('model refusal has no override', () => {
    expect(targetFeasibility({ printer_id: 1, plate_id: 1, status: 'incompatible', mapping: null,
      reason: { code: 'model_mismatch', message: 'Wrong model' } }).state).toBe('blocked_target');
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
