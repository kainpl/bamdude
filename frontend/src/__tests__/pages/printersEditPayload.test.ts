import { describe, it, expect } from 'vitest';
import { backupCompatibilityPatch } from '../../pages/printersEditPayload';

describe('backupCompatibilityPatch', () => {
  it('always sends an opaque RRGGBBFF colour', () => {
    expect(backupCompatibilityPatch({ normalize_color: true, canonical_color_rgba: '#1a1a1a', generic_base_material: false }).backup_compatibility.canonical_color_rgba).toBe('1A1A1AFF');
    expect(backupCompatibilityPatch({ normalize_color: false, canonical_color_rgba: '00000080', generic_base_material: true }).backup_compatibility.canonical_color_rgba).toBe('000000FF');
  });

  // PATCH /printers/{id} REPLACES the whole backup_compatibility namespace with
  // defaults for anything not sent, so the patch must always carry all three
  // fields — a switch left out of the payload reads back as off.
  it('always sends both switches, whatever they are set to', () => {
    expect(backupCompatibilityPatch({ normalize_color: true, canonical_color_rgba: 'FFFFFFFF', generic_base_material: true }).backup_compatibility)
      .toEqual({ normalize_color: true, canonical_color_rgba: 'FFFFFFFF', generic_base_material: true });
    expect(backupCompatibilityPatch({ normalize_color: false, canonical_color_rgba: 'FFFFFFFF', generic_base_material: false }).backup_compatibility)
      .toEqual({ normalize_color: false, canonical_color_rgba: 'FFFFFFFF', generic_base_material: false });
  });

  // This helper is the ONLY normaliser on the frontend side: the colour input
  // stores its raw `#rrggbb` and the server persists `RRGGBBFF`, so both shapes
  // reach it and it must answer the same way to each.
  it('takes the colour input\'s raw #rrggbb and the server\'s RRGGBBFF alike', () => {
    const raw = backupCompatibilityPatch({ normalize_color: true, canonical_color_rgba: '#1a2b3c', generic_base_material: false });
    const stored = backupCompatibilityPatch({ normalize_color: true, canonical_color_rgba: '1A2B3CFF', generic_base_material: false });
    expect(raw.backup_compatibility.canonical_color_rgba).toBe('1A2B3CFF');
    expect(stored.backup_compatibility.canonical_color_rgba).toBe('1A2B3CFF');
  });

  it('turns an empty colour into opaque black rather than sending nothing', () => {
    expect(backupCompatibilityPatch({ normalize_color: true, canonical_color_rgba: '', generic_base_material: false }).backup_compatibility.canonical_color_rgba).toBe('000000FF');
  });

  // The picker always hands over six digits, so a shorthand can only come from
  // a hand-edited row. Pinning the padding, not an expansion: `#abc` is NOT
  // `AABBCC` here, and the backend's lenient reader corrects anything it cannot
  // use anyway.
  it('does not expand a 3-char shorthand — it right-pads it like any short value', () => {
    expect(backupCompatibilityPatch({ normalize_color: true, canonical_color_rgba: '#abc', generic_base_material: false }).backup_compatibility.canonical_color_rgba).toBe('ABC000FF');
  });
});
