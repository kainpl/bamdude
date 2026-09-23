import { useEffect, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Plus, Search, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Customer } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProjectsTabs } from '../../components/projects/ProjectsTabs';
import { CustomersTable } from '../../components/customers/CustomersTable';
import { CustomerCard } from '../../components/customers/CustomerCard';
import { CustomerModal } from '../../components/customers/CustomerModal';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Button } from '../../components/Button';
import { ListViewToggle } from '../../components/ListViewToggle';
import { ListSortControl } from '../../components/ListSortControl';
import type { ListView } from '../../components/ListViewToggle';
import { PaginationBar } from '../../components/PaginationBar';
import { useListUrlState } from '../../hooks/useListUrlState';
import { parseListView, parsePageSize, usePersistedState } from '../../hooks/usePersistedState';
import { useSearchBox } from '../../hooks/useSearchBox';
import { invalidateAfterDelete } from '../../utils/queryInvalidation';

/**
 * Who the orders are for. Customers have no status of their own, so the only
 * filter is a search (name or contact) — one page at a time from the server
 * (spec projects-lists-parity). Search, sort and page live in the URL; the
 * view mode (table by default here) and the page size are preferences.
 */
export function CustomersPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const { page, q, sort, setPage, setQ, setSort, resetFilters, clampToLastPage } = useListUrlState({
    defaults: { sort: 'name-asc' },
  });
  const { typed, setTyped, forget } = useSearchBox(q, setQ);
  const [view, setView] = usePersistedState<ListView>('bamdude-customers-view', 'table', parseListView);
  const [perPage, setPerPage] = usePersistedState<number>('bamdude-customers-perPage', 24, parsePageSize);
  const [editing, setEditing] = useState<Customer | null | 'new'>(null);
  const [deleting, setDeleting] = useState<Customer | null>(null);

  const params = {
    ...(q ? { q } : {}),
    sort_by: sort,
    page,
    ...(perPage === -1 ? { all: true } : { per_page: perPage }),
  };
  const { data, isLoading, isPlaceholderData } = useQuery({
    queryKey: ['customers', params],
    queryFn: () => api.getCustomersPaged(params),
    placeholderData: keepPreviousData,
  });
  const customers = data?.items ?? [];
  const total = data?.meta.total ?? 0;
  // A delete (ours or someone else's) can leave us past the last page. Only an
  // answer for THIS view may clamp: the previous page's, still on screen while
  // the next loads, knows nothing about how many pages the new filter has.
  useEffect(() => {
    if (data && !isPlaceholderData) clampToLastPage(data.meta.last_page);
  }, [data, isPlaceholderData, clampToLastPage]);

  const sortOptions = [
    { key: 'name', label: t('customers.table.name') },
    { key: 'created', label: t('list.sort.created'), descFirst: true },
    { key: 'orders', label: t('customers.table.orders'), descFirst: true },
    { key: 'active', label: t('customers.table.active'), descFirst: true },
    { key: 'completed', label: t('customers.table.completed'), descFirst: true },
    { key: 'cancelled', label: t('customers.table.cancelled'), descFirst: true },
    { key: 'total_price', label: t('customers.table.totalPrice'), descFirst: true },
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
        items={t('customers.list.items', { count: total })}
        variant={variant}
      />
    ) : null;
  // The currency, the way every money-showing screen reads it (the table asks too).
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteCustomer(id),
    // Orders keep their history and lose the customer, so their rows move
    // too. The deleted customer's own entry is REMOVED rather than
    // invalidated, and the id says which — see `utils/queryInvalidation`.
    onSuccess: (_res, id) => {
      invalidateAfterDelete(queryClient, 'customer', id);
      showToast(t('customers.toast.deleted'));
      setDeleting(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <div className="p-4">
      <ProjectsTabs />

      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <h1 className="text-2xl font-semibold text-white">{t('customers.list.title')}</h1>
        {hasPermission('projects:create') && (
          <Button onClick={() => setEditing('new')}>
            <Plus className="w-4 h-4" />
            {t('customers.list.newCustomer')}
          </Button>
        )}
      </div>

      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <div className="relative">
          <Search className="w-4 h-4 text-bambu-gray absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
          <input
            type="search"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={t('customers.list.searchPlaceholder')}
            aria-label={t('customers.list.searchPlaceholder')}
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
        <div className="ml-auto flex items-center gap-3">
          {/* A table sorts from its headers; the cards need a control of their own. */}
          {view === 'cards' && <ListSortControl sort={sort} options={sortOptions} onChange={setSort} />}
          <ListViewToggle value={view} onChange={setView} />
        </div>
      </div>

      {!isLoading && total === 0 ? (
        q ? (
          <div className="flex items-center gap-3 text-bambu-gray text-sm">
            <span>{t('list.empty.noMatch')}</span>
            <Button
              variant="secondary"
              onClick={() => {
                forget();
                resetFilters();
              }}
            >
              {t('list.empty.reset')}
            </Button>
          </div>
        ) : (
          <p className="text-bambu-gray text-sm">{t('customers.list.empty')}</p>
        )
      ) : (
        // The previous page stays on screen while the next one loads — dimmed
        // and marked busy, so it is not read as the answer to the new question.
        <div
          data-testid="list-body"
          aria-busy={isPlaceholderData}
          className={`transition-opacity ${isPlaceholderData ? 'opacity-60' : ''}`}
        >
          {view === 'cards' ? (
            <>
              <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(260px,1fr))]">
                {customers.map((customer) => (
                  <CustomerCard
                    key={customer.id}
                    customer={customer}
                    currency={settings?.currency}
                    onEdit={setEditing}
                    onDelete={setDeleting}
                  />
                ))}
              </div>
              {total > 0 && <div className="mt-4">{pageBar('bare')}</div>}
            </>
          ) : (
            <CustomersTable
              customers={customers}
              onEdit={setEditing}
              onDelete={setDeleting}
              sort={sort}
              onSortChange={setSort}
              footer={pageBar('card')}
            />
          )}
        </div>
      )}

      {editing && <CustomerModal customer={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}

      {deleting && (
        <ConfirmModal
          title={t('customers.confirm.deleteTitle')}
          message={t('customers.confirm.deleteBody')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(deleting.id)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
