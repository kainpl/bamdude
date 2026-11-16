/**
 * `scripts/check-browser-baseline.mjs` reads everything the bundle can emit.
 *
 * It used to read only the `.js` files at the top of `static/assets`. A worker
 * a dependency ships as `.mjs`, or a chunk Vite puts in a sub-directory, would
 * have reached the browser unchecked — and a lookbehind or a class static
 * block in it is a parse error on Safari 16.0–16.3. Each case below runs the
 * real script over a fixture directory.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

const SCRIPT = join(process.cwd(), 'scripts', 'check-browser-baseline.mjs');
// Assembled, never written out: the source scan next door reads this tree as text.
const LOOKBEHIND = 'const r = /(?<' + '=a)b/;\n';
const STATIC_BLOCK = 'class A { static' + ' { A.x = 1; } }\n';
const CLEAN = 'export const x = 1;\n';

const made: string[] = [];

function check(files: Record<string, string>) {
  const dir = mkdtempSync(join(tmpdir(), 'baseline-'));
  made.push(dir);
  for (const [name, text] of Object.entries(files)) {
    mkdirSync(dirname(join(dir, name)), { recursive: true });
    writeFileSync(join(dir, name), text);
  }
  const run = spawnSync(process.execPath, [SCRIPT, dir], { encoding: 'utf8' });
  return { status: run.status, out: run.stdout + run.stderr };
}

afterEach(() => {
  for (const dir of made.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('the browser baseline check', () => {
  it('fails on a lookbehind in a top-level .js chunk', () => {
    const { status, out } = check({ 'index-abc.js': LOOKBEHIND });
    expect(status).toBe(1);
    expect(out).toContain('index-abc.js');
    expect(out).toContain('regex lookbehind');
  });

  it('fails on a lookbehind in a nested .mjs', () => {
    const { status, out } = check({ 'index-abc.js': CLEAN, 'pdfjs/pdf.worker.min.mjs': LOOKBEHIND });
    expect(status).toBe(1);
    expect(out).toContain('pdf.worker.min.mjs');
  });

  it('fails on a class static block in a top-level .mjs worker', () => {
    const { status, out } = check({ 'index-abc.js': CLEAN, 'worker-abc.mjs': STATIC_BLOCK });
    expect(status).toBe(1);
    expect(out).toContain('worker-abc.mjs');
    expect(out).toContain('class static initialisation block');
  });

  it('fails on a nested .cjs and a deeper .js', () => {
    expect(check({ 'index-abc.js': CLEAN, 'a/legacy.cjs': LOOKBEHIND }).status).toBe(1);
    expect(check({ 'index-abc.js': CLEAN, 'a/b/chunk.js': STATIC_BLOCK }).status).toBe(1);
  });

  it('passes a clean tree and counts every script in it', () => {
    const { status, out } = check({
      'index-abc.js': CLEAN,
      'worker-abc.mjs': CLEAN,
      'a/b/chunk.cjs': CLEAN,
      // not a script: a stylesheet or a map may quote anything
      'index-abc.css': LOOKBEHIND,
      'index-abc.js.map': LOOKBEHIND,
    });
    expect(out).toContain('3 bundle(s)');
    expect(status).toBe(0);
  });

  it('fails when the directory holds no script at all', () => {
    expect(check({ 'a/readme.txt': CLEAN }).status).toBe(1);
  });
});
