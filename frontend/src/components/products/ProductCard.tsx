import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Copy, Download, Eye, EyeOff, MoreVertical, Package, Pencil, Trash2 } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { ProductListItem } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';

interface ProductCardProps {
  product: ProductListItem;
  onEdit: (product: ProductListItem) => void;
  onDuplicate: (product: ProductListItem) => void;
  onToggleActive: (product: ProductListItem) => void;
  onDelete: (product: ProductListItem) => void;
}

/** Prevents the menu (and everything inside it) from bubbling into the card's
 *  own `<Link>` — without both `preventDefault` and `stopPropagation` a click
 *  on "Edit" would also navigate to the product page (same trap as
 *  `OrderCard`). */
function stopCardNavigation(e: React.MouseEvent) {
  e.preventDefault();
  e.stopPropagation();
}

const MENU_ITEM_CLASS = 'w-full px-3 py-2 text-left text-sm flex items-center gap-2 hover:bg-bambu-dark';

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
 */
export function ProductCard({ product, onEdit, onDuplicate, onToggleActive, onDelete }: ProductCardProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const [menuOpen, setMenuOpen] = useState(false);

  // Not a mutation: nothing on this page changes, and the card is inside a
  // `<Link>` — a failed download must say so where the operator clicked rather
  // than navigate anywhere.
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
    <Link
      to={`/products/${product.id}`}
      data-testid={`product-${product.id}-card`}
      className="block @container rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary hover:border-bambu-green/50 overflow-hidden"
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
            <div className="relative flex-shrink-0" onClick={stopCardNavigation}>
              <button
                type="button"
                data-testid="product-menu"
                onClick={(e) => {
                  stopCardNavigation(e);
                  setMenuOpen((v) => !v);
                }}
                className="p-1.5 rounded-lg hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
                aria-label={t('common.actions')}
              >
                <MoreVertical className="w-4 h-4" />
              </button>
              {menuOpen && (
                <>
                  <div
                    className="fixed inset-0 z-10"
                    onClick={(e) => {
                      stopCardNavigation(e);
                      setMenuOpen(false);
                    }}
                  />
                  <div
                    role="menu"
                    className="absolute right-0 top-8 z-20 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[180px]"
                  >
                    {hasPermission('projects:update') && (
                      <button
                        type="button"
                        role="menuitem"
                        className={`${MENU_ITEM_CLASS} text-white`}
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onEdit(product);
                          setMenuOpen(false);
                        }}
                      >
                        <Pencil className="w-4 h-4" />
                        {t('products.card.menu.edit')}
                      </button>
                    )}
                    {hasPermission('projects:create') && (
                      <button
                        type="button"
                        role="menuitem"
                        className={`${MENU_ITEM_CLASS} text-white`}
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onDuplicate(product);
                          setMenuOpen(false);
                        }}
                      >
                        <Copy className="w-4 h-4" />
                        {t('products.card.menu.duplicate')}
                      </button>
                    )}
                    <button
                      type="button"
                      role="menuitem"
                      className={`${MENU_ITEM_CLASS} text-white`}
                      onClick={(e) => {
                        stopCardNavigation(e);
                        exportProduct();
                        setMenuOpen(false);
                      }}
                    >
                      <Download className="w-4 h-4" />
                      {t('products.card.menu.export')}
                    </button>
                    {hasPermission('projects:update') && (
                      <button
                        type="button"
                        role="menuitem"
                        className={`${MENU_ITEM_CLASS} text-white`}
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onToggleActive(product);
                          setMenuOpen(false);
                        }}
                      >
                        {product.is_active ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                        {product.is_active ? t('products.card.menu.hide') : t('products.card.menu.show')}
                      </button>
                    )}
                    {hasPermission('projects:delete') && (
                      <button
                        type="button"
                        role="menuitem"
                        className={`${MENU_ITEM_CLASS} text-red-500`}
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onDelete(product);
                          setMenuOpen(false);
                        }}
                      >
                        <Trash2 className="w-4 h-4" />
                        {t('products.card.menu.delete')}
                      </button>
                    )}
                  </div>
                </>
              )}
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
        </div>
      </div>
    </Link>
  );
}
