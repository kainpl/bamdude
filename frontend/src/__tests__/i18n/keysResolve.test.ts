import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

/**
 * Every key the code asks for must exist in both locales.
 *
 * This exists because a whole block went missing in plain sight: the locations
 * card and picker ask for `printers.locations.*`, and the block sat inside
 * `queueCard` instead — so every string in that card rendered as its own key,
 * for as long as the card existed, and nothing failed. i18next falls back to
 * printing the key, which looks like a label until somebody reads it.
 *
 * Keys built at runtime (`t(\`a.b.${x}\`)`) are out of reach here by
 * construction; only literals are checked.
 */
const KEY = /(?<![\w$.])t\(\s*'([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)'/g;

function sourceFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name === '__tests__' || entry.name === 'locales' ? [] : sourceFiles(full);
    }
    return /\.tsx?$/.test(entry.name) ? [full] : [];
  });
}

function usedKeys(): string[] {
  const found = new Set<string>();
  for (const file of sourceFiles(path.resolve(__dirname, '../../'))) {
    const text = fs.readFileSync(file, 'utf8');
    for (const match of text.matchAll(KEY)) found.add(match[1]);
  }
  return [...found].sort();
}

/**
 * A pluralised key has no bare form — `t('a.b')` with a `count` is served by
 * `a.b_one` / `a.b_other` (and `_few` / `_many` in Ukrainian). Looking only for
 * the literal spelling reports every plural in the codebase as missing, which
 * is how a dozen perfectly healthy keys ended up on the known-damage list
 * below. Suffixes are CLDR's category names, which is what i18next appends.
 */
function parentOf(bundle: unknown, key: string): Record<string, unknown> | null {
  const parts = key.split('.');
  parts.pop();
  let node: unknown = bundle;
  for (const part of parts) {
    if (typeof node !== 'object' || node === null) return null;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === 'object' && node !== null ? (node as Record<string, unknown>) : null;
}

function resolves(bundle: unknown, key: string): boolean {
  const parent = parentOf(bundle, key);
  if (!parent) return false;
  const leaf = key.split('.').pop()!;
  if (typeof parent[leaf] === 'string') return true;
  // A plural counts as present only via `_other`, the catch-all every language
  // has. Whether the OTHER forms are there is a separate question, asked below
  // — a Ukrainian key with only `_other` resolves here and still renders as
  // itself at count 2.
  return typeof parent[`${leaf}_other`] === 'string';
}

/** CLDR category names, which is what i18next appends. */
const PLURAL_SUFFIXES = ['zero', 'one', 'two', 'few', 'many', 'other'];

function pluralFormsFor(bundle: unknown, key: string): string[] {
  const parent = parentOf(bundle, key);
  if (!parent) return [];
  const leaf = key.split('.').pop()!;
  return PLURAL_SUFFIXES.filter((suffix) => typeof parent[`${leaf}_${suffix}`] === 'string');
}

/**
 * Keys that were already rendering as themselves when this guard was written.
 *
 * Not an exemption — a list of known damage, so NEW drift fails loudly while
 * the rest get fixed as their features are touched. Deleting an entry is the
 * whole point; adding one needs a reason.
 *
 * Twenty entries left in one go when ``resolves`` learned about plural
 * suffixes: they were never damage, only keys the old lookup could not spell.
 * Parking them here hid the real question — whether every locale carries every
 * form — behind a list that read like a backlog.
 */
const KNOWN_MISSING = new Set([
  'common.copied',
  'common.copy',
  'common.saved',
  'common.target',
  'inventory.archiveFailed',
  'inventory.archiveSpoolNotFound',
  'inventory.deleteFailed',
  'inventory.deleteSpoolNotFound',
  'inventory.kProfileSaveFailed',
  'inventory.restoreFailed',
  'inventory.restoreSpoolNotFound',
  'inventory.spoolWeightManagedBySpoolman',
  'inventory.spoolmanCatalogLoadFailed',
  'inventory.spoolmanSpools',
  'inventory.spoolmanUnreachable',
  'inventory.tagClearFailed',
  'inventory.unassignFailed',
  'inventory.unassignSuccess',
  'printModal.plateNumber',
  'printers.connection.ethernet',
  'printers.networkLabel',
  'queue.removeFromQueue',
  'settings.preheatOverride_',
  'settings.spoolmanAmsSyncError',
  'settings.spoolmanAmsSyncErrorNotConfigured',
  'settings.spoolmanAmsSyncErrorUnreachable',
  'settings.spoolmanAmsSyncSuccess',
  'settings.toast.saveFailed',
  'smartPlugs.addSmartPlug.placeholders.searchEnergySensors',
  'smartPlugs.addSmartPlug.placeholders.searchEntities',
  'smartPlugs.addSmartPlug.placeholders.searchPowerSensors',
  'virtualPrinter.howItWorks.proxyStep1',
  'virtualPrinter.howItWorks.proxyStep2',
  'virtualPrinter.howItWorks.proxyStep3',
  'virtualPrinter.howItWorks.proxyStep4',
  'virtualPrinter.howItWorks.proxyStep5',
  'virtualPrinter.howItWorks.step4',
  'virtualPrinter.howItWorks.step5',
  'virtualPrinter.howItWorks.step6',
  'virtualPrinter.howItWorks.titleProxy',
  'zigbee.removedForced',
  'zigbee.removedLeft',
]);

describe('translation keys', () => {
  const keys = usedKeys();

  it('the known-damage list is still accurate', () => {
    // An entry that has since been fixed should be deleted from the list rather
    // than left to hide the next regression at the same key.
    expect([...KNOWN_MISSING].filter((key) => resolves(en, key) && resolves(uk, key))).toEqual([]);
  });

  it('finds the keys at all', () => {
    // A guard on the guard: a regex that matched nothing would make every
    // assertion below vacuously true.
    expect(keys.length).toBeGreaterThan(500);
  });

  it('every key the code asks for exists in English', () => {
    expect(keys.filter((key) => !resolves(en, key) && !KNOWN_MISSING.has(key))).toEqual([]);
  });

  it('every key the code asks for exists in Ukrainian', () => {
    expect(keys.filter((key) => !resolves(uk, key) && !KNOWN_MISSING.has(key))).toEqual([]);
  });

  it('every pluralised key carries all four Ukrainian forms', () => {
    // Ukrainian needs _one/_few/_many/_other; English only _one/_other. i18next
    // does NOT fall back from a missing _few to _other — it falls back to the
    // FALLBACK LANGUAGE, so translating a plural by copying the English pair
    // puts English on screen mid-Ukrainian-UI at counts 2, 3 and 4. Measured:
    // `forecast.spoolCount` at count 3 rendered "3 spools" in Ukrainian.
    //
    // Which is why the check is on forms and not on "does something render" —
    // something always renders, and it looks like a translation until you read
    // it. The counts that break are the ones a print farm sees most.
    const REQUIRED = ['one', 'few', 'many', 'other'];
    const incomplete = keys
      .filter((key) => pluralFormsFor(en, key).length > 0)
      .map((key) => ({ key, missing: REQUIRED.filter((f) => !pluralFormsFor(uk, key).includes(f)) }))
      .filter(({ missing }) => missing.length > 0)
      .map(({ key, missing }) => `${key} (missing ${missing.join(', ')})`);

    expect(incomplete).toEqual([]);
  });

  it('finds pluralised keys at all', () => {
    // A guard on the guard above: if pluralFormsFor found nothing anywhere, its
    // assertion would hold over an empty list and prove nothing.
    expect(keys.filter((key) => pluralFormsFor(en, key).length > 0).length).toBeGreaterThan(10);
  });
});
