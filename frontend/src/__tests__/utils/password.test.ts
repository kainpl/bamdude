/**
 * Tests for the password complexity util.
 *
 * Mirrors backend ``_validate_password_complexity``. BamDude's rule set
 * is intentionally softer than upstream Bambuddy's (no special-char
 * requirement) — three composition rules total: uppercase, lowercase,
 * digit. Plus min length 8.
 *
 * Adapted from upstream commit d0818327 / #1303.
 */

import { describe, expect, it } from 'vitest';
import { checkPasswordComplexity, isPasswordValid } from '../../utils/password';

describe('checkPasswordComplexity', () => {
  it('returns null for a fully valid password', () => {
    expect(checkPasswordComplexity('Abcdef12')).toBe(null);
    expect(checkPasswordComplexity('SuperLongPassw0rd')).toBe(null);
  });

  it('flags short passwords first regardless of other failures', () => {
    // 7 chars + only digits would also fail the other rules — short wins.
    expect(checkPasswordComplexity('1234567')).toBe('settings.toast.passwordTooShort');
    expect(checkPasswordComplexity('Ab1')).toBe('settings.toast.passwordTooShort');
  });

  it('reports the reporter exact case — 8-digit all-numeric — as missing uppercase', () => {
    // Upstream #1303 reporter typed "12345678" — long enough but no letters.
    expect(checkPasswordComplexity('12345678')).toBe('settings.toast.passwordNeedsUppercase');
  });

  it('flags missing uppercase before lowercase before digit (deterministic order)', () => {
    // "lowercase1234" — has lower + digit, missing upper. Upper rule fires first.
    expect(checkPasswordComplexity('lowercase1234')).toBe('settings.toast.passwordNeedsUppercase');
    // "UPPERCASE1234" — missing lower. Upper passes, lower fires.
    expect(checkPasswordComplexity('UPPERCASE1234')).toBe('settings.toast.passwordNeedsLowercase');
    // "UpperLower" — missing digit. Upper + lower pass, digit fires.
    expect(checkPasswordComplexity('UpperLower')).toBe('settings.toast.passwordNeedsDigit');
  });

  it('does NOT require a special character (softer than upstream)', () => {
    // BamDude §18.6 M-C: no special-char rule. "Abc12345" with NO special
    // must pass.
    expect(checkPasswordComplexity('Abc12345')).toBe(null);
  });

  it('exactly 8 characters is the lower bound (boundary)', () => {
    expect(checkPasswordComplexity('Abcdefg1')).toBe(null);
    expect(checkPasswordComplexity('Abcdef1')).toBe('settings.toast.passwordTooShort');
  });
});

describe('isPasswordValid', () => {
  it('returns true for valid passwords', () => {
    expect(isPasswordValid('Abcdef12')).toBe(true);
  });

  it('returns false for any failing rule', () => {
    expect(isPasswordValid('short')).toBe(false);
    expect(isPasswordValid('12345678')).toBe(false);
    expect(isPasswordValid('lowercase1')).toBe(false);
  });
});
