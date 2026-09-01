import { useState, useMemo, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle, TrendingDown, ShoppingCart, Check, BellOff,
  ChevronDown, ChevronUp, Info, Edit2, X, Lock,
  ArrowUp, ArrowDown, ArrowUpDown, Package, Trash2, BarChart2,
  CreditCard, PackageCheck, Download, RotateCcw,
} from 'lucide-react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, Legend,
} from 'recharts';
import { api } from '../api/client';
import { getSwatchStyle } from '../utils/colors';
import type {
  SkuForecastRow,
  ForecastListParams,
  ForecastChartSeriesEntry,
  ForecastLogisticsRow,
  FilamentSkuSettings,
  ShoppingListItem,
  SpoolListItem,
} from '../api/client';
import { invalidateForecastQueries } from '../utils/inventoryQueries';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { LoadingBlock } from './LoadingBlock';
import { PaginationBar } from './PaginationBar';

// ── Types ─────────────────────────────────────────────────────────────────────
//
// The panel is a RENDERER (2026-08-29 forecast-server-side, task 4): every
// forecast number — rates, safety stock, ROP, dates, alerts, chart series,
// logistics timelines, the CSV — comes from the server's forecast_engine.
// What remains client-side is presentation arithmetic over served numbers
// (progress-bar %, the add-to-cart duration suggestion, the margin editor's
// ≈g hint) — the spec's explicit carve-out.

type MarginUnit = 'days' | 'g' | 'kg';
type SortKey = 'material' | 'spools' | 'used' | 'days_left' | 'stock' | 'empty_by' | 'reorder_by';
type SpoolSortKey = 'id' | 'remaining' | 'used' | 'label';
type SortDir = 'asc' | 'desc';
type ChartDays = 7 | 30 | 180;

// ── Constants ─────────────────────────────────────────────────────────────────

const CHART_COLORS = ['#1DB954', '#3B82F6', '#F59E0B', '#EF4444', '#8B5CF6'];

// Sort preferences survive reloads, same pattern as the inventory table.
const FORECAST_SORT_KEY = 'bamdude-forecast-sort';
const FORECAST_SPOOL_SORT_KEY = 'bamdude-forecast-spool-sort';

// The server's sort-key set (GET /inventory/forecast). ⚠️ An unknown sort_by
// is a REAL 400 over there — every persisted value passes sanitizeSort()
// below BEFORE it can reach a request.
const SORT_KEYS: readonly SortKey[] = [
  'material', 'spools', 'used', 'days_left', 'stock', 'empty_by', 'reorder_by',
];

// The server caps per_page at 200; -1 is PaginationBar's "all" → `all=true`.
const PER_PAGE_OPTIONS = [25, 50, 100, 200];

function loadSort(storageKey: string): unknown {
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored) return JSON.parse(stored);
  } catch { /* ignore */ }
  return null;
}

function saveSort(storageKey: string, state: unknown) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(state));
  } catch { /* ignore */ }
}

/**
 * Sanitize a persisted sort to the server key set — a legacy or hand-edited
 * localStorage value must NEVER 400 the page (the server-driven-lists
 * lesson): anything unknown collapses to the client's historical default,
 * material ascending.
 */
function sanitizeSort(stored: unknown): { key: SortKey; dir: SortDir } {
  const fallback: { key: SortKey; dir: SortDir } = { key: 'material', dir: 'asc' };
  if (!stored || typeof stored !== 'object') return fallback;
  const { key, dir } = stored as { key?: unknown; dir?: unknown };
  if (typeof key !== 'string' || !(SORT_KEYS as readonly string[]).includes(key)) return fallback;
  return { key: key as SortKey, dir: dir === 'desc' ? 'desc' : 'asc' };
}

// ── Pure helpers ──────────────────────────────────────────────────────────────

/**
 * The grams a safety margin is worth — the EDITOR's live ≈g hint only (the
 * engine computes the real thing server-side with the same rule). 'days'
 * converts through the served rate (5 g/day placeholder when no rate exists
 * yet); 'g' and 'kg' are fixed weights.
 */
function marginGrams(value: number, unit: MarginUnit, dailyRateG: number | null): number {
  if (unit === 'g') return value;
  if (unit === 'kg') return value * 1000;
  return dailyRateG !== null ? dailyRateG * value : value * 5;
}

/** The engine's collapsed SKU key (NULL and '' share a group) — used only to
 *  LOOK UP served rows and settings rows, never to group anything. */
function skuKey(
  material: string | null,
  subtype: string | null,
  brand: string | null,
  colorName: string | null,
) {
  return `${material ?? ''}||${subtype ?? ''}||${brand ?? ''}||${colorName ?? ''}`;
}

function rowLabel(r: {
  material: string | null;
  subtype: string | null;
  brand: string | null;
  color_name: string | null;
}) {
  return [r.brand, r.material, r.subtype, r.color_name].filter(Boolean).join(' ');
}

function addDays(date: Date, days: number): Date {
  const d = new Date(date);
  d.setDate(d.getDate() + Math.round(days));
  return d;
}

/**
 * A served "YYYY-MM-DD" (UTC calendar day, spec §2.1) anchored at LOCAL
 * midnight so toLocaleDateString renders exactly the served calendar date in
 * every timezone — a bare `new Date(iso)` would parse UTC midnight and roll
 * back a day west of Greenwich.
 */
function servedDate(iso: string): Date {
  return new Date(`${iso}T00:00:00`);
}

