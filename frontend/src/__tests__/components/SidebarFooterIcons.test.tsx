import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * The footer icons are laid out by count, not by width.
 *
 * Asserted against the source: jsdom gives every element zero width, so a
 * layout assertion here would pass whatever the markup said. What can be
 * checked is that the sidebar still routes its icons through the helper that
 * knows the rules — the failure this guards is somebody re-introducing a hand
 * written row, which is how the footer came to have two fixed lines that could
 * not wrap at all.
 *
 * The rules themselves are tested in `utils/iconRows.test.ts`, where they are
 * arithmetic and can be tested properly.
 */
const LAYOUT = path.resolve(__dirname, '../../components/Layout.tsx');

describe('sidebar footer icons', () => {
  const source = fs.readFileSync(LAYOUT, 'utf8');

  it('lays the icons out through iconRows', () => {
    expect(source).toContain("import { iconRows } from '../utils/iconRows'");
    expect(source).toContain('iconRows(footerIcons).map');
  });

  it('asks whether the install button will draw before counting it', () => {
    // It renders null in most sessions. Counted blindly it leaves a hole in a
    // row, and the "never fewer than three on the last line" rule silently
    // becomes "sometimes two".
    expect(source).toContain('canInstall ? <InstallAppButton');
  });

  it('keeps every footer icon in one list', () => {
    // The old shape: two hand-written rows, neither able to wrap.
    expect(source).not.toContain('{/* Row 2: external links');
  });
});
