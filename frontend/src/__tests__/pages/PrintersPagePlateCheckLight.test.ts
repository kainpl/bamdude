/**
 * The plate check no longer switches the chamber light from the browser.
 *
 * It used to: switch on, wait 2.5 s, check, switch back — for this one
 * consumer, whatever the settings said, and nothing else (Telegram, the
 * finish photo, the wall) got the same courtesy. The light is the server's
 * business now, through the camera-light lease every capture goes through,
 * within the farm's and the printer's setting. A source read, like the
 * settings drift guard: the point is that nobody puts the shortcut back.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, '../../pages/PrintersPage.tsx'), 'utf8');

function bodyOf(name: string): string {
  const start = source.indexOf(`const ${name} = `);
  expect(start, `${name} moved or was renamed`).toBeGreaterThan(-1);
  const end = source.indexOf('\n  };', start);
  return source.slice(start, end);
}

describe('plate check and the chamber light', () => {
  it('opening the plate check does not switch the light from the browser', () => {
    expect(bodyOf('handleOpenPlateManagement')).not.toContain('setChamberLight');
    expect(bodyOf('handleOpenPlateManagement')).not.toContain('setTimeout');
  });

  it('closing the plate check does not switch the light either', () => {
    expect(bodyOf('closePlateCheckModal')).not.toContain('setChamberLight');
    expect(source).not.toContain('plateCheckLightWasOff');
  });
});
