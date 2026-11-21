/**
 * Every AMS slot names its colour with the slot's own material (upstream
 * 39010432, #2875) — the regular, HT and external slots, and the loaded
 * filaments the print dialog matches against.
 */
import { describe, it, expect } from 'vitest';
import printersPage from '../../pages/PrintersPage.tsx?raw';
import filamentMapping from '../../hooks/useFilamentMapping.ts?raw';

const flat = (s: string) => s.replace(/\s+/g, ' ');

describe('slot colour names ask with the material', () => {
  it('on the three slot kinds of the printer card', () => {
    const src = flat(printersPage);
    expect(src).toContain("getColorName((trayActual?.tray_color ?? tray.tray_color) || '', tray.tray_sub_brands, trayActual?.tray_info_idx ?? tray.tray_info_idx)");
    expect(src).toContain("getColorName((htTrayActual?.tray_color ?? tray.tray_color) || '', tray.tray_sub_brands, htTrayActual?.tray_info_idx ?? tray.tray_info_idx)");
    expect(src).toContain("getColorName(extTray.tray_color || '', extTray.tray_sub_brands, extTray.tray_info_idx)");
  });

  it('on the loaded filaments the print dialog matches', () => {
    const src = flat(filamentMapping);
    expect(src).toContain('getColorName(color, tray.tray_sub_brands, actual?.tray_info_idx ?? tray.tray_info_idx)');
    expect(src).toContain('getColorName(color, extTray.tray_sub_brands, extTray.tray_info_idx)');
  });

  it('never passes a synthesized Spoolman subtype as the assigned spool\'s colour', () => {
    const src = flat(printersPage);
    const synthesizedGuards = src.match(/color_name: spoolmanSpool\.color_name_is_synthesized \? null : \(spoolmanSpool\.color_name \?\? null\)/g) ?? [];
    expect(synthesizedGuards).toHaveLength(3);
  });
});

describe("the slot card's hover header paints the assigned spool (upstream #2967)", () => {
  it('passes the spool swatch on every assigned-spool site', () => {
    const src = flat(printersPage);
    for (const who of ['spoolmanSpool', 'assignment.spool']) {
      for (const field of ['rgba', 'extra_colors', 'effect_type']) {
        const hits = src.split(`${field}: ${who}.${field} ?? null`).length - 1;
        expect(hits, `${who}.${field}`).toBe(3);
      }
    }
  });
});
