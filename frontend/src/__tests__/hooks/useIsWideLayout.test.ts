/**
 * ⚠️ Exists because the layout switch is BEHAVIOURAL, not stylistic: the
 * process-settings panel is a disclosure below `lg` and permanently open once
 * it owns a column, with its toggle inert there. CSS cannot say that.
 *
 * The breakpoint must equal the `lg:` classes it is paired with — the two
 * disagreeing produces a half-applied layout, which is the failure mode this
 * whole hook exists to avoid.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import { useIsWideLayout } from '../../hooks/useIsWideLayout';

function stubMatchMedia(matches: boolean) {
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches,
    media: query,
    addEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.delete(cb),
  }));
  return {
    fire(next: boolean) {
      listeners.forEach((cb) => cb({ matches: next } as MediaQueryListEvent));
    },
  };
}

describe('useIsWideLayout', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('is true at the lg breakpoint', () => {
    stubMatchMedia(true);
    const { result } = renderHook(() => useIsWideLayout());
    expect(result.current).toBe(true);
  });

  it('is false below it', () => {
    stubMatchMedia(false);
    const { result } = renderHook(() => useIsWideLayout());
    expect(result.current).toBe(false);
  });

  it('follows a resize across the breakpoint', () => {
    const media = stubMatchMedia(false);
    const { result } = renderHook(() => useIsWideLayout());

    act(() => media.fire(true));

    expect(result.current).toBe(true);
  });

  it('queries exactly 1024px, the value `lg:` means', () => {
    // ⚠️ Pinned deliberately: a hook one pixel out from the classes beside it
    // produces a layout that is half applied at exactly one width, which is
    // the hardest kind of bug to see.
    const seen: string[] = [];
    vi.stubGlobal('matchMedia', (query: string) => {
      seen.push(query);
      return { matches: true, media: query, addEventListener: () => {}, removeEventListener: () => {} };
    });
    renderHook(() => useIsWideLayout());
    expect(seen).toContain('(min-width: 1024px)');
  });
});
