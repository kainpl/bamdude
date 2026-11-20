import { describe, it, expect } from 'vitest';
import { pickEmbeddedFilament, pickFilamentForSlot, pickDefault, pickPrinterFallback, pickProcessForField } from '../../utils/slicePresetPicker';
import { buildCompatibilityIndex } from '../../utils/slicerPrinterMatch';
import type { UnifiedPresetsResponse, UnifiedPresetsBySlot } from '../../api/client';

function makeUnified(overrides: Partial<UnifiedPresetsResponse> = {}): UnifiedPresetsResponse {
  const emptySlot = (): UnifiedPresetsBySlot => ({ printer: [], process: [], filament: [] });
  return {
    orca_cloud: emptySlot(),
    cloud: emptySlot(),
    local: emptySlot(),
    standard: emptySlot(),
    cloud_status: 'ok',
    orca_cloud_status: 'ok',
    ...overrides,
  };
}

// Pure-function tests for the filament slot picker. Pinned as their own suite so
// the printer-compat contract is visible without needing the modal mount.
describe('pickFilamentForSlot — printer-compat contract (#1851)', () => {
  // Index that recognises @BBL H2C / @BBL A1 tokens via the canonical
  // PRINTER_MODEL_MAP. Real production data comes through
  // ``api.getPrinterModels`` — the H2C / A1 fragments are the ones the
  // production registry ships.
  const index = buildCompatibilityIndex({
    'Bambu Lab A1': 'A1',
    'Bambu Lab H2C': 'H2C',
  });

  it('prefers a printer-compatible preset over a printer-mismatched one even with better colour match', () => {
    // The OP scenario for #1851: a Bambu Lab A1 is selected; the unused-slot
    // requirement carries the original H2C plate's PLA colour. With the legacy
    // soft-penalty scoring an H2C-bound preset whose colour matches exactly
    // could still rise above the A1-compatible PLA Basic whose colour doesn't,
    // and then the unused-slot substitution propagated the H2C-bound preset
    // across every unused slot — the CLI rejected with ``filament preset
    // Generic PLA @BBL H2C (slot 1) is not compatible with printer Bambu Lab A1
    // 0.4 nozzle``. The hard-skip contract makes sure a mismatched preset is
    // never chosen while any compatible alternative exists, irrespective of
    // metadata-score arithmetic.
    const presets = makeUnified({
      standard: {
        printer: [],
        process: [],
        filament: [
          {
            id: 'Generic PLA @BBL H2C',
            name: 'Generic PLA @BBL H2C',
            source: 'standard',
            filament_type: 'PLA',
            filament_colour: '#FF0000',
          },
          {
            id: 'Bambu PLA Basic @BBL A1',
            name: 'Bambu PLA Basic @BBL A1',
            source: 'standard',
            filament_type: 'PLA',
            filament_colour: '#FFFFFF',
          },
        ],
      },
    });
    const pick = pickFilamentForSlot(
      presets,
      { type: 'PLA', color: '#FF0000' },
      'Bambu Lab A1 0.4 nozzle',
      index,
    );
    expect(pick).toEqual({ source: 'standard', id: 'Bambu PLA Basic @BBL A1' });
  });

  it('falls back to a mismatched preset when no compatible alternative exists', () => {
    // Graceful degrade: when every available preset is printer-mismatched,
    // returning ``null`` would block the slice entirely. The picker keeps its
    // old behaviour of returning the best-scoring mismatch so the user sees a
    // populated dropdown they can correct, not an empty one.
    const presets = makeUnified({
      standard: {
        printer: [],
        process: [],
        filament: [
          {
            id: 'Generic PLA @BBL H2C',
            name: 'Generic PLA @BBL H2C',
            source: 'standard',
            filament_type: 'PLA',
            filament_colour: '#FF0000',
          },
        ],
      },
    });
    const pick = pickFilamentForSlot(
      presets,
      { type: 'PLA', color: '#FF0000' },
      'Bambu Lab A1 0.4 nozzle',
      index,
    );
    expect(pick).toEqual({ source: 'standard', id: 'Generic PLA @BBL H2C' });
  });

  it('treats a no-printer-context call as no-mismatch (every preset eligible)', () => {
    // ``printerName === null`` happens transiently on first render before the
    // printer pre-pick effect has run. ``presetCompatibility`` returns
    // ``unknown`` for every preset in that case, so the picker should just pick
    // by metadata score with no compatibility filter active.
    const presets = makeUnified({
      standard: {
        printer: [],
        process: [],
        filament: [
          {
            id: 'Generic PLA @BBL H2C',
            name: 'Generic PLA @BBL H2C',
            source: 'standard',
            filament_type: 'PLA',
            filament_colour: '#FF0000',
          },
        ],
      },
    });
    const pick = pickFilamentForSlot(
      presets,
      { type: 'PLA', color: '#FF0000' },
      null,
      index,
    );
    expect(pick).toEqual({ source: 'standard', id: 'Generic PLA @BBL H2C' });
  });

  it('returns null when there are no filament presets at all', () => {
    // Empty registry — both buckets stay empty and the pickDefault fallback
    // also has nothing to return.
    expect(pickFilamentForSlot(makeUnified(), { type: 'PLA', color: '#FF0000' }, 'Bambu Lab A1 0.4 nozzle', index)).toBe(
      pickDefault(makeUnified(), 'filament'),
    );
  });
});

