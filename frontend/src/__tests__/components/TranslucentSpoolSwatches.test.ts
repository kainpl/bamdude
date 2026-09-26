/**
 * A clear or translucent spool is drawn as one wherever its swatch appears
 * (upstream 73912d4f, #2912).
 *
 * Each of these painted `#${hex.slice(0, 6)}` — cutting the alpha off — so a
 * clear tray (RRGGBB00) previewed as solid black. They now go through
 * `getSwatchStyle`, which knows the checkerboard and partial alpha; the swatch
 * logic itself is tested in `utils/colors.test.ts`.
 */
import { describe, expect, it } from 'vitest';

import assign from '../../components/AssignSpoolModal.tsx?raw';
import configure from '../../components/ConfigureAmsSlotModal.tsx?raw';
import link from '../../components/LinkSpoolModal.tsx?raw';
import form from '../../components/SpoolFormModal.tsx?raw';
import picker from '../../components/spool-form/SpoolmanFilamentPicker.tsx?raw';

describe('spool swatches keep their alpha', () => {
  it('draw through getSwatchStyle, never a six-character cut', () => {
    for (const source of [assign, configure, link, picker]) {
      expect(source).toContain('getSwatchStyle(');
      expect(source).not.toMatch(/backgroundColor: `#\$\{[^}]*slice\(0, 6\)\}`/);
    }
    expect(assign).not.toContain('backgroundColor: `#${trayInfo.color}`');
    expect(link).not.toContain('`#${spool.filament_color_hex}`');
    expect(configure).not.toContain("const displayColor = colorHex || slotInfo.trayColor?.slice(0, 6)");
  });

  it('prefills a translucent catalogue filament with its own alpha', () => {
    // Rejecting eight characters prefilled a clear filament as 808080FF.
    expect(form).toContain('/^[0-9A-F]{6}(?:[0-9A-F]{2})?$/.test(rawHex)');
    expect(form).toContain('const prefillRgba = colorHex.length === 8 ? colorHex : `${colorHex}FF`;');
  });
});
