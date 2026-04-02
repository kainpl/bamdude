const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  EUR: '€',
  PLN: 'zł',
  UAH: '₴',
};

export function getCurrencySymbol(currencyCode: string): string {
  return CURRENCY_SYMBOLS[currencyCode.toUpperCase()] || currencyCode;
}

export const SUPPORTED_CURRENCIES = Object.entries(CURRENCY_SYMBOLS).map(([code, symbol]) => ({
  code,
  label: `${code} (${symbol})`,
}));
