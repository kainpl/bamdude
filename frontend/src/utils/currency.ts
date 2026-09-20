const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  EUR: '€',
  PLN: 'zł',
  UAH: '₴',
  BZD: 'BZ$',  // Belize Dollars (#1454)
};

export function getCurrencySymbol(currencyCode: string): string {
  return CURRENCY_SYMBOLS[currencyCode.toUpperCase()] || currencyCode;
}

export const SUPPORTED_CURRENCIES = Object.entries(CURRENCY_SYMBOLS).map(([code, symbol]) => ({
  code,
  label: `${code} (${symbol})`,
}));

/**
 * One amount, formatted the way the whole app already formats amounts.
 *
 * Symbol in front, always two decimals — the spelling that `getCurrencySymbol`
 * plus `toFixed(2)` had been repeated inline in eight files before this helper
 * existed. Every orders / products / customers page formats money through
 * here, so a bare `toLocaleString()` sneaking `30.5` in beside a `$30.50` from
 * the next screen over cannot happen again.
 *
 * A missing currency falls back to USD, matching the `settings?.currency ||
 * 'USD'` those callers all wrote: the settings query is unresolved on the
 * first paint, and an amount must still be readable then.
 */
export function formatMoney(value: number, currency: string | null | undefined): string {
  return `${getCurrencySymbol(currency || 'USD')}${value.toFixed(2)}`;
}
