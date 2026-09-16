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
});
