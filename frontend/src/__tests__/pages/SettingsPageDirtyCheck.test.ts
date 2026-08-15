/**
 * Every setting the page SENDS must also be one the page can NOTICE changing.
 *
 * SettingsPage decides whether to save by comparing baseline against local
 * field by field, in a hand-written list. The save payload below it is a
 * second hand-written list. Nothing connects the two, so a setting added to
 * one and not the other is accepted by the UI, moves on screen, and is never
 * written — with no error anywhere, because from the page's point of view
 * nothing changed.
 *
 * That is exactly how `delete_timelapse_after_attach` shipped broken: schema,
 * payload, toggle and translations all correct, and the toggle silently did
 * nothing. A rendering test would not have caught it — the control renders and
 * flips perfectly well.
 *
 * So this reads the source rather than the DOM. It is a drift guard, not a
 * behaviour test: the point is that the two lists cannot fall out of step
 * again, whichever of them the next person forgets.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, '../../pages/SettingsPage.tsx'), 'utf8');

/** Keys of the object literal handed to the settings mutation. */
function savedKeys(): string[] {
  const start = source.indexOf('const settingsToSave: AppSettingsUpdate = {');
  expect(start, 'the save payload literal moved or was renamed').toBeGreaterThan(-1);

  // Walk braces so a nested object inside the payload cannot end it early.
  let depth = 0;
  let end = start;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }

  const body = source.slice(start, end);
  return [...body.matchAll(/^\s{8}([a-z0-9_]+):/gm)].map((m) => m[1]);
}

/** The text of the `hasChanges` comparison. */
function dirtyCheckBody(): string {
  const start = source.indexOf('const hasChanges =');
  expect(start, 'the dirty check moved or was renamed').toBeGreaterThan(-1);
  const end = source.indexOf(';', start);
  return source.slice(start, end);
}

describe('SettingsPage save/dirty-check drift', () => {
  it('sends a non-trivial set of settings', () => {
    // Guards the guard: if the payload parser silently matched nothing, every
    // assertion below would pass while checking absolutely nothing.
    expect(savedKeys().length).toBeGreaterThan(20);
  });

  it('can notice a change in every setting it saves', () => {
    const body = dirtyCheckBody();
    const unnoticed = savedKeys().filter((key) => !body.includes(key));

    expect(
      unnoticed,
      `these settings are sent on save but absent from the hasChanges comparison, ` +
        `so changing them saves nothing: ${unnoticed.join(', ')}`
    ).toEqual([]);
  });

  it('still watches the setting that first exposed this', () => {
    expect(dirtyCheckBody()).toContain('delete_timelapse_after_attach');
  });
});
