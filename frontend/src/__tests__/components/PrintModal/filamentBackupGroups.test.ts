/**
 * The grouping rule behind the pre-print filament check.
 *
 * Every one of these failures is silent in the product: a wrong group either
 * warns about a printer that can finish the plate (which the operator learns to
 * click past) or stays quiet about one that cannot.
 */

import { describe, it, expect } from 'vitest';
import {
  NOMINAL_SPOOL_GRAMS,
  groupTraysForBackup,
  nominalGramsFromRemain,
  privateBackupGroup,
} from '../../../components/PrintModal/filamentBackupGroups';
import type { LoadedFilament } from '../../../hooks/useFilamentMapping';

const tray = (overrides: Partial<LoadedFilament> & { globalTrayId: number }): LoadedFilament => ({
  type: 'PLA',
  color: '#FF0000',
  colorName: 'Red',
  amsId: 0,
  trayId: overrides.globalTrayId,
  isHt: false,
  isExternal: false,
  label: `A${overrides.globalTrayId + 1}`,
  trayInfoIdx: '',
  ...overrides,
});

describe('groupTraysForBackup', () => {
  it('pools trays that share a preset and a colour', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00' }),
        tray({ globalTrayId: 1, trayInfoIdx: 'GFA00' }),
      ],
      true
    );

    expect(groups.get(0)?.key).toBe('GFA00|#FF0000');
    expect(groups.get(0)).toBe(groups.get(1));
    expect(groups.get(0)?.trayIds).toEqual([0, 1]);
    expect(groups.get(0)?.labels).toEqual(['A1', 'A2']);
  });

  it('⚠️ keeps the same preset in different colours apart — a swap must not change the part', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00', color: '#FF0000' }),
        tray({ globalTrayId: 1, trayInfoIdx: 'GFA00', color: '#00FF00' }),
      ],
      true
    );

    expect(groups.get(0)).not.toBe(groups.get(1));
    expect(groups.get(1)?.key).toBe('GFA00|#00FF00');
  });

  it('⚠️ keeps different presets apart even in the same colour — Basic is not Matte', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00' }),
        tray({ globalTrayId: 1, trayInfoIdx: 'GFA01' }),
      ],
      true
    );

    expect(groups.get(0)).not.toBe(groups.get(1));
  });

  it('falls back to type + colour when the tray carries no preset', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, type: 'PETG' }),
        tray({ globalTrayId: 1, type: 'PETG' }),
        tray({ globalTrayId: 2, type: 'PLA' }),
      ],
      true
    );

    expect(groups.get(0)?.key).toBe('PETG|#FF0000');
    expect(groups.get(0)).toBe(groups.get(1));
    expect(groups.get(2)).not.toBe(groups.get(0));
  });

  it('⚠️ a preset-less tray never joins a preset-carrying one, even of the same type and colour', () => {
    // Their keys are built from different fields; the printer's own rule is the
    // preset when it has one, so "PLA|#FF0000" and "GFA00|#FF0000" are not the
    // same pot and must not be merged by a helpful-looking fallback.
    const groups = groupTraysForBackup(
      [tray({ globalTrayId: 0, trayInfoIdx: 'GFA00' }), tray({ globalTrayId: 1, trayInfoIdx: '' })],
      true
    );

    expect(groups.get(0)).not.toBe(groups.get(1));
  });

  it('gives every tray its own group when backup is off', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00' }),
        tray({ globalTrayId: 1, trayInfoIdx: 'GFA00' }),
      ],
      false
    );

    expect(groups.get(0)).not.toBe(groups.get(1));
    expect(groups.get(0)?.trayIds).toEqual([0]);
    expect(groups.get(1)?.trayIds).toEqual([1]);
    expect(groups.get(0)?.key).not.toBe(groups.get(1)?.key);
  });

  it('answers nothing for a tray the printer never reported', () => {
    const groups = groupTraysForBackup([tray({ globalTrayId: 0 })], true);

    expect(groups.get(254)).toBeUndefined();
  });

  it('a private group is exactly one tray under a key no shared group can collide with', () => {
    const group = privateBackupGroup(254, 'External');

    expect(group).toEqual({ key: 'tray:254', trayIds: [254], labels: ['External'] });
    expect(group.key).not.toContain('|');
  });
});

describe('nominalGramsFromRemain', () => {
  it('scales a nominal 1 kg reel by the AMS fill percentage', () => {
    expect(nominalGramsFromRemain(45)).toBe(450);
    expect(nominalGramsFromRemain(100)).toBe(NOMINAL_SPOOL_GRAMS);
  });

  it('⚠️ refuses 0 — the firmware says 0 when it has nothing to report, not when a spool is empty', () => {
    expect(nominalGramsFromRemain(0)).toBeNull();
  });

  it('refuses the -1 the AMS sends for a tray it cannot measure', () => {
    expect(nominalGramsFromRemain(-1)).toBeNull();
  });

  it('refuses an absent reading and an out-of-range one', () => {
    expect(nominalGramsFromRemain(undefined)).toBeNull();
    expect(nominalGramsFromRemain(101)).toBeNull();
    expect(nominalGramsFromRemain(Number.NaN)).toBeNull();
  });

  it('takes a different reel size when one is known', () => {
    expect(nominalGramsFromRemain(50, 250)).toBe(125);
  });
});
