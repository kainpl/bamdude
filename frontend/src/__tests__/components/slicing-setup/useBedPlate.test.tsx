import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { SlicerKind } from '../../../hooks/useSlicerHealth';
import { useBedPlate } from '../../../components/slicing-setup/useBedPlate';
import { BED_PLATE_MEMORY_KEY, DEFAULT_BED_TYPE } from '../../../components/slicing-setup/bedPlates';

// R8 (spec slicing-setup r5): six plates, the Orca-only one reset for Bambu
// Studio and restored for OrcaSlicer, a set's plate named when refused, one
// remembered value written only by a person or a set.
beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe('useBedPlate (R8)', () => {
  it('starts on the remembered plate, else the default', () => {
    expect(renderHook(() => useBedPlate('orcaslicer')).result.current.value).toBe(DEFAULT_BED_TYPE);
    localStorage.setItem(BED_PLATE_MEMORY_KEY, 'Cool Plate');
    expect(renderHook(() => useBedPlate('orcaslicer')).result.current.value).toBe('Cool Plate');
  });

  it('Orca-only plate reset for Bambu with a note and restored for Orca', () => {
    let slicer: SlicerKind | null = 'orcaslicer';
    const { result, rerender } = renderHook(() => useBedPlate(slicer));
    act(() => result.current.choose('Textured Cool Plate'));
    expect(localStorage.getItem(BED_PLATE_MEMORY_KEY)).toBe('Textured Cool Plate');
    slicer = 'bambu_studio';
    rerender();
    expect(result.current.value).toBe(DEFAULT_BED_TYPE);
    expect(result.current.note).toEqual({ kind: 'reset', bed: 'Textured Cool Plate' });
    expect(localStorage.getItem(BED_PLATE_MEMORY_KEY)).toBe('Textured Cool Plate');
    slicer = 'orcaslicer';
    rerender();
    expect(result.current.value).toBe('Textured Cool Plate');
    expect(result.current.note).toBeNull();
  });

  it("a set's unknown plate is refused with its reason", () => {
    const { result } = renderHook(() => useBedPlate('bambu_studio'));
    let outcome: ReturnType<typeof result.current.applyFromSet> | undefined;
    act(() => {
      outcome = result.current.applyFromSet('Glass Plate');
    });
    expect(outcome).toEqual({ result: 'refused', reason: 'unknown' });
    expect(result.current.note).toEqual({ kind: 'unknown', bed: 'Glass Plate' });
    act(() => {
      outcome = result.current.applyFromSet('Textured Cool Plate');
    });
    expect(outcome).toEqual({ result: 'refused', reason: 'orcaOnly' });
    act(() => {
      outcome = result.current.applyFromSet('Cool Plate');
    });
    expect(outcome).toEqual({ result: 'applied' });
    expect(result.current.value).toBe('Cool Plate');
    expect(result.current.note).toBeNull();
  });

  it('works without storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    const { result } = renderHook(() => useBedPlate('orcaslicer'));
    act(() => result.current.choose('Engineering Plate'));
    expect(result.current.value).toBe('Engineering Plate');
  });
});
