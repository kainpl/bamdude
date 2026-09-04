/**
 * A unique preset match is not a colour match (upstream #2687).
 *
 * `tray_info_idx` names the filament VARIANT, not an individual spool: GFA00 is
 * PLA Basic, GFA01 PLA Matte, GFA17 PLA Translucent — in every colour Bambu
 * sells. The matcher accepted a uniquely-matching idx as definitive, on the
 * premise "same preset = same spool = same colour".
 *
 * With one Matte spool loaded, every Matte requirement matched it whatever
 * colour it was, and the colour comparison was never reached — so the panel
 * showed "(Ready)" with a green tick for dark red required against dark green
 * loaded, while picking that same tray by hand reported the mismatch correctly.
 *
 * Variant still decides *selection* among trays that agree on colour (#2650:
 * PLA Basic is not PLA Matte). It just no longer decides the verdict.
 */

import { describe, it, expect } from 'vitest';

import { buildFilamentComparison } from '../../hooks/useFilamentMapping';
import type { LoadedFilament } from '../../hooks/useFilamentMapping';

const MATTE = 'GFA01';
const BASIC = 'GFA00';
const RED = '#FF0000';
const GREEN = '#00FF00';

function tray(globalTrayId: number, color: string, trayInfoIdx: string, type = 'PLA'): LoadedFilament {
  return {
    globalTrayId,
    type,
    color,
    colorName: '',
    amsId: 0,
    trayId: globalTrayId,
    isHt: false,
    isExternal: false,
    trayInfoIdx,
    label: `AMS${globalTrayId}`,
    remain: 50,
  };
}

function need(color: string, trayInfoIdx: string, type = 'PLA') {
  return { filaments: [{ slot_id: 1, type, color, tray_info_idx: trayInfoIdx, used_grams: 10 }] };
}

describe('a filament variant is not a colour (#2687)', () => {
  it('reports a mismatch instead of a green tick when the colours differ', () => {
    // The reported case: one Matte spool, in green; the slice wants Matte red.
    const [row] = buildFilamentComparison(need(RED, MATTE), [tray(0, GREEN, MATTE)], {});

    expect(row.hasFilament).toBe(true);
    expect(row.colorMatch).toBe(false);
    expect(row.status).toBe('type_only');
  });

  it('prefers a correctly-coloured tray of another variant', () => {
    const loaded = [tray(0, GREEN, MATTE), tray(1, RED, BASIC)];
    const [row] = buildFilamentComparison(need(RED, MATTE), loaded, {});

    expect(row.loaded?.globalTrayId).toBe(1);
    expect(row.status).toBe('match');
  });

  it('still lets the variant decide between trays that agree on colour', () => {
    const loaded = [tray(0, RED, MATTE), tray(1, RED, BASIC)];
    const [row] = buildFilamentComparison(need(RED, MATTE), loaded, {});

    expect(row.loaded?.globalTrayId).toBe(0);
    expect(row.status).toBe('match');
  });

  it('treats a requirement with no colour as satisfied by any colour', () => {
    // The 3MF simply did not ask for one; that is not a mismatch.
    const [row] = buildFilamentComparison(need('', MATTE), [tray(0, GREEN, MATTE)], {});

    expect(row.colorMatch).toBe(true);
    expect(row.status).toBe('match');
  });

  it('reports a full mismatch when no tray of the type is loaded', () => {
    const [row] = buildFilamentComparison(need(RED, MATTE, 'TPU'), [tray(0, RED, MATTE, 'PETG')], {});

    expect(row.hasFilament).toBe(false);
    expect(row.status).toBe('mismatch');
  });
});
