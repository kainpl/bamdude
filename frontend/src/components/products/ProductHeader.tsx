import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Copy, Download, ExternalLink, Loader2, Pencil, RefreshCw, Trash2 } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { Product } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { cardNotesText } from './cardNotes';

interface ProductHeaderProps {
  product: Product;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onToggleActive: (next: boolean) => void;
}

/** One provenance field, rendered only when the product carries it. */
function Fact({ label, value }: { label: string; value: string }) {
  return (
    <p className="text-sm text-bambu-gray">
      {label}: <span className="text-white">{value}</span>
    </p>
  );
}

/**
 * What the product is, where its design came from, and every action on it.
 *
 * ⚠️ **The catalog switch is a real checkbox, not a styled `<div>`.** It is the
 * one control on this page that changes what other screens offer — a product
 * out of the catalog disappears from the order-line picker — so it has to be
 * reachable by keyboard and named to a screen reader like any other checkbox.
 *
 * `is_active` is one of the two fields the server refuses as an explicit null
 * (422), so the toggle always sends a boolean; there is no "clear it" state to
 * reach from here.
 *
 * ⚠️ **"Re-read from file…" is a PICKER, not a button.** A product can hold
 * several linked files and the server fills from exactly one of them, so there
 * is no "the" file to guess; and linking a file deliberately does not fill
 * anything on its own, which makes this the only way a re-read ever happens.
 * The fill never overwrites a value somebody typed — the notes say what it
 * left alone, and they arrive as CODES because only this layer knows which
 * language the operator reads.
 */
export function ProductHeader({ product, onEdit, onDuplicate, onDelete, onToggleActive }: ProductHeaderProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const canEdit = hasPermission('projects:update');
  const [rereadOpen, setRereadOpen] = useState(false);

  // ⚠️ Not `product.library_file_ids`, which carries only the DIRECT half:
  // `GET /library/files?product_id=` unions in the files of linked folders,
  // and the server re-reads happily from either. Same query key as
  // `LinkedFiles`, so opening the picker costs nothing on a page that already
  // listed them. Fetched only once the menu is opened — a header that is on
  // every product page should not pull a file list nobody asked for.
  const { data: files = [] } = useQuery({
    queryKey: ['product-files', product.id],
    queryFn: () => api.getLibraryFiles(undefined, true, undefined, [], false, product.id),
    enabled: rereadOpen && Number.isFinite(product.id),
  });

  // ⚠️ A menu that only closes on a click is a menu an operator using the
  // keyboard cannot get out of. Same handler shape as `FolderTreeSelect`'s.
  useEffect(() => {
    if (!rereadOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setRereadOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [rereadOpen]);

  const exportProduct = useMutation({
    mutationFn: () => api.downloadProductExport(product.id),
    onError: (e: Error) =>
      showToast(
        e instanceof ApiError ? t('products.toast.exportFailed', { status: e.status }) : e.message,
        'error',
      ),
  });

  const reread = useMutation({
    mutationFn: (fileId: number) => api.rereadProductCard(product.id, fileId),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['product', product.id] });
      queryClient.invalidateQueries({ queryKey: ['products'] });
      setRereadOpen(false);
      // One toast, every note in it: they are one answer to one question, and
      // five stacked toasts would push the first off screen before it is read.
      showToast(cardNotesText(t, result.notes));
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <header className="space-y-3">
      <nav className="flex items-center gap-1 text-sm text-bambu-gray">
        <Link to="/products" className="hover:text-white transition-colors">
          {t('products.header.breadcrumb')}
        </Link>
        <ChevronRight className="w-4 h-4" />
        <span className="text-white truncate">{product.name}</span>
      </nav>

      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0 space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-2xl font-semibold text-white">{product.name}</h1>
            {!product.is_active && (
              <span className="px-2 py-0.5 rounded text-xs font-medium bg-gray-200 dark:bg-gray-500/20 text-gray-600 dark:text-gray-400">
                {t('products.header.hidden')}
              </span>
            )}
          </div>

          <label className="flex items-center gap-2 text-sm text-white cursor-pointer w-fit">
            <input
              type="checkbox"
              checked={product.is_active}
              disabled={!canEdit}
              onChange={(e) => onToggleActive(e.target.checked)}
              className="accent-bambu-green"
              aria-label={t('products.header.inCatalog')}
            />
            {t('products.header.inCatalog')}
          </label>

          {product.designer && <Fact label={t('products.header.designer')} value={product.designer} />}
          {product.license && <Fact label={t('products.header.license')} value={product.license} />}
          {product.design_id && <Fact label={t('products.header.designId')} value={product.design_id} />}

          {product.source_url && (
            <a
              href={product.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-sm text-bambu-green hover:underline"
            >
              <ExternalLink className="w-4 h-4" />
              {t('products.header.source')}
            </a>
          )}

          {product.description && (
            <p className="text-sm text-bambu-gray whitespace-pre-wrap max-w-2xl">{product.description}</p>
          )}
          {product.notes && (
            <p className="text-sm text-bambu-gray whitespace-pre-wrap max-w-2xl italic">{product.notes}</p>
          )}
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          {canEdit && (
            <Button variant="secondary" onClick={onEdit}>
              <Pencil className="w-4 h-4" />
              {t('products.header.edit')}
            </Button>
          )}
          {hasPermission('projects:create') && (
            <Button variant="secondary" onClick={onDuplicate}>
              <Copy className="w-4 h-4" />
              {t('products.header.duplicate')}
            </Button>
          )}
          {/* Reading a product is enough to take one away — the export carries
              nothing the page does not already show. */}
          <Button variant="secondary" onClick={() => exportProduct.mutate()} disabled={exportProduct.isPending}>
            {exportProduct.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Download className="w-4 h-4" />
            )}
            {t('products.header.export')}
          </Button>
          {canEdit && (
            <div className="relative">
              <Button
                type="button"
                variant="secondary"
                onClick={() => setRereadOpen((v) => !v)}
                disabled={reread.isPending}
              >
                {reread.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
                {t('products.card.reread')}
              </Button>
              {rereadOpen && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setRereadOpen(false)} />
                  <div
                    role="menu"
                    className="absolute right-0 top-full mt-1 z-20 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[220px] max-h-64 overflow-y-auto"
                  >
                    {files.length === 0 ? (
                      // ⚠️ A disabled `menuitem`, not a `<p>`: a menu whose only
                      // row is not a row at all is a menu a screen reader reads
                      // as empty, and "there is nothing to re-read from" is the
                      // answer the operator came for.
                      <button
                        type="button"
                        role="menuitem"
                        disabled
                        className="w-full px-3 py-2 text-left text-sm text-bambu-gray cursor-not-allowed"
                      >
                        {t('products.card.rereadNoFiles')}
                      </button>
                    ) : (
                      files.map((file) => (
                        <button
                          key={file.id}
                          type="button"
                          role="menuitem"
                          className="w-full px-3 py-2 text-left text-sm text-white hover:bg-bambu-dark truncate"
                          onClick={() => reread.mutate(file.id)}
                        >
                          {file.filename}
                        </button>
                      ))
                    )}
                  </div>
                </>
              )}
            </div>
          )}
          {hasPermission('projects:delete') && (
            <Button type="button" variant="secondary" onClick={onDelete}>
              <Trash2 className="w-4 h-4" />
              {t('products.header.delete')}
            </Button>
          )}
        </div>
      </div>
    </header>
  );
}
