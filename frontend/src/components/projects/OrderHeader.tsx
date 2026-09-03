import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Ban, CheckCircle2, ChevronRight, Copy, ExternalLink, Pencil, RotateCcw, Tag, Trash2 } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { formatMoney } from '../../utils/currency';
import { Button } from '../Button';
import { StatusBadge } from './StatusBadge';
import { PriorityBadge } from './PriorityBadge';

interface OrderHeaderProps {
  order: Order;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onSetStatus: (status: ProjectStatus) => void;
}

/**
 * Who the order is for, what state it is in, and every action on it.
 *
 * ⚠️ **Margin is shown only beside a price.** `figures.margin` is null when the
 * order carries none — not zero, which would read as "sold at cost" — so the
 * pair is rendered together or not at all. Both go through `formatMoney`, like
 * every other amount on these pages: a bare `toLocaleString` here is how
 * `$30.50` on one screen became `30.5` on the next.
 */
export function OrderHeader({ order, onEdit, onDuplicate, onDelete, onSetStatus }: OrderHeaderProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  // The app-wide currency, fetched the way every other money-showing screen
  // fetches it; `formatMoney` covers the unresolved first paint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });

  const margin = order.figures.margin;
  const tags = order.tags ? order.tags.split(',').map((tag) => tag.trim()).filter(Boolean) : [];

  return (
    <header className="space-y-3">
      <nav className="flex items-center gap-1 text-sm text-bambu-gray">
        <Link to="/projects" className="hover:text-white transition-colors">
          {t('orders.header.breadcrumb')}
        </Link>
        <ChevronRight className="w-4 h-4" />
        <span className="text-white truncate">{order.name}</span>
      </nav>

      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0 space-y-2">
          <h1 className="text-2xl font-semibold text-white">{order.name}</h1>

          <div className="flex items-center gap-2 flex-wrap text-sm">
            {order.customer_id != null ? (
              <Link to={`/customers/${order.customer_id}`} className="text-bambu-gray hover:text-white transition-colors">
                {order.customer_name}
              </Link>
            ) : (
              <span className="text-bambu-gray">{t('orders.header.noCustomer')}</span>
            )}
            <StatusBadge status={order.status} />
            <PriorityBadge priority={order.priority} />
            {order.due_date && (
              <span className="text-bambu-gray">{new Date(order.due_date).toLocaleDateString()}</span>
            )}
          </div>

          {order.price != null && (
            <p className="text-sm text-bambu-gray">
              <span>
                {t('orders.header.price')}: <span className="text-white tabular-nums">{formatMoney(order.price, settings?.currency)}</span>
              </span>
              {margin != null && (
                <span>
                  {' · '}
                  {t('orders.header.margin')}:{' '}
                  <span className={`tabular-nums ${margin < 0 ? 'text-red-500' : 'text-white'}`}>
                    {formatMoney(margin, settings?.currency)}
                  </span>
                </span>
              )}
            </p>
          )}

          {tags.length > 0 && (
            <div className="flex items-center gap-2">
              <Tag className="w-4 h-4 text-bambu-gray" />
              <div className="flex flex-wrap gap-1">
                {tags.map((tag) => (
                  <span key={tag} className="px-2 py-0.5 bg-bambu-dark-tertiary text-bambu-gray text-xs rounded">
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          )}

          {order.url && (
            <a
              href={order.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-sm text-bambu-green hover:underline"
            >
              <ExternalLink className="w-4 h-4" />
              {t('orders.header.url')}
            </a>
          )}
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          {hasPermission('projects:update') && (
            <Button variant="secondary" onClick={onEdit}>
              <Pencil className="w-4 h-4" />
              {t('orders.header.edit')}
            </Button>
          )}
          {hasPermission('projects:create') && (
            <Button variant="secondary" onClick={onDuplicate}>
              <Copy className="w-4 h-4" />
              {t('orders.header.duplicate')}
            </Button>
          )}
          {hasPermission('projects:update') && order.status === 'active' && (
            <>
              <Button variant="secondary" onClick={() => onSetStatus('completed')}>
                <CheckCircle2 className="w-4 h-4" />
                {t('orders.header.complete')}
              </Button>
              <Button variant="secondary" onClick={() => onSetStatus('cancelled')}>
                <Ban className="w-4 h-4" />
                {t('orders.header.cancel')}
              </Button>
            </>
          )}
          {hasPermission('projects:update') && order.status !== 'active' && (
            <Button variant="secondary" onClick={() => onSetStatus('active')}>
              <RotateCcw className="w-4 h-4" />
              {t('orders.header.reopen')}
            </Button>
          )}
          {hasPermission('projects:delete') && (
            <Button variant="secondary" onClick={onDelete}>
              <Trash2 className="w-4 h-4" />
              {t('orders.header.delete')}
            </Button>
          )}
        </div>
      </div>
    </header>
  );
}
