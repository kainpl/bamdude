import { act, renderHook } from '@testing-library/react';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';

let entries: PerformanceEntry[];
let publish: (entries: PerformanceEntry[]) => void;
function resource(protocol: string, path = '/api/v1/printers/') {
  return { entryType: 'resource', name: new URL(path, window.location.href).href,
    nextHopProtocol: protocol } as PerformanceResourceTiming;
}

beforeEach(() => {
  vi.resetModules();
  entries = [];
  vi.spyOn(performance, 'getEntriesByType').mockImplementation(() => entries);
  vi.stubGlobal('PerformanceObserver', class {
    constructor(callback: PerformanceObserverCallback) {
      publish = (data) => callback({ getEntries: () => data } as PerformanceObserverEntryList, this as unknown as PerformanceObserver);
    }
    observe() {}
    disconnect() {}
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('camera live budget', () => {
  it('shares two unknown-protocol slots across wall and popup and releases them', async () => {
    const { useCameraLiveBudget } = await import('../../hooks/useCameraLiveBudget');
    const wall = renderHook(({ count }) => useCameraLiveBudget(count), { initialProps: { count: 8 } });
    const popup = renderHook(() => useCameraLiveBudget(1));
    expect(wall.result.current.granted).toBe(2);
    expect(popup.result.current.granted).toBe(0);
    wall.unmount();
    expect(popup.result.current.granted).toBe(1);
  });

  it.each(['h2', 'h3'])('allows the requested live count after confirmed %s API traffic', async (protocol) => {
    entries = [resource(protocol)];
    const { useCameraLiveBudget } = await import('../../hooks/useCameraLiveBudget');
    const wall = renderHook(() => useCameraLiveBudget(8));
    expect(wall.result.current.granted).toBe(8);
    expect(wall.result.current.protocol).toBe(protocol);
  });

  it('does not infer API transport from HTTPS, navigation, assets, cross-origin or empty timings', async () => {
    entries = [resource('h2', '/assets/app.js'), resource('h3', 'https://elsewhere.invalid/api/x'),
      { ...resource('h2'), entryType: 'navigation' }, resource('')];
    const { useCameraLiveBudget } = await import('../../hooks/useCameraLiveBudget');
    const wall = renderHook(() => useCameraLiveBudget(8));
    expect(wall.result.current.granted).toBe(2);
    expect(wall.result.current.protocol).toBe('unknown');
  });

  it('reacts to API timings and keeps a mixed HTTP/1 connection conservative', async () => {
    const { useCameraLiveBudget } = await import('../../hooks/useCameraLiveBudget');
    const wall = renderHook(() => useCameraLiveBudget(8));
    act(() => publish([resource('h2')]));
    expect(wall.result.current.granted).toBe(8);
    act(() => publish([resource('http/1.1')]));
    expect(wall.result.current.granted).toBe(2);
    act(() => publish([resource('h3')]));
    expect(wall.result.current.granted).toBe(2);
  });
});
