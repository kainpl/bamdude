/**
 * The app loads on iOS / Safari 16.0 (audit D12, upstream b3c67c69, #2971).
 *
 * Safari parses a regex lookbehind (`(?<=` / `(?<!`) only from 16.4, and a
 * regex LITERAL is validated when its module is compiled — so one of them
 * anywhere in the entry chunk leaves iOS 16.0–16.3 with a blank white page,
 * not a broken feature. esbuild does not rewrite regular expressions, so no
 * build target helps. Two reached our bundle: our own G-code highlighter, and
 * `remark-gfm`'s autolink-literal extension (via the folder README panel).
 *
 * `scripts/check-browser-baseline.mjs` fails the build if one reaches the
 * bundle again; this pins our own sources.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC = join(__dirname, '..', '..');

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === '__tests__' ? [] : sources(path);
    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

describe('the Safari 16.0 baseline', () => {
  it('no source carries a regex lookbehind', () => {
    const offenders = sources(SRC).filter((path) => /\(\?<[=!]/.test(readFileSync(path, 'utf8')));
    expect(offenders.map((path) => relative(SRC, path))).toEqual([]);
  });

  it('nothing imports remark-gfm — its autolink extension carries a lookbehind', () => {
    const offenders = sources(SRC).filter((path) => /from ['"]remark-gfm['"]/.test(readFileSync(path, 'utf8')));
    expect(offenders.map((path) => relative(SRC, path))).toEqual([]);
  });
});
