export const isStaleClaim = (code: string | null | undefined): boolean =>
  code === 'operator_cancelled_stale_claim' || code === 'noncurrent_execution_claim';

/**
 * A failed row's closed class and its detail as one line: «<class>: <detail>».
 *
 * The class is a ``failure_reasons`` key (a queue row's ``error_code``, an archive's
 * ``failure_reason``) shown through ``editArchive.failureReasons``; the detail is the
 * row's own sentence. A value that is no key (free text from an older build) reads as
 * it is, and a row with neither says nothing. Never parse the class out of the detail.
 */
export function failureText(
  t: (key: string, opts?: Record<string, unknown>) => string,
  code: string | null | undefined,
  detail: string | null | undefined,
): string {
  if (isStaleClaim(code)) return String(t('printStateRecovery.staleClaim'));
  const label = code ? String(t(`editArchive.failureReasons.${code}`, { defaultValue: code })) : '';
  const text = (detail ?? '').trim();
  if (label && text && label !== text) return `${label}: ${text}`;
  return label || text;
}
