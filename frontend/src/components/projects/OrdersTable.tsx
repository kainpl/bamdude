import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { OrderListItem } from '../../api/client';
import { ProgressBar } from './ProgressBar';
import { StatusBadge } from './StatusBadge';

type SortKey = 'name' | 'due' | 'progress' | 'remaining' | 'printing' | 'queued';

const remaining = (o: OrderListItem) => Math.max(0, o.ordered - o.printed - o.from_stock_units);

/**
 * The orders list as a table — the farm's roll-up (spec 2026-09-06, Slice F).
 * Every number is the server's; the only arithmetic is `remaining`, which is
 * the same subtraction `project_figures` does and is shown nowhere else on
 * the list. Default order: due date, then name; a header click sorts by that
 * column and clicks again to flip.
 */
export function OrdersTable({ orders }: { orders: OrderListItem[] }) {
  const { t } = useTranslation();
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: 'due', desc: false });

  const sorted = useMemo(() => {
    const value = (o: OrderListItem): string | number => {
      switch (sort.key) {
        case 'name': return o.name.toLowerCase();
        case 'due': return o.due_date ? Date.parse(o.due_date) : Number.MAX_SAFE_INTEGER;
        case 'progress': return o.progress;
        case 'remaining': return remaining(o);
        case 'printing': return o.prints_in_progress;
        case 'queued': return o.prints_queued;
      }
    };
    return [...orders].sort((a, b) => {
      const av = value(a), bv = value(b);
      const cmp = av < bv ? -1 : av > bv ? 1 : a.name.localeCompare(b.name);
      return sort.desc ? -cmp : cmp;
    });
  }, [orders, sort]);

  const header = (key: SortKey, label: string) => (
    <th className="font-normal p-2 text-left">
      <button type="button" onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : true }))} className="hover:text-white">
        {label}
        {sort.key === key && <span aria-hidden> {sort.desc ? '▼' : '▲'}</span>}
      </button>
    </th>
  );

  return (
    <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary">
      <table className="w-full text-sm">
        <thead className="text-xs text-bambu-gray bg-bambu-dark-secondary">
          <tr>
            {header('name', t('orders.table.name'))}
            <th className="font-normal p-2 text-left">{t('orders.table.customer')}</th>
            <th className="font-normal p-2 text-left">{t('orders.table.status')}</th>
            <th className="font-normal p-2 text-right">{t('orders.table.ordered')}</th>
            <th className="font-normal p-2 text-right">{t('orders.table.printed')}</th>
            {header('printing', t('orders.table.printing'))}
            {header('queued', t('orders.table.queued'))}
            {header('remaining', t('orders.table.remaining'))}
            {header('progress', t('orders.table.progress'))}
            {header('due', t('orders.table.due'))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((o) => {
            const overdue = o.due_date != null && o.status === 'active' && Date.parse(o.due_date) < Date.now();
            return (
              <tr key={o.id} className="border-t border-bambu-dark-tertiary text-white">
                <td className="p-2"><Link to={`/projects/${o.id}`} className="hover:underline">{o.name}</Link></td>
                <td className="p-2 text-bambu-gray">{o.customer_name ?? ''}</td>
                <td className="p-2"><StatusBadge status={o.status} /></td>
                <td className="p-2 text-right tabular-nums">{o.ordered}</td>
                <td className="p-2 text-right tabular-nums">
                  {o.printed}
                  {o.from_stock_units > 0 && <span className="text-xs text-bambu-gray"> +{o.from_stock_units}</span>}
                </td>
                <td className="p-2 text-right tabular-nums" data-testid={`order-${o.id}-printing`}>{o.prints_in_progress}</td>
                <td className="p-2 text-right tabular-nums" data-testid={`order-${o.id}-queued`}>{o.prints_queued}</td>
                <td className="p-2 text-right tabular-nums">{remaining(o)}</td>
                <td className="p-2 min-w-[8rem]"><ProgressBar value={o.printed} max={o.ordered} testId={`order-${o.id}-table-progress`} /></td>
                <td className={`p-2 text-xs ${overdue ? 'text-red-500' : 'text-bambu-gray'}`}>{o.due_date ? new Date(o.due_date).toLocaleDateString() : ''}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
