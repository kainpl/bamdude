import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { api } from '../../../api/client';
import { useSlicerChoice } from '../../../components/slicing-setup/useSlicerChoice';

// R7 (spec slicing-setup r5): the effective slicer, and a memory that keeps only
// a person's choice (§4.4.6).
const KEY = 'bamdude:slicer-pick:libraryFile:7';
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
);
function health(orca: boolean, bambu: boolean) {
  vi.spyOn(api, 'getSlicerHealth').mockImplementation(async (kind) => ({
    healthy: kind === 'orcaslicer' ? orca : bambu,
    version: '1',
    error: null,
  }) as never);
}

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ preferred_slicer: 'bambu_studio' } as never);
});
afterEach(() => vi.restoreAllMocks());

describe('useSlicerChoice (R7)', () => {
  it('a forced healthy slicer is not remembered', async () => {
    health(true, false);
    const { result } = renderHook(() => useSlicerChoice(KEY), { wrapper });
    await waitFor(() => expect(result.current.effective).toBe('orcaslicer'));
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it('remembered person choice wins when both are healthy', async () => {
    health(true, true);
    localStorage.setItem(KEY, 'orcaslicer');
    const { result } = renderHook(() => useSlicerChoice(KEY), { wrapper });
    await waitFor(() => expect(result.current.effective).toBe('orcaslicer'));
    act(() => result.current.choose('bambu_studio'));
    expect(result.current.effective).toBe('bambu_studio');
    expect(localStorage.getItem(KEY)).toBe('bambu_studio');
  });

  it('falls back to preferred_slicer when nobody chose', async () => {
    health(true, true);
    const { result } = renderHook(() => useSlicerChoice(KEY), { wrapper });
    await waitFor(() => expect(result.current.effective).toBe('bambu_studio'));
  });

  it('unknown while health loads', () => {
    vi.spyOn(api, 'getSlicerHealth').mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useSlicerChoice(KEY), { wrapper });
    expect(result.current.effective).toBeNull();
  });

  it('works without storage', async () => {
    health(true, true);
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    const { result } = renderHook(() => useSlicerChoice(KEY), { wrapper });
    act(() => result.current.choose('orcaslicer'));
    await waitFor(() => expect(result.current.effective).toBe('orcaslicer'));
  });
});
