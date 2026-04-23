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

/**
 * Recursively walks the locale tree and yields [path, stringValue] for every
 * leaf whose value is a string. Non-string leaves (numbers, bool, arrays,
 * undefined from spread mistakes) are ignored by `typeof === 'string'` so the
 * non-string-leaves test below catches them explicitly.
 */
const getStringLeaves = (obj: object, prefix = ''): [string, string][] => {
  return Object.entries(obj).flatMap(([key, value]): [string, string][] => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === 'string') return [[path, value]];
    if (typeof value === 'object' && value !== null) return getStringLeaves(value, path);
    return [];
  });
};

/** All placeholders ({{name}} / {{ name }}) in an ICU-ish template string. */
const PLACEHOLDER_RE = /\{\{\s*([^{}]+?)\s*\}\}/g;
const placeholders = (s: string): Set<string> => {
  const found = new Set<string>();
  for (const m of s.matchAll(PLACEHOLDER_RE)) found.add(m[1]);
  return found;
};

const setsEqual = (a: Set<string>, b: Set<string>) => {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
};

// CLDR plural categories differ per language — EN has {one, other}, UK has {one, few, many, other}.
// Parity check must normalize these suffixes so `inQueue_other` (en) and `inQueue_many` (uk) count
// as the same logical key. Strip any trailing _one/_two/_few/_many/_other/_zero.
const PLURAL_SUFFIX = /_(one|two|few|many|other|zero)$/;
const normalizeKey = (k: string) => k.replace(PLURAL_SUFFIX, '');

/** Group string leaves by their logical (plural-normalised) key, unioning
 * placeholder sets across all CLDR plural variants. Different plural forms
 * within the same language legitimately share placeholder sets, so we only
 * care that the total set of placeholders for a logical key matches across
 * locales. */
const collectPlaceholdersByLogicalKey = (obj: object): Map<string, Set<string>> => {
  const byKey = new Map<string, Set<string>>();
  for (const [path, value] of getStringLeaves(obj)) {
    const key = normalizeKey(path);
    const set = byKey.get(key) ?? new Set<string>();
    for (const p of placeholders(value)) set.add(p);
    byKey.set(key, set);
  }
  return byKey;
};

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

  // Regression guards adapted from upstream §11I (check-i18n-parity.mjs) on
  // the minimal side: we stay vitest-native (no extra CLI-gate for 2 locales)
  // but catch the failures that actually bit us before — non-string leaves
  // from spread/method mistakes, and placeholder-set mismatches where one
  // locale drifts to {{n}} while the other uses {{count}} and i18next
  // silently interpolates nothing.

  it('all leaf values are strings (no spread/method/array mistakes)', () => {
    const nonString: { locale: string; path: string; kind: string }[] = [];
    const walk = (locale: string, obj: object, prefix = '') => {
      for (const [key, value] of Object.entries(obj)) {
        const path = prefix ? `${prefix}.${key}` : key;
        if (typeof value === 'string') continue;
        if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
          walk(locale, value, path);
          continue;
        }
        nonString.push({ locale, path, kind: Array.isArray(value) ? 'array' : typeof value });
      }
    };
    walk('en', en);
    walk('uk', uk);
    expect(nonString, `Non-string leaf values found: ${JSON.stringify(nonString, null, 2)}`).toEqual([]);
  });

  it('placeholders match per logical key across locales', () => {
    const enPh = collectPlaceholdersByLogicalKey(en);
    const ukPh = collectPlaceholdersByLogicalKey(uk);

    const mismatches: { key: string; en: string[]; uk: string[] }[] = [];
    // Iterate the intersection — keys present in both locales. (Missing-key
    // parity is covered by the earlier two tests.)
    for (const key of enPh.keys()) {
      if (!ukPh.has(key)) continue;
      const e = enPh.get(key)!;
      const u = ukPh.get(key)!;
      if (!setsEqual(e, u)) {
        mismatches.push({
          key,
          en: [...e].sort(),
          uk: [...u].sort(),
        });
      }
    }

    expect(
      mismatches,
      `Placeholder mismatch in ${mismatches.length} key(s): ${JSON.stringify(mismatches.slice(0, 10), null, 2)}`
    ).toEqual([]);
  });
});
