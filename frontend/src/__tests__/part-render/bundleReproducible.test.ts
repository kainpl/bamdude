import { describe, it, expect } from 'vitest';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const FRONTEND = process.cwd();
const ROOT = join(FRONTEND, '..');
const TRACKED = join(ROOT, 'backend', 'app', 'data', 'part_render', 'manifest.json');

describe('part-render bundle', () => {
  it('rebuilds from its sources to the tracked bytes, from any working directory', () => {
    // Run from the repository root, not frontend/: the bytes must not depend on where the build ran.
    // A source edit committed without a rebuild fails here too -- the tracked bundle is stale.
    const out = mkdtempSync(join(tmpdir(), 'part-render-'));
    try {
      execFileSync(process.execPath, [join(FRONTEND, 'scripts', 'build-part-render.mjs'), '--out', out], { cwd: ROOT, stdio: 'pipe' });
      const built = JSON.parse(readFileSync(join(out, 'manifest.json'), 'utf8'));
      const tracked = JSON.parse(readFileSync(TRACKED, 'utf8'));
      expect(built.sha256).toBe(tracked.sha256);
      expect(built.modules).toEqual(tracked.modules);
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  }, 60_000);
});
