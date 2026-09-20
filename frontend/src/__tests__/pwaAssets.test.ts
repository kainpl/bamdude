// Every icon the manifest, the service-worker precache list and index.html
// name must exist under frontend/public — a renamed asset otherwise surfaces
// only as a broken install icon on somebody's phone.
import { describe, expect, it } from 'vitest';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
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
      ...(manifest.screenshots ?? []).map((s: { src: string }) => s.src),
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

  it('every /img/brand path a component or the README names exists', () => {
    // Walk the sources: the four marks the sidebar/login/overlay use never pass
    // through the manifest or the service worker, so the earlier tests miss them.
    const walk = (dir: string): string[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap((d) =>
        d.isDirectory() ? walk(resolve(dir, d.name)) : d.name.endsWith('.tsx') ? [resolve(dir, d.name)] : [],
      );
    const sources = walk(resolve(__dirname, '..')).filter((f) => !f.includes('__tests__'));
    const fromComponents = sources.flatMap((f) =>
      [...readFileSync(f, 'utf8').matchAll(/\/img\/brand\/[^'"`)\s]+/g)].map((m) => m[0]),
    );
    const readme = readFileSync(resolve(__dirname, '../../..', 'README.md'), 'utf8');
    const fromReadme = [...readme.matchAll(/static(\/img\/brand\/[^")\s]+)/g)].map((m) => m[1]);
    const all = [...new Set([...fromComponents, ...fromReadme])];
    expect(all.length).toBeGreaterThanOrEqual(5);
    assertOnDisk(all);
  });

  it('the service-worker cache name moves with the precache list', () => {
    // Adding a precached file without bumping CACHE_NAME leaves installed PWAs
    // on the old cache; the snapshot puts both in front of whoever edits either.
    const sw = read('public/sw.js');
    const cacheName = /const CACHE_NAME = '([^']+)'/.exec(sw)?.[1];
    const staticAssetsBlock = /const STATIC_ASSETS = \[([\s\S]*?)\];/.exec(sw)?.[1] ?? '';
    const precache = [...staticAssetsBlock.matchAll(/'(\/[^']*)'/g)].map((m) => m[1]);
    const pushIcons = [...sw.matchAll(/(?:icon|badge): '(\/[^']+)'/g)].map((m) => m[1]);
    expect([cacheName, ...precache, ...pushIcons]).toMatchInlineSnapshot(`
      [
        "bamdude-v4",
        "/",
        "/manifest.json",
        "/img/brand/favicon.ico",
        "/img/brand/mark-adaptive.svg",
        "/img/brand/icon-tile-192.png",
        "/img/brand/icon-tile-512.png",
        "/img/brand/apple-touch-icon.png",
        "/img/brand/lockup-compact-on-dark.svg",
        "/img/brand/lockup-compact-on-light.svg",
        "/fonts/inter-latin.woff2",
        "/fonts/inter-latin-ext.woff2",
        "/img/brand/icon-tile-192.png",
        "/img/brand/mark-on-dark-128.png",
      ]
    `);
  });
});
