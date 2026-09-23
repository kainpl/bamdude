import { useEffect, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { FileBox, Plus, Search, Upload, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Product, ProductListItem } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProjectsTabs } from '../../components/projects/ProjectsTabs';
import { invalidateAfterDelete, invalidateOrderViews } from '../../utils/queryInvalidation';
import { ProductCard } from '../../components/products/ProductCard';
import { ProductsTable } from '../../components/products/ProductsTable';
import { ListViewToggle } from '../../components/ListViewToggle';
import { ListSortControl } from '../../components/ListSortControl';
import type { ListView } from '../../components/ListViewToggle';
import { PaginationBar } from '../../components/PaginationBar';
import { useListUrlState } from '../../hooks/useListUrlState';
import { parseListView, parsePageSize, usePersistedState } from '../../hooks/usePersistedState';
import { useSearchBox } from '../../hooks/useSearchBox';
import { ProductCardDialog } from '../../components/products/ProductCardDialog';
import { FromFileDialog } from '../../components/products/FromFileDialog';
import { ImportProductDialog } from '../../components/products/ImportProductDialog';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Button } from '../../components/Button';

/**
 * The product catalog.
 *
 * One page at a time from the SERVER (`GET /products?page=…`, spec
 * projects-lists-parity): search, the catalog toggle and the sort are asked of
 * it, because a catalog of thousands must not travel so that a search box can
 * narrow it. The place in the list — page, search, sort, the toggle — lives in
 * the URL (Back, F5 and a shared link land on the same view); the view mode and
 * the page size are the viewer's preferences, kept in localStorage.
 */
