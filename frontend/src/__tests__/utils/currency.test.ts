import { describe, it, expect } from 'vitest';
import { formatMoney, getCurrencySymbol, SUPPORTED_CURRENCIES } from '../../utils/currency';

describe('getCurrencySymbol', () => {
  it('returns $ for USD', () => {
    expect(getCurrencySymbol('USD')).toBe('$');
  });

  it('returns € for EUR', () => {
    expect(getCurrencySymbol('EUR')).toBe('€');
  });

  it('returns zł for PLN', () => {
    expect(getCurrencySymbol('PLN')).toBe('zł');
  });

  it('returns ₴ for UAH', () => {
    expect(getCurrencySymbol('UAH')).toBe('₴');
  });

  it('returns BZ$ for BZD', () => {
    expect(getCurrencySymbol('BZD')).toBe('BZ$');
  });

  it('returns the code itself for unknown currencies', () => {
    expect(getCurrencySymbol('XYZ')).toBe('XYZ');
  });

  it('is case-insensitive', () => {
    expect(getCurrencySymbol('usd')).toBe('$');
    expect(getCurrencySymbol('eur')).toBe('€');
  });
});

describe('SUPPORTED_CURRENCIES', () => {
  it('contains USD', () => {
    expect(SUPPORTED_CURRENCIES.find((c) => c.code === 'USD')).toBeDefined();
  });

  it('contains UAH', () => {
    expect(SUPPORTED_CURRENCIES.find((c) => c.code === 'UAH')).toBeDefined();
  });

  it('contains BZD', () => {
    expect(SUPPORTED_CURRENCIES.find((c) => c.code === 'BZD')).toBeDefined();
  });

  it('has 5 entries', () => {
    expect(SUPPORTED_CURRENCIES).toHaveLength(5);
  });
});

describe('formatMoney', () => {
  it('puts the symbol in front and always shows two decimals', () => {
    expect(formatMoney(450, 'USD')).toBe('$450.00');
    expect(formatMoney(30.5, 'USD')).toBe('$30.50');
  });

  // `toFixed` is what the eight inline call sites this helper replaces already
  // used, so its rounding IS the convention — including the part where a
  // decimal midpoint like 1.005 is not representable in binary and rounds
  // down. Asserted rather than papered over: money here is a display figure
  // the server computed, never a value this helper is asked to total up.
  it('rounds to two decimals the way toFixed does', () => {
    expect(formatMoney(1.006, 'EUR')).toBe('€1.01');
    expect(formatMoney(1.004, 'EUR')).toBe('€1.00');
    expect(formatMoney(1.005, 'EUR')).toBe('€1.00');
    expect(formatMoney(0, 'UAH')).toBe('₴0.00');
  });

  it('uses the currency it is handed', () => {
    expect(formatMoney(12, 'PLN')).toBe('zł12.00');
    expect(formatMoney(12, 'XYZ')).toBe('XYZ12.00');
  });

  // The settings query is unresolved on the first paint, and every caller of
  // this helper renders before it lands.
  it('falls back to USD for a missing currency', () => {
    expect(formatMoney(7, null)).toBe('$7.00');
    expect(formatMoney(7, undefined)).toBe('$7.00');
    expect(formatMoney(7, '')).toBe('$7.00');
  });
});
