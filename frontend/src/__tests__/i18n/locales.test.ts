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

// CLDR plural categories differ per language — EN has {one, other}, UK has {one, few, many, other}.
// Parity check must normalize these suffixes so `inQueue_other` (en) and `inQueue_many` (uk) count
// as the same logical key. Strip any trailing _one/_two/_few/_many/_other/_zero.
const PLURAL_SUFFIX = /_(one|two|few|many|other|zero)$/;
const normalizeKey = (k: string) => k.replace(PLURAL_SUFFIX, '');

describe('i18n locale parity', () => {
  const enKeys = new Set(getKeys(en).map(normalizeKey));
  const ukKeys = new Set(getKeys(uk).map(normalizeKey));

  it('Ukrainian locale has all English logical keys', () => {
    const missingInUkrainian = [...enKeys].filter((k) => !ukKeys.has(k)).sort();
    expect(missingInUkrainian, `Missing ${missingInUkrainian.length} key(s) in Ukrainian locale`).toEqual([]);
  });

  it('English locale has all Ukrainian logical keys', () => {
    const missingInEnglish = [...ukKeys].filter((k) => !enKeys.has(k)).sort();
    expect(missingInEnglish, `Missing ${missingInEnglish.length} key(s) in English locale`).toEqual([]);
  });

  it('both locales cover the same set of logical keys', () => {
    expect(enKeys.size).toBe(ukKeys.size);
  });
});
