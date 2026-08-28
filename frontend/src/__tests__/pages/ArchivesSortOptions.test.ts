/**
 * The archive list can be sorted by what a print consumed.
 *
 * The server gained cost / energy / filament / duration ordering (adapted from
 * upstream #2636); this is the half that lets anyone reach it. Grid view sorts
 * through a dropdown, list view through clickable headers, and both write the
 * same `sortBy` so the two cannot disagree.
 *
 * ⚠️ Print Time carried a deliberate comment saying it had no header control
 * "because the sort runs on the server, which has no ordering for it". It does
 * now, so the header is live and the comment is gone — asserted here so the
 * pair cannot drift back apart.
 */

import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

const PAGE = readFileSync('src/pages/ArchivesPage.tsx', 'utf8');
const EN = readFileSync('src/i18n/locales/en.ts', 'utf8');
const UK = readFileSync('src/i18n/locales/uk.ts', 'utf8');

const MEASURED = ['cost', 'energy', 'filament', 'duration'] as const;

describe('archive sort options', () => {
  it('names every measured field the server understands', () => {
    for (const field of MEASURED) {
      expect(PAGE).toMatch(new RegExp(`SortField = [^;]*'${field}'`));
    }
  });

  it('opens each measured sort at the big end', () => {
    // The interesting print is the dear one, the long one, the heavy one.
    const block = PAGE.slice(PAGE.indexOf('FIRST_CLICK_DIR'), PAGE.indexOf('const sortByColumn'));
    for (const field of MEASURED) {
      expect(block).toContain(`${field}: 'desc'`);
    }
  });

  it('offers them in the grid-view dropdown', () => {
    for (const field of MEASURED) {
      expect(PAGE).toContain(`<option value="${field}">`);
    }
  });

  it('makes the Print Time header sort by real duration', () => {
    expect(PAGE).toContain("sortHeader('duration', t('archives.list.printTime'))");
    expect(PAGE).not.toContain('Print time carries no header control');
  });

  it('is labelled in both locales', () => {
    for (const locale of [EN, UK]) {
      for (const key of ['cost:', 'energy:', 'filament:']) {
        // inside archives.list, which also holds printer/date/size
        const list = locale.slice(locale.indexOf('    list: {', locale.indexOf('  archives: {')));
        expect(list.slice(0, 400)).toContain(key);
      }
    }
  });
});
