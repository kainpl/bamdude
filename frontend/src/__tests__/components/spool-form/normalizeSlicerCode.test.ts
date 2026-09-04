/**
 * `GFS` is not only the prefix Bambu Cloud puts on a setting_id — it is also
 * how the support-material FAMILIES are spelled: `GFS00` Support W, `GFS01`
 * Support G, `GFS04` PVA, `GFSNL02` SUNLU PLA Matte. The printer reports those
 * as `tray_info_idx`, and their presets are `GFSS00` / `GFSSNL02`.
 *
 * Stripping the S whenever the code starts with `GFS` turned `GFS00` into
 * `GF00`, an id that exists nowhere, and the form then sent it as the spool's
 * family — which the server refused with `unknown filament family`. Nothing
 * on a support spool in an AMS could be saved, not even its weight
 * (2026-09-04). A family id has one of two shapes — `GF` + one letter + two
 * digits, or `GF` + three letters + two digits — and the S comes off only
 * when what is left has one of them.
 */
import { describe, expect, it } from 'vitest';

import { normalizeSlicerCodeToFilamentId, resolveTargetFilamentId } from '../../../components/spool-form/utils';

describe('normalizeSlicerCodeToFilamentId', () => {
  it.each([
    ['GFSA00', 'GFA00'],
    ['GFSG99', 'GFG99'],
    ['GFSG99_00', 'GFG99'],
    ['GFSS00', 'GFS00'],
    ['GFSSNL02', 'GFSNL02'],
    ['GFA00', 'GFA00'],
    ['GFL99', 'GFL99'],
  ])('%s → %s (a setting_id loses its S, a family keeps its shape)', (code, expected) => {
    expect(normalizeSlicerCodeToFilamentId(code)).toBe(expected);
  });

  it.each(['GFS00', 'GFS01', 'GFS04', 'GFS99', 'GFSNL02', 'GFSNL08'])(
    '%s is a family already and keeps its S',
    (family) => {
      expect(normalizeSlicerCodeToFilamentId(family)).toBe(family);
    },
  );

  it.each(['P1a2b3c4d', '12', 'PETG', '', null, undefined])('%s cannot be resolved synchronously', (code) => {
    expect(normalizeSlicerCodeToFilamentId(code)).toBeNull();
  });
});

describe('resolveTargetFilamentId', () => {
  it('keeps a support family as the target', () => {
    expect(resolveTargetFilamentId('GFS00', undefined)).toBe('GFS00');
  });

  it('prefers a custom preset\'s inherited base', () => {
    expect(resolveTargetFilamentId('P1a2b3c4d', { base_id: 'GFSS00', filament_id: 'P1a2b3c4d' })).toBe('GFS00');
  });
});
