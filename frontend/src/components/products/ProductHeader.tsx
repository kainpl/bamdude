import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Copy, ExternalLink, Pencil, Trash2 } from 'lucide-react';
import type { Product } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { Button } from '../Button';

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
 * There is no cover image this pass (design decision 3): the field is read and
 * ignored until a route exists to serve it, and no linked file's thumbnail is
 * borrowed to stand in for one.
 */
export function ProductHeader({ product, onEdit, onDuplicate, onDelete, onToggleActive }: ProductHeaderProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const canEdit = hasPermission('projects:update');

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
          {hasPermission('projects:delete') && (
            <Button variant="secondary" onClick={onDelete}>
              <Trash2 className="w-4 h-4" />
              {t('products.header.delete')}
            </Button>
          )}
        </div>
      </div>
    </header>
  );
}
