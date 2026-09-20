import { useTranslation } from 'react-i18next';

interface SubmitBlockedHintProps {
  /** Already-translated names of the fields still standing in the way. */
  missing: string[];
}

/**
 * Says out loud why the submit button next to it is disabled.
 *
 * A disabled button with no explanation is indistinguishable from a broken
 * one: an operator filled in a user dialog, saw the Create button stay grey
 * and had nothing on screen to act on (reported 2026-09-08). Password rules
 * answer for themselves inside `PasswordField`; this covers the fields that
 * are simply still empty.
 */
export function SubmitBlockedHint({ missing }: SubmitBlockedHintProps) {
  const { t } = useTranslation();
  if (missing.length === 0) return null;
  return (
    <p className="text-xs text-bambu-gray text-right" role="status">
      {t('common.stillNeeded', { items: missing.join(', ') })}
    </p>
  );
}
