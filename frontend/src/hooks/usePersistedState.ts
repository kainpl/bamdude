import { useCallback, useState } from 'react';
import type { ListView } from '../components/ListViewToggle';

/**
 * A per-viewer preference kept in localStorage — a list's view mode, its page
 * size. Never the place in a list (that is the URL's, `useListUrlState`).
 *
 * Storage can be missing or throw (private windows, blocked site data), so
 * every read and write is guarded and the fallback always renders. A stored
 * value `parse` rejects (`undefined`) is treated as absent.
 */
export function usePersistedState<T extends string | number>(
  storageKey: string,
  fallback: T,
  parse?: (raw: string) => T | undefined,
): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw == null) return fallback;
      const parsed = parse ? parse(raw) : (raw as T);
      return parsed ?? fallback;
    } catch {
      return fallback;
    }
  });

  const set = useCallback(
    (next: T) => {
      setValue(next);
      try {
        localStorage.setItem(storageKey, String(next));
      } catch {
        // A preference that cannot be remembered still applies for this visit.
      }
    },
    [storageKey],
  );

  return [value, set];
}

/** Page sizes the lists offer; -1 is "all". */
export const PAGE_SIZES = [12, 24, 48, 96] as const;

export function parsePageSize(raw: string): number | undefined {
  const n = Number(raw);
  return n === -1 || (PAGE_SIZES as readonly number[]).includes(n) ? n : undefined;
}

export function parseListView(raw: string): ListView | undefined {
  return raw === 'cards' || raw === 'table' ? raw : undefined;
}
