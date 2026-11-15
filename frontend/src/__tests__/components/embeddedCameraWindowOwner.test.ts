/**
 * The floating camera window has ONE owner, and it is the signed-in app shell.
 *
 * A window that follows the user to every page holds a stream, the printer's
 * single camera connection and the chamber-light lease. It must therefore not
 * exist where nobody is signed in or where the page IS a camera surface: the
 * login page, the kiosk Cam Wall (`/camwall?token=…`), the stream overlay
 * (`/overlay/…`), the status monitor kiosk and the camera page (`/camera/…`).
 * All of those are routed outside `Layout`; this scan keeps the provider — and
 * the viewer it mounts — from being mounted anywhere else.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';

const SRC = join(__dirname, '..', '..');

function sources(dir: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (entry !== '__tests__' && entry !== 'node_modules') sources(path, found);
    } else if (/\.tsx?$/.test(entry)) {
      found.push(path);
    }
  }
  return found;
}

const mounting = (tag: string) =>
  sources(SRC)
    .filter((path) => readFileSync(path, 'utf8').includes(`<${tag}`))
    .map((path) => relative(SRC, path).replace(/\\/g, '/'));

describe('the floating camera window has one owner', () => {
  it('only Layout mounts the provider, and only the provider mounts the viewer', () => {
    expect(mounting('EmbeddedCameraProvider')).toEqual(['components/Layout.tsx']);
    expect(mounting('EmbeddedCameraViewer')).toEqual(['contexts/EmbeddedCameraContext.tsx']);
  });

  it('the login, camera, overlay and kiosk wall routes are declared outside Layout', () => {
    const app = readFileSync(join(SRC, 'App.tsx'), 'utf8');
    const shell = app.indexOf('<Layout />');
    expect(shell).toBeGreaterThan(-1);
    for (const path of ['"/login"', '"/camera/:printerId"', '"/camera/standalone/:cameraId"', '"/overlay/:printerId"', '"/camwall"']) {
      const at = app.indexOf(`path=${path}`);
      expect(at, path).toBeGreaterThan(-1);
      // Declared before the shell's route opens, i.e. not one of its children.
      expect(at, path).toBeLessThan(shell);
    }
    expect(app.match(/<Layout \/>/g)).toHaveLength(1);
  });
});
