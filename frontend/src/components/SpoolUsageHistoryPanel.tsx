import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Archive, ArrowDown, ArrowUp, ArrowUpDown, Clock, Loader2, Package, X } from 'lucide-react';
import { api } from '../api/client';
import type { SpoolUsageListItem } from '../api/client';
import { PaginationBar } from './PaginationBar';
import { useToast } from '../contexts/ToastContext';
import { getSwatchStyle, resolveSpoolColorName } from '../utils/colors';
import { getCurrencySymbol } from '../utils/currency';
import { formatDateTime, type DateFormat, type TimeFormat } from '../utils/date';
import { comparePrinterLike, printerLabel } from '../utils/printerLabel';
import { DEFAULT_SPOOL_DISPLAY_TEMPLATE, formatSpoolDisplayName } from '../utils/spoolName';
import { invalidateSpoolAndLocationQueries } from '../utils/inventoryQueries';

/**
 * The farm's whole filament ledger, on the Inventory page (2026-09-01).
 *
 * The per-spool tab in the spool dialog answers "where did THIS reel go"; this
 * answers "where did the filament go", which is a different question and the
 * one an operator actually starts from. Every dimension of it — the page, the
 * order, the filters, the search and the totals — is decided by the server
 * (`GET /inventory/usage?page=`): a farm with a year of prints has six figures
 * of rows here, and the last client-side feed of this table was a 5000-row
 * download the forecast rewrite deleted for exactly that reason.
 *
 * ⚠️ **The search box is the PAGE's**, not this panel's — one box, in one place,
 * whichever view is open, because a second one appearing a row lower when you
 * switch tabs is a control that moved for no reason the operator can see. The
 * text arrives as a prop and is debounced here, where the requests are made.
 *
 * The FILTERS are this panel's own — the page's ask about SPOOLS (archived,
 * unused, low stock, assigned) and none of that means anything about a
 * consumption event — but they wear the page's chip vocabulary and sit exactly
 * where the page's row sits, because switching views should not look like
 * switching applications.
 */

const SORT_KEYS = ['created_at', 'spool', 'print_name', 'printer', 'weight_used', 'percent_used', 'cost', 'status'] as const;
type SortKey = (typeof SORT_KEYS)[number];
type SortDir = 'asc' | 'desc';

const SORT_STORAGE_KEY = 'bamdude-usage-history-sort';

// The server caps per_page at 200; -1 is PaginationBar's "all" → `all=true`.
const PER_PAGE_OPTIONS = [25, 50, 100, 200];

const STATUS_COLORS: Record<string, string> = {
  completed: 'text-bambu-green',
  failed: 'text-red-700 dark:text-red-400',
  aborted: 'text-yellow-700 dark:text-yellow-400',
  ams_sync: 'text-blue-700 dark:text-blue-400',
  runout: 'text-orange-700 dark:text-orange-400',
};

// Only the two BamDude-invented statuses are translated — the print outcomes
// are the printer's own vocabulary and read the same in both locales. Same map
// as the per-spool list; anything unmapped falls back to the raw string so a
// status added later shows up rather than disappearing.
const STATUS_LABEL_KEYS: Record<string, string> = {
  ams_sync: 'inventory.usageStatusAmsSync',
  runout: 'inventory.usageStatusRunout',
};

const STATUS_HINT_KEYS: Record<string, string> = {
  ams_sync: 'inventory.usageStatusAmsSyncHint',
  runout: 'inventory.usageStatusRunoutHint',
};

// The page's own chip vocabulary, copied so this row belongs to the same page
// rather than resembling it (InventoryPage's material/brand dropdown chips).
const CHIP_BASE =
  'px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none';
const CHIP_ON = 'bg-bambu-green/20 text-bambu-green border-bambu-green/30';
const CHIP_OFF = 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary';

const chipClass = (active: boolean) => `${CHIP_BASE} ${active ? CHIP_ON : CHIP_OFF}`;

