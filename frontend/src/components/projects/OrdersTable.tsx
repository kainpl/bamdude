import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { OrderForecast, OrderListItem } from '../../api/client';
import { ProgressBar } from './ProgressBar';
import { StatusBadge } from './StatusBadge';
import { ForecastHint } from './ForecastHint';
import { etaFull, etaShort, hoursMinutes } from '../../utils/forecast';

type SortKey = 'name' | 'customer' | 'due' | 'progress' | 'remaining' | 'printing' | 'queued' | 'ready' | 'hours';

/** A fresh click on one of these sorts most-first; `name` and `due` sort
 *  ascending instead — A→Z, soonest due first. `ready` joins them (soonest
 *  ETA first); only `hours` (machine time left) sorts most-first. */
const DESC_FIRST: ReadonlySet<SortKey> = new Set(['printing', 'queued', 'remaining', 'progress', 'hours']);

/** Columns the SERVER can order the whole list by (spec projects-lists-parity,
 *  rule 5) — the same names as its `sort_by` keys. The two forecast columns
 *  are not among them: the forecast is a separate advisory batch over the rows
 *  on screen, so those sort within the page only. */
const SERVER_KEYS: ReadonlySet<SortKey> = new Set(['name', 'customer', 'due', 'progress', 'remaining', 'printing', 'queued']);

/**
 * The orders list as a table — the farm's roll-up (spec 2026-09-06, Slice F).
 * Every number is the server's.  In particular, `remaining` is the sum of
 * per-line deficits, so production surplus for one product cannot mask a
 * shortage in another. Default order: due date, then name; a header click
 * sorts by that column and clicks again to flip.
 *
 * With `sort` + `onSortChange` the rows are ONE PAGE of a server-sorted list:
 * a click on a server column asks the server (`sort_by`) and the rows keep its
 * order; a click on a forecast column sorts this page only, and its header
 * says so in the tooltip. The customer column sorts only there — on the
 * server, whose order puts orders without a customer last. `footer` (the page
 * bar) is drawn inside the same card, under the rows.
 */
export function OrdersTable({
  orders,
  forecasts,
  forecastError,
  sort: serverSort,
  onSortChange,
  footer,
}: {
  orders: OrderListItem[];
  forecasts?: Record<number, OrderForecast>;
  /** The list's `sort_by` (e.g. `updated-desc`) when the SERVER sorts it. */
  sort?: string;
  onSortChange?: (sortBy: string) => void;
  footer?: ReactNode;
  /** The batch fetch failed or was refused — three distinct states share these
   *  two cells, and only one of them is about the farm: «…» is still loading,
   *  «No estimate» means the simulation could place nothing, and a dash with
   *  the error hint means we never got an answer to read. */
  forecastError?: boolean;
}) {
  const { t } = useTranslation();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });
  const paged = onSortChange != null;
  // Paged: the rows arrive in the server's order, and only a forecast column
  // re-sorts them locally. Unpaged: the table sorts everything itself, as before.
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean } | null>(paged ? null : { key: 'due', desc: false });
  const [serverKey, serverDir] = (serverSort ?? '').split(/-(?=asc$|desc$)/);

  const sorted = useMemo(() => {
    if (!sort) return orders;
    const value = (o: OrderListItem): string | number => {
      switch (sort.key) {
        case 'name': return o.name.toLowerCase();
        // A server key — its header exists in paged mode only; listed for the switch.
        case 'customer': return o.customer_name?.toLowerCase() ?? '';
        case 'due': return o.due_date ? Date.parse(o.due_date) : Number.MAX_SAFE_INTEGER;
        case 'progress': return o.progress;
        case 'remaining': return o.remaining;
        case 'printing': return o.prints_in_progress;
        case 'queued': return o.prints_queued;
        case 'ready': return forecasts?.[o.id]?.eta_complete && forecasts[o.id]?.now_eta ? Date.parse(forecasts[o.id].now_eta!) : Number.MAX_SAFE_INTEGER;
        case 'hours': return forecasts?.[o.id]?.machine_seconds ?? -1;
      }
    };
    return [...orders].sort((a, b) => {
      const av = value(a), bv = value(b);
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return (sort.desc ? -cmp : cmp) || a.name.localeCompare(b.name);
    });
  }, [orders, sort, forecasts]);

  const header = (key: SortKey, label: string) => {
    const onServer = paged && SERVER_KEYS.has(key);
    const active = onServer ? !sort && serverKey === key : sort?.key === key;
    const desc = onServer ? serverDir === 'desc' : !!sort?.desc;
    const click = () => {
      if (onServer) {
        const next = serverKey === key ? (serverDir === 'desc' ? 'asc' : 'desc') : DESC_FIRST.has(key) ? 'desc' : 'asc';
        setSort(null);
        onSortChange!(`${key}-${next}`);
      } else {
        setSort((s) => ({ key, desc: s?.key === key ? !s.desc : DESC_FIRST.has(key) }));
      }
    };
    return (
      <th className="font-normal p-2 text-left" aria-sort={active ? (desc ? 'descending' : 'ascending') : undefined}>
        <button
          type="button"
          onClick={click}
          title={paged && !onServer ? t('orders.table.forecastSortHint') : undefined}
          className="hover:text-white"
        >
          {label}
          {active && <span aria-hidden> {desc ? '▼' : '▲'}</span>}
        </button>
      </th>
    );
  };

  return (
    <div className="rounded-xl border border-bambu-dark-tertiary overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs text-bambu-gray bg-bambu-dark-secondary">
            <tr>
              {header('name', t('orders.table.name'))}
              {paged ? header('customer', t('orders.table.customer')) : <th className="font-normal p-2 text-left">{t('orders.table.customer')}</th>}
              <th className="font-normal p-2 text-left">{t('orders.table.status')}</th>
              <th className="font-normal p-2 text-right">{t('orders.table.ordered')}</th>
              <th className="font-normal p-2 text-right">{t('orders.table.printed')}</th>
              <th className="font-normal p-2 text-right">{t('orders.table.fromStock')}</th>
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
                  <td className="p-2 text-right tabular-nums">{o.printed}</td>
                  <td className="p-2 text-right tabular-nums">{o.from_stock_units}</td>
                  <td className="p-2 text-right tabular-nums" data-testid={`order-${o.id}-printing`}>{o.prints_in_progress}</td>
                  <td className="p-2 text-right tabular-nums" data-testid={`order-${o.id}-queued`}>{o.prints_queued}</td>
                  <td className="p-2 text-right tabular-nums">{o.remaining}</td>
                  <td className="p-2 min-w-[8rem]"><ProgressBar value={o.covered_units} max={o.ordered} progress={o.progress} testId={`order-${o.id}-table-progress`} /></td>
                  <td className={`p-2 text-xs ${overdue ? 'text-red-500' : 'text-bambu-gray'}`}>{o.due_date ? new Date(o.due_date).toLocaleDateString() : ''}</td>
                  <td className="p-2 text-xs whitespace-nowrap" data-testid={`order-${o.id}-ready`}>
                    {forecastError ? (
                      <span title={t('farmForecast.error')}>—</span>
                    ) : o.status !== 'active' ? (
                      // Closed = nothing is planned, so there is nothing to date.
                      '—'
                    ) : !forecasts ? (
                      '…'
                    ) : !forecasts[o.id]?.eta_complete ? (
                      t('orders.figures.readyIncomplete')
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
      {footer}
    </div>
  );
}
