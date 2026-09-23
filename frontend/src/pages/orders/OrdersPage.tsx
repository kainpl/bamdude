import { useEffect, useMemo, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Plus, Search, X } from 'lucide-react';
import { api } from '../../api/client';
import type { OrderListItem, ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProjectsTabs } from '../../components/projects/ProjectsTabs';
import { OrderCard } from '../../components/projects/OrderCard';
import { OrdersTable } from '../../components/projects/OrdersTable';
import { OrderModal } from '../../components/projects/OrderModal';
import { FilamentStrip } from '../../components/projects/FilamentStrip';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Button } from '../../components/Button';
import { Select } from '../../components/Select';
import { ListViewToggle } from '../../components/ListViewToggle';
import { ListSortControl } from '../../components/ListSortControl';
import type { ListView } from '../../components/ListViewToggle';
import { PaginationBar } from '../../components/PaginationBar';
import { useListUrlState } from '../../hooks/useListUrlState';
import { parseListView, parsePageSize, usePersistedState } from '../../hooks/usePersistedState';
import { useSearchBox } from '../../hooks/useSearchBox';
import { invalidateAfterDelete, invalidateOrderViews } from '../../utils/queryInvalidation';

const GROUP_STORAGE_KEY = 'projects.groupByCustomer';
const VIEW_STORAGE_KEY = 'projects.view';
const PER_PAGE_STORAGE_KEY = 'projects.perPage';
const TABS: readonly (ProjectStatus | 'all')[] = ['active', 'completed', 'cancelled', 'all'];
/** Each view has its own default order (owner's ruling): the table is the
 *  deadline roll-up it always was, the cards are "what moved lately". An
 *  explicit `?sort=` applies to both. */
const DEFAULT_SORT = { table: 'due-asc', cards: 'updated-desc' } as const;

/** How many placeholder cards the first fetch draws. Enough to fill the top of
 *  a normal window without pretending to know how many orders there are. */
const SKELETON_CARDS = 6;

/**
 * The grid while the FIRST fetch is in flight.
 *
 * ⚠️ **`isLoading`, never `isFetching`.** A background refetch — every order
 * mutation invalidates `['projects']` — still has the orders on screen, and
 * replacing them with grey boxes for a moment is worse than showing figures
 * that are one request old. TanStack's `isLoading` is exactly "pending with no
 * data", which is the only state that has nothing to show.
 *
 * ⚠️ **The grey boxes are decoration; the STATUS is the sentence.** A grid of
 * `aria-hidden` placeholders is silence to a screen reader — the page reads as
 * having no orders, with nothing said about why. `role="status"` + `aria-busy`
 * on the wrapper, with one visually-hidden line inside, is what announces the
 * wait; the cards keep their `aria-hidden` so nobody hears six empty ones.
 */
