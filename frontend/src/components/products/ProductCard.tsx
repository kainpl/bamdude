import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Copy, Eye, EyeOff, MoreVertical, Package, Pencil, Trash2 } from 'lucide-react';
import type { ProductListItem } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';

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
 * ⚠️ **There is no cover before pass 4** (design decision 3). The tile is a
 * neutral placeholder on every card — `cover_image_filename` is read by the
 * type and deliberately ignored, because no server route serves it yet, and a
 * linked file's thumbnail is NOT a stand-in: it would show one part of a
 * multi-file product as if it were the product.
 */
export function ProductCard({ product, onEdit, onDuplicate, onToggleActive, onDelete }: ProductCardProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <Link
      to={`/products/${product.id}`}
      data-testid={`product-${product.id}-card`}
      className="block @container rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary hover:border-bambu-green/50 overflow-hidden"
    >
      <div className="p-4 flex gap-3 @max-[22rem]:flex-col">
        <div
          data-testid="product-cover-placeholder"
          className="w-20 h-20 @max-[22rem]:w-full @max-[22rem]:h-24 flex-shrink-0 rounded-lg bg-bambu-dark flex items-center justify-center"
        >
          <Package className="w-7 h-7 text-bambu-gray" />
        </div>

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
