/**
 * The Slice dialog's farm filters: which printer presets belong to an online
 * printer, and which filament presets are a loaded filament. The server says
 * what is online and loaded; these compare it with the preset listing by
 * identity — a model through the app's normalisation, a filament through the
 * ids of its family — never by a display name.
 */
import { describe, expect, it } from 'vitest';

import type { LoadedFilamentsResponse, OnlinePrinter } from '../../api/client';
import {
  filamentPresetIsLoaded,
  loadedFilamentsFor,
  printerPresetFitsFarmPrinter,
  printerPresetIsOnline,
} from '../../utils/sliceFarmFilter';

const printer = (over: Partial<OnlinePrinter>): OnlinePrinter => ({
  printer_id: 1,
  model: 'X1C',
  accepts: [],
  nozzle_diameters: [0.4],
  filaments: [],
  ...over,
});

const farm = (printers: OnlinePrinter[], families: LoadedFilamentsResponse['families'] = {}): LoadedFilamentsResponse => ({
  farm_printers: [],
  printers,
  families,
});

describe('printerPresetIsOnline', () => {
  it('matches a preset to an online printer of its model, however the model is spelled', () => {
    // the preset says "X1 Carbon", the printer row says "X1C"
    const online = farm([printer({ model: 'X1C' })]);
    expect(printerPresetIsOnline({ name: 'Bambu Lab X1 Carbon 0.4 nozzle' }, online, {})).toBe(true);
    expect(printerPresetIsOnline({ name: 'Bambu Lab A1 0.4 nozzle' }, online, {})).toBe(false);
  });

  it('needs the nozzle the preset states to be fitted', () => {
    const online = farm([printer({ model: 'X1C', nozzle_diameters: [0.4] })]);
    expect(printerPresetIsOnline({ name: 'Bambu Lab X1 Carbon 0.6 nozzle' }, online, {})).toBe(false);
    expect(printerPresetIsOnline({ name: 'Bambu Lab X1 Carbon 0.4 nozzle' }, online, {})).toBe(true);
  });

  it('does not rule a printer out on a nozzle nobody stated', () => {
    // the printer has not reported its nozzle yet
    expect(
      printerPresetIsOnline({ name: 'Bambu Lab X1 Carbon 0.6 nozzle' }, farm([printer({ nozzle_diameters: [] })]), {}),
    ).toBe(true);
    // the preset names a model and no nozzle
    expect(printerPresetIsOnline({ name: 'Bambu Lab X1 Carbon' }, farm([printer({})]), {})).toBe(true);
  });

  it('reads the model of a renamed preset from the preset it was saved from', () => {
    const online = farm([printer({ model: 'H2D' })]);
    const parents = { 'Garage machine': 'Bambu Lab H2D 0.4 nozzle' };
    expect(printerPresetIsOnline({ name: 'Garage machine' }, online, parents)).toBe(true);
    // without the parent the name says nothing, and nothing is guessed
    expect(printerPresetIsOnline({ name: 'Garage machine' }, online, {})).toBe(false);
  });

  it('counts a printer for the file models its config accepts', () => {
    const online = farm([printer({ model: 'X1E', accepts: ['X1C'] })]);
    expect(printerPresetIsOnline({ name: 'Bambu Lab X1 Carbon 0.4 nozzle' }, online, {})).toBe(true);
    // not the other way round: an X1C does not take X1E files unless its row says so
    expect(
      printerPresetIsOnline({ name: 'Bambu Lab X1E 0.4 nozzle' }, farm([printer({ model: 'X1C', accepts: [] })]), {}),
    ).toBe(false);
  });

  it('is false for everything when no printer is online', () => {
    expect(printerPresetIsOnline({ name: 'Bambu Lab X1 Carbon 0.4 nozzle' }, farm([]), {})).toBe(false);
  });
});

describe('loadedFilamentsFor', () => {
  const pla = { kind: 'ams' as const, material: 'PLA', family_id: 'GFA00', by_material: false };
  const petg = { kind: 'ams' as const, material: 'PETG', family_id: null, by_material: true };
  const online = farm([
    printer({ printer_id: 1, model: 'X1C', filaments: [pla] }),
    printer({ printer_id: 2, model: 'A1', filaments: [petg] }),
  ]);

  it('counts every online printer when no printer preset is chosen', () => {
    expect(loadedFilamentsFor(online, null, {})).toEqual([pla, petg]);
  });

  it('counts only the printers of the chosen preset’s model', () => {
    expect(loadedFilamentsFor(online, 'Bambu Lab A1 0.4 nozzle', {})).toEqual([petg]);
    expect(loadedFilamentsFor(online, 'My X1', { 'My X1': 'Bambu Lab X1 Carbon 0.4 nozzle' })).toEqual([pla]);
  });

  it('counts every online printer when the chosen preset’s model cannot be read', () => {
    expect(loadedFilamentsFor(online, 'Voron 2.4', {})).toEqual([pla, petg]);
  });
});