export function ProductsPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const { page, q, sort, extra, setPage, setQ, setSort, setExtra, resetFilters, clampToLastPage } = useListUrlState({
    defaults: { sort: 'name-asc', extra: { catalog: '1' } },
  });
  const inCatalog = extra.catalog !== '0';
  const [view, setView] = usePersistedState<ListView>('bamdude-products-view', 'cards', parseListView);
  const [perPage, setPerPage] = usePersistedState<number>('bamdude-products-perPage', 24, parsePageSize);
  const { typed, setTyped, forget } = useSearchBox(q, setQ);
  const [editing, setEditing] = useState<ProductListItem | null | 'new'>(null);
  const [fromFile, setFromFile] = useState(false);
  const [importing, setImporting] = useState(false);
  const [deleting, setDeleting] = useState<ProductListItem | null>(null);

  // `active: false` would be a filter of its own ("only what is hidden"), which
  // this toggle does not offer — off means "no filter", so the key is absent.
  const params = {
    ...(inCatalog ? { active: true } : {}),
    ...(q ? { q } : {}),
    sort_by: sort,
    page,
    ...(perPage === -1 ? { all: true } : { per_page: perPage }),
  };

  const { data, isLoading, isPlaceholderData } = useQuery({
    queryKey: ['products', params],
    // Arrow, never `queryFn: api.getProductsPaged` — TanStack would hand the
    // query context to a function whose only parameter is the params object.
    queryFn: () => api.getProductsPaged(params),
    // The old page stays on screen while the next one loads — no flash.
    placeholderData: keepPreviousData,
  });
  const products = data?.items ?? [];
  const total = data?.meta.total ?? 0;
  // A delete (ours or someone else's) can leave us past the last page. Only an
  // answer for THIS view may clamp: the previous page's, still on screen while
  // the next loads, knows nothing about how many pages the new filter has.
  useEffect(() => {
    if (data && !isPlaceholderData) clampToLastPage(data.meta.last_page);
  }, [data, isPlaceholderData, clampToLastPage]);
  // Only the search narrows: the catalog toggle OFF is the widest view there
  // is, so an empty answer there means the catalog is empty, not "no match".
  const filtered = q !== '';

  const sortOptions = [
    { key: 'name', label: t('products.table.name') },
    { key: 'updated', label: t('list.sort.updated'), descFirst: true },
    { key: 'created', label: t('list.sort.created'), descFirst: true },
    { key: 'parts', label: t('products.table.parts'), descFirst: true },
    { key: 'plates', label: t('products.table.plates'), descFirst: true },
    { key: 'orders', label: t('products.table.orders'), descFirst: true },
    { key: 'kits', label: t('products.table.kits'), descFirst: true },
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
        items={t('products.list.items', { count: total })}
        variant={variant}
      />
    ) : null;

  const toggleActive = useMutation({
    mutationFn: (product: ProductListItem) => api.updateProduct(product.id, { is_active: !product.is_active }),
    onSuccess: (saved) => {
      // ⚠️ The order views, which since Ruling 29 include the product keys: a
      // product that leaves the catalog is still on the lines of every order
      // that ordered it, and the cards and pickers reading those lines have to
      // be told. Naming `['product', id]` and `['products']` again here was two
      // refetches of each for one toggle.
      invalidateOrderViews(queryClient);
      showToast(saved.is_active ? t('products.toast.shown') : t('products.toast.hidden'));
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteProduct(id),
    // The LISTS, from the one place that decides which ones — an order card
    // renders the deleted product's cover, so `['projects']` goes with
    // `['products']` and neither site gets its own opinion about that. The id
    // goes too: from a grid, the deleted product's own detail entry is a ghost
    // nobody is watching — see `utils/queryInvalidation`.
    onSuccess: (_res, id) => {
      invalidateAfterDelete(queryClient, 'product', id);
      showToast(t('products.toast.deleted'));
      setDeleting(null);
    },
    // A product an order line uses answers 409 — the server's own sentence is
    // what the toast says, and the grid is left exactly as it was.
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const duplicate = useMutation({
    mutationFn: (id: number) => api.duplicateProduct(id),
    // ⚠️ No order view moves here, deliberately: the copy is a brand-new
    // product that no order line names yet. Invalidating them would refetch
    // every order on the way out of a page nobody is coming back to.
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      showToast(t('products.toast.duplicated'));
      navigate(`/products/${saved.id}`);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const openCreated = (created: Product) => {
    setFromFile(false);
    navigate(`/products/${created.id}`);
  };

  return (
    <div className="p-4">
      <ProjectsTabs />

      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <h1 className="text-2xl font-semibold text-white">{t('products.list.title')}</h1>
        {hasPermission('projects:create') && (
          <div className="flex items-center gap-2">
            <Button variant="secondary" onClick={() => setFromFile(true)}>
              <FileBox className="w-4 h-4" />
              {t('products.list.fromFile')}
            </Button>
            {/* An import INGESTS FILES INTO THE LIBRARY, so the server asks for
                the upload permission beside `projects:create`. The button is
                shown to anyone who may create a product — the refusal, when it
                comes, is the server's own sentence in the dialog. */}
            <Button variant="secondary" onClick={() => setImporting(true)}>
              <Upload className="w-4 h-4" />
              {t('products.list.import')}
            </Button>
            <Button onClick={() => setEditing('new')}>
              <Plus className="w-4 h-4" />
              {t('products.list.newProduct')}
            </Button>
          </div>
        )}
      </div>

      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <div className="relative">
          <Search className="w-4 h-4 text-bambu-gray absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
          <input
            type="search"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={t('products.list.search')}
            aria-label={t('products.list.search')}
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

        <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
          <input
            type="checkbox"
            checked={inCatalog}
            onChange={(e) => setExtra('catalog', e.target.checked ? '1' : '0')}
            className="accent-bambu-green"
            aria-label={t('products.list.inCatalog')}
          />
          {t('products.list.inCatalog')}
        </label>

        <div className="ml-auto flex items-center gap-3">
          {/* A table sorts from its headers; the cards need a control of their own. */}
          {view === 'cards' && <ListSortControl sort={sort} options={sortOptions} onChange={setSort} />}
          <ListViewToggle value={view} onChange={setView} />
        </div>
      </div>

      {!isLoading && total === 0 && (
        filtered ? (
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
          <p className="text-bambu-gray text-sm">{t('products.list.empty')}</p>
        )
      )}

      {/* The previous page stays on screen while the next one loads — dimmed
          and marked busy, so it is not read as the answer to the new question. */}
      <div
        data-testid="list-body"
        aria-busy={isPlaceholderData}
        className={`transition-opacity ${isPlaceholderData ? 'opacity-60' : ''}`}
      >
        {view === 'table' && products.length > 0 ? (
          <ProductsTable
            products={products}
            sort={sort}
            onSortChange={setSort}
            onEdit={setEditing}
            onDuplicate={(p) => duplicate.mutate(p.id)}
            onToggleActive={(p) => toggleActive.mutate(p)}
            onDelete={setDeleting}
            footer={pageBar('card')}
          />
        ) : (
          <>
            <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">
              {products.map((product) => (
                <ProductCard
                  key={product.id}
                  product={product}
                  onEdit={setEditing}
                  onDuplicate={(p) => duplicate.mutate(p.id)}
                  onToggleActive={(p) => toggleActive.mutate(p)}
                  onDelete={setDeleting}
                />
              ))}
            </div>
            {total > 0 && <div className="mt-4">{pageBar('bare')}</div>}
          </>
        )}
      </div>

      {editing && <ProductCardDialog product={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}

      {fromFile && <FromFileDialog onClose={() => setFromFile(false)} onCreated={openCreated} />}

      {importing && <ImportProductDialog onClose={() => setImporting(false)} />}

      {deleting && (
        <ConfirmModal
          title={t('products.confirm.deleteTitle')}
          message={t('products.confirm.deleteBody')}
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(deleting.id)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
