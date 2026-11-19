import { describe, it, expect } from 'vitest';
import type { PresetSource, UnifiedPreset, UnifiedPresetsResponse } from '../../../api/client';
import { buildPresetField, type SoftFilter } from '../../../components/slicing-setup/presetSets';
import { EMPTY_COMPATIBILITY_INDEX } from '../../../utils/slicerPrinterMatch';
import { matchesSourceFilter } from '../../../utils/presetPickerUtils';

// R2 / R3 (spec slicing-setup r5): the sets of one preset field.
const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';
const A1 = 'Bambu Lab A1 0.4 nozzle';

function p(source: PresetSource, id: string, fitsFor: string | null): UnifiedPreset {
  return { source, id, name: id, compatible_printers: fitsFor ? [fitsFor] : null } as UnifiedPreset;
}

function listing(process: UnifiedPreset[]): UnifiedPresetsResponse {
  const empty = { printer: [], process: [], filament: [] };
  const by = (s: PresetSource) => ({ ...empty, process: process.filter((x) => x.source === s) });
  return {
    local: by('local'),
    orca_cloud: by('orca_cloud'),
    cloud: by('cloud'),
    standard: by('standard'),
  } as unknown as UnifiedPresetsResponse;
}

const mine = (keep = true): SoftFilter => ({
  id: 'source',
  keep: (x) => matchesSourceFilter(x, 'mine'),
  pickKeep: keep ? (x) => matchesSourceFilter(x, 'mine') : null,
});
const fit = (mode: 'group' | 'hide' = 'group') => ({ printerName: X1C, compatIndex: EMPTY_COMPATIBILITY_INDEX, mode });
const ids = (field: ReturnType<typeof buildPresetField>) => field.sections.flatMap((s) => s.entries.map((e) => e.id));