// The filament preset a project names for a slot ("Assemble project" writes the
// one the operator chose) is that slot's default — when it is in the listing,
// fits the selected printer and is of the plate's material. Otherwise the
// ordinary pick answers.
describe('pickEmbeddedFilament', () => {
  const index = buildCompatibilityIndex({ 'Bambu Lab A1': 'A1', 'Bambu Lab H2C': 'H2C' });
  const A1 = 'Bambu Lab A1 0.4 nozzle';
  const filament = (name: string, filament_type: string) => ({ id: name, name, source: 'standard' as const, filament_type });
  const presets = makeUnified({
    standard: {
      printer: [],
      process: [],
      filament: [
        filament('Generic PLA @BBL A1', 'PLA'),
        filament('Bambu PLA Matte @BBL A1', 'PLA'),
        filament('Bambu PLA Matte @BBL H2C', 'PLA'),
        filament('Bambu PETG HF @BBL A1', 'PETG'),
      ],
    },
  });

  it('takes the named preset over the one the scoring would pick', () => {
    expect(pickFilamentForSlot(presets, { type: 'PLA', color: '' }, A1, index)).toEqual({
      source: 'standard',
      id: 'Generic PLA @BBL A1',
    });
    expect(pickEmbeddedFilament(presets, 'Bambu PLA Matte @BBL A1', { type: 'PLA' }, A1, index)).toEqual({
      source: 'standard',
      id: 'Bambu PLA Matte @BBL A1',
    });
  });

  it('answers nothing for a name that is not in the listing, or no name', () => {
    expect(pickEmbeddedFilament(presets, 'Some PLA nobody has', { type: 'PLA' }, A1, index)).toBeNull();
    expect(pickEmbeddedFilament(presets, null, { type: 'PLA' }, A1, index)).toBeNull();
    expect(pickEmbeddedFilament(presets, undefined, { type: 'PLA' }, A1, index)).toBeNull();
  });

  it('answers nothing for a preset made for another printer', () => {
    expect(pickEmbeddedFilament(presets, 'Bambu PLA Matte @BBL H2C', { type: 'PLA' }, A1, index)).toBeNull();
  });

  it('answers nothing for a preset of another material than the plate asks for', () => {
    expect(pickEmbeddedFilament(presets, 'Bambu PETG HF @BBL A1', { type: 'PLA' }, A1, index)).toBeNull();
    // a plate that states no material has nothing to contradict the name with
    expect(pickEmbeddedFilament(presets, 'Bambu PETG HF @BBL A1', { type: '' }, A1, index)).toEqual({
      source: 'standard',
      id: 'Bambu PETG HF @BBL A1',
    });
  });
});

// A name is not an identity: Bambu Cloud lists the stock presets under the
// names the bundle uses, and the two are different content (decision 5). The
// project names its filament by NAME only, so a name that several sources hold
// is resolved by something else — the preset the project was built from, when
// that is known; else the source of the chosen printer preset; else not at all.
describe('pickEmbeddedFilament — a name that several sources hold', () => {
  const index = buildCompatibilityIndex({ 'Bambu Lab A1': 'A1' });
  const A1 = 'Bambu Lab A1 0.4 nozzle';
  const NAME = 'Bambu PLA Basic @BBL A1';
  const presets = makeUnified({
    cloud: {
      printer: [],
      process: [],
      filament: [{ id: 'GFSA00', name: NAME, source: 'cloud', filament_type: 'PLA' }],
    },
    standard: {
      printer: [],
      process: [],
      filament: [
        { id: NAME, name: NAME, source: 'standard', filament_type: 'PLA' },
        { id: 'Only standard', name: 'Only standard', source: 'standard', filament_type: 'PLA' },
      ],
    },
  });
  const pla = { type: 'PLA' };

  it('takes the preset the project was built from, whatever tier comes first', () => {
    expect(
      pickEmbeddedFilament(presets, NAME, pla, A1, index, { built: { source: 'standard', id: NAME } }),
    ).toEqual({ source: 'standard', id: NAME });
    expect(
      pickEmbeddedFilament(presets, NAME, pla, A1, index, { built: { source: 'cloud', id: 'GFSA00' } }),
    ).toEqual({ source: 'cloud', id: 'GFSA00' });
  });

  it('ignores a built-from preset that is not the one the file names any more', () => {
    // The file was edited since: its slot names another preset now.
    expect(
      pickEmbeddedFilament(presets, 'Only standard', pla, A1, index, { built: { source: 'cloud', id: 'GFSA00' } }),
    ).toEqual({ source: 'standard', id: 'Only standard' });
  });

  it('takes the namesake in the source of the chosen printer preset', () => {
    expect(pickEmbeddedFilament(presets, NAME, pla, A1, index, { printerSource: 'standard' })).toEqual({
      source: 'standard',
      id: NAME,
    });
    expect(pickEmbeddedFilament(presets, NAME, pla, A1, index, { printerSource: 'cloud' })).toEqual({
      source: 'cloud',
      id: 'GFSA00',
    });
  });

  it('answers nothing when neither says which namesake is meant', () => {
    expect(pickEmbeddedFilament(presets, NAME, pla, A1, index)).toBeNull();
    expect(pickEmbeddedFilament(presets, NAME, pla, A1, index, { printerSource: 'local' })).toBeNull();
  });

  it('still takes a name only one source holds', () => {
    expect(pickEmbeddedFilament(presets, 'Only standard', pla, A1, index, { printerSource: 'cloud' })).toEqual({
      source: 'standard',
      id: 'Only standard',
    });
  });
});

