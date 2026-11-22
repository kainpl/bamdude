import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { PresetRef } from '../../../api/client';
import {
  POLICIES,
  applyChoice,
  blocksSubmit,
  useChoiceFields,
  type ChoiceField,
} from '../../../components/slicing-setup/choiceState';

// R6 (spec slicing-setup r5): one row of the matrix per test.
const A: PresetRef = { source: 'standard', id: 'A' };
const B: PresetRef = { source: 'standard', id: 'B' };
const field = (ref: PresetRef | null, origin: ChoiceField['origin'], gone = false): ChoiceField => ({ ref, origin, gone });
const listedOnly = (...refs: PresetRef[]) => (r: PresetRef) => refs.some((x) => x.id === r.id);
const always = () => true;

describe('applyChoice (R6)', () => {
  it('seed record listed: kept, origin record, not gone', () => {
    const out = applyChoice(field(null, 'auto'), { kind: 'seed', ref: A, origin: 'record', listed: true }, POLICIES.assemblyProcess);
    expect(out).toEqual({ field: field(A, 'record'), namedGone: false });
  });

  it('seed record not listed: kept, origin record, gone under the assembly policy', () => {
    const out = applyChoice(field(null, 'auto'), { kind: 'seed', ref: A, origin: 'record', listed: false }, POLICIES.assemblyProcess);
    expect(out.field).toEqual(field(A, 'record', true));
  });

  it('refresh, ref still listed: an automatic process that stopped fitting is re-picked', () => {
    const out = applyChoice(
      field(A, 'auto'),
      { kind: 'refresh', listed: always, usable: (r) => r.id !== 'A', repick: () => B },
      POLICIES.sliceProcess,
    );
    expect(out.field).toEqual(field(B, 'auto'));
  });

  it('refresh gone: Slice auto filament re-picked silently', () => {
    const out = applyChoice(
      field(A, 'auto'),
      { kind: 'refresh', listed: listedOnly(B), usable: always, repick: () => B },
      POLICIES.sliceFilament,
    );
    expect(out).toEqual({ field: field(B, 'auto'), namedGone: false });
  });

  it('refresh gone: Slice person filament re-picked and named', () => {
    const out = applyChoice(
      field(A, 'person'),
      { kind: 'refresh', listed: listedOnly(B), usable: always, repick: () => B },
      POLICIES.sliceFilament,
    );
    expect(out).toEqual({ field: field(B, 'auto'), namedGone: true });
  });

  it('refresh gone: assembly auto process re-picked; assembly person process blocks', () => {
    const ev = { kind: 'refresh' as const, listed: listedOnly(B), usable: always, repick: () => B };
    expect(applyChoice(field(A, 'auto'), ev, POLICIES.assemblyProcess).field).toEqual(field(B, 'auto'));
    expect(applyChoice(field(A, 'person'), ev, POLICIES.assemblyProcess).field).toEqual(field(A, 'person', true));
  });

  it('refresh gone: printer blocks everywhere', () => {
    const ev = { kind: 'refresh' as const, listed: listedOnly(B), usable: always, repick: () => B };
    for (const policy of [POLICIES.slicePrinter, POLICIES.calibrationPrinter, POLICIES.assemblyPrinter]) {
      expect(applyChoice(field(A, 'auto'), ev, policy).field).toEqual(field(A, 'auto', true));
      expect(applyChoice(field(A, 'person'), ev, policy).field).toEqual(field(A, 'person', true));
    }
  });

  it('gone returns: assembly restores; Slice printer stays blocked', () => {
    const ev = { kind: 'refresh' as const, listed: always, usable: always, repick: () => B };
    expect(applyChoice(field(A, 'person', true), ev, POLICIES.assemblyProcess).field).toEqual(field(A, 'person'));
    expect(applyChoice(field(A, 'person', true), ev, POLICIES.slicePrinter).field).toEqual(field(A, 'person', true));
  });

  it('printer changed: auto re-checked, person kept', () => {
    const ev = { kind: 'printerChanged' as const, usable: (r: PresetRef) => r.id !== 'A', repick: () => B };
    expect(applyChoice(field(A, 'auto'), ev, POLICIES.sliceProcess).field).toEqual(field(B, 'auto'));
    expect(applyChoice(field(A, 'person'), ev, POLICIES.sliceProcess).field).toEqual(field(A, 'person'));
  });

  it('nozzle changed: auto re-picked, person kept', () => {
    const ev = { kind: 'nozzleChanged' as const, usable: (r: PresetRef) => r.id !== 'A', repick: () => B };
    expect(applyChoice(field(A, 'auto'), ev, POLICIES.calibrationPrinter).field).toEqual(field(B, 'auto'));
    expect(applyChoice(field(A, 'person'), ev, POLICIES.calibrationPrinter).field).toEqual(field(A, 'person'));
  });

  it('filter changed: auto re-picked, person kept, empty stays empty', () => {
    const ev = { kind: 'filterChanged' as const, repick: () => B };
    expect(applyChoice(field(A, 'auto'), ev, POLICIES.sliceProcess).field).toEqual(field(B, 'auto'));
    expect(applyChoice(field(A, 'person'), ev, POLICIES.sliceProcess).field).toEqual(field(A, 'person'));
    expect(applyChoice(field(null, 'auto'), ev, POLICIES.sliceProcess).field).toEqual(field(null, 'auto'));
  });

  it('placeholder: person empty, never refilled, gone not cleared', () => {
    const cleared = applyChoice(field(A, 'auto'), { kind: 'personPicked', ref: null }, POLICIES.sliceFilament).field;
    expect(cleared).toEqual(field(null, 'person'));
    const refreshed = applyChoice(
      cleared,
      { kind: 'refresh', listed: always, usable: always, repick: () => B },
      POLICIES.sliceFilament,
    );
    expect(refreshed.field).toEqual(field(null, 'person'));
    const blocked = field(A, 'record', true);
    expect(applyChoice(blocked, { kind: 'personPicked', ref: null }, POLICIES.assemblyProcess).field).toEqual(blocked);
    expect(applyChoice(blocked, { kind: 'personPicked', ref: B }, POLICIES.assemblyProcess).field).toEqual(field(B, 'person'));
  });

  it('familyAnswered: empty auto filled, auto outside the family re-picked, person kept', () => {
    const ev = { kind: 'familyAnswered' as const, repick: () => B, belongs: (r: PresetRef) => r.id === 'B' };
    expect(applyChoice(field(null, 'auto'), ev, POLICIES.calibrationFilament).field).toEqual(field(B, 'auto'));
    expect(applyChoice(field(A, 'auto'), ev, POLICIES.calibrationFilament).field).toEqual(field(B, 'auto'));
    expect(applyChoice(field(B, 'auto'), { ...ev, repick: () => A }, POLICIES.calibrationFilament).field).toEqual(field(B, 'auto'));
    expect(applyChoice(field(A, 'person'), ev, POLICIES.calibrationFilament).field).toEqual(field(A, 'person'));
    expect(applyChoice(field(null, 'person'), ev, POLICIES.calibrationFilament).field).toEqual(field(null, 'person'));
  });

  it('pipeline applied: origin pipeline, block cleared', () => {
    expect(applyChoice(field(A, 'person', true), { kind: 'pipelineApplied', ref: B }, POLICIES.assemblyProcess).field).toEqual(
      field(B, 'pipeline'),
    );
  });

  it('reset: every field auto with the given pick', () => {
    expect(applyChoice(field(A, 'person', true), { kind: 'reset', pick: B }, POLICIES.calibrationFilament).field).toEqual(
      field(B, 'auto'),
    );
  });

  it('blocksSubmit: empty when required, or gone', () => {
    expect(blocksSubmit(field(null, 'auto'), true)).toBe(true);
    expect(blocksSubmit(field(null, 'auto'), false)).toBe(false);
    expect(blocksSubmit(field(A, 'person', true), false)).toBe(true);
    expect(blocksSubmit(field(A, 'person'), true)).toBe(false);
  });
});

describe('useChoiceFields', () => {
  it('keeps fields by key and forgets one without touching another', () => {
    const { result } = renderHook(() => useChoiceFields<'row1' | 'row2'>());
    act(() => {
      result.current.dispatch('row1', { kind: 'personPicked', ref: A }, POLICIES.assemblyProcess);
      result.current.dispatch('row2', { kind: 'personPicked', ref: B }, POLICIES.assemblyProcess);
    });
    act(() => result.current.forget('row1'));
    expect(result.current.get('row1')).toEqual(field(null, 'auto'));
    expect(result.current.get('row2')).toEqual(field(B, 'person'));
  });
});
