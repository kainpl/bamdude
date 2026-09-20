import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Eye, EyeOff, Check, Dot } from 'lucide-react';
import { PASSWORD_RULES } from '../utils/password';

interface PasswordFieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  autoComplete?: string;
  required?: boolean;
  id?: string;
  autoFocus?: boolean;
  /**
   * Show the complexity requirements under the field and tick them off as they
   * are met. Off for "current password" fields, where the rules describe the
   * NEW password and an old one that predates them is still valid.
   */
  showRules?: boolean;
  /** When given and different from `value`, the field says they do not match. */
  mustMatch?: string;
  // ⚠️ `required` is the HTML attribute only — it deliberately draws no
  // asterisk. Forms in this codebase mark their own required fields (the
  // advanced-auth user dialog puts a red `*` in the label itself), and a
  // component that added one of its own put an asterisk on the login page's
  // password beside an unmarked username.
}

/**
 * One password input: reveal toggle, and the requirements written out.
 *
 * ⚠️ Saying what is wanted is the point of this component. Every create /
 * change form disabled its submit button on the complexity rules while the
 * only message naming the unmet one lived in the click handler — behind the
 * very button that rule disabled. So a user typing a password that was merely
 * too short saw a dead button and no text anywhere: the explanation existed
 * and was unreachable by construction (reported 2026-09-08).
 *
 * The requirements are a LIST rather than a single first-failure line, because
 * they are worth knowing before the password is refused: being told "at least
 * 8 characters", typing eight, and only then being told a digit is also needed
 * is the same dead end one step later.
 *
 * The rules come from `utils/password.ts`, which mirrors the backend
 * validator. Nothing here may grow its own threshold: two forms had
 * hand-rolled `length < 6` checks while the server required 8 plus a mix, so
 * they let a password through that the API then refused.
 */
export function PasswordField({
  label,
  value,
  onChange,
  placeholder,
  autoComplete = 'new-password',
  required,
  id,
  autoFocus,
  showRules,
  mustMatch,
}: PasswordFieldProps) {
  const { t } = useTranslation();
  const [revealed, setRevealed] = useState(false);
  const [focused, setFocused] = useState(false);
  const generatedId = useId();
  const inputId = id ?? generatedId;

  // A confirmation is either right or wrong — there is nothing to guide, so it
  // keeps a single red line. An empty one is not yet a mistake.
  const mismatch = mustMatch !== undefined && value.length > 0 && value !== mustMatch;

  // The requirements appear once the user is at the field: an untouched form is
  // not the place to lecture, and the placeholder already carries the gist.
  const rulesVisible = Boolean(showRules) && (focused || value.length > 0);

  return (
    <div>
      <label htmlFor={inputId} className="block text-sm font-medium text-white mb-2">
        {label}
      </label>
      <div className="relative">
        <input
          id={inputId}
          type={revealed ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          className={`block w-full px-4 py-3 pr-12 bg-bambu-dark-secondary border rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors ${
            mismatch ? 'border-red-500' : 'border-bambu-dark-tertiary'
          }`}
          placeholder={placeholder}
          autoComplete={autoComplete}
          required={required}
          autoFocus={autoFocus}
          aria-invalid={mismatch ? true : undefined}
          aria-describedby={mismatch ? `${inputId}-error` : rulesVisible ? `${inputId}-rules` : undefined}
        />
        <button
          type="button"
          onClick={() => setRevealed(!revealed)}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white transition-colors"
          // Out of the tab order on purpose: tabbing from the password field
          // should reach the next field, not a view toggle.
          tabIndex={-1}
          aria-label={t(revealed ? 'common.hidePassword' : 'common.showPassword')}
          title={t(revealed ? 'common.hidePassword' : 'common.showPassword')}
        >
          {revealed ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
        </button>
      </div>
      {mismatch && (
        <p id={`${inputId}-error`} className="text-red-700 dark:text-red-400 text-xs mt-1">
          {t('settings.passwordsDoNotMatch')}
        </p>
      )}
      {rulesVisible && (
        <ul id={`${inputId}-rules`} className="mt-2 space-y-0.5">
          {PASSWORD_RULES.map((rule) => {
            const met = rule.test(value);
            return (
              <li
                key={rule.key}
                className={`flex items-center gap-1 text-xs transition-colors ${
                  met ? 'text-bambu-green' : 'text-bambu-gray'
                }`}
              >
                {/* A met rule is ticked; an unmet one gets a neutral dot, not a
                    cross — nothing is wrong yet, it is simply not done. */}
                {met ? (
                  <Check className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
                ) : (
                  <Dot className="w-3.5 h-3.5 flex-shrink-0" aria-hidden="true" />
                )}
                <span>{t(rule.labelKey)}</span>
                {/* The colour and the icon carry the state for everyone else. */}
                <span className="sr-only">
                  {t(met ? 'common.passwordRules.met' : 'common.passwordRules.unmet')}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
