import { describe, it, expect } from 'vitest';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

/**
 * The AMS backup-compatibility preview renders every per-slot reason through
 * `t('printers.amsCompat.reason.' + x)`, so a reason without a string reaches
 * the operator as its raw key.
 *
 * The other half of this pin is
 * `backend/tests/unit/services/test_ams_backup_compatibility.py::test_the_closed_list_of_reasons_is_the_one_both_locales_carry`,
 * which asserts the same six against `services/ams_backup_compatibility.REASONS`.
 * A new reason therefore breaks BOTH sides until its two strings exist — and a
 * retired one (`printer_busy`) cannot be left behind in a locale either.
 */
const REASONS = [
  'policy_off',
  'rfid_slot_excluded',
  'external_slot_excluded',
  'base_material_not_allowed',
  'generic_preset_unavailable',
  'slot_empty',
];

describe('printers.amsCompat.reason', () => {
  it.each([
    ['en', en],
    ['uk', uk],
  ])('carries exactly the backend\'s closed list of reasons (%s)', (_name, locale) => {
    const reasons = (locale as { printers: { amsCompat: { reason: Record<string, string> } } }).printers.amsCompat
      .reason;
    expect(Object.keys(reasons).sort()).toEqual([...REASONS].sort());
    for (const key of REASONS) {
      expect(reasons[key], key).toBeTruthy();
    }
  });
});
