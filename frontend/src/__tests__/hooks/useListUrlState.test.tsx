/**
 * The place in a list lives in the URL; preferences live in localStorage
 * (spec projects-lists-parity, rules 12–13, 16).
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { MemoryRouter, useSearchParams } from 'react-router';
import type { ReactNode } from 'react';
import { useListUrlState } from '../../hooks/useListUrlState';
import { usePersistedState } from '../../hooks/usePersistedState';

function wrapper(initial: string) {
  return ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>;
}

describe('useListUrlState', () => {
  it('reads page, q, sort and extras from the URL and defaults the rest', () => {
    const { result } = renderHook(
      () => useListUrlState({ defaults: { sort: 'updated-desc', extra: { tab: 'active' } } }),
      { wrapper: wrapper('/projects?page=3&q=gear&tab=completed') },
    );
    expect(result.current.page).toBe(3);
    expect(result.current.q).toBe('gear');
    expect(result.current.sort).toBe('updated-desc');
    expect(result.current.extra.tab).toBe('completed');
  });

  it('treats a nonsense page as page 1', () => {
    const { result } = renderHook(() => useListUrlState({ defaults: {} }), { wrapper: wrapper('/x?page=abc') });
    expect(result.current.page).toBe(1);
  });

  it('resets the page to 1 when q, sort or an extra changes, and writes the URL', () => {
    const { result } = renderHook(
      () => ({
        state: useListUrlState({ defaults: { sort: 'name-asc', extra: { tab: 'active' } } }),
        params: useSearchParams()[0],
      }),
      { wrapper: wrapper('/products?page=4') },
    );
    act(() => result.current.state.setQ('lamp'));
    expect(result.current.state.page).toBe(1);
    expect(result.current.params.get('q')).toBe('lamp');
    expect(result.current.params.get('page')).toBeNull(); // page 1 is the default → not written
    act(() => result.current.state.setPage(2));
    expect(result.current.params.get('page')).toBe('2');
    act(() => result.current.state.setExtra('tab', 'all'));
    expect(result.current.state.page).toBe(1);
    act(() => result.current.state.setSort('name-desc'));
    expect(result.current.params.get('sort')).toBe('name-desc');
    act(() => result.current.state.setSort('name-asc'));
    expect(result.current.params.get('sort')).toBeNull(); // the default is never written
    act(() => result.current.state.setExtra('tab', 'active'));
    expect(result.current.params.get('tab')).toBeNull();
  });

  it('resets search and filters in one write, keeping the sort', () => {
    const { result } = renderHook(
      () => ({
        state: useListUrlState({ defaults: { sort: 'name-asc', extra: { catalog: '1' } } }),
        params: useSearchParams()[0],
      }),
      { wrapper: wrapper('/products?q=zzz&catalog=0&page=3&sort=parts-desc') },
    );
    act(() => result.current.state.resetFilters());
    expect(result.current.params.toString()).toBe('sort=parts-desc');
    expect(result.current.state.extra.catalog).toBe('1');
  });

  it('clamps to the last page after the list shrank', () => {
    const { result } = renderHook(() => useListUrlState({ defaults: {} }), { wrapper: wrapper('/customers?page=9') });
    act(() => result.current.clampToLastPage(2));
    expect(result.current.page).toBe(2);
  });
});

describe('usePersistedState', () => {
  beforeEach(() => localStorage.clear());

  it('starts from the fallback, remembers what it is set to, and reads it back', () => {
    const first = renderHook(() => usePersistedState<string>('k-view', 'cards'));
    expect(first.result.current[0]).toBe('cards');
    act(() => first.result.current[1]('table'));
    expect(first.result.current[0]).toBe('table');
    expect(localStorage.getItem('k-view')).toBe('table');

    const again = renderHook(() => usePersistedState<string>('k-view', 'cards'));
    expect(again.result.current[0]).toBe('table');
  });

  it('refuses a stored value its parser rejects', () => {
    localStorage.setItem('k-per-page', 'lots');
    const { result } = renderHook(() =>
      usePersistedState('k-per-page', 24, (raw) => {
        const n = Number(raw);
        return Number.isInteger(n) && (n === -1 || n > 0) ? n : undefined;
      }),
    );
    expect(result.current[0]).toBe(24);
  });
});
