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

/**
 * A parser that accepts only the page's modes (spec workshop-lists, rule 12):
 * a stored value that is not one of them — an old mode, a future one, garbage —
 * reads as absent, so the page's default applies.
 */
export function listViewParser<V extends string>(modes: readonly V[]): (raw: string) => V | undefined {
  return (raw) => ((modes as readonly string[]).includes(raw) ? (raw as V) : undefined);
}

/** The one list of the cards/table modes: the parser and `useCardsTableViews` both read it, so a
 *  mode added to the switch can never be dropped by the stored-choice parser. */
export const CARDS_TABLE_MODES: readonly ListView[] = ['cards', 'table'];

export const parseListView = listViewParser<ListView>(CARDS_TABLE_MODES);

/** The orders page's views (spec workshop-order-views, rule 1) — its own type: products and
 *  customers stay on `ListView`. */
export type OrdersView = 'table' | 'cards' | 'kanban' | 'workspace' | 'deadlines';

export const ORDERS_VIEW_MODES: readonly OrdersView[] = ['cards', 'table', 'kanban', 'workspace', 'deadlines'];

export const parseOrdersView = listViewParser<OrdersView>(ORDERS_VIEW_MODES);

export type ArchivesView = 'grid' | 'list' | 'calendar';
export const ARCHIVES_VIEW_MODES: readonly ArchivesView[] = ['grid', 'list', 'calendar'];
export const parseArchivesView = listViewParser<ArchivesView>(ARCHIVES_VIEW_MODES);

export type LibraryView = 'grid' | 'list' | 'columns';
export const LIBRARY_VIEW_MODES: readonly LibraryView[] = ['grid', 'list', 'columns'];
export const parseLibraryView = listViewParser<LibraryView>(LIBRARY_VIEW_MODES);

export type QueueView = 'expanded' | 'all' | 'timeline';
const parseCurrentQueueView = listViewParser<QueueView>(['expanded', 'all', 'timeline']);
export function parseQueueView(raw: string): QueueView | undefined {
  return raw === 'compact' ? 'expanded' : parseCurrentQueueView(raw);
}

export type PrintersPageView = 'cards' | 'camwall';
export const parsePrintersPageView = listViewParser<PrintersPageView>(['cards', 'camwall']);
