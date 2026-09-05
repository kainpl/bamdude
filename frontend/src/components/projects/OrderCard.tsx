import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Pencil, Copy, CheckCircle2, RotateCcw, Ban, Trash2, Package } from 'lucide-react';
import { api } from '../../api/client';
import type { OrderListItem, ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { CardActionMenu, CardActionMenuItem } from '../CardActionMenu';
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

/**
 * One order in the grid. Every figure (`ordered`, `printed`, `progress`,
 * `lines_count`) is displayed exactly as the server sent it — never
 * recomputed here (design decision 8).
 *
 * ⚠️ **The link is an OVERLAY, not the card's wrapper.** The menu button used
 * to sit inside the `<a>` — invalid HTML — and every one of its items had to
 * cancel the navigation its own click caused. One item added without that
 * guard navigated instead of acting, and a keyboard activation navigated
 * whatever the guard said. Now the anchor covers the card from on top
 * (`absolute inset-0`, named by `aria-label` because it wraps no text) and the
 * menu is an ordinary sibling above it: there is nothing left to cancel.
 */
export function OrderCard({ order, onEdit, onDuplicate, onSetStatus, onDelete }: OrderCardProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const overdue = isOverdue(order);
  // Three at most: the strip is a hint at what is in the order, not its contents.
  const lineProducts = (order.line_products ?? []).slice(0, 3);

  return (
    <div
      data-testid={`order-${order.id}-card`}
      className="relative @container rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary hover:border-bambu-green/50 overflow-hidden"
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
            {/* Above the overlay link, so the trigger is clickable at all. */}
            <div className="relative z-10 flex-shrink-0">
              <CardActionMenu label={t('common.actions')} testId={`order-${order.id}-menu`} width={160}>
                {(close) => (
                  <>
                    {hasPermission('projects:update') && (
                      <CardActionMenuItem
                        onSelect={() => {
                          onEdit(order);
                          close();
                        }}
                      >
                        <Pencil className="w-4 h-4" />
                        {t('orders.card.menu.edit')}
                      </CardActionMenuItem>
                    )}
                    {hasPermission('projects:create') && (
                      <CardActionMenuItem
                        onSelect={() => {
                          onDuplicate(order);
                          close();
                        }}
                      >
                        <Copy className="w-4 h-4" />
                        {t('orders.card.menu.duplicate')}
                      </CardActionMenuItem>
                    )}
                    {hasPermission('projects:update') && order.status === 'active' && (
                      <CardActionMenuItem
                        onSelect={() => {
                          onSetStatus(order, 'completed');
                          close();
                        }}
                      >
                        <CheckCircle2 className="w-4 h-4" />
                        {t('orders.card.menu.complete')}
                      </CardActionMenuItem>
                    )}
                    {hasPermission('projects:update') && order.status !== 'active' && (
                      <CardActionMenuItem
                        onSelect={() => {
                          onSetStatus(order, 'active');
                          close();
                        }}
                      >
                        <RotateCcw className="w-4 h-4" />
                        {t('orders.card.menu.reopen')}
                      </CardActionMenuItem>
                    )}
                    {hasPermission('projects:update') && order.status === 'active' && (
                      <CardActionMenuItem
                        onSelect={() => {
                          onSetStatus(order, 'cancelled');
                          close();
                        }}
                      >
                        <Ban className="w-4 h-4" />
                        {t('orders.card.menu.cancel')}
                      </CardActionMenuItem>
                    )}
                    {hasPermission('projects:delete') && (
                      <CardActionMenuItem
                        danger
                        onSelect={() => {
                          onDelete(order);
                          close();
                        }}
                      >
                        <Trash2 className="w-4 h-4" />
                        {t('orders.card.menu.delete')}
                      </CardActionMenuItem>
                    )}
                  </>
                )}
              </CardActionMenu>
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

          {/* Beside the printed count, and only when there is something to say
              (pass 8, Decision 5). `printed` stays literal — the farm printed
              that many — and this is the other half of "done".
              ⚠️ `?? 0` because the LIST response does not carry the field yet:
              the order DETAIL's figures do, the list's do not, so today this
              renders nothing. `> 0`, never a bare `&&` on the number. */}
          {(order.from_stock_units ?? 0) > 0 && (
            <p className="text-xs text-bambu-gray" data-testid={`order-${order.id}-from-stock`}>
              {t('stock.order.fromStock', { n: order.from_stock_units })}
            </p>
          )}

          <p className="text-xs text-bambu-gray">{t('orders.card.lines', { count: order.lines_count })}</p>
        </div>
      </div>

      <Link
        to={`/projects/${order.id}`}
        aria-label={order.name}
        className="absolute inset-0 rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
      />
    </div>
  );
}
