/**
 * A slot under the backup-compatibility policy carries `actual` beside its live
 * fields. Everything that reasons about the SPOOL reads actual; everything that
 * reasons about the FIRMWARE (backup-group reconstruction) keeps reading live.
 */
import { describe, it, expect } from 'vitest';
import { buildLoadedFilaments } from '../../hooks/useFilamentMapping';
import { computeBackupGroups } from '../../utils/amsHelpers';
import type { PrinterStatus } from '../../api/client';

const status = {
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99', remain: 50,
          actual: { tray_color: 'FF0000FF', tray_type: 'PETG', tray_info_idx: 'GFG00', cols: [] } },
        { id: 1, tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99', remain: 50,
          actual: { tray_color: '0000FFFF', tray_type: 'PETG', tray_info_idx: 'GFG00', cols: [] } },
        { id: 2, tray_type: 'PLA', tray_color: 'FFFFFFFF', tray_info_idx: 'GFA00', remain: 50, actual: null },
      ],
    },
  ],
  vt_tray: [],
} as unknown as PrinterStatus;

describe('buildLoadedFilaments and the advertised profile', () => {
  it('reads the actual spool and remembers what was advertised', () => {
    const [a, b, c] = buildLoadedFilaments(status);
    expect([a.color, a.trayInfoIdx, a.advertisedColor, a.advertisedTrayInfoIdx]).toEqual(['#FF0000', 'GFG00', '#000000', 'GFG99']);
    expect([b.color, b.trayInfoIdx]).toEqual(['#0000FF', 'GFG00']);
    expect([c.color, c.trayInfoIdx, c.advertisedColor]).toEqual(['#FFFFFF', 'GFA00', undefined]);
  });

  it('backup-group reconstruction keeps looking through the firmware eyes', () => {
    const groups = computeBackupGroups(status.ams, undefined, false);
    const pair = groups.find((g) => g.members.length === 2);
    // Advertised black+black pair, although the spools are red and blue.
    expect(pair?.members.map((m) => m.slotIdx).sort()).toEqual([0, 1]);
  });
});
