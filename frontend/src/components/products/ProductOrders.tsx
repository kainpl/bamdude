import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import { StatusBadge } from '../projects/StatusBadge';
import { ProgressBar } from '../projects/ProgressBar';

/**
 * Every order that asks for this product.
 *
 * The rows are the order LIST rows — `printed` and `ordered` are the server's
 * counts across the whole order, not this product's share of it. That is the
 * honest number to show here: an order's progress is what tells the operator
 * whether opening it is worth it, and inventing a per-product slice of it on
 * the client would be a fourth place that counts prints (design decision 8).
 *
 * The query key is the shared `['projects', …]` prefix, so an order edited
 * anywhere else drops this list too.
 */
export function ProductOrders({ productId }: { productId: number }) {
  const { t } = useTranslation();

  const { data: orders = [], isLoading } = useQuery({
    queryKey: ['projects', { product_id: productId }],
    // Arrow, never `queryFn: api.getOrders` — TanStack would hand the query
    // context to a function whose only parameter is the params object.
    queryFn: () => api.getOrders({ product_id: productId }),
    enabled: Number.isFinite(productId),
  });

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-white">{t('products.orders.title')}</h2>

      {!isLoading && orders.length === 0 && <p className="text-sm text-bambu-gray">{t('products.orders.empty')}</p>}

      {orders.length > 0 && (
        <div className="rounded-xl border border-bambu-dark-tertiary divide-y divide-bambu-dark-tertiary">
          {orders.map((order) => (
            <div key={order.id} className="flex items-center gap-3 flex-wrap p-3">
              <Link to={`/projects/${order.id}`} className="text-white hover:text-bambu-green transition-colors truncate">
                {order.name}
              </Link>
              {order.customer_name && <span className="text-sm text-bambu-gray truncate">{order.customer_name}</span>}
              <StatusBadge status={order.status} />
              <div className="ml-auto w-40">
                <ProgressBar value={order.printed} max={order.ordered} testId={`product-order-${order.id}-progress`} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
