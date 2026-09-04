import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { MoreVertical, Pencil, Copy, CheckCircle2, RotateCcw, Ban, Trash2, Package } from 'lucide-react';
import { api } from '../../api/client';
import type { OrderListItem, ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { StatusBadge } from './StatusBadge';
import { PriorityBadge } from './PriorityBadge';
import { ProgressBar } from './ProgressBar';

interface OrderCardProps {
  order: OrderListItem;
  onEdit: (order: OrderListItem) => void;
  onDuplicate: (order: OrderListItem) => void;
  onSetStatus: (order: OrderListItem, status: ProjectStatus) => void;
  onDelete: (order: OrderListItem) => void;
}

/** `active` orders past their due date are the only ones flagged — a closed
 *  order's date is history, not a deadline it missed. */
function isOverdue(order: OrderListItem): boolean {
  if (order.status !== 'active' || !order.due_date) return false;
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  return new Date(order.due_date) < startOfToday;
}

/** Prevents the menu (and everything inside it) from bubbling into the card's
 *  own `<Link>` — without both `preventDefault` and `stopPropagation` a click
 *  on "Edit" would also navigate to the order page. */
function stopCardNavigation(e: React.MouseEvent) {
  e.preventDefault();
  e.stopPropagation();
}

/**
 * One order in the grid. Every figure (`ordered`, `printed`, `progress`,
 * `lines_count`) is displayed exactly as the server sent it — never
 * recomputed here (design decision 8).
 */
export function OrderCard({ order, onEdit, onDuplicate, onSetStatus, onDelete }: OrderCardProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const overdue = isOverdue(order);
  // Three at most: the strip is a hint at what is in the order, not its contents.
  const lineProducts = (order.line_products ?? []).slice(0, 3);

  return (
    <Link
      to={`/projects/${order.id}`}
      data-testid={`order-${order.id}-card`}
      className="block @container rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary hover:border-bambu-green/50 overflow-hidden"
    >
      <div className="h-1.5" style={{ backgroundColor: order.color || '#6b7280' }} />

      <div className="p-4 flex gap-3 @max-[22rem]:flex-col">
        {order.cover_image_filename ? (
          <img
            src={api.getProjectCoverImageUrl(order.id)}
            alt=""
            className="w-20 h-20 @max-[22rem]:w-full @max-[22rem]:h-24 flex-shrink-0 rounded-lg object-cover bg-bambu-dark"
          />
        ) : (
          lineProducts.length > 0 && (
            <div className="flex gap-1 flex-shrink-0">
              {lineProducts.map((line, i) =>
                line.has_cover ? (
                  <img
                    key={`${line.product_id}-${i}`}
                    data-testid="product-cover"
                    src={api.getProductCoverImageUrl(line.product_id)}
                    alt=""
                    className="w-9 h-9 rounded-lg object-cover bg-bambu-dark"
                  />
                ) : (
                  // A line whose product has no cover still keeps its tile: the
                  // strip's length is how many lines the order has, and dropping
                  // the coverless ones would quietly misreport that.
                  <div
                    key={`${line.product_id}-${i}`}
                    data-testid="product-cover-placeholder"
                    className="w-9 h-9 rounded-lg bg-bambu-dark flex items-center justify-center"
                  >
                    <Package className="w-4 h-4 text-bambu-gray" />
                  </div>
                ),
              )}
            </div>
          )
        )}

        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex items-start justify-between gap-2">
            <h3 className="font-semibold text-white truncate">{order.name}</h3>
            <div className="relative flex-shrink-0" onClick={stopCardNavigation}>
              <button
                type="button"
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
                  <div className="absolute right-0 top-8 z-20 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 min-w-[160px]">
                    {hasPermission('projects:update') && (
                      <button
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onEdit(order);
                          setMenuOpen(false);
                        }}
                      >
                        <Pencil className="w-4 h-4" />
                        {t('orders.card.menu.edit')}
                      </button>
                    )}
                    {hasPermission('projects:create') && (
                      <button
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onDuplicate(order);
                          setMenuOpen(false);
                        }}
                      >
                        <Copy className="w-4 h-4" />
                        {t('orders.card.menu.duplicate')}
                      </button>
                    )}
                    {hasPermission('projects:update') && order.status === 'active' && (
                      <button
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onSetStatus(order, 'completed');
                          setMenuOpen(false);
                        }}
                      >
                        <CheckCircle2 className="w-4 h-4" />
                        {t('orders.card.menu.complete')}
                      </button>
                    )}
                    {hasPermission('projects:update') && order.status !== 'active' && (
                      <button
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onSetStatus(order, 'active');
                          setMenuOpen(false);
                        }}
                      >
                        <RotateCcw className="w-4 h-4" />
                        {t('orders.card.menu.reopen')}
                      </button>
                    )}
                    {hasPermission('projects:update') && order.status === 'active' && (
                      <button
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onSetStatus(order, 'cancelled');
                          setMenuOpen(false);
                        }}
                      >
                        <Ban className="w-4 h-4" />
                        {t('orders.card.menu.cancel')}
                      </button>
                    )}
                    {hasPermission('projects:delete') && (
                      <button
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm flex items-center gap-2 text-red-500 hover:bg-bambu-dark"
                        onClick={(e) => {
                          stopCardNavigation(e);
                          onDelete(order);
                          setMenuOpen(false);
                        }}
                      >
                        <Trash2 className="w-4 h-4" />
                        {t('orders.card.menu.delete')}
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>

          {order.customer_name && <p className="text-sm text-bambu-gray truncate">{order.customer_name}</p>}

          <div className="flex items-center gap-1.5 flex-wrap">
            <StatusBadge status={order.status} />
            <PriorityBadge priority={order.priority} />
          </div>

          {order.due_date && (
            <p className={`text-xs ${overdue ? 'text-red-500' : 'text-bambu-gray'}`}>
              {new Date(order.due_date).toLocaleDateString()}
              {overdue && <span> · {t('orders.card.overdue')}</span>}
            </p>
          )}

          <ProgressBar value={order.printed} max={order.ordered} testId={`order-${order.id}-progress`} />

          <p className="text-xs text-bambu-gray">{t('orders.card.lines', { count: order.lines_count })}</p>
        </div>
      </div>
    </Link>
  );
}
