/**
 * A spool that already exists can be edited (#1905).
 *
 * A spool created by Quick Add, a CSV import or an RFID scan has no slicer
 * preset, brand or subtype. Reopening it in Edit demanded all three before
 * anything could be saved, so changing its storage location, its cost or a note
 * was impossible — and the Quick Add toggle that waives those fields is
 * create-only, so there was no way out of it. Copy had the same gate.
 *
 * The rule the form advertises and the rule it enforces are the same function,
 * `spoolDetailsRequired`, precisely so they cannot drift apart again: a field
 * marked required but unchecked (or the reverse) is how this went unnoticed.
 *
 * The backend has only ever required `material` — see `SpoolBase` in
 * `schemas/spool.py`.
 */
import { describe, expect, it } from 'vitest';

import { defaultFormData, spoolDetailsRequired, validateForm } from '../../../components/spool-form/types';
import type { SpoolFormData } from '../../../components/spool-form/types';

// What a Quick Add / CSV / RFID row actually looks like: material and nothing
// else the form used to insist on.
const stockSpool: SpoolFormData = { ...defaultFormData, material: 'PLA' };

describe('validateForm — details are required only when creating', () => {
  it('blocks a plain create until preset, brand and subtype are filled', () => {
    const { isValid, errors } = validateForm(stockSpool, false, false, 'create');

    expect(isValid).toBe(false);
    expect(errors.filament_family_id).toBeTruthy();
    expect(errors.brand).toBeTruthy();
    expect(errors.subtype).toBeTruthy();
  });

  it('lets a stock spool be edited', () => {
    // The whole bug: this used to fail, so its shelf location could never change.
    expect(validateForm(stockSpool, false, false, 'edit').isValid).toBe(true);
  });

  it('lets a stock spool be copied', () => {
    // Copy has no Quick Add toggle at all, so it was the harder half to escape.
    expect(validateForm(stockSpool, false, false, 'copy').isValid).toBe(true);
  });

  it('still requires material everywhere', () => {
    const noMaterial: SpoolFormData = { ...defaultFormData, material: '' };

    for (const mode of ['create', 'edit', 'copy'] as const) {
      expect(validateForm(noMaterial, false, false, mode).errors.material).toBeTruthy();
    }
  });

  it('defaults to create when no mode is given', () => {
    // Guards every caller that has not been updated: the strict rule stays the
    // default rather than being silently relaxed for everyone.
    expect(validateForm(stockSpool).isValid).toBe(false);
  });

  it('still validates the fields that are about shape, not completeness', () => {
    const badHex: SpoolFormData = { ...defaultFormData, material: 'PLA', extra_colors: 'nothex' };

    expect(validateForm(badHex, false, false, 'edit').errors.extra_colors).toBeTruthy();
  });
});

describe('spoolDetailsRequired — the markers and the check share one rule', () => {
  it('is true only for a plain create', () => {
    expect(spoolDetailsRequired(false, false, 'create')).toBe(true);
  });

  it('is false for quick add, Spoolman, edit and copy', () => {
    expect(spoolDetailsRequired(true, false, 'create')).toBe(false);
    expect(spoolDetailsRequired(false, true, 'create')).toBe(false);
    expect(spoolDetailsRequired(false, false, 'edit')).toBe(false);
    expect(spoolDetailsRequired(false, false, 'copy')).toBe(false);
  });

  it('agrees with validateForm in every combination', () => {
    // The invariant, stated directly: a required marker appears exactly when
    // the missing field would actually be refused.
    for (const quickAdd of [false, true]) {
      for (const spoolman of [false, true]) {
        for (const mode of ['create', 'edit', 'copy'] as const) {
          const required = spoolDetailsRequired(quickAdd, spoolman, mode);
          const refused = !validateForm(stockSpool, quickAdd, spoolman, mode).isValid;
          expect(refused).toBe(required);
        }
      }
    }
  });
});
