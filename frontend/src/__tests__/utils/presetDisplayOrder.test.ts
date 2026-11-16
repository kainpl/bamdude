import { describe, expect, it } from 'vitest';

import type { UnifiedPreset } from '../../api/client';
import {
  PRESET_GROUP_ORDER,
  groupPresetsForDisplay,
  presetGroupOf,
  sortPresetsForDisplay,
} from '../../utils/presetDisplayOrder';

const named = (...names: string[]) => names.map((name) => ({ name }));
const sorted = (slot: 'printer' | 'process' | 'filament', ...names: string[]) =>
  sortPresetsForDisplay(named(...names), slot).map((p) => p.name);

describe('sortPresetsForDisplay', () => {
  it('orders layer heights by value, not by text', () => {
    expect(sorted('process', '0.20mm Standard @BBL X1C', '0.08mm Extra Fine @BBL X1C', '0.2mm Draft', '0.12mm Fine @BBL X1C')).toEqual([
      '0.08mm Extra Fine @BBL X1C',
      '0.12mm Fine @BBL X1C',
      '0.2mm Draft',
      '0.20mm Standard @BBL X1C',
    ]);
  });

  it('orders material grades by number', () => {
    expect(sorted('filament', 'Fiberon PA12-CF', 'Fiberon PA6-CF', 'Fiberon PA612-CF')).toEqual([
      'Fiberon PA6-CF',
      'Fiberon PA12-CF',
      'Fiberon PA612-CF',
    ]);
  });

  it('lists Bambu first, then Generic, then every other brand', () => {
    expect(sorted('filament', 'eSUN PLA+', 'Generic PLA', 'Bambu PLA Basic', 'Overture PLA', 'bambu ABS')).toEqual([
      'bambu ABS',
      'Bambu PLA Basic',
      'Generic PLA',
      'eSUN PLA+',
      'Overture PLA',
    ]);
  });

  it('lists support materials after the real filaments', () => {
    expect(sorted('filament', 'Bambu Support For PLA/PETG', 'Generic PVA', 'Generic PLA', 'Polymaker PolyLite PLA', 'Bambu PLA Basic')).toEqual([
      'Bambu PLA Basic',
      'Generic PLA',
      'Polymaker PolyLite PLA',
      'Bambu Support For PLA/PETG',
      'Generic PVA',
    ]);
  });

  it('does not treat "support" as a material outside the filament slot', () => {
    expect(sorted('process', 'Tree support draft', '0.20mm Standard')).toEqual(['0.20mm Standard', 'Tree support draft']);
  });

  it('ignores case and lists a GUI clone beside the preset it copies', () => {
    expect(sorted('printer', 'bambu Lab P1S 0.4 nozzle', '# Bambu Lab A1 0.4 nozzle', 'Bambu Lab H2D 0.4 nozzle')).toEqual([
      '# Bambu Lab A1 0.4 nozzle',
      'Bambu Lab H2D 0.4 nozzle',
      'bambu Lab P1S 0.4 nozzle',
    ]);
  });

  it('leaves the list it was given alone', () => {
    const input = named('b', 'a');
    sortPresetsForDisplay(input, 'process');
    expect(input.map((p) => p.name)).toEqual(['b', 'a']);
  });
});

describe('preset groups', () => {
  const preset = (source: UnifiedPreset['source'], id: string, name: string): UnifiedPreset => ({ source, id, name });

  it("puts the user's own sources before the stock ones", () => {
    expect([...PRESET_GROUP_ORDER]).toEqual(['local', 'orca_cloud', 'cloud_own', 'cloud', 'standard']);
  });

  it("tells the user's own Bambu Cloud presets from Bambu's stock ones", () => {
    expect(presetGroupOf(preset('cloud', 'PFUS9ac902733670a9', 'My PLA'))).toBe('cloud_own');
    expect(presetGroupOf(preset('cloud', 'GFSA00_02', 'Bambu PLA Basic @BBL X1C'))).toBe('cloud');
    expect(presetGroupOf(preset('local', '4', 'Imported'))).toBe('local');
    expect(presetGroupOf(preset('orca_cloud', 'a-uuid', 'Orca'))).toBe('orca_cloud');
    expect(presetGroupOf(preset('standard', 'Generic PLA', 'Generic PLA'))).toBe('standard');
  });

  it('keeps a stock preset of the same name once per source', () => {
    // A name is not an identity: the Standard copy is the running slicer's
    // bundle, the Bambu Cloud copy is Bambu Studio's content.
    const groups = groupPresetsForDisplay(
      [
        preset('standard', 'Bambu PLA Basic @BBL X1C', 'Bambu PLA Basic @BBL X1C'),
        preset('cloud', 'GFSA00_02', 'Bambu PLA Basic @BBL X1C'),
        preset('cloud', 'PFUSaaaa', 'My PLA'),
        preset('local', '7', 'Zebra PLA'),
        preset('local', '8', 'Alpha PLA'),
      ],
      'filament',
    );
    expect(groups.map((g) => [g.group, g.entries.map((p) => p.name)])).toEqual([
      ['local', ['Alpha PLA', 'Zebra PLA']],
      ['cloud_own', ['My PLA']],
      ['cloud', ['Bambu PLA Basic @BBL X1C']],
      ['standard', ['Bambu PLA Basic @BBL X1C']],
    ]);
  });
});