function OrdersSkeleton() {
  const { t } = useTranslation();
  return (
    <div role="status" aria-busy="true" data-testid="orders-skeleton">
      <span className="sr-only">{t('common.loading')}</span>
      <div aria-hidden="true" className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">
        {Array.from({ length: SKELETON_CARDS }, (_, i) => (
          <div
            key={i}
            className="animate-pulse rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary overflow-hidden"
          >
            <div className="h-1.5 bg-bambu-dark-tertiary" />
            <div className="p-4 flex gap-3">
              <div className="w-20 h-20 flex-shrink-0 rounded-lg bg-bambu-dark" />
              <div className="flex-1 space-y-2 py-1">
                <div className="h-4 w-2/3 rounded bg-bambu-dark" />
                <div className="h-3 w-1/3 rounded bg-bambu-dark" />
                <div className="h-2 w-full rounded bg-bambu-dark" />
                <div className="h-3 w-1/4 rounded bg-bambu-dark" />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** `Map` (not a plain object) so the group order matches first appearance in
 *  the already-filtered list, rather than an object's own key-insertion
 *  quirks with numeric-looking names. */
function groupBy<T>(items: T[], keyFn: (item: T) => string): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = keyFn(item);
    const existing = groups.get(key);
    if (existing) existing.push(item);
    else groups.set(key, [item]);
  }
  return groups;
}

/**
 * The order list: status tabs, a customer filter, a search and an optional
 * grouping — one page at a time from the server (spec projects-lists-parity).
 *
 * The tab counts are the server's `totals`: every filter but the status, so
 * the tabs tell the truth under the chosen customer or search without a
 * request per tab. The place in the list (tab, customer, search, sort, page)
 * lives in the URL; the view mode, the grouping and the page size are the
 * viewer's preferences. Grouping groups the PAGE — it is not a sort. The
 * default sort follows the view (`DEFAULT_SORT`).
 */
export function OrdersPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [view, setViewPref] = usePersistedState<ListView>(VIEW_STORAGE_KEY, 'cards', parseListView);
  const { page, q, sort, extra, setPage, setQ, setSort, setExtra, resetFilters, clampToLastPage } = useListUrlState({
    defaults: { sort: DEFAULT_SORT[view], extra: { tab: 'active', customer: '' } },
  });
  // Another view is another default order, so the page it stood on means nothing there.
  const setView = (next: ListView) => {
    setViewPref(next);
    setPage(1);
  };
  const tab: ProjectStatus | 'all' = (TABS as readonly string[]).includes(extra.tab)
    ? (extra.tab as ProjectStatus | 'all')
    : 'active';
  const customerId = extra.customer && Number.isInteger(Number(extra.customer)) ? Number(extra.customer) : null;
  const { typed, setTyped, forget } = useSearchBox(q, setQ);
  const [perPage, setPerPage] = usePersistedState<number>(PER_PAGE_STORAGE_KEY, 24, parsePageSize);
  const [groupByCustomer, setGroupByCustomer] = useState<boolean>(() => {
    try {
      return localStorage.getItem(GROUP_STORAGE_KEY) === '1';
    } catch {
      return false;
    }
  });
  const [editing, setEditing] = useState<OrderListItem | null | 'new'>(null);
  const [deleting, setDeleting] = useState<OrderListItem | null>(null);

  const params = {
    ...(tab !== 'all' ? { status: tab } : {}),
    ...(customerId != null ? { customer_id: customerId } : {}),
    ...(q ? { q } : {}),
    sort_by: sort,
    page,
    ...(perPage === -1 ? { all: true } : { per_page: perPage }),
  };
  const { data, isLoading, isPlaceholderData } = useQuery({
    queryKey: ['projects', params],
    queryFn: () => api.getOrdersPaged(params),
    // The old page stays on screen while the next one loads — no skeleton flash.
    placeholderData: keepPreviousData,
  });
  const { data: customers = [] } = useQuery({ queryKey: ['customers'], queryFn: api.getCustomers });
  // A delete (ours or someone else's) can leave us past the last page. Only an
  // answer for THIS view may clamp: the previous page's, still on screen while
  // the next loads, knows nothing about how many pages the new filter has.
  useEffect(() => {
    if (data && !isPlaceholderData) clampToLastPage(data.meta.last_page);
  }, [data, isPlaceholderData, clampToLastPage]);

  const counts = data?.totals ?? { active: 0, completed: 0, cancelled: 0, all: 0 };
  const visible = useMemo(() => data?.items ?? [], [data]);
  const total = data?.meta.total ?? 0;
  const filtered = q !== '' || customerId != null;
  const groups = groupByCustomer ? groupBy(visible, (o) => o.customer_name ?? t('orders.list.noCustomer')) : null;

  // Only ACTIVE orders are forecast: «closed = nothing is planned» is the
  // product rule everywhere else, and the endpoint answers a closed order with
  // an empty forecast — asking for one buys a row of nulls (spec Decision 9).
  const forecastIds = visible.filter((o) => o.status === 'active').map((o) => o.id);
  // The forecast is only meaningful in table view — cards don't show it, and
  // the farm-wide simulation isn't cheap enough to run on every tab.
  const forecastQuery = useQuery({
    queryKey: ['orders-forecast', forecastIds],
    queryFn: () => api.getOrdersForecast(forecastIds),
    enabled: view === 'table' && forecastIds.length > 0,
    staleTime: 30_000,
  });
  // The farm-wide filament strip over the list — every active order, not just the visible tab/filter.
  const filamentQuery = useQuery({ queryKey: ['orders-filament'], queryFn: api.getOrdersFilament, staleTime: 30_000 });
  // `undefined` while loading — every cell reads «…». A FAILED fetch is its
  // own state, passed down as `forecastError`: mapping it to `{}` here made
  // every row read «No estimate», which means «the farm could not place this
  // order», and sent the operator looking for a scheduling problem that was
  // really a dead request.
  const forecasts = useMemo(() => {
    if (!forecastQuery.data) return undefined;
    return Object.fromEntries(forecastQuery.data.orders.map((f) => [f.project_id, f]));
  }, [forecastQuery.data]);

  // `CustomerListFigures` and `CustomerFigures` are computed from these very
  // orders, so every status change moves a customer tile — and this page
  // does not know whose order it just touched, which is why every key in the
  // set is a prefix. See `utils/queryInvalidation.ts`.
  const invalidate = () => invalidateOrderViews(queryClient);

  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: number; status: ProjectStatus }) => api.updateOrder(id, { status }),
    onSuccess: invalidate,
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteOrder(id),
    // ⚠️ The id is passed because this is a LIST: the deleted order's own
    // `['project', id]` entry has no observer here, so nothing would ever
    // clear it and the next visit to a reused id — or a Back into the route
    // that just went — would render it out of cache inside the 60 s
    // `staleTime`. The order PAGE passes no id; it uses `useForgetOnUnmount`
    // instead, for the reason spelled out in `utils/queryInvalidation`.
    onSuccess: (_res, id) => {
      invalidateAfterDelete(queryClient, 'order', id);
      showToast(t('orders.toast.deleted'));
      setDeleting(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const duplicate = useMutation({
    mutationFn: (id: number) => api.duplicateOrder(id),
    onSuccess: (saved) => {
      invalidate();
      showToast(t('orders.toast.duplicated'));
      navigate(`/projects/${saved.id}`);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const toggleGroupByCustomer = (value: boolean) => {
    setGroupByCustomer(value);
    try {
      localStorage.setItem(GROUP_STORAGE_KEY, value ? '1' : '0');
    } catch {
      // Private browsing / storage disabled — the toggle still works this session.
    }
  };

  const tabs: { key: ProjectStatus | 'all'; label: string; count: number }[] = [
    { key: 'active', label: t('orders.status.active'), count: counts.active },
    { key: 'completed', label: t('orders.status.completed'), count: counts.completed },
    { key: 'cancelled', label: t('orders.status.cancelled'), count: counts.cancelled },
    { key: 'all', label: t('orders.list.tabAll'), count: counts.all },
  ];

  const sortOptions = [
    { key: 'updated', label: t('list.sort.updated'), descFirst: true },
    { key: 'created', label: t('list.sort.created'), descFirst: true },
    { key: 'name', label: t('orders.table.name') },
    { key: 'due', label: t('orders.table.due') },
    { key: 'priority', label: t('orders.modal.priority'), descFirst: true },
    { key: 'customer', label: t('orders.table.customer') },
    { key: 'progress', label: t('orders.table.progress'), descFirst: true },
    { key: 'remaining', label: t('orders.table.remaining'), descFirst: true },
    { key: 'printing', label: t('orders.table.printing'), descFirst: true },
    { key: 'queued', label: t('orders.table.queued'), descFirst: true },
  ];

  const pageBar = (variant: 'card' | 'bare') =>
    data ? (
      <PaginationBar
        page={data.meta.current_page}
        totalPages={data.meta.last_page}
        perPage={perPage}
        total={total}
        onPageChange={setPage}
        onPerPageChange={(n) => {
          setPerPage(n);
          setPage(1);
        }}
        items={t('orders.list.items', { count: total })}
        variant={variant}
      />
    ) : null;

  const renderCard = (order: OrderListItem) => (
    <OrderCard
      key={order.id}
      order={order}
      onEdit={setEditing}
      onDuplicate={(o) => duplicate.mutate(o.id)}
      onSetStatus={(o, status) => setStatus.mutate({ id: o.id, status })}
      onDelete={setDeleting}
    />
  );

  return (
    <div className="p-4">
      <ProjectsTabs />

      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <h1 className="text-2xl font-semibold text-white">{t('orders.list.title')}</h1>
        {hasPermission('projects:create') && (
          <Button onClick={() => setEditing('new')}>
            <Plus className="w-4 h-4" />
            {t('orders.list.newOrder')}
          </Button>
        )}
      </div>

      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <div role="tablist" className="flex gap-1 border-b border-bambu-dark-tertiary">
          {tabs.map(({ key, label, count }) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setExtra('tab', key)}
              className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
                tab === key ? 'border-bambu-green text-white' : 'border-transparent text-bambu-gray hover:text-white'
              }`}
            >
              {label} ({count})
            </button>
          ))}
        </div>

        <div className="relative">
          <Search className="w-4 h-4 text-bambu-gray absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
          <input
            type="search"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={t('orders.list.searchPlaceholder')}
            aria-label={t('orders.list.searchPlaceholder')}
            className="pl-9 pr-8 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
          />
          {typed && (
            <button
              type="button"
              onClick={() => setTyped('')}
              aria-label={t('list.search.clear')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        <Select value={customerId ?? ''} onChange={(e) => setExtra('customer', e.target.value)}>
          <option value="">{t('orders.list.customerFilterAll')}</option>
          {customers.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </Select>

        <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
          <input
            type="checkbox"
            checked={groupByCustomer}
            onChange={(e) => toggleGroupByCustomer(e.target.checked)}
            className="accent-bambu-green"
            aria-label={t('orders.list.groupByCustomer')}
          />
          {t('orders.list.groupByCustomer')}
        </label>

        {/* A table sorts from its headers; the cards need a control of their own. */}
        {view === 'cards' && <ListSortControl sort={sort} options={sortOptions} onChange={setSort} />}

        <ListViewToggle value={view} onChange={setView} />
      </div>

      {filamentQuery.data && <FilamentStrip farm={filamentQuery.data} />}

      {!isLoading && total === 0 && (
        filtered ? (
          <div className="flex items-center gap-3 text-bambu-gray text-sm">
            <span>{t('list.empty.noMatch')}</span>
            <Button
              variant="secondary"
              onClick={() => {
                forget();
                resetFilters(['tab']);
              }}
            >
              {t('list.empty.reset')}
            </Button>
          </div>
        ) : (
          <p className="text-bambu-gray text-sm">{t(`orders.list.empty.${tab}`)}</p>
        )
      )}

      {isLoading ? (
        <OrdersSkeleton />
      ) : (
        // The previous page stays on screen while the next one loads — dimmed
        // and marked busy, so it is not read as the answer to the new question.
        <div
          data-testid="list-body"
          aria-busy={isPlaceholderData}
          className={`transition-opacity ${isPlaceholderData ? 'opacity-60' : ''}`}
        >
          {groups ? (
            <>
              <div className="space-y-4">
                {[...groups.entries()].map(([customerName, group]) => (
                  <section key={customerName}>
                    <h2 className="text-lg font-medium text-white mb-2">{customerName}</h2>
                    {view === 'table' ? (
                      <OrdersTable
                        orders={group}
                        forecasts={forecasts}
                        forecastError={forecastQuery.isError}
                        sort={sort}
                        onSortChange={setSort}
                      />
                    ) : (
                      <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">{group.map(renderCard)}</div>
                    )}
                  </section>
                ))}
              </div>
              {/* Several tables, one bar — it belongs to the page, not to a group. */}
              {total > 0 && <div className="mt-4">{pageBar('bare')}</div>}
            </>
          ) : view === 'table' ? (
            total > 0 && (
              <OrdersTable
                orders={visible}
                forecasts={forecasts}
                forecastError={forecastQuery.isError}
                sort={sort}
                onSortChange={setSort}
                footer={pageBar('card')}
              />
            )
          ) : (
            <>
              <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">{visible.map(renderCard)}</div>
              {total > 0 && <div className="mt-4">{pageBar('bare')}</div>}
            </>
          )}
        </div>
      )}

      {editing && (
        <OrderModal order={editing === 'new' ? null : editing} defaultCustomerId={customerId} onClose={() => setEditing(null)} />
      )}

      {deleting && (
        <ConfirmModal
          title={t('orders.confirm.deleteTitle')}
          message={t('orders.confirm.deleteBody')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(deleting.id)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
