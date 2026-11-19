import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, render, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { api, ApiError, type LoadedFilamentsResponse, type UnifiedPreset } from '../../../api/client';
import { usePresetFilters } from '../../../components/slicing-setup/usePresetFilters';
import { FARM_MODELS_KEY, LEGACY_ONLINE_KEY, SOURCE_FILTER_KEY, __resetSharedMemoryForTests } from '../../../components/slicing-setup/memory';
import { __resetPresetContextForTests } from '../../../utils/presetContext';

// Task 6 (plan r6): the shared filters, their memory and the one farm read.
const auth = { permissions: new Set(['library:upload', 'printers:read']), userId: 1 };
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: auth.userId }, hasPermission: (p: string) => auth.permissions.has(p) }),
}));

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

const farm = (over: Partial<LoadedFilamentsResponse> = {}): LoadedFilamentsResponse => ({
  printers: [],
  families: {},
  farm_printers: [{ printer_id: 1, model: 'X1C', accepts: [], nozzle_diameters: [0.4] }],
  ...over,
});
const printer = (name: string) => ({ id: name, name, source: 'standard' }) as UnifiedPreset;
const ctx = { slot: 'printer' as const, printerName: null, parentsBySource: {} };

beforeEach(() => {
  localStorage.clear();
  __resetSharedMemoryForTests();
  __resetPresetContextForTests();
  auth.permissions = new Set(['library:upload', 'printers:read']);
});
afterEach(() => vi.restoreAllMocks());

describe('usePresetFilters', () => {
  it('two mounted consumers share one value', () => {
    const seen: string[] = [];
    const handle: { set: ((v: 'all') => void) | null } = { set: null };
    function First() {
      const f = usePresetFilters({ source: true });
      handle.set = f.setSource;
      seen.push(`first:${f.source}`);
      return null;
    }
    function Second() {
      const f = usePresetFilters({ source: true });
      seen.push(`second:${f.source}`);
      return null;
    }
    const client = newClient();
    render(
      <QueryClientProvider client={client}>
        <First />
        <Second />
      </QueryClientProvider>,
    );
    expect(seen).toContain('first:mine');
    act(() => handle.set?.('all'));
    expect(seen[seen.length - 1]).toBe('second:all');
    expect(localStorage.getItem(SOURCE_FILTER_KEY)).toBe('all');
  });

  it('farm models seeds once from the legacy online key; a present new 0 wins', () => {
    localStorage.setItem(LEGACY_ONLINE_KEY, '1');
    const a = renderHook(() => usePresetFilters({ farmModels: true }), { wrapper: wrapper(newClient()) });
    expect(a.result.current.onlyFarmModels).toBe(true);
    a.unmount();

    localStorage.clear();
    __resetSharedMemoryForTests();
    localStorage.setItem(LEGACY_ONLINE_KEY, '1');
    localStorage.setItem(FARM_MODELS_KEY, '0');
    const b = renderHook(() => usePresetFilters({ farmModels: true }), { wrapper: wrapper(newClient()) });
    expect(b.result.current.onlyFarmModels).toBe(false);
    b.unmount();

    localStorage.clear();
    __resetSharedMemoryForTests();
    localStorage.setItem(LEGACY_ONLINE_KEY, 'yes');
    const c = renderHook(() => usePresetFilters({ farmModels: true }), { wrapper: wrapper(newClient()) });
    expect(c.result.current.onlyFarmModels).toBe(false);
  });

  it('works without storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    const { result } = renderHook(() => usePresetFilters({ source: true, loaded: true }), { wrapper: wrapper(newClient()) });
    expect(result.current.source).toBe('mine');
    act(() => result.current.setOnlyLoaded(true));
    expect(result.current.onlyLoaded).toBe(true);
  });

  it('a forbidden or failed farm read never clears a switch', async () => {
    localStorage.setItem(FARM_MODELS_KEY, '1');
    vi.spyOn(api, 'getSlicerLoadedFilaments').mockRejectedValue(new ApiError('nope', 403));
    const { result } = renderHook(() => usePresetFilters({ farmModels: true }), { wrapper: wrapper(newClient()) });
    await waitFor(() => expect(result.current.farmStatus).toBe('forbidden'));
    expect(result.current.onlyFarmModels).toBe(true);
    expect(localStorage.getItem(FARM_MODELS_KEY)).toBe('1');
  });

  it('stale farm narrows the display and steers no pick', async () => {
    localStorage.setItem(FARM_MODELS_KEY, '1');
    const spy = vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValueOnce(farm());
    const client = newClient();
    const { result } = renderHook(() => usePresetFilters({ farmModels: true }), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.farmStatus).toBe('ready'));
    spy.mockRejectedValueOnce(new Error('down'));
    await act(async () => {
      result.current.retry();
    });
    await waitFor(() => expect(result.current.farmStatus).toBe('stale'));
    const [farmFilter] = result.current.softFilters(ctx);
    expect(farmFilter.id).toBe('farm');
    expect(farmFilter.keep(printer('Bambu Lab A1 0.4 nozzle'))).toBe(false);
    expect(farmFilter.pickKeep).toBeNull();
  });

  it('farmRead always reads with switches off', async () => {
    const spy = vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValue(farm());
    renderHook(() => usePresetFilters({ source: true, farmRead: 'always' }), { wrapper: wrapper(newClient()) });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  });

  it('no read without both rights, even with farmRead always', async () => {
    auth.permissions = new Set(['library:upload']);
    const spy = vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValue(farm());
    const { result } = renderHook(() => usePresetFilters({ farmRead: 'always' }), { wrapper: wrapper(newClient()) });
    expect(result.current.offered).toBe(false);
    await new Promise((r) => setTimeout(r, 20));
    expect(spy).not.toHaveBeenCalled();
  });

  it('missing farm_printers narrows nothing', async () => {
    localStorage.setItem(FARM_MODELS_KEY, '1');
    vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValue({ printers: [], families: {} } as LoadedFilamentsResponse);
    const { result } = renderHook(() => usePresetFilters({ farmModels: true }), { wrapper: wrapper(newClient()) });
    await waitFor(() => expect(result.current.farmStatus).toBe('ready'));
    expect(result.current.softFilters(ctx)).toEqual([]);
  });

  it('a farm model keeps the printer presets of a farm printer only', async () => {
    localStorage.setItem(FARM_MODELS_KEY, '1');
    vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValue(farm());
    const { result } = renderHook(() => usePresetFilters({ farmModels: true }), { wrapper: wrapper(newClient()) });
    await waitFor(() => expect(result.current.farmStatus).toBe('ready'));
    const [f] = result.current.softFilters(ctx);
    expect(f.keep(printer('Bambu Lab X1 Carbon 0.4 nozzle'))).toBe(true);
    expect(f.keep(printer('Bambu Lab A1 0.4 nozzle'))).toBe(false);
    expect(f.pickKeep).not.toBeNull();
  });

  it('retry does nothing without a key', () => {
    auth.permissions = new Set(['library:upload', 'printers:read', 'cloud:auth']);
    vi.spyOn(api, 'getCloudStatus').mockReturnValue(new Promise(() => {}));
    const spy = vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValue(farm());
    const { result } = renderHook(() => usePresetFilters({ farmRead: 'always' }), { wrapper: wrapper(newClient()) });
    act(() => result.current.retry());
    expect(spy).not.toHaveBeenCalled();
  });
});
