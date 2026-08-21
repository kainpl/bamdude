/**
 * The filament manager's filters, remembered across visits (#users).
 *
 * Column layout, sort, grouping and page size were already remembered; the
 * filters themselves were not, so every return to the inventory started from
 * "all active spools" no matter what you had narrowed to. One key rather than
 * ten: they are answered together and cleared together by **Clear filters**,
 * and ten keys would go stale one at a time.
 *
 * ⚠️ **Not the storage-location filter.** That one is not state at all — the
 * page reads it from `?location_id=` so it can be linked to. Copying it here
 * would give it two homes, and the saved copy would fight the link somebody
 * just followed.
 *
 * ⚠️ **Restoring a filter is only safe because the page SAYS it is filtered.**
 * Each narrowed chip is highlighted, "Clear filters" appears, and the empty
 * state knows filters are on. The library's filters are deliberately NOT
 * persisted for the opposite reason — a silently restored filter on a page that
 * looks unfiltered is a bug report about missing spools.
 */

export type ArchiveFilter = 'active' | 'archived';
export type UsageFilter = 'all' | 'used' | 'new' | 'lowstock';
export type StockFilter = 'all' | 'stock' | 'configured';
/**
 * Whether the spool sits in a printer at all — deliberately not *which* one.
 *
 * ⚠️ Not a narrower storage-location filter. That asks "where in the shelf",
 * this asks "is it loaded or is it stock", which is the question behind "what
 * can I actually queue right now" and "what is left on the shelf to take".
 * Answering it by picking printers one at a time is what this replaces.
 */
export type AssignedFilter = 'all' | 'assigned' | 'unassigned';
export type ViewMode = 'table' | 'cards' | 'forecast';

export const FILTERS_KEY = 'bamdude-inventory-filters';

export type StoredFilters = {
  archiveFilter: ArchiveFilter;
  usageFilter: UsageFilter;
  materialFilter: string;
  brandFilter: string;
  colorFilter: string;
  categoryFilter: string;
  spoolFilter: string;
  stockFilter: StockFilter;
  assignedFilter: AssignedFilter;
  search: string;
  viewMode: ViewMode;
};

export const DEFAULT_FILTERS: StoredFilters = {
  archiveFilter: 'active',
  usageFilter: 'all',
  materialFilter: '',
  brandFilter: '',
  colorFilter: '',
  categoryFilter: '',
  spoolFilter: '',
  stockFilter: 'all',
  assignedFilter: 'all',
  search: '',
  viewMode: 'table',
};

const ARCHIVE_FILTERS: ArchiveFilter[] = ['active', 'archived'];
const USAGE_FILTERS: UsageFilter[] = ['all', 'used', 'new', 'lowstock'];
const STOCK_FILTERS: StockFilter[] = ['all', 'stock', 'configured'];
const ASSIGNED_FILTERS: AssignedFilter[] = ['all', 'assigned', 'unassigned'];
const VIEW_MODES: ViewMode[] = ['table', 'cards', 'forecast'];

/**
 * ⚠️ **Enum values are validated; free-text ones cannot be.** Anything outside
 * a union falls back to its default rather than through — a value we stopped
 * supporting would otherwise filter every spool out of a list whose controls
 * all read "no filter". A material or brand that no longer exists is kept as
 * written and folded into its dropdown by `withCurrentValue`: dropping it here
 * would silently edit the user's saved filter.
 */
export function loadFilters(): StoredFilters {
  try {
    const stored = localStorage.getItem(FILTERS_KEY);
    if (!stored) return DEFAULT_FILTERS;
    const raw = JSON.parse(stored) as Partial<StoredFilters>;
    const text = (v: unknown) => (typeof v === 'string' ? v : '');
    const oneOf = <T,>(v: unknown, allowed: T[], fallback: T): T =>
      allowed.includes(v as T) ? (v as T) : fallback;
    return {
      archiveFilter: oneOf(raw.archiveFilter, ARCHIVE_FILTERS, 'active'),
      usageFilter: oneOf(raw.usageFilter, USAGE_FILTERS, 'all'),
      materialFilter: text(raw.materialFilter),
      brandFilter: text(raw.brandFilter),
      colorFilter: text(raw.colorFilter),
      categoryFilter: text(raw.categoryFilter),
      spoolFilter: text(raw.spoolFilter),
      stockFilter: oneOf(raw.stockFilter, STOCK_FILTERS, 'all'),
      assignedFilter: oneOf(raw.assignedFilter, ASSIGNED_FILTERS, 'all'),
      search: text(raw.search),
      viewMode: oneOf(raw.viewMode, VIEW_MODES, 'table'),
    };
  } catch { /* ignore */ }
  return DEFAULT_FILTERS;
}

export function saveFilters(state: StoredFilters) {
  try {
    // Nothing narrowed and the default view — drop the key instead of writing a
    // blob that says "no filters", so cleared really is cleared.
    const isDefault = (Object.keys(DEFAULT_FILTERS) as (keyof StoredFilters)[])
      .every((k) => state[k] === DEFAULT_FILTERS[k]);
    if (isDefault) localStorage.removeItem(FILTERS_KEY);
    else localStorage.setItem(FILTERS_KEY, JSON.stringify(state));
  } catch { /* ignore */ }
}

/**
 * The current value, folded into a dropdown's options when the data no longer
 * offers it — a brand whose last spool was deleted while the filter was saved.
 * Without this the `<select>` falls back to displaying its placeholder while
 * still filtering by the missing value: an empty list, and a control claiming
 * it is not the reason.
 */
export function withCurrentValue(options: string[], value: string): string[] {
  return value && !options.includes(value) ? [...options, value] : options;
}

/** Same, for the spool chip — its options are catalog ids, the filter a string. */
export function withCurrentId(options: number[], value: string): number[] {
  const id = Number(value);
  return value && Number.isFinite(id) && !options.includes(id) ? [...options, id] : options;
}
