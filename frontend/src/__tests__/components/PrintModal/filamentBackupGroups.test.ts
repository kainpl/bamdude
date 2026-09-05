/**
 * The grouping rule behind the pre-print filament check.
 *
 * Every one of these failures is silent in the product: a wrong group either
 * warns about a printer that can finish the plate (which the operator learns to
 * click past) or stays quiet about one that cannot.
 */

import { describe, it, expect } from 'vitest';
import {
  groupTraysForBackup,
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

    expect(groups.get(0)?.key).toBe('x|GFA00|#FF0000');
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
    expect(groups.get(1)?.key).toBe('x|GFA00|#00FF00');
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

    expect(groups.get(0)?.key).toBe('x|type:PETG|#FF0000');
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

  it('⚠️ never lets the external spool join an AMS group — backup does not feed from it', () => {
    // `buildLoadedFilaments` appends `vt_tray` with a real preset and the same
    // normalised colour, so an external holder looked identical to a fourth AMS
    // slot. BS builds its remain map from the AMS list alone; letting the
    // external spool in would have paid its weight into a pool the printer
    // cannot actually draw on, suppressing a real warning.
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00' }),
        tray({ globalTrayId: 254, trayInfoIdx: 'GFA00', isExternal: true, amsId: -1, label: 'External' }),
      ],
      true
    );

    expect(groups.get(0)).not.toBe(groups.get(254));
    expect(groups.get(254)).toEqual({ key: 'tray:254', trayIds: [254], labels: ['External'] });
    expect(groups.get(0)?.trayIds).toEqual([0]);
  });

  it('⚠️ keeps two extruders apart — no swap crosses nozzles', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00', extruderId: 0 }),
        tray({ globalTrayId: 4, trayInfoIdx: 'GFA00', extruderId: 1, label: 'B1' }),
      ],
      true
    );

    expect(groups.get(0)).not.toBe(groups.get(4));
    expect(groups.get(0)?.key).toBe('0|GFA00|#FF0000');
    expect(groups.get(4)?.key).toBe('1|GFA00|#FF0000');
  });

  it('pools two AMS units bound to the same extruder', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00', extruderId: 0 }),
        tray({ globalTrayId: 4, trayInfoIdx: 'GFA00', extruderId: 0, label: 'B1' }),
      ],
      true
    );

    expect(groups.get(0)).toBe(groups.get(4));
    expect(groups.get(0)?.trayIds).toEqual([0, 4]);
  });

  it('pools single-nozzle trays, where no tray has an extruder at all', () => {
    const groups = groupTraysForBackup(
      [
        tray({ globalTrayId: 0, trayInfoIdx: 'GFA00' }),
        tray({ globalTrayId: 4, trayInfoIdx: 'GFA00', label: 'B1' }),
      ],
      true
    );

    expect(groups.get(0)).toBe(groups.get(4));
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
