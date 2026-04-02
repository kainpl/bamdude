import { describe, it, expect } from 'vitest';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

/**
 * Recursively extracts all keys from a nested object as dot-notation paths.
 * Example: { foo: { bar: 'baz' } } => ['foo.bar']
 */
const getKeys = (obj: object, prefix = ''): string[] => {
  return Object.entries(obj).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === 'object' && value !== null
      ? getKeys(value, path)
      : [path];
  });
};

describe('i18n locale parity', () => {
  const enKeys = new Set(getKeys(en));
  const ukKeys = new Set(getKeys(uk));

  it('Ukrainian locale has all English keys', () => {
    const missingInUkrainian = [...enKeys].filter((k) => !ukKeys.has(k)).sort();
    expect(missingInUkrainian, `Missing ${missingInUkrainian.length} key(s) in Ukrainian locale`).toEqual([]);
  });

  it('English locale has all Ukrainian keys', () => {
    const missingInEnglish = [...ukKeys].filter((k) => !enKeys.has(k)).sort();
    expect(missingInEnglish, `Missing ${missingInEnglish.length} key(s) in English locale`).toEqual([]);
  });

  it('both locales have the same number of keys', () => {
    expect(enKeys.size).toBe(ukKeys.size);
  });
});
