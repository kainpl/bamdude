import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { api } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProductGallery } from '../../components/products/ProductGallery';
import { ProductHeader } from '../../components/products/ProductHeader';
import { CompositionTable } from '../../components/products/CompositionTable';
import { PlatesByFile } from '../../components/products/PlatesByFile';
import { ProductAttachments } from '../../components/products/ProductAttachments';
import { LinkedFiles } from '../../components/products/LinkedFiles';
import { ProductOrders } from '../../components/products/ProductOrders';
import { ProductCardDialog } from '../../components/products/ProductCardDialog';
import { ConfirmModal } from '../../components/ConfirmModal';
import { invalidateAfterDelete, invalidateOrderViews } from '../../utils/queryInvalidation';
import { useForgetOnUnmount } from '../../hooks/useForgetOnUnmount';

/**
 * One product: what it is, what it is made of, what prints it, and who wants it.
 *
 * The page composes sections and owns nothing but dialog state and the three
 * whole-product actions. Everything below the header fetches its own slice, so
 * a slow plate walk never holds up the composition table.
 *
 * ⚠️ **A delete can be refused.** A product an order line uses answers 409, and
 * the operator is meant to take it out of the catalog instead — so the failure
 * is a toast over an untouched page, never a navigation away from a product
 * that still exists.
 */
export function ProductPage() {
  const { t } = useTranslation();
  const { id: idParam } = useParams<{ id: string }>();
  const id = Number(idParam);
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const forgetProduct = useForgetOnUnmount(['product', id]);

  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const {
    data: product,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['product', id],
    queryFn: () => api.getProduct(id),
    enabled: Number.isFinite(id),
    // This page keeps its data when a REFETCH fails (see the long note below),
    // so it is the cache's job to say the figures are older than they look.
    meta: { refreshToast: true },
  });

  // ⚠️ The order views go too. A product's NAME, its cover and whether it is
  // in the catalog are all rendered on order cards and inside an order's
  // lines, so a change here that stopped at `['products']` left every order
  // view showing the old one until its own 60 s `staleTime` expired.
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['product', id] });
    queryClient.invalidateQueries({ queryKey: ['products'] });
    invalidateOrderViews(queryClient);
  };

  const toggleActive = useMutation({
    // `is_active` is one of the two fields the server refuses as an explicit
    // null (422), so the boolean is always sent.
    mutationFn: (next: boolean) => api.updateProduct(id, { is_active: next }),
    onSuccess: (saved) => {
      invalidate();
      showToast(saved.is_active ? t('products.toast.shown') : t('products.toast.hidden'));
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const duplicate = useMutation({
    mutationFn: () => api.duplicateProduct(id),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      showToast(t('products.toast.duplicated'));
      navigate(`/products/${saved.id}`);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteProduct(id),
    // ⚠️ The LISTS only — not `invalidate()`. Marking `['product', id]` stale
    // asks TanStack to refetch a product that no longer exists while this page
    // is still mounted, which lands a 404 in the query and can flash the error
    // state over a page that is on its way out. `['projects']` goes with it:
    // an order card renders this product's cover.
    onSuccess: () => {
      invalidateAfterDelete(queryClient, 'product');
      showToast(t('products.toast.deleted'));
      // ⚠️ The entry goes when this page UNMOUNTS, not on the next line: a
      // `removeQueries` here would run while the page is still mounted (React
      // has only scheduled the route change) and its own observer would refetch
      // the product that was just deleted. Armed here, dropped on unmount — see
      // `useForgetOnUnmount`. Without it a Back inside the 60 s `staleTime`
      // renders the deleted product out of cache.
      forgetProduct();
      navigate('/products');
    },
    // A product an order line uses answers 409 — the server's own sentence is
    // what the toast says, and the page is left exactly as it was.
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  if (isLoading) {
    return (
      <div className="p-4 md:p-6 flex items-center gap-2 text-bambu-gray">
        <Loader2 className="w-4 h-4 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }
  // ⚠️ **Data presence is asked FIRST, and that order is load-bearing.**
  // TanStack v5 flips `status` to "error" on ANY failed fetch — a background
  // REFETCH of a query that still holds good data included — and it keeps
  // `data` while it does. This page invalidates `['product', id]` on every
  // mutation it and its sections make (the catalog toggle, part create / edit /
  // delete / merge, both alias calls, both unlinks), so a refetch is in flight
  // routinely; one that fails would, on an `isError`-first check, throw the
  // whole rendered page away and show a load error over a product still sitting
  // in the cache.
  //
  // With no data, the two cases still read apart: a fetch that FAILED is not a
  // product that is gone. "This product no longer exists" over an expired
  // session, a proxy hiccup or a 500 sends the operator hunting for a deletion
  // nobody performed, so the server's own sentence is shown instead.
  if (!product) {
    return isError ? (
      <div className="p-4 md:p-6 text-sm text-red-500">
        {t('products.page.loadFailed')} {(error as Error)?.message}
      </div>
    ) : (
      <div className="p-4 md:p-6 text-bambu-gray text-sm">{t('products.page.notFound')}</div>
    );
  }

  const canEdit = hasPermission('projects:update');

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Top-down, per the parent spec: what the thing LOOKS like, then what it
          is, then what it is made of, then what prints it, then its papers,
          then its files, then who wants it. */}
      <ProductGallery product={product} canEdit={canEdit} />

      <ProductHeader
        product={product}
        onEdit={() => setEditing(true)}
        onDuplicate={() => duplicate.mutate()}
        onDelete={() => setDeleting(true)}
        onToggleActive={(next) => toggleActive.mutate(next)}
      />

      <CompositionTable product={product} canEdit={canEdit} />

      <PlatesByFile productId={product.id} />

      <ProductAttachments product={product} canEdit={canEdit} />

      <LinkedFiles product={product} canEdit={canEdit} />

      <div className="space-y-2">
        <ProductOrders productId={product.id} />
        {/* ⚠️ Units DELIVERED against orders — every order status, capped at
            each line's need. Not "units ever printed": a print nobody ordered
            is not in it, and neither is the eleventh of ten. */}
        <p className="text-sm text-bambu-gray">
          {t('products.card.unitsPrintedTotal')}:{' '}
          <span className="text-white" data-testid="product-units-printed-total">
            {product.units_printed_total}
          </span>
        </p>
      </div>

      {editing && <ProductCardDialog product={product} onClose={() => setEditing(false)} />}

      {deleting && (
        <ConfirmModal
          title={t('products.confirm.deleteTitle')}
          message={t('products.confirm.deleteBody')}
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate()}
          onCancel={() => setDeleting(false)}
        />
      )}
    </div>
  );
}
