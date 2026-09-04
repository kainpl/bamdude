import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { FileBox, Plus, Search, Upload } from 'lucide-react';
import { api } from '../../api/client';
import type { Product, ProductListItem, ProductListParams } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProjectsTabs } from '../../components/projects/ProjectsTabs';
import { invalidateAfterDelete, invalidateOrderViews } from '../../utils/queryInvalidation';
import { ProductCard } from '../../components/products/ProductCard';
import { ProductCardDialog } from '../../components/products/ProductCardDialog';
import { FromFileDialog } from '../../components/products/FromFileDialog';
import { ImportProductDialog } from '../../components/products/ImportProductDialog';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Button } from '../../components/Button';

/** How long the search box waits before it becomes a request. */
const DEBOUNCE_MS = 300;

/**
 * The product catalog.
 *
 * Both filters are asked of the SERVER (`GET /products?active=&q=`), unlike the
 * order list, which fetches once and filters in the browser: the orders page
 * needs the whole list anyway to count its tabs, and this one has nothing to
 * count — a catalog of thousands must not travel so that a search box can
 * narrow it.
 */
export function ProductsPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [typed, setTyped] = useState('');
  const [q, setQ] = useState('');
  const [inCatalog, setInCatalog] = useState(true);
  const [editing, setEditing] = useState<ProductListItem | null | 'new'>(null);
  const [fromFile, setFromFile] = useState(false);
  const [importing, setImporting] = useState(false);
  const [deleting, setDeleting] = useState<ProductListItem | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setQ(typed.trim()), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [typed]);

  // `active: false` would be a filter of its own ("only what is hidden"), which
  // this toggle does not offer — off means "no filter", so the key is absent.
  const params: ProductListParams = { ...(inCatalog ? { active: true } : {}), ...(q ? { q } : {}) };

  const { data: products = [], isLoading } = useQuery({
    queryKey: ['products', params],
    // Arrow, never `queryFn: api.getProducts` — TanStack would hand the query
    // context to a function whose only parameter is the params object.
    queryFn: () => api.getProducts(params),
  });

  const toggleActive = useMutation({
    mutationFn: (product: ProductListItem) => api.updateProduct(product.id, { is_active: !product.is_active }),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      queryClient.invalidateQueries({ queryKey: ['product', saved.id] });
      // ⚠️ And the order views: a product that leaves the catalog is
      // still on the lines of every order that ordered it, and the cards
      // and pickers reading those lines have to be told.
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
    <div className="p-4 md:p-6">
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
            className="pl-9 pr-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
          />
        </div>

        <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
          <input
            type="checkbox"
            checked={inCatalog}
            onChange={(e) => setInCatalog(e.target.checked)}
            className="accent-bambu-green"
            aria-label={t('products.list.inCatalog')}
          />
          {t('products.list.inCatalog')}
        </label>
      </div>

      {!isLoading && products.length === 0 && <p className="text-bambu-gray text-sm">{t('products.list.empty')}</p>}

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