describe('filamentPresetIsLoaded', () => {
  const families = {
    GFA00: {
      name: 'Bambu PLA Basic',
      cloud: ['GFSA00', 'GFSA00_02', 'PFUSmine'],
      orca_cloud: ['orca-uuid'],
      local: ['17'],
      standard: ['Bambu PLA Basic @BBL X1C'],
    },
  };
  const basic = [{ kind: 'ams' as const, material: 'PLA', family_id: 'GFA00', by_material: false }];

  it('matches a family’s presets by the id each tier lists them under', () => {
    const is = (source: 'cloud' | 'orca_cloud' | 'local' | 'standard', id: string) =>
      filamentPresetIsLoaded({ source, id, filament_type: 'PLA' }, basic, families);
    expect(is('standard', 'Bambu PLA Basic @BBL X1C')).toBe(true);
    expect(is('cloud', 'PFUSmine')).toBe(true);
    expect(is('cloud', 'GFSA00_07')).toBe(true); // another version of the same stock preset
    expect(is('orca_cloud', 'orca-uuid')).toBe(true);
    expect(is('local', '17')).toBe(true);
  });

  it('does not take another preset of the same material for the family', () => {
    // same type, another filament: a loaded Bambu PLA Basic is not Generic PLA
    expect(
      filamentPresetIsLoaded({ source: 'standard', id: 'Generic PLA @BBL X1C', filament_type: 'PLA' }, basic, families),
    ).toBe(false);
    // an id of one tier is not an id of another
    expect(filamentPresetIsLoaded({ source: 'local', id: 'PFUSmine', filament_type: 'PLA' }, basic, families)).toBe(false);
  });

  it('matches a source that states only a material by material type', () => {
    const typed = [{ kind: 'ams' as const, material: 'ASA-GF', family_id: null, by_material: true }];
    expect(filamentPresetIsLoaded({ source: 'standard', id: 'Some ASA-GF', filament_type: 'ASA-GF' }, typed, {})).toBe(true);
    expect(filamentPresetIsLoaded({ source: 'standard', id: 'Generic ASA', filament_type: 'ASA' }, typed, {})).toBe(false);
    // a preset that states no type is not guessed into the list
    expect(filamentPresetIsLoaded({ source: 'cloud', id: 'PFUSx', filament_type: null }, typed, {})).toBe(false);
  });

  it('is false when nothing is loaded', () => {
    expect(filamentPresetIsLoaded({ source: 'standard', id: 'Generic PLA', filament_type: 'PLA' }, [], families)).toBe(false);
  });
});

// D4 (spec slicing-setup r5): the farm-models switch compares a printer preset
// with every non-archived farm printer the same way the online filter did.
describe('printerPresetFitsFarmPrinter', () => {
  const x1c = { model: 'X1C', accepts: [], nozzle_diameters: [0.4] };
  it('exact model and a fitted nozzle', () =>
    expect(printerPresetFitsFarmPrinter({ name: 'Bambu Lab X1 Carbon 0.4 nozzle' }, x1c, null)).toBe(true));
  it('a nozzle the printer does not carry', () =>
    expect(printerPresetFitsFarmPrinter({ name: 'Bambu Lab X1 Carbon 0.6 nozzle' }, x1c, null)).toBe(false));
  it('an unknown nozzle list excludes no size', () =>
    expect(
      printerPresetFitsFarmPrinter({ name: 'Bambu Lab X1 Carbon 0.6 nozzle' }, { ...x1c, nozzle_diameters: [] }, null),
    ).toBe(true));
  it('a model the printer accepts counts', () =>
    expect(
      printerPresetFitsFarmPrinter({ name: 'Bambu Lab X1 0.4 nozzle' }, { ...x1c, accepts: ['X1'] }, null),
    ).toBe(true));
  it('another model does not', () =>
    expect(printerPresetFitsFarmPrinter({ name: 'Bambu Lab A1 0.4 nozzle' }, x1c, null)).toBe(false));
});