function loadSort(): { key: SortKey; dir: SortDir } {
  const fallback: { key: SortKey; dir: SortDir } = { key: 'created_at', dir: 'desc' };
  try {
    const stored = localStorage.getItem(SORT_STORAGE_KEY);
    if (!stored) return fallback;
    const parsed = JSON.parse(stored) as { key?: unknown; dir?: unknown };
    // A legacy or hand-edited value must never reach the server as a sort it
    // does not know — the endpoint falls back rather than 400ing, but a
    // silently ignored sort is a control that lies about what it did.
    if (typeof parsed.key !== 'string' || !(SORT_KEYS as readonly string[]).includes(parsed.key)) return fallback;
    return { key: parsed.key as SortKey, dir: parsed.dir === 'asc' ? 'asc' : 'desc' };
  } catch {
    return fallback;
  }
}

/**
 * A local calendar day → the UTC instant it starts at.
 *
 * The picker speaks in the operator's days; the column is stored in UTC. Only
 * the browser knows which timezone to bridge them with, which is why the
 * conversion happens here and the server takes absolute instants.
 */
function localDayStart(day: string): string | undefined {
  if (!day) return undefined;
  const parsed = new Date(`${day}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}

/** The exclusive end of a local calendar day — the start of the day after. */
function localDayEnd(day: string): string | undefined {
  if (!day) return undefined;
  const parsed = new Date(`${day}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return undefined;
  parsed.setDate(parsed.getDate() + 1);
  return parsed.toISOString();
}

interface SpoolUsageHistoryPanelProps {
  /** The page's search box, verbatim. Debounced here, where requests are made. */
  search?: string;
  /** Lets this panel's "Clear filters" empty the page's box too — one button
   *  that leaves nothing behind, like the page's own. */
  onClearSearch?: () => void;
  /** Opens the spool behind a row. Omitted, the spool cell is plain text —
   *  the panel never navigates on its own. */
  onOpenSpool?: (spoolId: number) => void;
}

export function SpoolUsageHistoryPanel({ search = '', onClearSearch, onOpenSpool }: SpoolUsageHistoryPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [debouncedSearch, setDebouncedSearch] = useState(search);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [printerId, setPrinterId] = useState('');
  const [material, setMaterial] = useState('');
  const [brand, setBrand] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  // ⚠️ Both default to 'all', which the spool list has no equivalent of: over
  // there a tab is always active OR archived. A history is the record of what
  // was burned, and retiring or unloading the reel afterwards does not un-burn
  // it — so nothing is hidden until somebody asks for it to be.
  const [archived, setArchived] = useState<'all' | 'active' | 'archived'>('all');
  const [assigned, setAssigned] = useState<'all' | 'assigned' | 'unassigned'>('all');
  const [initialSort] = useState(loadSort);
  const [sortKey, setSortKey] = useState<SortKey>(initialSort.key);
  const [sortDir, setSortDir] = useState<SortDir>(initialSort.dir);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(50);

  // Typing is not a request. Every other control commits immediately; only the
  // search waits, because it is the one that changes on every keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Any filter/sort change invalidates the current page — adjusted right here
  // during render (the ForecastPanel pattern): a useEffect would land one
  // render late and let a stale-page request fire first.
  const pageResetSignature = JSON.stringify([debouncedSearch, statuses, printerId, material, brand, archived, assigned, dateFrom, dateTo, sortKey, sortDir]);
  const [prevPageResetSignature, setPrevPageResetSignature] = useState(pageResetSignature);
  let effectivePage = page;
  if (pageResetSignature !== prevPageResetSignature) {
    setPrevPageResetSignature(pageResetSignature);
    setPage(1);
    effectivePage = 1;
  }

  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });
  const timeFormat = (settings?.time_format ?? 'system') as TimeFormat;
  const dateFormat = (settings?.date_format ?? 'system') as DateFormat;
  const currency = getCurrencySymbol(settings?.currency || 'USD');
  // The SAME template the table and the cards render a spool with. A ledger
  // that named reels its own way would be a second vocabulary for one shelf —
  // "SUNLU PLA Black" here and whatever the operator configured over there.
  const spoolDisplayTemplate = settings?.spool_display_template || DEFAULT_SPOOL_DISPLAY_TEMPLATE;

  const { data: facets } = useQuery({
    queryKey: ['usage-history', 'facets'],
    queryFn: api.getUsageHistoryFacets,
    staleTime: 60_000,
  });

  const params = useMemo(() => ({
    q: debouncedSearch || undefined,
    status: statuses.length > 0 ? statuses : undefined,
    printer_id: printerId || undefined,
    material: material || undefined,
    brand: brand || undefined,
    archived: archived === 'all' ? undefined : archived,
    assigned: assigned === 'all' ? undefined : assigned,
    date_from: localDayStart(dateFrom),
    date_to: localDayEnd(dateTo),
    sort_by: `${sortKey}_${sortDir}`,
    page: effectivePage,
    ...(perPage === -1 ? { all: true } : { per_page: perPage }),
  }), [debouncedSearch, statuses, printerId, material, brand, archived, assigned, dateFrom, dateTo, sortKey, sortDir, effectivePage, perPage]);

  const historyQuery = useQuery({
    queryKey: ['usage-history', 'page', params],
    queryFn: () => api.getUsageHistory(params),
    placeholderData: (previous) => previous,
  });

  const rows = historyQuery.data?.items ?? [];
  const meta = historyQuery.data?.meta;
  const totals = historyQuery.data?.totals;

  // Out-of-range page (rows vanished under us — a delete, or another session's
  // print landing): clamp to the last real page rather than showing nothing.
  if (meta && perPage !== -1 && effectivePage > meta.last_page) {
    setPage(meta.last_page);
  }

  const deleteMutation = useMutation({
    mutationFn: ({ spoolId, usageId }: { spoolId: number; usageId: number }) =>
      api.deleteSpoolUsageRecord(spoolId, usageId),
    onSuccess: () => {
      // Deleting a row returns its weight to the spool, so this moves the
      // inventory and everything computed off it, not just this list.
      queryClient.invalidateQueries({ queryKey: ['usage-history'] });
      queryClient.invalidateQueries({ queryKey: ['spool-usage'] });
      void invalidateSpoolAndLocationQueries(queryClient, ['inventory-spools']);
      showToast(t('inventory.usageRecordDeleted'), 'success');
    },
    onError: () => showToast(t('common.error'), 'error'),
  });

  const toggleSort = (key: SortKey) => {
    const nextDir: SortDir = sortKey === key ? (sortDir === 'asc' ? 'desc' : 'asc') : key === 'created_at' ? 'desc' : 'asc';
    setSortKey(key);
    setSortDir(nextDir);
    try {
      localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify({ key, dir: nextDir }));
    } catch { /* ignore */ }
  };

  const toggleStatus = (status: string) => {
    setStatuses((prev) => (prev.includes(status) ? prev.filter((s) => s !== status) : [...prev, status]));
  };

  const hasFilters =
    search !== '' ||
    statuses.length > 0 ||
    printerId !== '' ||
    material !== '' ||
    brand !== '' ||
    archived !== 'all' ||
    assigned !== 'all' ||
    dateFrom !== '' ||
    dateTo !== '';

  const clearFilters = () => {
    setStatuses([]);
    setPrinterId('');
    setMaterial('');
    setBrand('');
    setArchived('all');
    setAssigned('all');
    setDateFrom('');
    setDateTo('');
    onClearSearch?.();
  };

  // The segmented-chip look the spool table uses, so the two rows are one
  // control vocabulary rather than two that resemble each other.
  const segment = (active: boolean, tone: 'green' | 'amber' = 'green') =>
    `flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
      active
        ? tone === 'amber'
          ? 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400'
          : 'bg-bambu-green/20 text-bambu-green'
        : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
    }`;

  const statusLabel = (status: string) =>
    STATUS_LABEL_KEYS[status] ? t(STATUS_LABEL_KEYS[status]) : status;

  /**
   * A retired printer reads as "Printer 5 (Archived)" here exactly as it does in
   * stats, archives and the calendar — its name may have been reused or gone
   * misleading since it burned this filament, and a ledger is precisely where
   * that matters. `printerLabel` is the single place that rule lives.
   */
  const labelForPrinter = (id: number, name: string | null, archived: boolean) =>
    printerLabel({ id, name: name ?? '', archived }, id, t);

  // Archived sink to the bottom of the dropdown, both groups A→Z by label —
  // the shared comparator, not a second ordering that would drift from it.
  const printerOptions = useMemo(
    () => [...(facets?.printers ?? [])]
      .map((p) => ({ id: p.id, name: p.name ?? '', archived: p.archived }))
      .sort((a, b) => comparePrinterLike(a, b, t)),
    [facets, t],
  );

  const spoolLabel = (row: SpoolUsageListItem) => {
    if (!row.spool) return t('inventory.usageView.deletedSpool');
    // The catalog resolves a colour the spool never named, exactly as the list
    // does — the template then interpolates the resolved one.
    const colorName = resolveSpoolColorName(row.spool.color_name, row.spool.rgba);
    const name = formatSpoolDisplayName({ ...row.spool, color_name: colorName }, spoolDisplayTemplate);
    // A template that renders to nothing for this spool still has to leave
    // something clickable — the id is the one field always there.
    return name || `#${row.spool.id}`;
  };

  // A plain function, deliberately NOT a component declared in this body: a
  // component type recreated on every render remounts its whole subtree, which
  // here means the sort button loses focus the moment anything above it changes.
  const sortHeader = (column: SortKey, label: string, align: 'left' | 'right' = 'left') => (
    // Geometry, weight and colour states copied from the spool table's header
    // (InventoryPage) so the two tables read as one family: py-3 px-4, xs
    // uppercase, green when it is the active sort, and a dimmed idle arrow on
    // every sortable column rather than none. The <button> stays — unlike the
    // spool table's clickable <th>, it is reachable from the keyboard.
    <th
      key={column}
      className={`py-3 px-4 text-xs font-medium uppercase tracking-wide select-none ${
        align === 'right' ? 'text-right' : 'text-left'
      } ${sortKey === column ? 'text-bambu-green' : 'text-bambu-gray'}`}
    >
      <button
        type="button"
        onClick={() => toggleSort(column)}
        className="inline-flex items-center gap-1 hover:text-bambu-green transition-colors"
      >
        {label}
        {sortKey === column ? (
          sortDir === 'asc' ? <ArrowUp className="w-3 h-3" /> : <ArrowDown className="w-3 h-3" />
        ) : (
          <ArrowUpDown className="w-3 h-3 opacity-30" />
        )}
      </button>
    </th>
  );

  return (
    <div className="space-y-4">
      {/* Filter chips row — segmented groups, chip dropdowns, hairline
          separators and a trailing count, exactly as the page's own row does
          it, and standing in the same place. */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Status chips, built from the FACETS: a status this farm has never
            recorded is not offered, and one added in a future release appears
            here without a frontend change. */}
        {(facets?.statuses ?? []).length > 0 && (
          <>
            <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
              {(facets?.statuses ?? []).map((status) => (
                <button
                  key={status}
                  onClick={() => toggleStatus(status)}
                  title={STATUS_HINT_KEYS[status] ? t(STATUS_HINT_KEYS[status]) : undefined}
                  className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                    statuses.includes(status)
                      ? 'bg-bambu-green/20 text-bambu-green'
                      : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
                  }`}
                >
                  {statusLabel(status)}
                </button>
              ))}
            </div>
            <div className="w-px h-5 bg-bambu-dark-tertiary" />
          </>
        )}

        {/* The spool's state. ⚠️ "All" leads and is the default — the one way
            this differs from the spool table, where a tab is always one or the
            other. Nothing about a burned gram stops being true when the reel is
            retired, so nothing is hidden until asked. */}
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button onClick={() => setArchived('all')} className={segment(archived === 'all')}>
            {t('inventory.all')}
          </button>
          <button onClick={() => setArchived('active')} className={segment(archived === 'active')}>
            <Package className="w-3.5 h-3.5" />
            {t('inventory.active')}
          </button>
          <button onClick={() => setArchived('archived')} className={segment(archived === 'archived')}>
            <Archive className="w-3.5 h-3.5" />
            {t('inventory.archived')}
          </button>
        </div>

        {/* Loaded in a printer, or on the shelf — the same three states the
            spool table offers, and the same amber for "on the shelf". */}
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button onClick={() => setAssigned('all')} className={segment(assigned === 'all')}>
            {t('inventory.all')}
          </button>
          <button
            onClick={() => setAssigned('assigned')}
            title={t('inventory.inPrinterHint')}
            className={segment(assigned === 'assigned')}
          >
            {t('inventory.inPrinter')}
          </button>
          <button
            onClick={() => setAssigned('unassigned')}
            title={t('inventory.onShelfHint')}
            className={segment(assigned === 'unassigned', 'amber')}
          >
            {t('inventory.onShelf')}
          </button>
        </div>

        <div className="w-px h-5 bg-bambu-dark-tertiary" />

        <select
          value={printerId}
          onChange={(e) => setPrinterId(e.target.value)}
          className={chipClass(printerId !== '')}
        >
          <option value="">{t('inventory.usageView.colPrinter')}</option>
          <option value="__none__">{t('inventory.usageView.noPrinter')}</option>
          {printerOptions.map((printer) => (
            <option key={printer.id} value={String(printer.id)}>
              {labelForPrinter(printer.id, printer.name, printer.archived)}
            </option>
          ))}
        </select>

        <select
          value={material}
          onChange={(e) => setMaterial(e.target.value)}
          className={chipClass(material !== '')}
        >
          <option value="">{t('inventory.material')}</option>
          {(facets?.materials ?? []).map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>

        <select
          value={brand}
          onChange={(e) => setBrand(e.target.value)}
          className={chipClass(brand !== '')}
        >
          <option value="">{t('inventory.brand')}</option>
          {(facets?.brands ?? []).map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>

        <div className="w-px h-5 bg-bambu-dark-tertiary" />

        <input
          type="date"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          aria-label={t('inventory.usageView.dateFrom')}
          title={t('inventory.usageView.dateFrom')}
          className={chipClass(dateFrom !== '')}
        />
        <span className="text-bambu-gray text-xs">—</span>
        <input
          type="date"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          aria-label={t('inventory.usageView.dateTo')}
          title={t('inventory.usageView.dateTo')}
          className={chipClass(dateTo !== '')}
        />

        {hasFilters && (
          <button
            onClick={clearFilters}
            className="flex items-center gap-1 text-xs text-bambu-gray hover:text-bambu-green transition-colors"
          >
            <X className="w-3.5 h-3.5" />
            {t('inventory.clearFilters')}
          </button>
        )}

        {/* Where the page puts its results count, for the same reason.
            ⚠️ The total is for the WHOLE filter, not the page on screen — that
            is the number this view exists to show. */}
        {totals && (
          <span className="ml-auto text-xs text-bambu-gray">
            {meta?.total ?? 0} {t('inventory.usageView.rows')}
            {' · '}
            {t('inventory.usageView.totals', { weight: totals.weight_used.toFixed(1) })}
            {totals.cost !== null && ` · ${currency}${totals.cost.toFixed(2)}`}
          </span>
        )}
      </div>

      {historyQuery.isLoading ? (
        <div className="flex justify-center py-10">
          <Loader2 className="w-6 h-6 animate-spin text-bambu-green" />
        </div>
      ) : rows.length === 0 ? (
        <div className="text-center py-12 text-bambu-gray text-sm bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg">
          <Clock className="w-6 h-6 mx-auto mb-2 opacity-50" />
          {hasFilters ? t('inventory.usageView.emptyFiltered') : t('inventory.noUsageHistory')}
        </div>
      ) : (
        <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/30">
                  {sortHeader('created_at', t('inventory.usageView.colDate'))}
                  {sortHeader('spool', t('inventory.usageView.colSpool'))}
                  {sortHeader('print_name', t('inventory.usageView.colPrint'))}
                  {sortHeader('printer', t('inventory.usageView.colPrinter'))}
                  {sortHeader('weight_used', t('inventory.usageView.colWeight'), 'right')}
                  {sortHeader('percent_used', '%', 'right')}
                  {sortHeader('cost', t('inventory.usageView.colCost'), 'right')}
                  {sortHeader('status', t('inventory.usageView.colStatus'))}
                  <th className="py-3 px-4" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="group border-b border-bambu-dark-tertiary/50 last:border-0 hover:bg-bambu-dark-tertiary/30">
                    <td className="py-3 px-4 text-bambu-gray whitespace-nowrap">
                      {formatDateTime(row.created_at, timeFormat, dateFormat)}
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2 min-w-0">
                        <span
                          className="w-3 h-3 rounded-full border border-black/20 flex-shrink-0"
                          style={getSwatchStyle(row.spool?.rgba ?? null)}
                        />
                        {onOpenSpool ? (
                          <button
                            type="button"
                            onClick={() => onOpenSpool(row.spool_id)}
                            className="text-white hover:text-bambu-green transition-colors truncate text-left"
                          >
                            {spoolLabel(row)}
                          </button>
                        ) : (
                          <span className="text-white truncate">{spoolLabel(row)}</span>
                        )}
                        {row.spool?.archived && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-bambu-dark-tertiary text-bambu-gray flex-shrink-0">
                            {t('inventory.usageView.archivedSpool')}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-3 px-4 text-white max-w-[18rem]">
                      <span className="block truncate" title={row.print_name ?? undefined}>{row.print_name || '—'}</span>
                    </td>
                    <td className="py-3 px-4 text-bambu-gray whitespace-nowrap">
                      {row.printer_id !== null
                        ? labelForPrinter(row.printer_id, row.printer_name, row.printer_archived)
                        : '—'}
                    </td>
                    <td className="py-3 px-4 text-right text-white font-medium whitespace-nowrap">{row.weight_used.toFixed(1)}g</td>
                    <td className="py-3 px-4 text-right text-bambu-gray whitespace-nowrap">{row.percent_used}%</td>
                    <td className="py-3 px-4 text-right text-bambu-gray whitespace-nowrap">
                      {row.cost !== null ? `${currency}${row.cost.toFixed(2)}` : '—'}
                    </td>
                    <td className="py-3 px-4 whitespace-nowrap">
                      <span
                        className={STATUS_COLORS[row.status] || 'text-bambu-gray'}
                        title={STATUS_HINT_KEYS[row.status] ? t(STATUS_HINT_KEYS[row.status]) : undefined}
                      >
                        {statusLabel(row.status)}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-right">
                      <button
                        type="button"
                        onClick={() => deleteMutation.mutate({ spoolId: row.spool_id, usageId: row.id })}
                        disabled={deleteMutation.isPending}
                        title={t('inventory.deleteUsageRecord')}
                        aria-label={t('inventory.deleteUsageRecord')}
                        className="text-bambu-gray/40 hover:text-red-600 dark:hover:text-red-400 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity disabled:opacity-30"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <PaginationBar
            page={meta?.current_page ?? 1}
            totalPages={meta?.last_page ?? 1}
            perPage={perPage}
            total={meta?.total ?? 0}
            onPageChange={setPage}
            onPerPageChange={(size) => { setPerPage(size); setPage(1); }}
            items={t('inventory.usageView.rows')}
            perPageOptions={PER_PAGE_OPTIONS}
          />
        </div>
      )}
    </div>
  );
}
