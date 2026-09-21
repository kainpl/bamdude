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

/**
 * The same question once the operator has allowed a base-material match.
 *
 * The rule is the backend's, and the dialog must rank trays by it or it
 * promises a slot the dispatcher would not pick: with the option ON the profile
 * id is neither an eligibility condition nor a selection priority, and the
 * material the FILE declares is what gets compared; with it OFF the same
 * profile is required wherever the id is known on both sides.
 */
describe('the profile id once a base-material match is allowed', () => {
  /** Inside `colorsAreSimilar`' tolerance of RED, but not equal to it. */
  const NEARLY_RED = '#E61414';

  const asked = (
    extra: { tray_info_idx?: string; ignore_profile?: boolean; strict_profile_match?: boolean },
    loaded: LoadedFilament[],
    type = 'PLA',
    color = RED,
  ) =>
    buildFilamentComparison(
      { filaments: [{ slot_id: 1, type, color, used_grams: 10, ...extra }] },
      loaded,
      {},
    )[0];

  it('does not let the asked-for profile move the chosen tray', () => {
    // Two spools of the same material in the same colour, differing only by
    // profile. Which one the dialog picks must no longer depend on the id.
    const loaded = [tray(0, RED, BASIC), tray(1, RED, MATTE)];

    const matte = asked({ tray_info_idx: MATTE, ignore_profile: true }, loaded);
    const basic = asked({ tray_info_idx: BASIC, ignore_profile: true }, loaded);

    expect(matte.loaded?.globalTrayId).toBe(basic.loaded?.globalTrayId);
    expect(matte.status).toBe('match');
  });

  it('takes the exact colour of another profile over a near colour of the asked-for one', () => {
    const loaded = [tray(0, NEARLY_RED, MATTE), tray(1, RED, BASIC)];

    expect(asked({ tray_info_idx: MATTE, ignore_profile: true }, loaded).loaded?.globalTrayId).toBe(1);
    // Without the option the profile still decides selection first (#2650:
    // Basic is not Matte), which is the opposite order.
    expect(asked({ tray_info_idx: MATTE }, loaded).loaded?.globalTrayId).toBe(0);
  });

  it('refuses a different known profile when the option is off, whatever the family', () => {
    // Parity with the backend's `variant_mismatch`: it compares the two ids it
    // knows and never asked whether the profile's family had been resolved.
    const loaded = [tray(0, RED, 'GFG99', 'PETG')];
    const strict = { tray_info_idx: 'Pa240002', strict_profile_match: true };

    expect(asked(strict, loaded, 'PETG').status).toBe('mismatch');
    expect(asked({ tray_info_idx: 'Pa240002' }, loaded, 'PETG').status).toBe('match');
  });

  it('⚠️ ignores the profile when BOTH flags are set, rather than letting strict win', () => {
    // The two flags answer the same question and a caller can carry both: the
    // routing snapshot says «allow base material match» while a stored
    // requirement still remembers it was once strict. `ignore_profile` is the
    // later word and must be structurally exclusive, not exclusive by the
    // convention that nobody sets both.
    const loaded = [tray(0, RED, 'GFG99', 'PETG')];
    const both = { tray_info_idx: 'Pa240002', strict_profile_match: true, ignore_profile: true };

    expect(asked(both, loaded, 'PETG').status).toBe('match');
  });
});
