import { describe, expect, it } from 'vitest';
import { builtinAssignments, slotInventoryMode } from '../../utils/slotInventoryMode';

describe('slotInventoryMode', () => {
  it('is the built-in inventory while Spoolman is off or its status is unknown', () => {
    expect(slotInventoryMode(undefined)).toBe('builtin');
    expect(slotInventoryMode({ enabled: false, connected: false })).toBe('builtin');
  });

  it('is Spoolman while it answers', () => {
    expect(slotInventoryMode({ enabled: true, connected: true })).toBe('spoolman');
  });

  // The card used to fall back to the built-in dialog here, and its Assign
  // wrote a built-in row in Spoolman mode.
  it('never falls back to the built-in inventory while Spoolman is on but unreachable', () => {
    expect(slotInventoryMode({ enabled: true, connected: false })).toBe('spoolman-unreachable');
  });
});

describe('builtinAssignments', () => {
  const rows = [{ spool_id: 7, printer_id: 1, ams_id: 0, tray_id: 0 }];

  it('hands the rows on in the built-in mode', () => {
    expect(builtinAssignments(false, rows)).toBe(rows);
  });

  // A stray built-in row shares its numeric id with an unrelated Spoolman
  // spool and put a slot badge on it.
  it('uses none of them while Spoolman is the inventory', () => {
    expect(builtinAssignments(true, rows)).toBeUndefined();
  });
});
