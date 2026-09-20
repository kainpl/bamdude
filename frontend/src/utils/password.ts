/**
 * Frontend password-complexity rules — mirror
 * ``backend/app/schemas/auth.py:_validate_password_complexity``.
 *
 * BamDude's rules are intentionally softer than upstream's (no special-
 * character requirement) per the §18.6 M-C decision: NIST SP 800-63B
 * advises against composition rules beyond length + basic mix, and
 * operator friction was causing real installs to pick worse-remembered
 * passwords. Four rules total:
 *
 *   - min 8 characters
 *   - at least one uppercase letter
 *   - at least one lowercase letter
 *   - at least one digit
 *
 * ⚠️ The two sides must not drift, and they have — twice, in opposite
 * directions. ``backend/tests/unit/test_password_rules_match_the_frontend.py``
 * reads THIS file and fails on a changed constant, check or order.
 *
 * The order matches the backend validator so fixing one rule doesn't
 * immediately trip a different message — the user fixes the problem they
 * were just told about (upstream Bambuddy #1303 / commit d0818327).
 */
export type PasswordRuleKey =
  | 'settings.toast.passwordTooShort'
  | 'settings.toast.passwordNeedsUppercase'
  | 'settings.toast.passwordNeedsLowercase'
  | 'settings.toast.passwordNeedsDigit';

const MIN_PASSWORD_LENGTH = 8;

export interface PasswordRule {
  /** i18n key of the sentence shown when this rule is the one blocking. */
  key: PasswordRuleKey;
  /**
   * i18n key of the short form, for the requirement list a field shows while
   * the user types. "At least 8 characters" reads better four times over than
   * "Password must contain at least one uppercase letter" does.
   */
  labelKey: string;
  test: (password: string) => boolean;
}

/** The rules, in the order they are checked and displayed. */
export const PASSWORD_RULES: PasswordRule[] = [
  {
    key: 'settings.toast.passwordTooShort',
    labelKey: 'common.passwordRules.length',
    test: (password) => password.length >= MIN_PASSWORD_LENGTH,
  },
  {
    key: 'settings.toast.passwordNeedsUppercase',
    labelKey: 'common.passwordRules.uppercase',
    test: (password) => /[A-Z]/.test(password),
  },
  {
    key: 'settings.toast.passwordNeedsLowercase',
    labelKey: 'common.passwordRules.lowercase',
    test: (password) => /[a-z]/.test(password),
  },
  {
    key: 'settings.toast.passwordNeedsDigit',
    labelKey: 'common.passwordRules.digit',
    test: (password) => /\d/.test(password),
  },
];

/**
 * Returns ``null`` when the password passes every rule, else the i18n key of
 * the FIRST failing one. Callers pass that key into ``t(...)`` for display.
 */
export function checkPasswordComplexity(password: string): PasswordRuleKey | null {
  return PASSWORD_RULES.find((rule) => !rule.test(password))?.key ?? null;
}

/** Convenience: ``true`` iff the password satisfies every rule. */
export function isPasswordValid(password: string): boolean {
  return checkPasswordComplexity(password) === null;
}
