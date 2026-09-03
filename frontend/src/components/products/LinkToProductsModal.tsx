import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link2, Loader2, Package, X } from 'lucide-react';
import { api } from '../../api/client';
import type { ProductRef } from '../../api/client';
import { Button } from '../Button';
import { useToast } from '../../contexts/ToastContext';
import { selectableProducts } from '../../utils/projects';

/** What the modal needs of the row it was opened from. A file and a folder
 *  differ only in which name field they carry and which update call saves
 *  them — everything else about the question is identical, which is why the
 *  File Manager's two nearly identical inline modals are one component now. */
export interface LinkToProductsItem {
  id: number;
  /** Folders. */
  name?: string;
  /** Files. */
  filename?: string;
  products?: ProductRef[];
}

interface LinkToProductsModalProps {
  kind: 'file' | 'folder';
  item: LinkToProductsItem;
  onClose: () => void;
}

/**
 * Link one library file or folder to the products it is a design for.
 *
 * ⚠️ **The chip set IS the answer.** Saving with nothing selected is the
 * explicit "unlink from everything" path — `product_ids: []` — not a no-op, so
 * there is no separate red "wipe all" button to disagree with the chips.
 *
 * ⚠️ **A folder link cascades to its files server-side**, so the file list has
 * to be invalidated after a folder save as well; the product side
 * (`['product-files']`, `['product-folders']`) is what the product page reads.
 */
export function LinkToProductsModal({ kind, item, onClose }: LinkToProductsModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [selectedIds, setSelectedIds] = useState<Set<number>>(
    () => new Set((item.products ?? []).map((p) => p.id)),
  );

  const { data: allProducts } = useQuery({
    queryKey: ['products', {}],
    queryFn: () => api.getProducts({}),
  });

  // Whatever this item is already linked to stays offered, in the catalog or
  // not — see `selectableProducts`.
  const products = useMemo(
    () => selectableProducts(allProducts, selectedIds),
    [allProducts, selectedIds],
  );

  const toggle = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const save = useMutation({
    mutationFn: async (productIds: number[]) => {
      if (kind === 'folder') await api.updateLibraryFolder(item.id, { product_ids: productIds });
      else await api.updateLibraryFile(item.id, { product_ids: productIds });
    },
    onSuccess: (_, productIds) => {
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      queryClient.invalidateQueries({ queryKey: ['products'] });
      queryClient.invalidateQueries({ queryKey: ['product-files'] });
      queryClient.invalidateQueries({ queryKey: ['product-folders'] });
      // Literal keys, one per outcome: the i18n guard only sees keys spelled
      // out at a `t(` call, and a ternary inside one hides them from it.
      const unlinked = productIds.length === 0;
      if (kind === 'folder') {
        showToast(
          unlinked ? t('fileManager.toast.folderUnlinked') : t('fileManager.toast.folderLinked'),
          'success',
        );
      } else {
        showToast(
          unlinked ? t('fileManager.toast.fileUnlinked') : t('fileManager.toast.fileLinked'),
          'success',
        );
      }
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const label = item.name ?? item.filename ?? '';

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <Link2 className="w-5 h-5 text-bambu-green" />
            {kind === 'folder' ? t('fileManager.linkFolder') : t('fileManager.linkFile')}
          </h2>
          <button type="button" onClick={onClose} className="p-1 hover:bg-bambu-dark rounded">
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-bambu-gray">
            {kind === 'folder'
              ? t('fileManager.linkFolderDescription', { name: label })
              : t('fileManager.linkFileDescription', { name: label })}
          </p>

          {/* Chip multi-select. Selected = filled + ×, unselected = outline. */}
          <div className="bg-bambu-dark rounded-lg p-3">
            {products.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {products.map((product) => {
                  const selected = selectedIds.has(product.id);
                  return (
                    <button
                      key={product.id}
                      type="button"
                      onClick={() => toggle(product.id)}
                      title={
                        selected ? t('fileManager.removeFromProduct', { name: product.name }) : product.name
                      }
                      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${
                        selected
                          ? 'border-transparent bg-bambu-green text-white'
                          : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray'
                      } ${product.is_active === false ? 'opacity-60 italic' : ''}`}
                    >
                      <Package className="w-3 h-3" />
                      {product.name}
                      {selected && <X className="w-3 h-3 ml-0.5 opacity-80" />}
                    </button>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-bambu-gray text-center py-4">{t('fileManager.noProductsFound')}</p>
            )}
            {selectedIds.size === 0 && (
              <p className="text-xs text-bambu-gray italic mt-2">{t('fileManager.noProductsSelected')}</p>
            )}
          </div>
        </div>

        <div className="p-4 border-t border-bambu-dark-tertiary flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => save.mutate([...selectedIds])} disabled={save.isPending}>
            {save.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.save')}
          </Button>
        </div>
      </div>
    </div>
  );
}
