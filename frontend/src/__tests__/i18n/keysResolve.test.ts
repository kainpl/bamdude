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

function resolves(bundle: unknown, key: string): boolean {
  let node: unknown = bundle;
  for (const part of key.split('.')) {
    if (typeof node !== 'object' || node === null) return false;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === 'string';
}

/**
 * Keys that were already rendering as themselves when this guard was written.
 *
 * Not an exemption — a list of known damage, so NEW drift fails loudly while
 * these 63 get fixed as their features are touched. Deleting an entry is the
 * whole point; adding one needs a reason.
 */
const KNOWN_MISSING = new Set([
  'common.copied',
  'common.copy',
  'common.saved',
  'common.target',
  'forecast.addNSpools',
  'forecast.alertCount',
  'forecast.moreSpools',
  'forecast.shoppingListItems',
  'forecast.spoolCount',
  'inventory.archiveFailed',
  'inventory.archiveSpoolNotFound',
  'inventory.bulk.selectedCount',
  'inventory.bulkEdit.willChange',
  'inventory.deleteFailed',
  'inventory.deleteSpoolNotFound',
  'inventory.kProfileSaveFailed',
  'inventory.restoreFailed',
  'inventory.restoreSpoolNotFound',
  'inventory.saveFailed',
  'inventory.spoolWeightManagedBySpoolman',
  'inventory.spoolmanCatalogLoadFailed',
  'inventory.spoolmanSpools',
  'inventory.spoolmanUnreachable',
  'inventory.tagClearFailed',
  'inventory.unassignFailed',
  'inventory.unassignSuccess',
  'modelViewer.plateCount',
  'printModal.plateNumber',
  'printers.connection.ethernet',
  'printers.networkLabel',
  'printers.queue.inQueue',
  'printers.summary.available',
  'printers.summary.offline',
  'printers.summary.printing',
  'printers.summary.problem',
  'queue.removeFromQueue',
  'settings.encryption.decryptionBrokenError',
  'settings.encryption.legacyRowsWarning',
  'settings.encryption.migrationErrorWarning',
  'settings.preheatOverride_',
  'settings.second',
  'settings.spoolmanAmsSyncError',
  'settings.spoolmanAmsSyncErrorNotConfigured',
  'settings.spoolmanAmsSyncErrorUnreachable',
  'settings.spoolmanAmsSyncSuccess',
  'settings.time',
  'settings.toast.saveFailed',
  'settings.twoFa.backupCodesRemaining',
  'settings.zigbee.forgetDone',
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
});
