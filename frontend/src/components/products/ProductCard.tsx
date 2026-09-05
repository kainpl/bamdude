import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Copy, Download, Eye, EyeOff, Package, Pencil, Trash2 } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { ProductListItem } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { CardActionMenu, CardActionMenuItem } from '../CardActionMenu';

interface ProductCardProps {
  product: ProductListItem;
  onEdit: (product: ProductListItem) => void;
  onDuplicate: (product: ProductListItem) => void;
  onToggleActive: (product: ProductListItem) => void;
  onDelete: (product: ProductListItem) => void;
}

/**
 * One product in the grid.
 *
 * ⚠️ **The tile reads `has_cover`, never `cover_image_filename`.** `has_cover`
 * is the EFFECTIVE cover — the explicit column OR the first picture — and the
 * column is null for every product whose cover is that implicit default, which
 * is most of them. A card that asked the column would show the placeholder over
 * a product that plainly has a picture.
 *
 * A linked file's thumbnail is still NOT a stand-in: it would show one part of
 * a multi-file product as if it were the product.
 *
 * ⚠️ **The link is an OVERLAY, not the card's wrapper** — same trap and same
 * fix as `OrderCard`: the menu was a `<button>` inside an `<a>` and every item
 * had to undo the navigation its own click caused.
 */
export function ProductCard({ product, onEdit, onDuplicate, onToggleActive, onDelete }: ProductCardProps) {
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
    <div
      data-testid={`product-${product.id}-card`}
      className="relative @container rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary hover:border-bambu-green/50 overflow-hidden"
    >
      <div className="p-4 flex gap-3 @max-[22rem]:flex-col">
        {product.has_cover ? (
          <img
            data-testid="product-cover"
            src={api.getProductCoverImageUrl(product.id)}
            alt=""
            className="w-20 h-20 @max-[22rem]:w-full @max-[22rem]:h-24 flex-shrink-0 rounded-lg object-cover bg-bambu-dark"
          />
        ) : (
          <div
            data-testid="product-cover-placeholder"
            className="w-20 h-20 @max-[22rem]:w-full @max-[22rem]:h-24 flex-shrink-0 rounded-lg bg-bambu-dark flex items-center justify-center"
          >
            <Package className="w-7 h-7 text-bambu-gray" />
          </div>
        )}

        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex items-start justify-between gap-2">
            <h3 className="font-semibold text-white truncate">{product.name}</h3>
            {/* Above the overlay link, so the trigger is clickable at all. */}
            <div className="relative z-10 flex-shrink-0">
              <CardActionMenu label={t('common.actions')} testId="product-menu">
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
            </div>
          </div>

          {!product.is_active && (
            <span className="inline-block px-2 py-0.5 rounded-full text-xs bg-bambu-dark text-bambu-gray">
              {t('products.card.inactive')}
            </span>
          )}

          <p className="text-xs text-bambu-gray">
            {t('products.card.parts', { count: product.parts_count })} ·{' '}
            {t('products.card.plates', { count: product.plates_count })}
          </p>

          {/* `> 0`, never a bare `&&` on the number — `{0 && …}` renders the 0. */}
          {product.lines_count > 0 && (
            <p className="text-xs text-bambu-gray">{t('products.card.inOrders', { count: product.lines_count })}</p>
          )}

          {/* Free stock (pass 8). Shown ONLY when there is some: a badge reading
              "0 kits in stock" on every product in the catalog is noise, and the
              number comes free with the list response, so nothing is fetched to
              decide. Same `> 0` guard and the same reason as above. */}
          {product.kits_available > 0 && (
            <span
              data-testid="product-kits-badge"
              className="inline-block px-2 py-0.5 rounded-full text-xs bg-bambu-green/15 text-bambu-green"
            >
              {t('stock.card.kits', { count: product.kits_available })}
            </span>
          )}
        </div>
      </div>

      <Link
        to={`/products/${product.id}`}
        aria-label={product.name}
        className="absolute inset-0 rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
      />
    </div>
  );
}
