import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { OrderForecast, OrderListItem } from '../../api/client';
import { ProgressBar } from './ProgressBar';
import { StatusBadge } from './StatusBadge';
import { ForecastHint } from './ForecastHint';
import { etaFull, etaShort, hoursMinutes } from '../../utils/forecast';

type SortKey = 'name' | 'due' | 'progress' | 'remaining' | 'printing' | 'queued' | 'ready' | 'hours';

const remaining = (o: OrderListItem) => Math.max(0, o.ordered - o.printed - o.from_stock_units);

/** A fresh click on one of these sorts most-first; `name` and `due` sort
 *  ascending instead — A→Z, soonest due first. `ready` joins them (soonest
 *  ETA first); only `hours` (machine time left) sorts most-first. */
const DESC_FIRST: ReadonlySet<SortKey> = new Set(['printing', 'queued', 'remaining', 'progress', 'hours']);

/**
 * The orders list as a table — the farm's roll-up (spec 2026-09-06, Slice F).
 * Every number is the server's; the only arithmetic is `remaining`, which is
 * the same subtraction `project_figures` does and is shown nowhere else on
 * the list. Default order: due date, then name; a header click sorts by that
 * column and clicks again to flip.
 */
export function OrdersTable({
  orders,
  forecasts,
  forecastError,
}: {
  orders: OrderListItem[];
  forecasts?: Record<number, OrderForecast>;
  /** The batch fetch failed or was refused — three distinct states share these
   *  two cells, and only one of them is about the farm: «…» is still loading,
   *  «No estimate» means the simulation could place nothing, and a dash with
   *  the error hint means we never got an answer to read. */
  forecastError?: boolean;
}) {
  const { t } = useTranslation();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });
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
        case 'ready': return forecasts?.[o.id]?.now_eta ? Date.parse(forecasts[o.id].now_eta!) : Number.MAX_SAFE_INTEGER;
        case 'hours': return forecasts?.[o.id]?.machine_seconds ?? -1;
      }
    };
    return [...orders].sort((a, b) => {
      const av = value(a), bv = value(b);
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return (sort.desc ? -cmp : cmp) || a.name.localeCompare(b.name);
    });
  }, [orders, sort, forecasts]);

  const header = (key: SortKey, label: string) => (
    <th className="font-normal p-2 text-left">
      <button type="button" onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : DESC_FIRST.has(key) }))} className="hover:text-white">
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
            {header('ready', t('orders.table.readyAt'))}
            {header('hours', t('orders.table.machineHours'))}
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
                <td className="p-2 text-xs whitespace-nowrap" data-testid={`order-${o.id}-ready`}>
                  {forecastError ? (
                    <span title={t('farmForecast.error')}>—</span>
                  ) : o.status !== 'active' ? (
                    // Closed = nothing is planned, so there is nothing to date.
                    '—'
                  ) : !forecasts ? (
                    '…'
                  ) : !forecasts[o.id]?.now_eta ? (
                    t('farmForecast.unavailable')
                  ) : (
                    <>
                      <span title={etaFull(forecasts[o.id].now_eta, settings?.time_format, settings?.date_format)}>
                        {etaShort(forecasts[o.id].now_eta, settings?.time_format)}
                      </span>{' '}
                      <ForecastHint forecast={forecasts[o.id]} />
                      {forecasts[o.id].after_eta && forecasts[o.id].after_eta !== forecasts[o.id].now_eta && (
                        <div className="text-bambu-gray" data-testid={`order-${o.id}-after`}>
                          {t('orders.figures.afterAhead', { count: forecasts[o.id].ahead_count, when: etaShort(forecasts[o.id].after_eta, settings?.time_format) })}
                        </div>
                      )}
                    </>
                  )}
                </td>
                <td className="p-2 text-right tabular-nums" data-testid={`order-${o.id}-machine-hours`}>
                  {forecastError ? (
                    <span title={t('farmForecast.error')}>—</span>
                  ) : o.status !== 'active' ? (
                    '—'
                  ) : forecasts ? (
                    hoursMinutes(forecasts[o.id]?.machine_seconds)
                  ) : (
                    '…'
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
