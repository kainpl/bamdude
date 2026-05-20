/**
 * Frontend password-complexity checker — mirrors
 * ``backend/app/schemas/auth.py:_validate_password_complexity``.
 *
 * BamDude's rules are intentionally softer than upstream's (no special-
 * character requirement) per the §18.6 M-C decision: NIST SP 800-63B
 * advises against composition rules beyond length + basic mix, and
 * operator friction was causing real installs to pick worse-remembered
 * passwords. Three rules total:
 *
 *   - min 8 characters
 *   - at least one uppercase letter
 *   - at least one lowercase letter
 *   - at least one digit
 *
 * The check order matches the backend validator so fixing one rule
 * doesn't immediately trip a different message — the user fixes the
 * problem they were just told about (upstream Bambuddy #1303 / commit
 * d0818327).
 *
 * Returns ``null`` when the password passes all rules, else the i18n
 * key of the FIRST failing rule. Callers pass that key into ``t(...)``
 * for display.
 */
export type PasswordRuleKey =
  | 'settings.toast.passwordTooShort'
  | 'settings.toast.passwordNeedsUppercase'
  | 'settings.toast.passwordNeedsLowercase'
  | 'settings.toast.passwordNeedsDigit';

const MIN_PASSWORD_LENGTH = 8;

export function checkPasswordComplexity(password: string): PasswordRuleKey | null {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return 'settings.toast.passwordTooShort';
  }
  if (!/[A-Z]/.test(password)) {
    return 'settings.toast.passwordNeedsUppercase';
  }
  if (!/[a-z]/.test(password)) {
    return 'settings.toast.passwordNeedsLowercase';
  }
  if (!/\d/.test(password)) {
    return 'settings.toast.passwordNeedsDigit';
  }
  return null;
}

/** Convenience: ``true`` iff the password satisfies every rule. */
export function isPasswordValid(password: string): boolean {
  return checkPasswordComplexity(password) === null;
}
