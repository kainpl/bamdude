import { describe, it, expect } from 'vitest';
import { pickFilamentForSlot, pickDefault } from '../../utils/slicePresetPicker';
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
