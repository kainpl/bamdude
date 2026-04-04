import { describe, it, expect } from 'vitest';
import { getCurrencySymbol, SUPPORTED_CURRENCIES } from '../../utils/currency';

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

  it('has 4 entries', () => {
    expect(SUPPORTED_CURRENCIES).toHaveLength(4);
  });
});