describe('buildPresetField (R2)', () => {
  it('pick offer has no relaxation, no pinned ref, no other printers', () => {
    const data = listing([p('local', 'mine-mismatch', A1), p('standard', 'std-fit', X1C)]);
    const field = buildPresetField({
      listing: data,
      slot: 'process',
      fit: fit(),
      filters: [mine()],
      pinned: { source: 'standard', id: 'std-fit' },
    });
    const all = [...data.local.process, ...data.standard.process];
    expect(all.filter(field.pickKeep).map((x) => x.id)).toEqual([]);
  });

  it('relaxes Mine when it hides every fitting entry', () => {
    const field = buildPresetField({
      listing: listing([p('local', 'mine-mismatch', A1), p('standard', 'std-fit', X1C)]),
      slot: 'process',
      fit: fit(),
      filters: [mine()],
      pinned: null,
    });
    expect(field.relaxed).toEqual(['source']);
    expect(ids(field)).toContain('std-fit');
  });

  it('relaxes loaded before source', () => {
    const loaded: SoftFilter = { id: 'loaded', keep: (x) => x.id === 'mine-mismatch', pickKeep: null };
    const field = buildPresetField({
      listing: listing([p('local', 'mine-mismatch', A1), p('standard', 'std-fit', X1C)]),
      slot: 'process',
      fit: fit(),
      filters: [mine(), loaded],
      pinned: null,
    });
    expect(field.relaxed).toEqual(['loaded', 'source']);
  });

  it('Show all needs exactly one filter off', () => {
    const loaded: SoftFilter = { id: 'loaded', keep: () => false, pickKeep: null };
    const both = buildPresetField({
      listing: listing([p('local', 'mine-fit', X1C), p('standard', 'std-fit', X1C)]),
      slot: 'process',
      fit: fit(),
      filters: [mine(), loaded],
      pinned: null,
    });
    expect(both.relaxed).toEqual(['loaded']);
    const withoutLoaded = buildPresetField({
      listing: listing([p('local', 'mine-fit', X1C), p('standard', 'std-fit', X1C)]),
      slot: 'process',
      fit: fit(),
      filters: [mine()],
      pinned: null,
    });
    expect(withoutLoaded.relaxed).toEqual([]);
    expect(ids(withoutLoaded)).toEqual(['mine-fit']);
  });

  it('stale farm: shown narrowed, picks unsteered', () => {
    const stale: SoftFilter = { id: 'loaded', keep: (x) => x.id === 'A', pickKeep: null };
    const data = listing([p('standard', 'A', X1C), p('standard', 'B', X1C)]);
    const field = buildPresetField({ listing: data, slot: 'process', fit: fit(), filters: [stale], pinned: null });
    expect(ids(field)).toEqual(['A']);
    expect(field.pickKeep(data.standard.process[1])).toBe(true);
  });

  it('nothing fits: the bound in source groups, filtered, noFit', () => {
    const field = buildPresetField({
      listing: listing([p('local', 'mine-mismatch', A1), p('standard', 'std-mismatch', A1)]),
      slot: 'process',
      fit: fit(),
      filters: [mine()],
      pinned: null,
    });
    expect(field.noFit).toBe(true);
    expect(ids(field)).toEqual(['mine-mismatch']);
    expect(field.others).toEqual([]);
  });

  it('hide mode leaves other printers out; group mode keeps them trailing', () => {
    const data = listing([p('standard', 'fit', X1C), p('standard', 'other', A1)]);
    const group = buildPresetField({ listing: data, slot: 'process', fit: fit('group'), filters: [], pinned: null });
    expect(group.others.map((x) => x.id)).toEqual(['other']);
    const hide = buildPresetField({ listing: data, slot: 'process', fit: fit('hide'), filters: [], pinned: null });
    expect(hide.others).toEqual([]);
    expect(ids(hide)).toEqual(['fit']);
  });

  it('pinned joined after, marked, and listed once', () => {
    const data = listing([p('standard', 'fit', X1C), p('standard', 'other', A1)]);
    const field = buildPresetField({
      listing: data,
      slot: 'process',
      fit: fit('hide'),
      filters: [],
      pinned: { source: 'standard', id: 'other' },
    });
    expect(field.pinnedOutside).toBe('otherPrinter');
    const listed = [...ids(field), ...field.others.map((x) => x.id)];
    expect(listed.filter((x) => x === 'other')).toHaveLength(1);
  });

  it('a filtered pinned ref is joined and marked filtered', () => {
    const field = buildPresetField({
      listing: listing([p('local', 'mine-fit', X1C), p('standard', 'std-fit', X1C)]),
      slot: 'process',
      fit: fit(),
      filters: [mine()],
      pinned: { source: 'standard', id: 'std-fit' },
    });
    expect(field.pinnedOutside).toBe('filtered');
    expect(ids(field)).toEqual(['mine-fit', 'std-fit']);
  });

  it('empty bound with disable', () => {
    const field = buildPresetField({
      listing: listing([p('standard', 'a', X1C)]),
      slot: 'process',
      bound: { keep: () => false, whenEmpty: 'disable' },
      filters: [],
      pinned: null,
    });
    expect(field.boundEmpty).toBe(true);
    expect(field.boundFallback).toBe(false);
    expect(ids(field)).toEqual([]);
  });

  it('empty bound with showAll shows the whole catalogue, picks nothing', () => {
    const data = listing([p('standard', 'a', X1C), p('standard', 'b', X1C)]);
    const field = buildPresetField({
      listing: data,
      slot: 'process',
      bound: { keep: () => false, whenEmpty: 'showAll' },
      filters: [],
      pinned: { source: 'standard', id: 'b' },
    });
    expect(field.boundFallback).toBe(true);
    expect(ids(field)).toEqual(['a', 'b']);
    expect(field.pinnedOutside).toBe('outsideBound');
    expect(data.standard.process.filter(field.pickKeep)).toEqual([]);
  });
});
