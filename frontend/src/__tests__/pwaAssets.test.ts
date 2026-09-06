// Every icon the manifest, the service-worker precache list and index.html
// name must exist under frontend/public — a renamed asset otherwise surfaces
// only as a broken install icon on somebody's phone.
import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const PUBLIC = resolve(__dirname, '../../public');
const read = (rel: string) => readFileSync(resolve(__dirname, '../..', rel), 'utf8');

function assertOnDisk(paths: string[]) {
  const missing = paths.filter((p) => !existsSync(resolve(PUBLIC, p.replace(/^\//, ''))));
  expect(missing).toEqual([]);
}

describe('PWA assets', () => {
  it('manifest icons and shortcut icons exist', () => {
    const manifest = JSON.parse(read('public/manifest.json'));
    const icons = [
      ...manifest.icons.map((i: { src: string }) => i.src),
      ...manifest.shortcuts.flatMap((s: { icons: { src: string }[] }) => s.icons.map((i) => i.src)),
      ...manifest.screenshots.map((s: { src: string }) => s.src),
    ];
    assertOnDisk(icons);
  });

  it('manifest colours are the brand ink', () => {
    const manifest = JSON.parse(read('public/manifest.json'));
    expect(manifest.theme_color).toBe('#12150F');
    expect(manifest.background_color).toBe('#0B0D0A');
  });

  it('service-worker precache list and push icons exist', () => {
    const sw = read('public/sw.js');
    const paths = [...sw.matchAll(/'(\/(?:img|fonts)\/[^']+)'/g)].map((m) => m[1]);
    expect(paths.length).toBeGreaterThan(5);
    assertOnDisk(paths);
  });

  it('index.html icon links exist', () => {
    const html = read('index.html');
    const hrefs = [...html.matchAll(/<link[^>]+rel="(?:icon|apple-touch-icon)"[^>]+href="([^"]+)"/g)].map((m) => m[1]);
    expect(hrefs.length).toBeGreaterThanOrEqual(3);
    assertOnDisk(hrefs);
  });
});
