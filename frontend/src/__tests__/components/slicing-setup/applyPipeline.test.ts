import { describe, it, expect } from 'vitest';
import type { SlicerPipeline, UnifiedPresetsResponse } from '../../../api/client';
import { applyPipeline } from '../../../components/slicing-setup/applyPipeline';

// R11 (spec slicing-setup r5): a set's application, positional and bounded by
// the target's slots.
const empty = { printer: [], process: [], filament: [] };
const listing = {
  local: empty,
  orca_cloud: empty,
  cloud: empty,
  standard: {
    printer: [{ id: 'P', name: 'P', source: 'standard' }],
    process: [{ id: 'Q', name: 'Q', source: 'standard' }],
    filament: [
      { id: 'F1', name: 'F1', source: 'standard' },
      { id: 'F2', name: 'F2', source: 'standard' },
    ],
  },
} as unknown as UnifiedPresetsResponse;
const set = (filaments: string[], bed: string | null = null): SlicerPipeline =>
  ({
    id: 1,
    name: 'set',
    description: null,
    printer_preset: { source: 'standard', id: 'P' },
    process_preset: { source: 'standard', id: 'Q' },
    filament_presets: filaments.map((id) => ({ source: 'standard', id })),
    bed_type: bed,
    created_by: null,
    created_at: '',
    updated_at: '',
  }) as SlicerPipeline;
const all = { printer: true, process: true, plate: true, filaments: 'all' as const };

describe('applyPipeline (R11)', () => {
  it('unknown slot 0, valid slot 1, untouched tail', () => {
    const out = applyPipeline(set(['GONE', 'F2']), listing, all, 'orcaslicer', 3);
    expect(out.filaments).toEqual([{ unresolved: true }, { ref: { source: 'standard', id: 'F2' } }, null]);
    expect(out.printer).toEqual({ ref: { source: 'standard', id: 'P' } });
  });

  it('a set longer than the target', () => {
    expect(applyPipeline(set(['F1', 'F2', 'F1']), listing, all, null, 2).filaments).toHaveLength(2);
  });

  it('first fills slot 0 only', () => {
    const out = applyPipeline(set(['F1', 'F2']), listing, { ...all, filaments: 'first' }, null, 2);
    expect(out.filaments).toEqual([{ ref: { source: 'standard', id: 'F1' } }, null]);
  });

  it('fields a place does not fill stay null', () => {
    const out = applyPipeline(set(['F1'], 'Cool Plate'), listing, { printer: true, process: true, plate: false, filaments: 'none' }, null, 1);
    expect(out.plate).toBeNull();
    expect(out.filaments).toEqual([null]);
  });

  it('plate: unknown and orca-only refused with reasons; a valid plate comes back as a BedType', () => {
    expect(applyPipeline(set([], 'Glass'), listing, all, null, 0).plate).toEqual({ refused: 'unknown' });
    expect(applyPipeline(set([], 'Textured Cool Plate'), listing, all, 'bambu_studio', 0).plate).toEqual({ refused: 'orcaOnly' });
    expect(applyPipeline(set([], 'Cool Plate'), listing, all, 'bambu_studio', 0).plate).toEqual({ value: 'Cool Plate' });
    expect(applyPipeline(set([], null), listing, all, null, 0).plate).toBeNull();
  });
});
