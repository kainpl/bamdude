import { describe, expect, it } from 'vitest';

import type { UnifiedPreset, UnifiedPresetsResponse } from '../../api/client';
import { buildPrinterParents, printerPresetLineage } from '../../utils/printerPresetParents';

function listing(
  printers: Partial<Record<'local' | 'orca_cloud' | 'cloud' | 'standard', Partial<UnifiedPreset>[]>>,
): UnifiedPresetsResponse {
  const tier = (source: 'local' | 'orca_cloud' | 'cloud' | 'standard') => ({
    printer: (printers[source] ?? []).map((p) => ({ id: p.name as string, source, ...p })) as UnifiedPreset[],
    process: [],
    filament: [],
  });
  return {
    local: tier('local'),
    orca_cloud: tier('orca_cloud'),
    cloud: tier('cloud'),
    standard: tier('standard'),
    cloud_status: 'ok',
    orca_cloud_status: 'ok',
  } as UnifiedPresetsResponse;
}

describe('buildPrinterParents', () => {
  it('maps a printer preset to the preset it was saved from, across sources', () => {
    const parents = buildPrinterParents(
      listing({
        local: [{ name: 'Garage H2D', inherits: 'Bambu Lab H2D 0.4 nozzle' }],
        orca_cloud: [{ name: 'Skew X1C', inherits: 'Bambu Lab X1 Carbon 0.4 nozzle' }],
        cloud: [{ name: 'Cloud A1', inherits: 'Bambu Lab A1 0.4 nozzle' }],
        standard: [{ name: 'Bambu Lab H2D 0.4 nozzle' }],
      }),
    );
    expect(parents).toEqual({
      'Garage H2D': 'Bambu Lab H2D 0.4 nozzle',
      'Skew X1C': 'Bambu Lab X1 Carbon 0.4 nozzle',
      'Cloud A1': 'Bambu Lab A1 0.4 nozzle',
    });
  });

  it('keeps the first source that names a parent, in the dialog tier order', () => {
    const parents = buildPrinterParents(
      listing({
        local: [{ name: 'Mine', inherits: 'Bambu Lab H2D 0.4 nozzle' }],
        orca_cloud: [{ name: 'Mine', inherits: 'Bambu Lab A1 0.4 nozzle' }],
      }),
    );
    expect(parents.Mine).toBe('Bambu Lab H2D 0.4 nozzle');
  });

  it('skips a preset that names itself or nothing', () => {
    const parents = buildPrinterParents(
      listing({ local: [{ name: 'Self', inherits: 'Self' }, { name: 'None', inherits: null }, { name: 'Empty', inherits: '' }] }),
    );
    expect(parents).toEqual({});
  });

  it('is safe against names that collide with Object.prototype', () => {
    const parents = buildPrinterParents(listing({ local: [{ name: 'constructor', inherits: 'Bambu Lab A1 0.4 nozzle' }] }));
    expect(Object.hasOwn(parents, 'constructor')).toBe(true);
    expect(printerPresetLineage('toString', parents)).toEqual(['toString']);
  });
});

describe('printerPresetLineage', () => {
  it('lists the preset first, then each preset it was saved from', () => {
    expect(printerPresetLineage('C', { C: 'B', B: 'A' })).toEqual(['C', 'B', 'A']);
  });

  it('stops when presets name each other', () => {
    expect(printerPresetLineage('A', { A: 'B', B: 'A' })).toEqual(['A', 'B']);
  });

  it('is just the name when nothing is known', () => {
    expect(printerPresetLineage('A', {})).toEqual(['A']);
  });
});