function formatDate(date: Date): string {
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatDateShort(date: Date): string {
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ── Main component ────────────────────────────────────────────────────────────

export function ForecastPanel() {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { t } = useTranslation();
  const { hasPermission, hasAnyPermission } = useAuth();

  const canRead = hasPermission('inventory:forecast_read');
  const canWrite = hasAnyPermission('inventory:forecast_write', 'inventory:update');

  // All hooks must run unconditionally — guard render is deferred until after hooks
  const [alertsOpen, setAlertsOpen] = useState(false);
  // ONE read of the persisted sort: the two halves share a single stored
  // object, so a per-initializer sanitizeSort(loadSort(…)) meant two
  // localStorage round-trips and two JSON.parses for one value.
  const [initialSort] = useState(() => sanitizeSort(loadSort(FORECAST_SORT_KEY)));
  const [sortKey, setSortKey] = useState<SortKey>(initialSort.key);
  const [sortDir, setSortDir] = useState<SortDir>(initialSort.dir);
  const [materialFilter, setMaterialFilter] = useState('');
  const [brandFilter, setBrandFilter] = useState('');
  const [cartModal, setCartModal] = useState<SkuForecastRow | null>(null);
  const [listOpen, setListOpen] = useState(false);
  const [chartDays, setChartDays] = useState<ChartDays>(30);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(50);

  // Any filter/sort change invalidates the current page — adjusted right here
  // during render (the FileManagerPage pageResetSignature pattern): a
  // useEffect would land one render late and let a stale-page request fire
  // first with {newFilter, oldPage}. No selection exists in this panel, so
  // the reset moves only the page.
  const pageResetSignature = JSON.stringify([materialFilter, brandFilter, sortKey, sortDir]);
  const [prevPageResetSignature, setPrevPageResetSignature] = useState(pageResetSignature);
  let effectivePage = page;
  if (pageResetSignature !== prevPageResetSignature) {
    setPrevPageResetSignature(pageResetSignature);
    setPage(1);
    effectivePage = 1;
  }

  // ── The table's ONE feed: server-sorted, server-filtered, server-paged.
  // The four heavy queries this replaces (the all=true spool feed, the
  // 5000-row usage feed, and the two math memos over them) are gone — the
  // server computes, this panel renders.
  const forecastParams = useMemo((): ForecastListParams => ({
    sort_by: `${sortKey}_${sortDir}`,
    ...(materialFilter ? { material: materialFilter } : {}),
    ...(brandFilter ? { brand: brandFilter } : {}),
    page: effectivePage,
    ...(perPage === -1 ? { all: true } : { per_page: perPage }),
  }), [sortKey, sortDir, materialFilter, brandFilter, effectivePage, perPage]);

  const forecastQuery = useQuery({
    queryKey: ['inventory-forecast', forecastParams],
    queryFn: () => api.getForecast(forecastParams),
    enabled: canRead,
    placeholderData: (prev) => prev,
  });

  const rows = forecastQuery.data?.items ?? [];
  const meta = forecastQuery.data?.meta;
  const alertCount = forecastQuery.data?.alert_count ?? 0;
  const globalLeadTime = forecastQuery.data?.global_lead_time_days ?? 0;

  // Out-of-range page (rows vanished under us): clamp to the last real page.
  // Render-phase like the signature reset; the inequality guard settles it.
  if (meta && perPage !== -1 && effectivePage > meta.last_page) {
    setPage(meta.last_page);
  }

  // The alert BANNERS need the alert rows themselves, farm-wide — the paged
  // table slice cannot supply them. alerts_only=true&all=true is the intended
  // feed (T3 contract); it fires only while the badge shows a non-zero count.
  const alertsQuery = useQuery({
    queryKey: ['inventory-forecast', 'alerts'],
    queryFn: () => api.getForecast({ alerts_only: true, all: true }),
    enabled: canRead && alertCount > 0,
  });
  const alertRows = alertsQuery.data?.items ?? [];

  // The shopping-list panel joins its items to farm-wide forecast rows (avg
  // spool weight, lead times, break badges) — all=true over tens of finished
  // SKU rows, fetched only while the panel is open.
  const cartRowsQuery = useQuery({
    queryKey: ['inventory-forecast', 'cart-rows'],
    queryFn: () => api.getForecast({ all: true }),
    enabled: canRead && listOpen,
  });

  const chartQuery = useQuery({
    queryKey: ['inventory-forecast-chart', chartDays],
    queryFn: () => api.getForecastChart(chartDays),
    enabled: canRead,
  });
  const chartSeries = chartQuery.data?.series ?? [];

  const logisticsQuery = useQuery({
    queryKey: ['inventory-forecast-logistics'],
    queryFn: () => api.getForecastLogistics(),
    enabled: canRead && listOpen,
  });

  const { data: skuSettingsList = [] } = useQuery({ queryKey: ['sku-settings'], queryFn: api.getSkuSettings, staleTime: 60_000, enabled: canRead });
  const { data: shoppingList = [] } = useQuery({ queryKey: ['shopping-list'], queryFn: api.getShoppingList, staleTime: 30_000, enabled: canRead });

  // Filter dropdown options — the facets endpoint, spanning BOTH tabs on
  // purpose: archived-only SKUs stay forecast rows for 90 days, so an
  // active-only facet list could not name them. A superset option is
  // harmless (a filter matching nothing yields an empty page, same as the
  // inventory page's stale-name case).
  const { data: facets } = useQuery({
    queryKey: ['inventory-spools', 'facets', 'all'],
    queryFn: () => api.getSpoolFacets(),
    enabled: canRead,
  });
  const uniqueMaterials = facets?.materials ?? [];
  const uniqueBrands = facets?.brands ?? [];

  // The settings EDITORS need the raw per-SKU rows (lead override, margin
  // value/unit) — a CRUD surface the forecast row deliberately doesn't carry.
  // Resolution mirrors the engine: exact key first, then the colourless
  // fallback row pre-colour-grouping users still have.
  const settingsMap = useMemo(() => {
    const m = new Map<string, FilamentSkuSettings>();
    for (const s of skuSettingsList) m.set(skuKey(s.material, s.subtype, s.brand, s.color_name), s);
    return m;
  }, [skuSettingsList]);

  const resolveSkuSettings = (
    material: string | null, subtype: string | null, brand: string | null, colorName: string | null,
  ): FilamentSkuSettings | null =>
    settingsMap.get(skuKey(material, subtype, brand, colorName)) ??
    (colorName !== null ? settingsMap.get(skuKey(material, subtype, brand, null)) ?? null : null);

  // ── Read permission guard — all hooks above this point ──────────────────────
  if (!canRead) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-bambu-gray gap-3">
        <Lock className="w-8 h-8 opacity-40" />
        <p className="text-sm">{t('forecast.noReadAccess')}</p>
      </div>
    );
  }

  function handleSort(key: SortKey) {
    // Dates and days-left start soonest-first; quantities start largest-first.
    const dir: SortDir = sortKey === key
      ? (sortDir === 'asc' ? 'desc' : 'asc')
      : (['days_left', 'empty_by', 'reorder_by', 'material'].includes(key) ? 'asc' : 'desc');
    setSortKey(key);
    setSortDir(dir);
    saveSort(FORECAST_SORT_KEY, { key, dir });
  }

  const shoppingListBadge = shoppingList.length > 0 ? shoppingList.length : null;
  const hasBreakAlert = alertRows.some((r) => r.stock_break_alert);

  return (
    <div className="space-y-5">

      {/* ── Toolbar ── */}
      <div className="flex flex-wrap items-center gap-3">
        {/* Alert button — count is the served farm-wide alert_count, never the page */}
        {alertCount > 0 && (
          <button
            onClick={() => setAlertsOpen((o) => !o)}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm font-medium transition-colors ${
              hasBreakAlert
                ? 'bg-red-100 dark:bg-red-500/15 border-red-300 dark:border-red-500/30 text-red-700 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-500/25'
                : 'bg-yellow-100 dark:bg-yellow-500/15 border-yellow-300 dark:border-yellow-500/30 text-yellow-700 dark:text-yellow-300 hover:bg-yellow-200 dark:hover:bg-yellow-500/25'
            }`}
          >
            <AlertTriangle className="w-4 h-4" />
            {t('forecast.alertCount', { count: alertCount })}
            {alertsOpen ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          </button>
        )}

        {/* Global lead time */}
        {canWrite && (
          <GlobalLeadTimeSetting
            value={globalLeadTime}
            onSave={(v) => {
              // The editor closes synchronously on click, so a rejected PUT
              // would otherwise revert the number with no explanation (plus
              // an unhandled rejection) — the dismissable-failure rule.
              api.updateSettings({ forecast_global_lead_time_days: v })
                .then(() => {
                  queryClient.invalidateQueries({ queryKey: ['settings'] });
                  invalidateForecastQueries(queryClient);
                  showToast(t('forecast.globalLeadTimeSaved'), 'success');
                })
                .catch(() => showToast(t('forecast.failedSaveSettings'), 'error'));
            }}
          />
        )}

        {/* Material filter */}
        <select
          value={materialFilter}
          onChange={(e) => setMaterialFilter(e.target.value)}
          className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
            materialFilter
              ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
              : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
          }`}
        >
          <option value="">{t('inventory.material')}</option>
          {uniqueMaterials.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>

        {/* Brand filter */}
        <select
          value={brandFilter}
          onChange={(e) => setBrandFilter(e.target.value)}
          className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
            brandFilter
              ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
              : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
          }`}
        >
          <option value="">{t('inventory.brand')}</option>
          {uniqueBrands.map((b) => <option key={b} value={b}>{b}</option>)}
        </select>

        {/* Shopping list toggle */}
        <button
          onClick={() => setListOpen((o) => !o)}
          className="relative flex items-center gap-2 px-3 py-1.5 rounded-lg border border-bambu-dark-tertiary text-bambu-gray hover:bg-bambu-dark-tertiary text-sm transition-colors ml-auto"
        >
          <ShoppingCart className="w-4 h-4" />
          <span className="hidden sm:inline">{t('forecast.shoppingList')}</span>
          {shoppingListBadge && (
            <span className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-bambu-green text-white text-[10px] font-bold flex items-center justify-center">
              {shoppingListBadge}
            </span>
          )}
        </button>
      </div>

      {/* ── Collapsed alerts panel — served alert rows, farm-wide ──
          ⚠️ alertCount gates the RENDER, not just the toggle above. When the
          last alert clears (a snooze, or stock moving), alertsQuery goes
          disabled — and TanStack never refetches a disabled query, not even
          on focus — so alertRows keeps serving the pre-clear rows. Without
          this guard the banner panel would keep asserting an alert that no
          longer exists, with its collapse button already unmounted. */}
      {alertsOpen && alertCount > 0 && alertRows.length > 0 && (
        <div className="space-y-2">
          {alertRows.map((r) => (
            <AlertBanner
              key={skuKey(r.material, r.subtype, r.brand, r.color_name)}
              row={r}
              onCart={() => setCartModal(r)}
            />
          ))}
        </div>
      )}

      {/* ── Shopping list panel ── */}
      {listOpen && (
        <ShoppingListPanel
          items={shoppingList}
          rows={cartRowsQuery.data?.items ?? []}
          // Both feeds start on the click that OPENS this panel, so "not
          // answered yet" is a state the user can reach and act inside —
          // see the disabled Mark-received button and the logistics
          // loading block, both of which read these.
          rowsPending={cartRowsQuery.isPending}
          logistics={logisticsQuery.data ?? []}
          logisticsPending={logisticsQuery.isPending}
          globalLeadTime={globalLeadTime}
          resolveSkuSettings={resolveSkuSettings}
          canWrite={canWrite}
          onClose={() => setListOpen(false)}
          onRemove={(id) => {
            api.removeFromShoppingList(id)
              .then(() => {
                queryClient.invalidateQueries({ queryKey: ['shopping-list'] });
                queryClient.invalidateQueries({ queryKey: ['inventory-forecast-logistics'] });
              })
              .catch(() => showToast(t('forecast.failedSaveSettings'), 'error'));
          }}
          onClear={() => {
            api.clearShoppingList()
              .then(() => {
                queryClient.invalidateQueries({ queryKey: ['shopping-list'] });
                queryClient.invalidateQueries({ queryKey: ['inventory-forecast-logistics'] });
              })
              .catch(() => showToast(t('forecast.failedSaveSettings'), 'error'));
          }}
        />
      )}

      {/* ── Usage + projection chart — the served top-5 series ── */}
      {chartSeries.length > 0 && (
        <UsageChart series={chartSeries} days={chartDays} onDaysChange={setChartDays} />
      )}

      {/* ── Table ── */}
      {forecastQuery.isLoading ? (
        <LoadingBlock label={t('common.loading')} />
      ) : rows.length === 0 && !materialFilter && !brandFilter ? (
        <div className="flex flex-col items-center justify-center py-16 text-bambu-gray">
          <TrendingDown className="w-10 h-10 mb-3 opacity-40" />
          <p className="text-sm">{t('forecast.noSpools')}</p>
        </div>
      ) : (
        <div className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/30">
                  {/* Color dot */}
                  <th className="w-8 px-4 py-3" />
                  <SortableTh col="material" active={sortKey} dir={sortDir} onSort={handleSort}>
                    {t('forecast.sku')}
                  </SortableTh>
                  <SortableTh col="spools" active={sortKey} dir={sortDir} onSort={handleSort}>
                    {t('forecast.spools')}
                  </SortableTh>
                  <SortableTh col="stock" active={sortKey} dir={sortDir} onSort={handleSort}>
                    {t('forecast.stock')}
                  </SortableTh>
                  <SortableTh col="used" active={sortKey} dir={sortDir} onSort={handleSort}>
                    {t('forecast.dailyRate')}
                  </SortableTh>
                  <SortableTh col="days_left" active={sortKey} dir={sortDir} onSort={handleSort}>
                    {t('forecast.daysLeft')}
                  </SortableTh>
                  <SortableTh col="empty_by" active={sortKey} dir={sortDir} onSort={handleSort}>
                    {t('forecast.emptyBy')}
                  </SortableTh>
                  <SortableTh col="reorder_by" active={sortKey} dir={sortDir} onSort={handleSort}>
                    {t('forecast.reorderBy')}
                  </SortableTh>
                  {/* Actions */}
                  <th className="w-24 px-4 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-bambu-dark-tertiary">
                {rows.map((r) => (
                  <ForecastRow
                    key={skuKey(r.material, r.subtype, r.brand, r.color_name)}
                    row={r}
                    settings={resolveSkuSettings(r.material, r.subtype, r.brand, r.color_name)}
                    globalLeadTime={globalLeadTime}
                    canWrite={canWrite}
                    onSaved={() => {
                      queryClient.invalidateQueries({ queryKey: ['sku-settings'] });
                      invalidateForecastQueries(queryClient);
                    }}
                    onCart={() => setCartModal(r)}
                    showToast={showToast}
                  />
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination — counts from the served meta, archives-style */}
          {meta && (
            <PaginationBar
              page={meta.current_page}
              totalPages={meta.last_page}
              perPage={perPage}
              total={meta.total}
              items={t('forecast.skus')}
              variant="card"
              perPageOptions={PER_PAGE_OPTIONS}
              onPageChange={(p) => setPage(p)}
              onPerPageChange={(size) => { setPerPage(size); setPage(1); }}
            />
          )}

          {/* Legend */}
          <div className="flex flex-wrap items-center gap-4 px-4 py-3 text-xs text-bambu-gray border-t border-bambu-dark-tertiary bg-bambu-dark-tertiary/20">
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-bambu-green inline-block" />
              {t('forecast.trendLegend')}
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-blue-400 inline-block" />
              {t('forecast.estimatedLegend')}
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-bambu-gray/40 inline-block" />
              {t('forecast.noDataLegend')}
            </span>
          </div>
        </div>
      )}

      {/* ── Add to cart modal ── */}
      {cartModal && (
        <AddToCartModal
          row={cartModal}
          onClose={() => setCartModal(null)}
          onAdd={(item) => {
            api.addToShoppingList(item).then(() => {
              queryClient.invalidateQueries({ queryKey: ['shopping-list'] });
              queryClient.invalidateQueries({ queryKey: ['inventory-forecast-logistics'] });
              showToast(t('forecast.addedToCart'), 'success');
              setCartModal(null);
              setListOpen(true);
            }).catch(() => showToast(t('forecast.failedAddItem'), 'error'));
          }}
        />
      )}
    </div>
  );
}

// ── Sortable th ───────────────────────────────────────────────────────────────

function SortableTh<K extends string>({
  col, active, dir, onSort, children,
}: {
  col: K;
  active: K | null;
  dir: SortDir;
  onSort: (k: K) => void;
  children: React.ReactNode;
}) {
  const isActive = active === col;
  return (
    <th
      className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide cursor-pointer select-none hover:text-white transition-colors"
      onClick={() => onSort(col)}
    >
      <span className="inline-flex items-center">
        {children}
        {isActive
          ? dir === 'asc'
            ? <ArrowUp className="w-3 h-3 ml-1 text-bambu-green" />
            : <ArrowDown className="w-3 h-3 ml-1 text-bambu-green" />
          : <ArrowUpDown className="w-3 h-3 ml-1 opacity-40" />
        }
      </span>
    </th>
  );
}

// ── Alert Banner ──────────────────────────────────────────────────────────────

function AlertBanner({ row: r, onCart }: { row: SkuForecastRow; onCart: () => void }) {
  const { t } = useTranslation();
  const label = rowLabel(r);
  const isBreak = r.stock_break_alert;

  return (
    <div className={`flex items-center gap-3 px-4 py-3 rounded-lg border text-sm ${
      isBreak ? 'bg-red-50 dark:bg-red-500/10 border-red-300 dark:border-red-500/30 text-red-700 dark:text-red-300' : 'bg-yellow-50 dark:bg-yellow-500/10 border-yellow-300 dark:border-yellow-500/30 text-yellow-700 dark:text-yellow-300'
    }`}>
      <AlertTriangle className="w-4 h-4 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="font-medium">{label}</span>
        {isBreak ? (
          <span className="ml-2 text-xs opacity-80">
            {t('forecast.stockBreakRisk')} — {t('forecast.stockBreakDetail', { days: r.days_remaining, lt: r.eff_lead_time_days })}
          </span>
        ) : (
          <span className="ml-2 text-xs opacity-80">
            {t('forecast.reorderNow')} — {t('forecast.reorderTriggerPassed', { date: r.reorder_trigger_date ? formatDate(servedDate(r.reorder_trigger_date)) : '—' })}
          </span>
        )}
      </div>
      <button
        onClick={onCart}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded border border-current text-xs opacity-70 hover:opacity-100 transition-opacity"
      >
        <ShoppingCart className="w-3 h-3" /> {t('forecast.order')}
      </button>
    </div>
  );
}

// ── Usage + Projection Chart ──────────────────────────────────────────────────

const CHART_TIMEFRAMES: { label: string; value: ChartDays }[] = [
  { label: '1W', value: 7 },
  { label: '1M', value: 30 },
  { label: '6M', value: 180 },
];

/**
 * Renders the SERVED top-5 projection series verbatim — dates, gram values
 * and per-series ROP reference lines all come off the wire. The series also
 * carries a day-bucketed `usage` history (new server capability); the shipped
 * chart draws the projection only, so usage stays unrendered for parity.
 */
function UsageChart({ series: served, days: maxDays, onDaysChange }: {
  series: ForecastChartSeriesEntry[];
  days: ChartDays;
  onDaysChange: (d: ChartDays) => void;
}) {
  const { t } = useTranslation();

  const series = served.map((s, idx) => ({
    key: skuKey(s.sku.material, s.sku.subtype, s.sku.brand, s.sku.color_name),
    label: rowLabel({ ...s.sku }),
    color: CHART_COLORS[idx % CHART_COLORS.length],
    rop: s.rop_g,
    byDate: new Map(s.projection.map(([d, g]) => [d, g])),
  }));

  // Served projections are day-consecutive from today and stop at their first
  // zero — the sorted union of dates IS the x-axis, already trimmed.
  const dates = [...new Set(served.flatMap((s) => s.projection.map(([d]) => d)))].sort();
  const chartData = dates.map((d) => {
    const row: Record<string, number | string> = { label: formatDateShort(servedDate(d)) };
    for (const s of series) row[s.key] = s.byDate.get(d) ?? 0;
    return row;
  });

  const ropLines = series.filter((s) => s.rop > 0);

  return (
    <div className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary p-4">
      <div className="flex items-center gap-2 mb-4">
        <TrendingDown className="w-4 h-4 text-bambu-green" />
        <h3 className="text-sm font-semibold text-white">{t('forecast.chartTitle')}</h3>
        <span className="text-xs text-bambu-gray ml-1 hidden sm:inline">{t('forecast.dashedLinesROP')}</span>
        <div className="ml-auto flex items-center bg-bambu-dark-tertiary rounded-lg p-0.5">
          {CHART_TIMEFRAMES.map((tf) => (
            <button
              key={tf.value}
              onClick={() => onDaysChange(tf.value)}
              className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${
                maxDays === tf.value
                  ? 'bg-bambu-dark-secondary text-white shadow'
                  : 'text-bambu-gray hover:text-white'
              }`}
            >
              {tf.label}
            </button>
          ))}
        </div>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <defs>
            {series.map((s) => (
              <linearGradient key={s.key} id={`grad-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={s.color} stopOpacity={0.25} />
                <stop offset="95%" stopColor={s.color} stopOpacity={0.02} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" strokeOpacity={0.5} />
          <XAxis
            dataKey="label"
            tick={{ fill: '#6B7280', fontSize: 10 }}
            interval={Math.max(0, Math.ceil(Math.max(0, chartData.length - 1) / 8) - 1)}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            tick={{ fill: '#6B7280', fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(v: number) => v >= 1000 ? `${(v / 1000).toFixed(1)}kg` : `${v}g`}
            width={48}
          />
          <Tooltip
            content={({ label: dateLabel, payload }) => {
              if (!payload?.length) return null;
              return (
                <div style={{ background: '#1a1a2e', border: '1px solid #374151', borderRadius: 8, fontSize: 12, padding: '8px 12px' }}>
                  <div style={{ color: '#9CA3AF', marginBottom: 6 }}>{dateLabel}</div>
                  {payload.map((p) => {
                    const s = series.find((x) => x.key === String(p.dataKey));
                    if (typeof p.value !== 'number') return null;
                    return (
                      <div key={String(p.dataKey)} style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#E5E7EB', marginBottom: 2 }}>
                        <span style={{ color: s?.color ?? '#9CA3AF', fontSize: 10 }}>●</span>
                        <span>{s?.label ?? String(p.dataKey)}</span>
                        <span style={{ color: '#9CA3AF', marginLeft: 4 }}>{p.value}g</span>
                      </div>
                    );
                  })}
                </div>
              );
            }}
          />
          <Legend
            formatter={(value) => {
              const s = series.find((x) => x.key === value);
              return <span style={{ color: '#9CA3AF', fontSize: 11 }}>{s?.label ?? value}</span>;
            }}
          />
          {series.map((s) => (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              stroke={s.color}
              strokeWidth={2}
              fill={`url(#grad-${s.key})`}
              dot={false}
              activeDot={{ r: 3 }}
            />
          ))}
          {ropLines.map((s) => (
            <ReferenceLine
              key={`rop-${s.key}`}
              y={s.rop}
              stroke={s.color}
              strokeDasharray="4 3"
              strokeOpacity={0.6}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Global lead time setting (compact inline) ─────────────────────────────────

function GlobalLeadTimeSetting({ value, onSave }: { value: number; onSave: (v: number) => void }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [input, setInput] = useState(String(value));

  function save() {
    const v = parseInt(input, 10);
    if (isNaN(v) || v < 0) return;
    onSave(v);
    setEditing(false);
  }

  return (
    <div className="flex items-center gap-2 px-3 py-1.5 bg-bambu-dark-tertiary/40 rounded-lg border border-bambu-dark-tertiary text-xs text-bambu-gray">
      <Info className="w-3.5 h-3.5 flex-shrink-0" aria-label={t('forecast.globalLeadTimeHint')} />
      <span className="hidden sm:inline">{t('forecast.globalLeadTime')}:</span>
      {editing ? (
        <form className="flex items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <input
            type="number" min={0} max={365}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            className="w-14 px-1.5 py-0.5 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-sm text-white focus:outline-none focus:border-bambu-green"
            autoFocus
          />
          <span className="text-bambu-gray">d</span>
          <button type="submit" className="px-2 py-0.5 bg-bambu-green text-white text-xs rounded hover:bg-bambu-green/80">{t('forecast.save')}</button>
          <button type="button" onClick={() => setEditing(false)} className="text-xs text-bambu-gray hover:text-white">✕</button>
        </form>
      ) : (
        <div className="flex items-center gap-1.5">
          <span className="font-semibold text-white">{value}d</span>
          <button onClick={() => { setInput(String(value)); setEditing(true); }} className="p-0.5 text-bambu-gray hover:text-white rounded transition-colors">
            <Edit2 className="w-3 h-3" />
          </button>
        </div>
      )}
    </div>
  );
}

// ── Forecast Row ──────────────────────────────────────────────────────────────

function ForecastRow({
  row, settings, globalLeadTime, canWrite, onSaved, onCart, showToast,
}: {
  row: SkuForecastRow;
  settings: FilamentSkuSettings | null;
  globalLeadTime: number;
  canWrite: boolean;
  onSaved: () => void;
  onCart: () => void;
  showToast: (msg: string, type: 'success' | 'error') => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  // Nested per-spool table sort; null = the served order. One shared
  // preference for every group — a per-group memory would be noise.
  const [spoolSortKey, setSpoolSortKey] = useState<SpoolSortKey | null>(
    () => (loadSort(FORECAST_SPOOL_SORT_KEY) as { key?: SpoolSortKey | null } | null)?.key ?? null,
  );
  const [spoolSortDir, setSpoolSortDir] = useState<SortDir>(
    () => (loadSort(FORECAST_SPOOL_SORT_KEY) as { dir?: SortDir } | null)?.dir === 'desc' ? 'desc' : 'asc',
  );
  const [editingLead, setEditingLead] = useState(false);
  const [editingMargin, setEditingMargin] = useState(false);
  const [leadInput, setLeadInput] = useState(String(settings?.lead_time_days ?? 0));
  const [marginInput, setMarginInput] = useState(String(settings?.safety_margin_value ?? 14));
  const [marginUnit, setMarginUnit] = useState<MarginUnit>(settings?.safety_margin_unit ?? 'days');

  // Sync inputs when remote settings change and the field is not actively being edited.
  useEffect(() => {
    if (!editingLead) setLeadInput(String(settings?.lead_time_days ?? 0));
  }, [settings?.lead_time_days, editingLead]);
  useEffect(() => {
    if (!editingMargin) {
      setMarginInput(String(settings?.safety_margin_value ?? 14));
      setMarginUnit(settings?.safety_margin_unit ?? 'days');
    }
  }, [settings?.safety_margin_value, settings?.safety_margin_unit, editingMargin]);

  // ── The expanded row's LAZY spool fetch (task 4, step 2). The server
  // filters narrow the transfer (material/brand/colour when expressible;
  // subtype and NULL fields have no filter language), and the row's served
  // spool_ids — the engine's own group membership — do the EXACT narrowing.
  // Keyed under the ['inventory-spools'] prefix so every spool-mutation
  // invalidation refreshes it for free.
  const detailKey = skuKey(row.material, row.subtype, row.brand, row.color_name);
  const detailQuery = useQuery({
    queryKey: ['inventory-spools', 'sku-detail', detailKey],
    queryFn: () => api.getSpoolsPaged({
      ...(row.material ? { material: row.material } : {}),
      ...(row.brand ? { brand: row.brand } : {}),
      ...(row.color_name ? { colors: [row.color_name] } : {}),
      archived: 'active',
      all: true,
    }),
    enabled: expanded && row.spool_ids.length > 1,
  });
  const spoolIdSet = useMemo(() => new Set(row.spool_ids), [row.spool_ids]);
  const groupSpools = useMemo(
    () => (detailQuery.data?.items ?? []).filter((s) => spoolIdSet.has(s.id)),
    [detailQuery.data, spoolIdSet],
  );

  const upsertMutation = useMutation({
    mutationFn: api.upsertSkuSettings,
    onSuccess: () => { onSaved(); showToast(t('forecast.settingsSaved'), 'success'); },
    onError: () => showToast(t('forecast.failedSaveSettings'), 'error'),
  });

  // Served, colourless-fallback included — the engine resolves snooze the
  // same way the settings map does.
  const snoozed = row.alerts_snoozed;

  const label = rowLabel(row);
  // Use getSwatchStyle so a Clear (alpha=00) lead spool renders as a
  // checkerboard rather than collapsing to solid black (#1545). The served
  // rgba is the group's swatch (live spools preferred, archived fallback) —
  // it must not go grey the moment the last spool of the colour is archived.
  const colorStyle = row.rgba ? getSwatchStyle(row.rgba) : { backgroundColor: '#4B5563' };
  const remainPct = row.total_label_g > 0 ? Math.round((row.total_remaining_g / row.total_label_g) * 100) : 0;

  const daysColor = snoozed ? 'text-bambu-gray'
    : row.days_remaining === null ? 'text-bambu-gray'
    : row.stock_break_alert ? 'text-red-700 dark:text-red-400'
    : row.reorder_alert ? 'text-yellow-700 dark:text-yellow-400'
    : row.days_remaining < 30 ? 'text-yellow-700 dark:text-yellow-400'
    : 'text-green-700 dark:text-green-400';

  function upsert(lead: number, marginVal: number, marginUnitArg: MarginUnit, alertsSnoozed = snoozed) {
    upsertMutation.mutate({ material: row.material ?? '', subtype: row.subtype, brand: row.brand, color_name: row.color_name, lead_time_days: lead, safety_margin_value: marginVal, safety_margin_unit: marginUnitArg, alerts_snoozed: alertsSnoozed });
  }

  function toggleSnooze(e: React.MouseEvent) {
    e.stopPropagation();
    upsert(settings?.lead_time_days ?? 0, settings?.safety_margin_value ?? 14, settings?.safety_margin_unit ?? 'days', !snoozed);
  }

  const tierBadge = row.rate_tier === 'history'
    ? <span className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-bambu-green/15 text-bambu-green"><span className="w-1.5 h-1.5 rounded-full bg-bambu-green" />{t('forecast.trend')}</span>
    : row.rate_tier === 'delta'
    ? <span className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-400/15 text-blue-700 dark:text-blue-400"><span className="w-1.5 h-1.5 rounded-full bg-blue-400" />{t('forecast.estimated')}</span>
    : <span className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-bambu-dark-tertiary text-bambu-gray/60"><span className="w-1.5 h-1.5 rounded-full bg-bambu-gray/40" />{t('forecast.noData')}</span>;

  const rowAlertBorder = snoozed ? '' : row.stock_break_alert ? 'bg-red-500/5' : row.reorder_alert ? 'bg-yellow-500/5' : '';

  function handleSpoolSort(key: SpoolSortKey) {
    const dir: SortDir = spoolSortKey === key
      ? (spoolSortDir === 'asc' ? 'desc' : 'asc')
      : (key === 'id' ? 'asc' : 'desc');
    setSpoolSortKey(key);
    setSpoolSortDir(dir);
    saveSort(FORECAST_SPOOL_SORT_KEY, { key, dir });
  }

  const sortedSpools = spoolSortKey === null
    ? groupSpools
    : [...groupSpools].sort((a, b) => {
        let va = 0; let vb = 0;
        switch (spoolSortKey) {
          case 'id': va = a.id; vb = b.id; break;
          case 'remaining':
            va = Math.max(0, a.label_weight - a.weight_used);
            vb = Math.max(0, b.label_weight - b.weight_used);
            break;
          case 'used':
            va = Math.max(0, a.weight_used - (a.weight_used_baseline ?? 0));
            vb = Math.max(0, b.weight_used - (b.weight_used_baseline ?? 0));
            break;
          case 'label': va = a.label_weight; vb = b.label_weight; break;
        }
        const cmp = va < vb ? -1 : va > vb ? 1 : 0;
        return spoolSortDir === 'asc' ? cmp : -cmp;
      });

  return (
    <>
      <tr
        className={`border-b border-bambu-dark-tertiary/50 cursor-pointer hover:bg-bambu-dark-tertiary/30 transition-colors ${rowAlertBorder} ${snoozed ? 'opacity-50' : ''}`}
        onClick={() => setExpanded((e) => !e)}
      >
        {/* Color dot */}
        <td className="px-4 py-3">
          <span
            className="block w-5 h-5 rounded-full border border-black/20"
            style={colorStyle}
          />
        </td>

        {/* SKU */}
        <td className="px-4 py-3">
          <span className="text-sm text-white">{label}</span>
        </td>

        {/* Spools */}
        <td className="px-4 py-3">
          <span className="text-sm text-bambu-gray">{row.total_spools}</span>
        </td>

        {/* Stock */}
        <td className="px-4 py-3 min-w-[140px]">
          <div className="flex items-center gap-2">
            <div className="flex-1 h-2 bg-bambu-dark-tertiary rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full ${remainPct > 50 ? 'bg-bambu-green' : remainPct > 20 ? 'bg-yellow-500' : 'bg-red-500'}`}
                style={{ width: `${Math.min(remainPct, 100)}%` }}
              />
            </div>
            <span className="text-xs text-bambu-gray min-w-[40px] text-right">{Math.round(row.total_remaining_g)}g</span>
          </div>
        </td>

        {/* Rate */}
        <td className="px-4 py-3">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-sm text-white">{row.rate_g_day !== null ? `${row.rate_g_day.toFixed(1)}g/d` : '—'}</span>
            {tierBadge}
          </div>
        </td>

        {/* Days left */}
        <td className="px-4 py-3">
          <span className={`text-sm font-semibold ${daysColor}`}>
            {row.days_remaining !== null ? `${row.days_remaining}d` : <span className="text-bambu-gray font-normal">—</span>}
          </span>
        </td>

        {/* Empty by */}
        <td className="px-4 py-3">
          <span className="text-sm text-bambu-gray">
            {row.projected_empty_date ? formatDate(servedDate(row.projected_empty_date)) : '—'}
          </span>
        </td>

        {/* Reorder by */}
        <td className="px-4 py-3">
          <span className={`text-sm ${!snoozed && row.reorder_alert ? 'text-yellow-700 dark:text-yellow-400' : 'text-bambu-gray'}`}>
            {row.reorder_trigger_date ? formatDate(servedDate(row.reorder_trigger_date)) : '—'}
          </span>
        </td>

        {/* Actions */}
        <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-end gap-1">
            {canWrite && (
              <button
                onClick={onCart}
                className="p-1.5 text-bambu-gray hover:text-bambu-green rounded transition-colors"
                title={t('forecast.addToCart')}
              >
                <ShoppingCart className="w-4 h-4" />
              </button>
            )}
            {!snoozed && (row.stock_break_alert ? (
              <AlertTriangle className="w-4 h-4 text-red-600 dark:text-red-400" aria-label={t('forecast.stockBreakRisk')} />
            ) : row.reorder_alert ? (
              <AlertTriangle className="w-4 h-4 text-yellow-600 dark:text-yellow-400" aria-label={t('forecast.reorderNow')} />
            ) : row.days_remaining !== null ? (
              <Check className="w-4 h-4 text-bambu-green/50" />
            ) : null)}
            {canWrite && (
              <button
                onClick={toggleSnooze}
                className={`p-1 rounded transition-colors ${snoozed ? 'text-amber-600/80 dark:text-amber-400/80 hover:text-amber-700 dark:hover:text-amber-300' : 'text-slate-400 hover:text-white'}`}
                title={t(snoozed ? 'forecast.alertsEnabled' : 'forecast.alertsSnoozed')}
              >
                <BellOff className="w-3.5 h-3.5" />
              </button>
            )}
            <button
              onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
              className="p-1.5 text-bambu-gray hover:text-white rounded transition-colors"
            >
              {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            </button>
          </div>
        </td>
      </tr>

      {/* ── Expanded detail row ── */}
      {expanded && (
        <tr className="bg-bambu-dark-tertiary/10">
          <td colSpan={9} className="px-4 py-4">
            <div className="space-y-3">

              {/* Single compact row: read-only stats + editable settings */}
              <div className={`grid gap-2 ${canWrite ? 'grid-cols-2 sm:grid-cols-4' : 'grid-cols-2'}`}>
                <LogisticStat
                  label={t('forecast.effectiveLeadTime')}
                  value={`${row.eff_lead_time_days}d`}
                  hint={t('forecast.effectiveLeadTimeHint', { global: globalLeadTime, sku: settings?.lead_time_days ?? 0 })}
                />
                <LogisticStat
                  label={t('forecast.reorderPoint')}
                  value={`${Math.round(row.reorder_point_g ?? 0)}g`}
                  hint={t('forecast.reorderPointHint')}
                />
                {canWrite && (
                  <>
                    <SettingField
                      label={t('forecast.skuLeadTimeOverride')}
                      hint={t('forecast.skuLeadTimeHint')}
                      unit={t('forecast.leadTime')}
                      editing={editingLead}
                      value={settings?.lead_time_days ?? 0}
                      inputValue={leadInput}
                      onInputChange={setLeadInput}
                      onEdit={() => { setLeadInput(String(settings?.lead_time_days ?? 0)); setEditingLead(true); }}
                      onSave={() => {
                        const v = parseInt(leadInput, 10);
                        if (!isNaN(v) && v >= 0) { upsert(v, settings?.safety_margin_value ?? 14, marginUnit); setEditingLead(false); }
                      }}
                      onCancel={() => setEditingLead(false)}
                      isPending={upsertMutation.isPending}
                      saveLabel={t('forecast.save')}
                      cancelLabel={t('forecast.cancel')}
                    />
                    <SafetyMarginField
                      value={settings?.safety_margin_value ?? 14}
                      unit={marginUnit}
                      editing={editingMargin}
                      inputValue={marginInput}
                      dailyRateG={row.rate_g_day}
                      onInputChange={setMarginInput}
                      onUnitChange={(u) => setMarginUnit(u)}
                      onEdit={() => { setMarginInput(String(settings?.safety_margin_value ?? 14)); setMarginUnit(settings?.safety_margin_unit ?? 'days'); setEditingMargin(true); }}
                      onSave={() => {
                        const v = parseInt(marginInput, 10);
                        if (!isNaN(v) && v >= 0) { upsert(settings?.lead_time_days ?? 0, v, marginUnit); setEditingMargin(false); }
                      }}
                      onCancel={() => setEditingMargin(false)}
                      isPending={upsertMutation.isPending}
                      saveLabel={t('forecast.save')}
                      cancelLabel={t('forecast.cancel')}
                      safetyMarginLabel={t('forecast.safetyMarginLabel')}
                    />
                  </>
                )}
              </div>

              {/* Individual spools — shown when the group has >1 LIVE spool;
                  fetched lazily on expand (see detailQuery above). */}
              {row.spool_ids.length > 1 && (
                <div className="border-t border-bambu-dark-tertiary pt-3">
                  <p className="text-xs text-bambu-gray mb-2">{t('forecast.individualSpools')}</p>
                  {detailQuery.isLoading ? (
                    <LoadingBlock label={t('common.loading')} />
                  ) : detailQuery.isError ? (
                    /* "Error is not empty": without this the nested table
                       renders its headers over zero rows, which is exactly
                       what a SKU with no spools looks like — a silently dead
                       surface. Say so, and offer the way back. */
                    <div className="flex items-center gap-3 px-3 py-3 rounded-lg border border-red-200 dark:border-red-500/20 bg-red-50 dark:bg-red-500/10">
                      <AlertTriangle className="w-4 h-4 text-red-600 dark:text-red-400 flex-shrink-0" />
                      <span className="text-xs text-red-700 dark:text-red-300">{t('forecast.individualSpoolsFailed')}</span>
                      <button
                        onClick={() => void detailQuery.refetch()}
                        className="ml-auto px-2 py-1 rounded text-xs font-medium border border-red-300 dark:border-red-500/30 text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-500/20 transition-colors"
                      >
                        {t('forecast.retry')}
                      </button>
                    </div>
                  ) : (
                    <div className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary">
                      <table className="w-full">
                        <thead>
                          <tr className="border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/30">
                            <SortableTh col="id" active={spoolSortKey} dir={spoolSortDir} onSort={handleSpoolSort}>#</SortableTh>
                            <SortableTh col="remaining" active={spoolSortKey} dir={spoolSortDir} onSort={handleSpoolSort}>{t('inventory.remaining')}</SortableTh>
                            <SortableTh col="used" active={spoolSortKey} dir={spoolSortDir} onSort={handleSpoolSort}>{t('inventory.used')}</SortableTh>
                            <SortableTh col="label" active={spoolSortKey} dir={spoolSortDir} onSort={handleSpoolSort}>{t('forecast.labelWeight')}</SortableTh>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-bambu-dark-tertiary">
                          {sortedSpools.map((s: SpoolListItem) => {
                            const remaining = Math.max(0, s.label_weight - s.weight_used);
                            const pct = s.label_weight > 0 ? (remaining / s.label_weight) * 100 : 0;
                            return (
                              <tr key={s.id} className="hover:bg-bambu-dark-tertiary/30 transition-colors">
                                <td className="px-4 py-2">
                                  <span className="text-xs font-mono text-bambu-gray/70">#{s.id}</span>
                                </td>
                                <td className="px-4 py-2">
                                  <div className="flex items-center gap-3">
                                    <div className="w-24 h-1.5 bg-bambu-dark-tertiary rounded-full overflow-hidden flex-shrink-0">
                                      <div
                                        className={`h-full rounded-full ${pct > 50 ? 'bg-bambu-green' : pct > 20 ? 'bg-yellow-500' : 'bg-red-500'}`}
                                        style={{ width: `${Math.min(pct, 100)}%` }}
                                      />
                                    </div>
                                    <span className="text-sm text-white">{Math.round(remaining)}g</span>
                                  </div>
                                </td>
                                <td className="px-4 py-2">
                                  {/* Per-spool "consumed" stays consistent with the
                                      dashboard's "Total Consumed" — baseline-aware
                                      so "Reset usage to 0" zeroes this cell too (#1390). */}
                                  <span className="text-sm text-bambu-gray">{Math.round(Math.max(0, s.weight_used - (s.weight_used_baseline ?? 0)))}g</span>
                                </td>
                                <td className="px-4 py-2">
                                  <span className="text-sm text-bambu-gray">{s.label_weight}g</span>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ── Logistic stat chip ────────────────────────────────────────────────────────

function LogisticStat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="bg-bambu-dark-tertiary/40 rounded-lg p-3" title={hint}>
      <div className="text-xs font-medium text-white mb-1">{label}</div>
      <div className="text-lg font-semibold text-white">{value}</div>
    </div>
  );
}

// ── Setting field ─────────────────────────────────────────────────────────────

function SettingField({
  label, hint, unit, editing, value, inputValue,
  onInputChange, onEdit, onSave, onCancel, isPending,
  saveLabel = 'Save', cancelLabel = 'Cancel',
}: {
  label: string; hint: string; unit: string; editing: boolean;
  value: number; inputValue: string;
  onInputChange: (v: string) => void; onEdit: () => void;
  onSave: () => void; onCancel: () => void; isPending: boolean;
  saveLabel?: string; cancelLabel?: string;
}) {
  return (
    <div className="bg-bambu-dark-tertiary/40 rounded-lg p-3 space-y-1">
      <div className="flex items-center gap-1.5">
        <span className="text-xs font-medium text-white">{label}</span>
        <span title={hint}><Info className="w-3 h-3 text-bambu-gray/50" /></span>
      </div>
      {editing ? (
        <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); onSave(); }}>
          <input
            type="number" min={0} max={365}
            value={inputValue} onChange={(e) => onInputChange(e.target.value)}
            className="w-20 px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-sm text-white focus:outline-none focus:border-bambu-green"
            autoFocus disabled={isPending}
          />
          <span className="text-xs text-bambu-gray">{unit}</span>
          <button type="submit" disabled={isPending} className="px-2 py-1 bg-bambu-green text-white text-xs rounded hover:bg-bambu-green/80 disabled:opacity-50">{saveLabel}</button>
          <button type="button" onClick={onCancel} disabled={isPending} className="px-2 py-1 text-xs text-bambu-gray hover:text-white">{cancelLabel}</button>
        </form>
      ) : (
        <div className="flex items-center gap-2">
          <span className="text-lg font-semibold text-white">{value}</span>
          <span className="text-xs text-bambu-gray">{unit}</span>
          <button onClick={onEdit} className="p-1 text-bambu-gray hover:text-white rounded transition-colors"><Edit2 className="w-3 h-3" /></button>
        </div>
      )}
    </div>
  );
}

// ── Safety margin field (dual unit: days | grams) ────────────────────────────

function SafetyMarginField({
  value, unit, editing, inputValue, dailyRateG,
  onInputChange, onUnitChange, onEdit, onSave, onCancel, isPending,
  saveLabel = 'Save', cancelLabel = 'Cancel', safetyMarginLabel = 'Safety Margin',
}: {
  value: number; unit: MarginUnit; editing: boolean; inputValue: string;
  dailyRateG: number | null;
  onInputChange: (v: string) => void; onUnitChange: (u: MarginUnit) => void;
  onEdit: () => void; onSave: () => void; onCancel: () => void; isPending: boolean;
  saveLabel?: string; cancelLabel?: string; safetyMarginLabel?: string;
}) {
  const { t } = useTranslation();
  const displayG = unit === 'days' && dailyRateG === null ? null : Math.round(marginGrams(value, unit, dailyRateG));
  const approxDays = dailyRateG !== null && dailyRateG > 0 && unit !== 'days'
    ? Math.round(marginGrams(value, unit, dailyRateG) / dailyRateG)
    : null;
  const hint = unit === 'days'
    ? t('forecast.safetyMarginHintDays', {
        approx: displayG !== null ? t('forecast.safetyMarginHintDaysApprox', { g: displayG }) : '',
      })
    : t('forecast.safetyMarginHintG', {
        approx: approxDays !== null ? t('forecast.safetyMarginHintGApprox', { days: approxDays }) : '',
      });

  return (
    <div className="bg-bambu-dark-tertiary/40 rounded-lg p-3 space-y-1">
      <div className="flex items-center gap-1.5">
        <span className="text-xs font-medium text-white">{safetyMarginLabel}</span>
        <span title={hint}><Info className="w-3 h-3 text-bambu-gray/50" /></span>
      </div>
      {editing ? (
        <form className="flex items-center gap-2 flex-wrap" onSubmit={(e) => { e.preventDefault(); onSave(); }}>
          <input
            type="number" min={0} max={unit === 'g' ? 1000000 : unit === 'kg' ? 10000 : 365}
            value={inputValue} onChange={(e) => onInputChange(e.target.value)}
            className="w-20 px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-sm text-white focus:outline-none focus:border-bambu-green"
            autoFocus disabled={isPending}
          />
          {/* Unit toggle */}
          <div className="flex bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded overflow-hidden text-xs">
            <button type="button" onClick={() => onUnitChange('days')} className={`px-2 py-1 transition-colors ${unit === 'days' ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'}`}>days</button>
            <button type="button" onClick={() => onUnitChange('g')} className={`px-2 py-1 transition-colors ${unit === 'g' ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'}`}>g</button>
            <button type="button" onClick={() => onUnitChange('kg')} className={`px-2 py-1 transition-colors ${unit === 'kg' ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'}`}>kg</button>
          </div>
          <button type="submit" disabled={isPending} className="px-2 py-1 bg-bambu-green text-white text-xs rounded hover:bg-bambu-green/80 disabled:opacity-50">{saveLabel}</button>
          <button type="button" onClick={onCancel} disabled={isPending} className="px-2 py-1 text-xs text-bambu-gray hover:text-white">{cancelLabel}</button>
        </form>
      ) : (
        <div className="flex items-center gap-2">
          <span className="text-lg font-semibold text-white">{value}</span>
          <span className="text-xs text-bambu-gray">{unit}</span>
          {displayG !== null && unit === 'days' && (
            <span className="text-lg font-semibold text-white">≈ {displayG}g</span>
          )}
          {unit !== 'days' && dailyRateG !== null && dailyRateG > 0 && (
            <span className="text-lg font-semibold text-white">≈ {Math.round(marginGrams(value, unit, dailyRateG) / dailyRateG)}d</span>
          )}
          <button onClick={onEdit} className="p-1 text-bambu-gray hover:text-white rounded transition-colors"><Edit2 className="w-3 h-3" /></button>
        </div>
      )}
    </div>
  );
}

// ── Shopping list panel ───────────────────────────────────────────────────────

function ShoppingListPanel({
  items, rows, rowsPending, logistics, logisticsPending, globalLeadTime,
  resolveSkuSettings, canWrite, onClose, onRemove, onClear,
}: {
  items: ShoppingListItem[];
  rows: SkuForecastRow[];
  /** The cart-rows feed has not answered yet — every rowFor() is null for a
   *  reason that is NOT "no such SKU". Gates the received button. */
  rowsPending: boolean;
  logistics: ForecastLogisticsRow[];
  /** The logistics feed has not answered yet — a null series is a WAIT, not
   *  the server's definitive "no usage data". */
  logisticsPending: boolean;
  globalLeadTime: number;
  resolveSkuSettings: (
    material: string | null, subtype: string | null, brand: string | null, colorName: string | null,
  ) => FilamentSkuSettings | null;
  canWrite: boolean;
  onClose: () => void;
  onRemove: (id: number) => void;
  onClear: () => void;
}) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [view, setView] = useState<'list' | 'logistics'>('list');

  /**
   * Item ids whose spools have already been created on the server.
   *
   * ⚠️ Receiving is THREE writes, and the button stays enabled after a failure
   * — that recoverability is the whole point of creating before patching. But
   * "retry" must RESUME, not repeat: without this, a failure after the create
   * (status PATCH 503s, connection drops) turned the retry the toast advises
   * into a second batch of N spools. A ref, not state: it is read inside
   * `mutationFn` at call time, so it must never be a stale closure, and
   * nothing renders off it.
   */
  const spoolsCreatedFor = useRef<Set<number>>(new Set());

  const statusMutation = useMutation({
    mutationFn: async ({ id, status, item, avgSpoolG }: {
      id: number;
      status: 'pending' | 'purchased' | 'received';
      item?: ShoppingListItem;
      avgSpoolG?: number;
    }) => {
      if (status === 'received' && item) {
        // ⚠️ ORDER IS LOAD-BEARING: create the spools FIRST, flip the status
        // only once they exist. The other way round (as shipped) commits
        // `received` server-side and then discovers the create failed — the
        // row comes back Received with its button permanently disabled, the
        // inventory silently short by N spools, and no in-UI way back.
        if (spoolsCreatedFor.current.has(id)) {
          // A resumed retry: the spools exist, only the tail failed.
          await api.updateShoppingListStatus(id, status);
          await api.removeFromShoppingList(id);
          spoolsCreatedFor.current.delete(id);
          return;
        }
        const spoolWeight = avgSpoolG ?? 1000;
        const spoolBase: Parameters<typeof api.bulkCreateSpools>[0] = {
          material: item.material,
          subtype: item.subtype,
          brand: item.brand,
          label_weight: spoolWeight,
          core_weight: 0,
          core_weight_catalog_id: null,
          color_name: item.color_name, rgba: null, extra_colors: null, effect_type: null,
          nozzle_temp_min: null, nozzle_temp_max: null,
          note: item.note ?? null,
          tag_uid: null, tray_uuid: null,
          data_origin: 'manual', tag_type: null,
          cost_per_kg: null,
          last_scale_weight: null, last_weighed_at: null,
          weight_used: 0,
          slicer_filament: null, slicer_filament_name: null,
          added_full: null, last_used: null, encode_time: null,
          category: 'Stock',
          low_stock_threshold_pct: null,
          purchase_date: null, filament_diameter: '1.75', lot: null,
        };
        await api.bulkCreateSpools(spoolBase, item.quantity_spools);
        // Recorded BEFORE the next await, which is the one that can fail.
        spoolsCreatedFor.current.add(id);
        await api.updateShoppingListStatus(id, status);
        await api.removeFromShoppingList(id);
        spoolsCreatedFor.current.delete(id);
        return;
      }
      await api.updateShoppingListStatus(id, status);
    },
    // onSettled, not onSuccess: a failure can land between the three calls
    // (spools created, status not flipped), so the screen must be refreshed
    // from the server either way rather than left asserting the pre-click
    // state over a half-applied change.
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['shopping-list'] });
      queryClient.invalidateQueries({ queryKey: ['spools'] });
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      // Receiving spools moves the stock the forecast is computed over.
      invalidateForecastQueries(queryClient);
    },
    onError: () => showToast(t('forecast.receiveFailed'), 'error'),
  });

  // Served rows keyed by the engine's collapsed SKU key — the join the
  // whole panel renders through.
  const rowsByKey = useMemo(() => {
    const m = new Map<string, SkuForecastRow>();
    for (const r of rows) m.set(skuKey(r.material, r.subtype, r.brand, r.color_name), r);
    return m;
  }, [rows]);
  const logisticsById = useMemo(
    () => new Map(logistics.map((l) => [l.item_id, l])),
    [logistics],
  );

  const rowFor = (item: ShoppingListItem): SkuForecastRow | null =>
    rowsByKey.get(skuKey(item.material, item.subtype, item.brand, item.color_name)) ?? null;

  // The badge/list-marker predicate — the shipped client's, verbatim, over
  // served row fields (note the ≤: deliberately looser than the logistics
  // rows' served break flag, exactly as shipped).
  const breakAlerts = useMemo(() =>
    items.filter((item) => {
      const f = rowFor(item);
      if (!f || f.rate_g_day === null) return false;
      return f.stock_break_alert || (f.days_remaining !== null && f.days_remaining <= f.eff_lead_time_days);
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, rowsByKey]
  );

  function downloadCsv() {
    // Server-generated CSV — the export endpoint owns columns and escaping.
    api.downloadShoppingListCsv().catch(() => showToast(t('forecast.exportFailed'), 'error'));
  }

  return (
    <div className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/30">
        <div className="flex items-center gap-3">
          <ShoppingCart className="w-4 h-4 text-bambu-green" />
          <h3 className="text-sm font-semibold text-white">{t('forecast.shoppingList')}</h3>
          <span className="text-xs text-bambu-gray">{t('forecast.shoppingListItems', { count: items.length })}</span>
          {/* View toggle */}
          {items.length > 0 && (
            <div className="flex bg-bambu-dark-tertiary rounded-md p-0.5 ml-1">
              <button
                onClick={() => setView('list')}
                className={`flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium rounded transition-colors ${view === 'list' ? 'bg-bambu-dark-secondary text-white shadow' : 'text-bambu-gray hover:text-white'}`}
              >
                <Package className="w-3 h-3" />
                {t('forecast.listView')}
              </button>
              <button
                onClick={() => setView('logistics')}
                className={`flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium rounded transition-colors ${view === 'logistics' ? 'bg-bambu-dark-secondary text-white shadow' : 'text-bambu-gray hover:text-white'}`}
              >
                <BarChart2 className="w-3 h-3" />
                {t('forecast.logisticsView')}
                {breakAlerts.length > 0 && (
                  <span className="w-3.5 h-3.5 rounded-full bg-red-500 text-white text-[9px] font-bold flex items-center justify-center">
                    {breakAlerts.length}
                  </span>
                )}
              </button>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          {items.length > 0 && (
            <>
              <button onClick={downloadCsv} className="flex items-center gap-1.5 text-xs text-bambu-gray hover:text-white transition-colors px-2 py-1 rounded border border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary">
                <Download className="w-3 h-3" />
                {t('forecast.downloadCsv')}
              </button>
              {canWrite && (
                <button onClick={onClear} className="text-xs text-red-700 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 transition-colors px-2 py-1 rounded border border-red-200 dark:border-red-500/20 hover:bg-red-50 dark:hover:bg-red-500/10">
                  {t('forecast.clearAll')}
                </button>
              )}
            </>
          )}
          <button onClick={onClose} className="p-1 text-bambu-gray hover:text-white transition-colors"><X className="w-4 h-4" /></button>
        </div>
      </div>

      {items.length === 0 ? (
        <div className="flex flex-col items-center py-8 text-bambu-gray">
          <Package className="w-8 h-8 mb-2 opacity-30" />
          <p className="text-sm">{t('forecast.shoppingListEmpty')}</p>
        </div>
      ) : view === 'list' ? (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/20">
                <th className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.qty')}</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.material')}</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.weight')}</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.leadTime')}</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.expectedRestock')}</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.status')}</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.note')}</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('forecast.actions')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bambu-dark-tertiary">
              {items.map((item) => {
                const lbl = [item.brand, item.material, item.subtype, item.color_name].filter(Boolean).join(' ');
                const hasBreak = breakAlerts.some((a) => a.id === item.id);
                const f = rowFor(item);
                // The engine's archived-INCLUSIVE spool size, the same number
                // the Add-to-cart dialog divides by (:1861) — one rule, every
                // consumer. The live totals cannot answer it: a SKU held by
                // the 90-day archived-only window, precisely the one you are
                // being told to reorder, serves total_spools 0.
                //
                // ⚠️ Mark received writes this as a real `label_weight`, so
                // the 1000 g fallback is not cosmetic here. It survives for
                // exactly two cases: the server sent null (no spool of the SKU
                // carries a label weight at all), and an unanswered cart-rows
                // feed (`f === null`) — rowsPending disables the button below
                // for the second.
                const avgSpoolG = f?.avg_spool_label_g ?? 1000;
                const totalWeightG = Math.round(item.quantity_spools * avgSpoolG);
                const lt = f?.eff_lead_time_days ?? globalLeadTime ?? 0;
                const restockDate = lt > 0 ? addDays(new Date(), lt) : null;
                const isPurchased = item.status === 'purchased' || item.status === 'received';
                const isReceived = item.status === 'received';
                const isMutating = statusMutation.isPending;

                return (
                  <tr key={item.id} className={`hover:bg-bambu-dark-tertiary/30 transition-colors ${hasBreak && !isPurchased ? 'bg-red-500/5' : ''}`}>
                    {/* Qty */}
                    <td className="px-4 py-2.5">
                      <span className="text-sm font-semibold text-bambu-green">{item.quantity_spools}×</span>
                    </td>
                    {/* Material */}
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-white">{lbl}</span>
                        {hasBreak && !isPurchased && (
                          <AlertTriangle className="w-3.5 h-3.5 text-red-600 dark:text-red-400 flex-shrink-0" aria-label={t('forecast.stockBreakBefore')} />
                        )}
                      </div>
                    </td>
                    {/* Weight */}
                    <td className="px-4 py-2.5">
                      <span className="text-sm text-white">
                        {totalWeightG >= 1000 ? `${(totalWeightG / 1000).toFixed(1)}kg` : `${totalWeightG}g`}
                      </span>
                    </td>
                    {/* Lead time */}
                    <td className="px-4 py-2.5">
                      <span className="text-sm text-bambu-gray">{lt > 0 ? `${lt}d` : '—'}</span>
                    </td>
                    {/* Expected restock */}
                    <td className="px-4 py-2.5">
                      <span className="text-sm text-bambu-gray">
                        {restockDate ? formatDate(restockDate) : '—'}
                      </span>
                    </td>
                    {/* Status badge — read-only */}
                    <td className="px-4 py-2.5">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                        isReceived ? 'bg-bambu-green/20 text-bambu-green' :
                        isPurchased ? 'bg-blue-100 dark:bg-blue-400/20 text-blue-700 dark:text-blue-400' :
                        'bg-bambu-dark-tertiary text-bambu-gray'
                      }`}>
                        {isReceived ? t('forecast.received') : isPurchased ? t('forecast.purchased') : t('forecast.pending')}
                      </span>
                    </td>
                    {/* Note */}
                    <td className="px-4 py-2.5">
                      <span className="text-xs text-bambu-gray">{item.note || '—'}</span>
                    </td>
                    {/* Actions */}
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1">
                        {canWrite && (
                          <>
                            {/* Purchased icon — available when pending */}
                            <button
                              onClick={() => statusMutation.mutate({ id: item.id, status: isPurchased ? 'pending' : 'purchased' })}
                              disabled={isMutating || isReceived}
                              title={isPurchased ? t('forecast.resetToPending') : t('forecast.markPurchased')}
                              className={`p-1.5 rounded transition-colors disabled:opacity-30 ${
                                isPurchased
                                  ? 'text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300'
                                  : 'text-blue-600/50 dark:text-blue-400/50 hover:text-blue-600 dark:hover:text-blue-400'
                              }`}
                            >
                              {isPurchased ? <RotateCcw className="w-4 h-4" /> : <CreditCard className="w-4 h-4" />}
                            </button>
                            {/* Received icon — available only after purchasing */}
                            <button
                              onClick={() => statusMutation.mutate({ id: item.id, status: 'received', item, avgSpoolG })}
                              // rowsPending: creating spools at the 1000 g
                              // fallback because the feed was still in flight
                              // is silent and permanent — wait for the answer.
                              disabled={isMutating || !isPurchased || isReceived || rowsPending}
                              title={t('forecast.markReceived')}
                              className="p-1.5 rounded transition-colors text-bambu-green/50 hover:text-bambu-green disabled:opacity-30"
                            >
                              <PackageCheck className="w-4 h-4" />
                            </button>
                            {/* Delete */}
                            <button
                              onClick={() => onRemove(item.id)}
                              className="p-1 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 transition-colors"
                              title={t('forecast.remove')}
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        /* Logistics view — exclude received items */
        <div className="divide-y divide-bambu-dark-tertiary">
          {items.filter((item) => item.status !== 'received').map((item) => (
            <CartLogisticsRow
              key={item.id}
              item={item}
              logistics={logisticsById.get(item.id) ?? null}
              logisticsPending={logisticsPending}
              row={rowFor(item)}
              skuLeadTime={resolveSkuSettings(item.material, item.subtype, item.brand, item.color_name)?.lead_time_days ?? 0}
              globalLeadTime={globalLeadTime}
              canWrite={canWrite}
              onRemove={() => onRemove(item.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Cart logistics row ────────────────────────────────────────────────────────

/**
 * Renders the SERVED logistics timeline for one cart item. The series
 * carries the arrival date TWICE (pre/post bump) so the type="linear" area
 * draws a clean vertical step; the break day is `stock_break_day` VERBATIM
 * (never derived from the series — its rounding zeroes a day later in
 * general). The only client arithmetic left is trivial display work over
 * served numbers: the arrival step height and the bridge-gap spool count.
 */
function CartLogisticsRow({
  item, logistics, logisticsPending, row, skuLeadTime, globalLeadTime, canWrite, onRemove,
}: {
  item: ShoppingListItem;
  logistics: ForecastLogisticsRow | null;
  /** The feed is still in flight — a missing row means "not yet", not "none". */
  logisticsPending: boolean;
  row: SkuForecastRow | null;
  skuLeadTime: number;
  globalLeadTime: number;
  canWrite: boolean;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const label = [item.brand, item.material, item.subtype, item.color_name].filter(Boolean).join(' ');

  const series = logistics?.series ?? null;
  const arrivalDay = logistics?.arrival_day ?? null;
  const breakDay = logistics?.stock_break_day ?? null;
  const hasBreak = logistics?.stock_break_before_arrival ?? false;
  const ropG = logistics?.rop_g ?? null;
  const safetyG = logistics?.safety_stock_g ?? null;

  const points = useMemo(
    () => series?.map(([d, g]) => ({ label: formatDateShort(servedDate(d)), stock: g })) ?? null,
    [series],
  );
  // The step height at the doubled arrival date — the served post-bump minus
  // pre-bump values (display only; the series itself is the truth).
  const arrivalG = series !== null && arrivalDay !== null && series.length > arrivalDay + 1
    ? series[arrivalDay + 1][1] - series[arrivalDay][1]
    : null;
  const arrivalLabel = points !== null && arrivalDay !== null ? points[arrivalDay]?.label : undefined;
  const breakLabel = points !== null && breakDay !== null ? points[breakDay]?.label : undefined;
  const distinctDays = points !== null ? Math.max(1, points.length - 1) : 1;

  return (
    <div className={`px-4 py-4 ${hasBreak ? 'bg-red-500/5' : ''}`}>
      {/* Row header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 min-w-0">
          {hasBreak
            ? <AlertTriangle className="w-4 h-4 text-red-600 dark:text-red-400 flex-shrink-0" />
            : <Check className="w-4 h-4 text-bambu-green/60 flex-shrink-0" />
          }
          <span className="text-sm font-medium text-white truncate">{label}</span>
          <span className="text-xs text-bambu-gray flex-shrink-0">{t('forecast.spoolCount', { count: item.quantity_spools })} ordered</span>
        </div>
        {canWrite && (
          <button onClick={onRemove} className="p-1 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 transition-colors flex-shrink-0">
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Break alert — the day is the served stock_break_day, rendered verbatim */}
      {hasBreak && breakDay !== null && (
        <div className="mb-3 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/20 text-xs text-red-700 dark:text-red-300">
          <span className="font-medium">{t('forecast.stockBreakIn', { days: breakDay })}</span>
          {' '}{t('forecast.stockRunsOutBefore', { lt: arrivalDay ?? row?.eff_lead_time_days ?? 0 })}
          {row !== null && row.rate_g_day !== null && (
            <span> {t('forecast.atRate', { rate: row.rate_g_day.toFixed(1) })}{' '}
              {/* Same spool size as the Add-to-cart dialog and the received
                  write — the served archived-inclusive mean. Divide by the
                  live totals here and the banner says "order 2 more" while
                  the dialog for that very SKU says 3. */}
              <span className="font-semibold">{t('forecast.moreSpools', { count: Math.ceil((row.rate_g_day * row.eff_lead_time_days - row.total_remaining_g) / (row.avg_spool_label_g ?? 1000)) })}</span>
              {' '}{t('forecast.bridgeGap')}
            </span>
          )}
        </div>
      )}

      {/* A WAIT and a definitive negative are different claims: points===null
          covers both the server's series:null AND a logistics feed that has
          not answered (the map lookup misses). Only say "no usage data" once
          the feed has settled — the placeholder is the whole point of this
          view, so asserting it while loading is the worst possible lie. */}
      {logisticsPending ? (
        <LoadingBlock label={t('common.loading')} className="py-4 text-bambu-gray" />
      ) : points === null ? (
        <div className="py-4 text-center text-xs text-bambu-gray">
          {t('forecast.noUsageData')}
        </div>
      ) : (
        <>
          {/* Key stats row */}
          <div className="grid grid-cols-5 gap-2 mb-3">
            <div className="bg-bambu-dark-tertiary/40 rounded-lg px-2.5 py-2 text-center">
              <div className="text-xs text-bambu-gray mb-0.5">{t('forecast.stock')}</div>
              <div className="text-sm font-semibold text-white">{row !== null ? `${Math.round(row.total_remaining_g)}g` : '—'}</div>
            </div>
            <div className="bg-bambu-dark-tertiary/40 rounded-lg px-2.5 py-2 text-center">
              <div className="text-xs text-bambu-gray mb-0.5">{t('forecast.leadTime')}</div>
              <div className="text-sm font-semibold text-white">{arrivalDay ?? 0}d</div>
              <div className="text-[10px] text-bambu-gray/60">max(g:{globalLeadTime}, sku:{skuLeadTime})</div>
            </div>
            <div className="bg-bambu-dark-tertiary/40 rounded-lg px-2.5 py-2 text-center">
              <div className="text-xs text-bambu-gray mb-0.5">{t('forecast.safetyMarginLabel')}</div>
              <div className="text-sm font-semibold text-white">{safetyG !== null ? `${Math.round(safetyG)}g` : '—'}</div>
            </div>
            <div className={`rounded-lg px-2.5 py-2 text-center ${hasBreak ? 'bg-red-100 dark:bg-red-500/15' : 'bg-bambu-dark-tertiary/40'}`}>
              <div className="text-xs text-bambu-gray mb-0.5">{t('forecast.daysLeft')}</div>
              <div className={`text-sm font-semibold ${hasBreak ? 'text-red-700 dark:text-red-400' : 'text-green-700 dark:text-green-400'}`}>
                {row?.days_remaining ?? '—'}d
              </div>
            </div>
            {arrivalG !== null && (
              <div className="bg-bambu-green/15 rounded-lg px-2.5 py-2 text-center">
                <div className="text-xs text-bambu-gray mb-0.5">{t('forecast.onArrival')}</div>
                <div className="text-sm font-semibold text-bambu-green">{Math.round(arrivalG)}g</div>
                <div className="text-[10px] text-bambu-gray/60">+{t('forecast.spoolCount', { count: item.quantity_spools })}</div>
              </div>
            )}
          </div>

          {/* Chart */}
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                {/* Post-arrival fill: always green */}
                <linearGradient id={`cart-post-${item.id}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#1DB954" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#1DB954" stopOpacity={0.03} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" strokeOpacity={0.4} />
              <XAxis
                dataKey="label"
                tick={{ fill: '#6B7280', fontSize: 9 }}
                interval={Math.max(0, Math.ceil(distinctDays / 6) - 1)}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: '#6B7280', fontSize: 9 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v: number) => v >= 1000 ? `${(v / 1000).toFixed(1)}kg` : `${v}g`}
                width={44}
              />
              <Tooltip
                contentStyle={{ background: '#1a1a2e', border: '1px solid #374151', borderRadius: 8, fontSize: 11 }}
                labelStyle={{ color: '#9CA3AF' }}
                formatter={(value, name) => {
                  if (typeof value !== 'number') return '';
                  if (name === 'stock') return `${value}g — ${t('forecast.stock')}`;
                  return `${value}`;
                }}
              />
              {/* Single stock area — linear interpolation renders the vertical step correctly
                  because the two duplicate-label points at arrival day create an instant jump */}
              <Area
                type="linear"
                dataKey="stock"
                stroke="#1DB954"
                strokeWidth={2}
                fill={`url(#cart-post-${item.id})`}
                dot={false}
                activeDot={{ r: 3 }}
              />
              {/* Reorder point */}
              {ropG !== null && ropG > 0 && (
                <ReferenceLine
                  y={ropG}
                  stroke="#F59E0B"
                  strokeDasharray="5 3"
                  strokeOpacity={0.8}
                  label={{ value: 'ROP', position: 'insideTopRight', fill: '#F59E0B', fontSize: 9 }}
                />
              )}
              {/* Safety stock floor */}
              {safetyG !== null && safetyG > 0 && (
                <ReferenceLine
                  y={safetyG}
                  stroke="#6B7280"
                  strokeDasharray="3 3"
                  strokeOpacity={0.6}
                  label={{ value: 'SS', position: 'insideTopRight', fill: '#6B7280', fontSize: 9 }}
                />
              )}
              {/* Arrival / lead-time-end vertical line — the doubled series date */}
              {arrivalLabel !== undefined && (
                <ReferenceLine
                  x={arrivalLabel}
                  stroke="#3B82F6"
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                  strokeOpacity={0.9}
                  label={{ value: `+${arrivalG !== null && arrivalG >= 1000 ? `${(arrivalG / 1000).toFixed(1)}kg` : `${Math.round(arrivalG ?? 0)}g`} arrives (d${arrivalDay})`, position: 'insideTopLeft', fill: '#3B82F6', fontSize: 9 }}
                />
              )}
              {/* Stock break — the served break day's x position */}
              {breakLabel !== undefined && (
                <ReferenceLine
                  x={breakLabel}
                  stroke="#EF4444"
                  strokeWidth={1.5}
                  strokeOpacity={0.9}
                  label={{ value: 'OUT', position: 'insideTopLeft', fill: '#EF4444', fontSize: 9 }}
                />
              )}
            </AreaChart>
          </ResponsiveContainer>

          {/* Legend */}
          <div className="flex flex-wrap items-center gap-3 mt-2 text-[10px] text-bambu-gray">
            <span className="flex items-center gap-1"><span className="inline-block w-4 border-t-2 border-yellow-400 border-dashed" /> {t('forecast.ropLabel')}</span>
            <span className="flex items-center gap-1"><span className="inline-block w-4 border-t border-bambu-gray border-dashed" /> {t('forecast.safetyStockLegend')}</span>
            <span className="flex items-center gap-1"><span className="inline-block w-4 border-t-2 border-blue-400 border-dashed" /> {t('forecast.stockArrivalLegend')}</span>
            {hasBreak && <span className="flex items-center gap-1 text-red-700 dark:text-red-400"><span className="inline-block w-4 border-t-2 border-red-400" /> {t('forecast.stockoutLegend')}</span>}
          </div>
        </>
      )}
    </div>
  );
}

// ── Add to Cart Modal ─────────────────────────────────────────────────────────

function AddToCartModal({
  row: f, onClose, onAdd,
}: {
  row: SkuForecastRow;
  onClose: () => void;
  onAdd: (item: { material: string; subtype: string | null; brand: string | null; color_name: string | null; quantity_spools: number; note: string | null }) => void;
}) {
  const { t } = useTranslation();
  const label = rowLabel(f);
  const [mode, setMode] = useState<'qty' | 'duration'>('qty');
  const [qty, setQty] = useState('1');
  const [durationDays, setDurationDays] = useState('30');
  const [note, setNote] = useState('');

  // Trivial client arithmetic over the row's served numbers (spec §2.3's
  // carve-out). `avg_spool_label_g` is the engine's archived-INCLUSIVE mean —
  // "how big is a spool of this SKU" is answered by every spool you have ever
  // had of it, which is what the shipped panel's `group.allSpools` mean did
  // (02a85eee:ForecastPanel.tsx:1908-1910). The live totals beside it cannot
  // answer it: a SKU held by the 90-day archived-only window — precisely the
  // SKU you are being told to reorder — has total_spools == 0 AND
  // total_label_g == 0 (task-4 review, Minor 5).
  //
  // 1000 g survives ONLY for a null, which the server sends when no spool of
  // the SKU carries a label weight at all. It is a documented guess, never a
  // number derived from nothing.
  const spoolsForDuration = useMemo(() => {
    if (!f.rate_g_day || f.rate_g_day <= 0) return null;
    const neededG = f.rate_g_day * Number(durationDays);
    const avgSpoolG = f.avg_spool_label_g ?? 1000;
    return Math.ceil(neededG / avgSpoolG);
  }, [f, durationDays]);

  const finalQty = mode === 'qty' ? parseInt(qty, 10) || 1 : (spoolsForDuration ?? 1);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    onAdd({ material: f.material ?? '', subtype: f.subtype, brand: f.brand, color_name: f.color_name, quantity_spools: finalQty, note: note || null });
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="bg-bambu-dark-secondary rounded-2xl border border-bambu-dark-tertiary w-full max-w-sm shadow-2xl">
        <div className="flex items-center justify-between px-5 pt-5 pb-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <ShoppingCart className="w-5 h-5 text-bambu-green" />
            <h2 className="text-base font-semibold text-white">{t('forecast.addToCartTitle')}</h2>
          </div>
          <button onClick={onClose} className="p-1 text-bambu-gray hover:text-white transition-colors"><X className="w-5 h-5" /></button>
        </div>

        <form onSubmit={submit} className="p-5 space-y-4">
          <div className="text-sm text-bambu-gray">{label}</div>

          <div className="flex bg-bambu-dark-tertiary rounded-lg p-0.5">
            <button
              type="button"
              onClick={() => setMode('qty')}
              className={`flex-1 py-1.5 text-xs font-medium rounded-md transition-colors ${mode === 'qty' ? 'bg-bambu-dark-secondary text-white shadow' : 'text-bambu-gray hover:text-white'}`}
            >
              {t('forecast.byQuantity')}
            </button>
            <button
              type="button"
              onClick={() => setMode('duration')}
              className={`flex-1 py-1.5 text-xs font-medium rounded-md transition-colors ${mode === 'duration' ? 'bg-bambu-dark-secondary text-white shadow' : 'text-bambu-gray hover:text-white'}`}
            >
              {t('forecast.byDuration')}
            </button>
          </div>

          {mode === 'qty' ? (
            <div className="space-y-1.5">
              <label className="text-xs text-bambu-gray">{t('forecast.numberOfSpools')}</label>
              <input
                type="number" min={1} max={99}
                value={qty} onChange={(e) => setQty(e.target.value)}
                className="w-full px-3 py-2 bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
                autoFocus
              />
            </div>
          ) : (
            <div className="space-y-2">
              <div className="space-y-1.5">
                <label className="text-xs text-bambu-gray">{t('forecast.lastHowManyDays')}</label>
                <input
                  type="number" min={1} max={365}
                  value={durationDays} onChange={(e) => setDurationDays(e.target.value)}
                  className="w-full px-3 py-2 bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
                  autoFocus
                />
              </div>
              {f.rate_g_day !== null ? (
                <div className="flex items-center gap-2 px-3 py-2 bg-bambu-dark-tertiary/50 rounded-lg">
                  <span className="text-xs text-bambu-gray">≈</span>
                  <span className="text-sm font-semibold text-bambu-green">{t('forecast.spoolCount', { count: spoolsForDuration ?? 0 })}</span>
                  <span className="text-xs text-bambu-gray">at {f.rate_g_day.toFixed(1)}g/day</span>
                </div>
              ) : (
                <div className="text-xs text-yellow-700 dark:text-yellow-400 px-1">{t('forecast.noUsageQty')}</div>
              )}
            </div>
          )}

          <div className="space-y-1.5">
            <label className="text-xs text-bambu-gray">{t('forecast.noteOptional')}</label>
            <input
              type="text" maxLength={200}
              value={note} onChange={(e) => setNote(e.target.value)}
              placeholder={t('forecast.notePlaceholder')}
              className="w-full px-3 py-2 bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/40 focus:outline-none focus:border-bambu-green"
            />
          </div>

          <div className="flex items-center gap-3 pt-1">
            <button
              type="submit"
              className="flex-1 py-2 bg-bambu-green text-white text-sm font-medium rounded-lg hover:bg-bambu-green/80 transition-colors"
            >
              {t('forecast.addNSpools', { count: finalQty })}
            </button>
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-bambu-gray hover:text-white border border-bambu-dark-tertiary rounded-lg transition-colors">
              {t('forecast.cancel')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
