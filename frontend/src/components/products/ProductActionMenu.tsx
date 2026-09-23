import { useTranslation } from 'react-i18next';
import { Copy, Download, Eye, EyeOff, Pencil, Trash2 } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { ProductListItem } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { CardActionMenu, CardActionMenuItem } from '../CardActionMenu';

export interface ProductActions {
  onEdit: (product: ProductListItem) => void;
  onDuplicate: (product: ProductListItem) => void;
  onToggleActive: (product: ProductListItem) => void;
  onDelete: (product: ProductListItem) => void;
}

/**
 * The one menu of a catalog product — the card and the table row both render
 * THIS, so the two views can never offer different actions for the same row.
 */
export function ProductActionMenu({
  product,
  onEdit,
  onDuplicate,
  onToggleActive,
  onDelete,
  testId = 'product-menu',
}: ProductActions & { product: ProductListItem; testId?: string }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();

  // Not a mutation: nothing on this page changes, and a failed download must
  // say so where the operator clicked rather than navigate anywhere.
  const exportProduct = async () => {
    try {
      await api.downloadProductExport(product.id);
    } catch (e) {
      showToast(
        e instanceof ApiError ? t('products.toast.exportFailed', { status: e.status }) : (e as Error).message,
        'error',
      );
    }
  };

  return (
    <CardActionMenu label={t('common.actions')} testId={testId}>
      {(close) => (
        <>
          {hasPermission('projects:update') && (
            <CardActionMenuItem
              onSelect={() => {
                onEdit(product);
                close();
              }}
            >
              <Pencil className="w-4 h-4" />
              {t('products.card.menu.edit')}
            </CardActionMenuItem>
          )}
          {hasPermission('projects:create') && (
            <CardActionMenuItem
              onSelect={() => {
                onDuplicate(product);
                close();
              }}
            >
              <Copy className="w-4 h-4" />
              {t('products.card.menu.duplicate')}
            </CardActionMenuItem>
          )}
          <CardActionMenuItem
            onSelect={() => {
              exportProduct();
              close();
            }}
          >
            <Download className="w-4 h-4" />
            {t('products.card.menu.export')}
          </CardActionMenuItem>
          {hasPermission('projects:update') && (
            <CardActionMenuItem
              onSelect={() => {
                onToggleActive(product);
                close();
              }}
            >
              {product.is_active ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              {product.is_active ? t('products.card.menu.hide') : t('products.card.menu.show')}
            </CardActionMenuItem>
          )}
          {hasPermission('projects:delete') && (
            <CardActionMenuItem
              danger
              onSelect={() => {
                onDelete(product);
                close();
              }}
            >
              <Trash2 className="w-4 h-4" />
              {t('products.card.menu.delete')}
            </CardActionMenuItem>
          )}
        </>
      )}
    </CardActionMenu>
  );
}
