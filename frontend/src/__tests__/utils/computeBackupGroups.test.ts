/**
 * computeBackupGroups — the pairing rule behind the AMS Backup modal (#1762).
 *
 * The rule is deliberately strict and mirrors the backend's material identity:
 * two slots back each other up ONLY when they carry the same Bambu preset
 * (tray_info_idx) AND the same colour. Anything looser would tell the user two
 * spools are interchangeable when the firmware will not actually switch
 * between them.
 */

import { describe, it, expect } from 'vitest';
import { computeBackupGroups, type AmsUnitLike } from '../../utils/amsHelpers';

const tray = (id: number, type: string | null, preset: string | null, color: string | null) => ({
  id,
  tray_type: type,
  tray_sub_brands: type ? `Bambu ${type}` : null,
  tray_color: color,
  tray_info_idx: preset,
});

const unit = (id: number, trays: ReturnType<typeof tray>[]): AmsUnitLike => ({ id, tray: trays });

describe('computeBackupGroups', () => {
  it('pairs slots with the same preset and colour', () => {
    const groups = computeBackupGroups(
      [unit(0, [tray(0, 'PLA', 'GFA00', 'FF0000FF'), tray(1, 'PLA', 'GFA00', 'FF0000')])],
      undefined,
      false,
    );
    const pairs = groups.filter(g => g.members.length >= 2);
    expect(pairs).toHaveLength(1);
    // Alpha is stripped for identity, so FF0000FF and FF0000 are the same red.
    expect(pairs[0].members.map(m => m.globalTrayId)).toEqual([0, 1]);
  });

  it('does NOT pair the same preset in different colours', () => {
    const groups = computeBackupGroups(
      [unit(0, [tray(0, 'PETG', 'GFG00', 'FF0000'), tray(1, 'PETG', 'GFG00', '0000FF')])],
      undefined,
      false,
    );
    expect(groups.filter(g => g.members.length >= 2)).toHaveLength(0);
    expect(groups).toHaveLength(2); // both returned as lone slots
  });

  it('never pairs slots without a preset, however alike they look', () => {
    const groups = computeBackupGroups(
      [unit(0, [tray(0, 'PLA', null, 'FF0000'), tray(1, 'PLA', null, 'FF0000')])],
      undefined,
      false,
    );
    expect(groups.filter(g => g.members.length >= 2)).toHaveLength(0);
  });

  it('skips empty slots entirely', () => {
    const groups = computeBackupGroups(
      [unit(0, [tray(0, 'PLA', 'GFA00', 'FF0000'), tray(1, null, null, null)])],
      undefined,
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(1);
  });

  it('does not pair across extruders on a dual-nozzle printer', () => {
    // Same preset + colour, but AMS 0 feeds the right extruder and AMS 1 the
    // left — the firmware cannot cross them even with backup on.
    const groups = computeBackupGroups(
      [unit(0, [tray(0, 'PLA', 'GFA00', 'FF0000')]), unit(1, [tray(0, 'PLA', 'GFA00', 'FF0000')])],
      { '0': 0, '1': 1 },
      true,
    );
    expect(groups.filter(g => g.members.length >= 2)).toHaveLength(0);
    expect(groups).toHaveLength(2);
    expect(groups.map(g => g.extruder).sort()).toEqual([0, 1]);
  });

  it('pairs across AMS units on the same extruder', () => {
    const groups = computeBackupGroups(
      [unit(0, [tray(0, 'PLA', 'GFA00', 'FF0000')]), unit(1, [tray(0, 'PLA', 'GFA00', 'FF0000')])],
      { '0': 0, '1': 0 },
      true,
    );
    const pairs = groups.filter(g => g.members.length >= 2);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].members.map(m => m.amsId)).toEqual([0, 1]);
  });

  it('tolerates a duplicated AMS unit in the status payload', () => {
    // Observed in the wild on merged MQTT partials — a duplicate would
    // otherwise render the same physical slot twice inside one ring.
    const u = unit(0, [tray(0, 'PLA', 'GFA00', 'FF0000')]);
    const groups = computeBackupGroups([u, u], undefined, false);
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(1);
  });

  it('returns nothing when there are no AMS units', () => {
    expect(computeBackupGroups([], undefined, false)).toEqual([]);
    expect(computeBackupGroups(undefined, undefined, false)).toEqual([]);
  });
});
