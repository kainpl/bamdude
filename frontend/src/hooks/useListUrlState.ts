import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router';

/**
 * The PLACE in a list — page, search, sort, and page-specific keys such as the
 * orders tab — lives in the URL, so Back, F5 and a link to a colleague return
 * the same view (spec projects-lists-parity, rule 13). Preferences (view mode,
 * page size) do not belong here; they are `usePersistedState`'s.
 *
 * Page 1 and default values are never written: a clean URL is the default view.
 * Any change of q / sort / an extra resets the page — a filter that keeps you
 * on page 4 of a two-page result is a blank screen. Writes `replace` the
 * history entry: typing a search must not leave one Back-step per keystroke.
 */
export interface ListUrlDefaults {
  sort?: string;
  /** Page-specific keys and their defaults; only these are read from the URL. */
  extra?: Record<string, string>;
}

export function useListUrlState({ defaults: given }: { defaults: ListUrlDefaults }) {
  // Callers pass a literal; keyed by content so the setters below stay stable.
  const defaultsKey = JSON.stringify(given);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by content on purpose
  const defaults = useMemo(() => given, [defaultsKey]);

  const [params, setParams] = useSearchParams();
  const page = Math.max(1, parseInt(params.get('page') ?? '1', 10) || 1);
  const q = params.get('q') ?? '';
  const sort = params.get('sort') ?? defaults.sort ?? '';
  const extra = useMemo(() => {
    const out: Record<string, string> = { ...(defaults.extra ?? {}) };
    for (const key of Object.keys(defaults.extra ?? {})) {
      const value = params.get(key);
      if (value != null) out[key] = value;
    }
    return out;
  }, [params, defaults]);

  const write = useCallback(
    (mutate: (next: URLSearchParams) => void) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          mutate(next);
          if (next.get('page') === '1') next.delete('page');
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const setPage = useCallback((p: number) => write((n) => n.set('page', String(Math.max(1, p)))), [write]);
  const setQ = useCallback(
    (value: string) =>
      write((n) => {
        if (value) n.set('q', value);
        else n.delete('q');
        n.delete('page');
      }),
    [write],
  );
  const setSort = useCallback(
    (value: string) =>
      write((n) => {
        if (value && value !== defaults.sort) n.set('sort', value);
        else n.delete('sort');
        n.delete('page');
      }),
    [write, defaults],
  );
  const setExtra = useCallback(
    (key: string, value: string) =>
      write((n) => {
        if (value && value !== defaults.extra?.[key]) n.set(key, value);
        else n.delete(key);
        n.delete('page');
      }),
    [write, defaults],
  );
  /**
   * The empty state's «Reset»: search and every extra back to default, page 1,
   * sort kept — in ONE write. Two setters in a row would each start from the
   * same stale URL and the second would undo the first. `keep` names extras
   * that are a place rather than a filter (the orders tab).
   */
  const resetFilters = useCallback(
    (keep: string[] = []) =>
      write((n) => {
        n.delete('q');
        n.delete('page');
        for (const key of Object.keys(defaults.extra ?? {})) if (!keep.includes(key)) n.delete(key);
      }),
    [write, defaults],
  );
  /** After a delete (or someone else's), the page we stand on may no longer exist. */
  const clampToLastPage = useCallback(
    (last: number) => {
      if (page > last) setPage(last);
    },
    [page, setPage],
  );

  return { page, q, sort, extra, setPage, setQ, setSort, setExtra, resetFilters, clampToLastPage };
}
