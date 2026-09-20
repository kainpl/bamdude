import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { api, STOCK_REASONS } from '../../api/client';
import { useStockMovements } from '../../hooks/useStock';
import type { StockJournalFilters } from '../../hooks/useStock';
import { formatDateOnly } from '../../utils/date';
import type { DateFormat } from '../../utils/date';
import { Button } from '../Button';
import { isNoteToken, signed } from '../products/stockMovementHelpers';
import { MovementSource } from '../products/MovementSource';
import { Select } from '../Select';

/**
 * The farm's ledger, newest first, with the older pages loaded on demand.
 *
 * ⚠️ **The cursor is the server's.** `next_before_id` comes back only on a
 * full page; the hook turns its absence into "no next page", and the footer says so
 * — the operator must never read the oldest row shown as the first movement
 * there ever was.
 */
export function StockJournal() {
  const { t } = useTranslation();
  const [filters, setFilters] = useState<StockJournalFilters>({});
  const { data, isError, hasNextPage, fetchNextPage, isFetchingNextPage } = useStockMovements(filters);
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });
  const dateFormat = (settings?.date_format || 'system') as DateFormat;
  // The filter's options come from the CATALOG, not from the (possibly
  // filtered) summary this page shows: a product whose shelf just zeroed out
  // still has history to filter by, and a product already picked here must
  // never vanish from the list because the summary's own filters moved.
  // `include_adhoc` is explicit — a one-off product can hold stock and
  // history too, and neither the summary nor the journal applies an origin
  // filter, so the options must not either.
  const { data: catalog = [] } = useQuery({
    queryKey: ['products', { include_adhoc: true }],
    queryFn: () => api.getProducts({ include_adhoc: true }),
  });

  const rows = data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <section className="space-y-3" data-testid="stock-journal">
      <div className="flex items-end gap-3 flex-wrap">
        <h2 className="text-lg font-medium text-white">{t('stock.page.journal')}</h2>
        <label className="text-xs text-bambu-gray flex flex-col gap-1">
          {t('stock.page.filterProduct')}
          <Select
            value={filters.product_id ?? ''}
            onChange={(e) => setFilters((f) => ({ ...f, product_id: e.target.value ? Number(e.target.value) : undefined }))}
          >
            <option value="">{t('stock.page.anyProduct')}</option>
            {catalog.filter((p) => p.parts_count > 0).map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </Select>
        </label>
        <label className="text-xs text-bambu-gray flex flex-col gap-1">
          {t('stock.page.filterReason')}
          <Select
            value={filters.reason ?? ''}
            onChange={(e) => setFilters((f) => ({ ...f, reason: e.target.value || undefined }))}
          >
            <option value="">{t('stock.page.anyReason')}</option>
            {STOCK_REASONS.map((r) => (
              <option key={r} value={r}>{t(`stock.reason.${r}`)}</option>
            ))}
          </Select>
        </label>
      </div>

      {!data ? (
        isError ? (
          <p className="text-sm text-red-500">{t('stock.page.error')}</p>
        ) : (
          <p className="flex items-center gap-2 text-sm text-bambu-gray"><Loader2 className="w-4 h-4 animate-spin" />{t('common.loading')}</p>
        )
      ) : rows.length === 0 ? (
        <p className="text-sm text-bambu-gray">
          {t(filters.product_id != null || filters.reason ? 'stock.page.journalEmptyFiltered' : 'stock.page.journalEmpty')}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-bambu-gray text-left">
                <th className="font-normal p-2">{t('stock.date')}</th>
                <th className="font-normal p-2">{t('stock.page.product')}</th>
                <th className="font-normal p-2">{t('stock.part')}</th>
                <th className="font-normal p-2">{t('stock.change')}</th>
                <th className="font-normal p-2">{t('stock.reasonColumn')}</th>
                <th className="font-normal p-2">{t('stock.reference')}</th>
                <th className="font-normal p-2">{t('stock.noteColumn')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => {
                const note = m.note ? (isNoteToken(m.note) ? t(`stock.note.${m.note}`) : m.note) : null;
                return (
                  <tr key={m.id} data-testid={`stock-movement-${m.id}`} className="border-t border-bambu-dark-tertiary text-white">
                    {/* ⚠️ `formatDateOnly`, never `new Date(x).toLocaleDateString()`:
                        the column is NAIVE UTC (no `Z`), which the platform
                        parser reads as LOCAL time — at UTC+3 the last three
                        hours of every UTC day would be dated yesterday. The
                        helper appends the `Z` and honours the user's own
                        `date_format`, exactly as `OrderPrints` does. */}
                    <td className="p-2 text-bambu-gray whitespace-nowrap">{formatDateOnly(m.created_at, undefined, dateFormat)}</td>
                    <td className="p-2">{m.product_name}</td>
                    <td className="p-2">{m.part_name}</td>
                    <td className={`p-2 tabular-nums ${m.delta > 0 ? 'text-bambu-green' : 'text-red-400'}`}>{signed(m.delta)}</td>
                    <td className="p-2">{t(`stock.reason.${m.reason}`, { defaultValue: m.reason })}</td>
                    <td className="p-2"><MovementSource movement={m} /></td>
                    <td className="p-2 text-bambu-gray">{note ?? '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {data && rows.length > 0 && (
        hasNextPage ? (
          <Button size="sm" variant="secondary" onClick={() => fetchNextPage()} disabled={isFetchingNextPage}>
            {t('stock.page.loadOlder')}
          </Button>
        ) : (
          <p className="text-xs text-bambu-gray">{t('stock.page.wholeLedger')}</p>
        )
      )}
    </section>
  );
}