// R5 (spec slicing-setup r5): the printer's last automatic step works on the
// whole catalogue and never picks a model card; a process pick over a field's
// offer never takes a mismatch from it.
describe('pickPrinterFallback (R5)', () => {
  const printers = (names: string[]) =>
    makeUnified({
      standard: {
        printer: names.map((name) => ({ id: name, name, source: 'standard' as const })),
        process: [],
        filament: [],
      },
    });

  it('prefers 0.4 machine presets, never a model card', () => {
    expect(
      pickPrinterFallback(printers(['Bambu Lab A1', 'Bambu Lab A1 0.6 nozzle', 'Bambu Lab A1 0.4 nozzle']), {}),
    ).toEqual({ source: 'standard', id: 'Bambu Lab A1 0.4 nozzle' });
  });
  it('takes any machine preset when none is 0.4', () =>
    expect(pickPrinterFallback(printers(['Bambu Lab A1', 'Bambu Lab A1 0.6 nozzle']), {})).toEqual({
      source: 'standard',
      id: 'Bambu Lab A1 0.6 nozzle',
    }));
  it('picks nothing when only model cards exist', () =>
    expect(pickPrinterFallback(printers(['Bambu Lab A1']), {})).toBeNull());
});

describe('pickProcessForField (R5)', () => {
  const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';
  const proc = (source: 'local' | 'standard', id: string, fitsFor: string) => ({
    id,
    name: id,
    source,
    compatible_printers: [fitsFor],
  });
  const index = buildCompatibilityIndex({});
  const by = makeUnified({
    local: { printer: [], process: [proc('local', 'mine-mismatch', 'Bambu Lab A1 0.4 nozzle')], filament: [] },
    standard: { printer: [], process: [proc('standard', 'std-fit', X1C)], filament: [] },
  });
  const onlyMine = (p: { source: string }) => p.source === 'local';

  it('never takes a mismatch from the offer', () =>
    expect(pickProcessForField(by, onlyMine, X1C, index)).toEqual({ source: 'standard', id: 'std-fit' }));
  it('a usable file process wins whatever the offer', () => {
    const withFile = makeUnified({
      local: { printer: [], process: [proc('local', 'mine-fit', X1C)], filament: [] },
      standard: { printer: [], process: [proc('standard', 'std-fit', X1C)], filament: [] },
    });
    expect(
      pickProcessForField(withFile, onlyMine, X1C, index, { file: { ref: { source: 'standard', id: 'std-fit' } } }),
    ).toEqual({ source: 'standard', id: 'std-fit' });
  });
  it('a record ref that does not fit gives way to the process the file names', () => {
    // Slice today: the assembly record first, then the 3MF's process name.
    const both = makeUnified({
      local: { printer: [], process: [proc('local', 'mine-mismatch', 'Bambu Lab A1 0.4 nozzle')], filament: [] },
      standard: { printer: [], process: [proc('standard', 'std-fit', X1C), proc('standard', 'std-2', X1C)], filament: [] },
    });
    const file = { ref: { source: 'local' as const, id: 'mine-mismatch' }, name: 'std-2' };
    expect(pickProcessForField(both, () => true, X1C, index, { file })).toEqual({ source: 'standard', id: 'std-2' });
  });
  it('fittingOnly: a catalogue of mismatches only picks nothing', () => {
    const mismatches = makeUnified({
      standard: { printer: [], process: [proc('standard', 'a1-only', 'Bambu Lab A1 0.4 nozzle')], filament: [] },
    });
    expect(pickProcessForField(mismatches, () => true, X1C, index, { fittingOnly: true })).toBeNull();
    expect(pickProcessForField(mismatches, () => true, X1C, index)).toEqual({ source: 'standard', id: 'a1-only' });
  });
});
