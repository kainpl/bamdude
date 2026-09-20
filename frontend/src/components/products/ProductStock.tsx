import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, PackageCheck } from 'lucide-react';
import { api, STOCK_MOVEMENT_LIMIT } from '../../api/client';
import { useProductStock } from '../../hooks/useProductStock';
import { formatDateOnly } from '../../utils/date';
import type { DateFormat } from '../../utils/date';
import { Button } from '../Button';
import { AdjustStockDialog } from './AdjustStockDialog';
import { MovementSource } from './MovementSource';
import { isNoteToken, signed } from './stockMovementHelpers';

interface ProductStockProps {
  productId: number;
  /** `projects:update` — the page asks the question once and hands the answer
   *  down, exactly as it does for every other section. */
  canEdit: boolean;
}

/**
 * The product's free stock: what is on the shelf, how many kits it makes, and
 * every movement that got it there (pass 8, Decision 6).
 *
 * ⚠️ **Every number here is the server's.** `kits_available` is the `min` over
 * the counted parts of `balance / qty_per_unit` and is computed in the ledger,
 * not out of the `balances` list below it — the two would drift the first time
 * a part stopped counting, and the operator would be told a kit is available
 * that the reservation then refuses.
 *
 * ⚠️ **A movement's part is named from `part_name`, never looked up in
 * `balances`.** A part that stopped counting keeps its history but leaves the
 * balances, so the lookup would render a blank for exactly the rows that need
 * explaining.
 *
 * ⚠️ **A failed fetch is not an empty shelf, and neither is a pending one.**
 * "Nothing on the shelf yet" over a request that never came back tells the
 * operator their stock is gone — and this section's whole job is to say what is
 * there. The three states are rendered apart, in one ternary, for both tables.
 *
 * ⚠️ **Data before status** (finding I1). The gate below is `!data`, never
 * `isError`: a BACKGROUND refetch that fails leaves the shelf on screen exactly
 * as it was, the same rule the order, product and customer pages follow. The
 * hook carries `meta: { refreshToast: true }` so that silence is reported once,
 * rather than by blanking a section somebody is reading.
 */
export function ProductStock({ productId, canEdit }: ProductStockProps) {
  const { t } = useTranslation();
  const [adjusting, setAdjusting] = useState(false);

  const { data, isError } = useProductStock(productId);
  // The user's own date format, fetched the way every other date-showing screen
  // fetches it; `formatDateOnly` covers the unresolved first paint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });
  const dateFormat = (settings?.date_format || 'system') as DateFormat;

  const balances = data?.balances ?? [];
  const movements = data?.movements ?? [];

  // ⚠️ **One query, so ONE unsettled state for the whole section.** The shelf
  // and the ledger below it come out of the same request, so a spinner (or an
  // error) per table would be the same sentence printed twice; and rendering
  // the ledger's "nothing has moved yet" beside a failed shelf would assert
  // something this component does not know. Both tables — and the movements
  // heading — therefore live behind this early return.
  //
  // ⚠️ Gated on `!data`, NOT on `isError`: with data in hand the section keeps
  // rendering it whatever the last refetch did (finding I1). So this branch is
  // the FIRST load only — pending, or failed with nothing to show.
  if (!data) {
    return (
      <section className="space-y-3" data-testid="product-stock">
        <h2 className="text-lg font-medium text-white flex items-center gap-2">
          <PackageCheck className="w-5 h-5 text-bambu-green" />
          {t('stock.title')}
        </h2>
        {isError ? (
          <p className="text-sm text-red-500" data-testid="stock-error">
            {t('stock.error')}
          </p>
        ) : (
          <p className="flex items-center gap-2 text-sm text-bambu-gray">
            <Loader2 className="w-4 h-4 animate-spin" />
            {t('common.loading')}
          </p>
        )}
      </section>
    );
  }

  return (
    <section className="space-y-3" data-testid="product-stock">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className="text-lg font-medium text-white flex items-center gap-2">
          <PackageCheck className="w-5 h-5 text-bambu-green" />
          {t('stock.title')}
        </h2>
        {canEdit && balances.length > 0 && (
          <Button size="sm" variant="secondary" onClick={() => setAdjusting(true)}>
            {t('stock.adjust.open')}
          </Button>
        )}
      </div>

      {/* ⚠️ An empty `balances` means the product COUNTS nothing — every counted
          part is in the answer, with a 0 where nothing has moved (finding M2).
          So a shelf that is merely empty renders the table full of zeros under
          a «0 комплектів» headline, which is the honest reading: the parts
          exist, there are none of them. This sentence is only for a product
          with no printed part to count at all, where a table of nothing and a
          kit count would both be noise. */}
      {balances.length === 0 ? (
        <p className="text-sm text-bambu-gray" data-testid="stock-no-counted-parts">
          {t('stock.noCountedParts')}
        </p>
      ) : (
        <div className="space-y-2">
          <p className="text-xl font-semibold text-white" data-testid="stock-kits">
            {t('stock.kits', { count: data?.kits_available ?? 0 })}
          </p>
          <p className="text-xs text-bambu-gray">{t('stock.kitsHint')}</p>

          <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-bambu-gray text-left">
                  <th className="font-normal p-2">{t('stock.part')}</th>
                  <th className="font-normal p-2">{t('stock.perUnit')}</th>
                  <th className="font-normal p-2">{t('stock.balance')}</th>
                </tr>
              </thead>
              <tbody>
                {balances.map((b) => (
                  <tr key={b.part_id} className="border-t border-bambu-dark-tertiary text-white">
                    <td className="p-2">{b.name}</td>
                    <td className="p-2 tabular-nums">{b.qty_per_unit}</td>
                    <td className="p-2 tabular-nums" data-testid={`stock-balance-${b.part_id}`}>
                      {b.balance}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <h3 className="text-sm font-medium text-white pt-1">{t('stock.movements')}</h3>
      {/* Reached only on a SETTLED success (see the early return above), so
          "nothing has moved yet" is a fact about the ledger and not about the
          request. */}
      {movements.length === 0 ? (
        <p className="text-sm text-bambu-gray">{t('stock.noMovements')}</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-bambu-gray text-left">
                <th className="font-normal p-2">{t('stock.date')}</th>
                <th className="font-normal p-2">{t('stock.part')}</th>
                <th className="font-normal p-2">{t('stock.change')}</th>
                <th className="font-normal p-2">{t('stock.reasonColumn')}</th>
                <th className="font-normal p-2">{t('stock.reference')}</th>
                <th className="font-normal p-2">{t('stock.noteColumn')}</th>
              </tr>
            </thead>
            <tbody>
              {movements.map((m) => {
                const note = m.note ? (isNoteToken(m.note) ? t(`stock.note.${m.note}`) : m.note) : null;
                return (
                  <tr
                    key={m.id}
                    data-testid={`stock-movement-${m.id}`}
                    className="border-t border-bambu-dark-tertiary text-white"
                  >
                    {/* ⚠️ `formatDateOnly`, never `new Date(x).toLocaleDateString()`:
                        the column is NAIVE UTC (no `Z`), which the platform
                        parser reads as LOCAL time — at UTC+3 the last three
                        hours of every UTC day would be dated yesterday. The
                        helper appends the `Z` and honours the user's own
                        `date_format`, exactly as `OrderPrints` does. */}
                    <td className="p-2 text-bambu-gray whitespace-nowrap">
                      {formatDateOnly(m.created_at, undefined, dateFormat)}
                    </td>
                    <td className="p-2">{m.part_name}</td>
                    <td className={`p-2 tabular-nums ${m.delta > 0 ? 'text-bambu-green' : 'text-red-400'}`}>
                      {signed(m.delta)}
                    </td>
                    {/* An unknown reason prints its own token rather than a
                        blank: the set is closed today, and a row that says
                        nothing at all is worse than one that says a word the
                        operator can search for. */}
                    <td className="p-2">{t(`stock.reason.${m.reason}`, { defaultValue: m.reason })}</td>
                    <td className="p-2">
                      <MovementSource movement={m} />
                    </td>
                    <td className="p-2 text-bambu-gray">{note ?? '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {/* ⚠️ Say when the ledger is CUT (finding M3). A full page of exactly the
          limit is indistinguishable from a complete history, and a product with
          two years of prints has plenty more — the operator would read the
          oldest row shown as the first movement there ever was. The comparison
          is against the constant the request itself uses. */}
      {movements.length === STOCK_MOVEMENT_LIMIT && (
        <p className="text-xs text-bambu-gray" data-testid="stock-movements-truncated">
          {t('stock.showingLast', { count: STOCK_MOVEMENT_LIMIT })}
        </p>
      )}

      {adjusting && (
        <AdjustStockDialog
          productId={productId}
          parts={balances.map((b) => ({ part_id: b.part_id, name: b.name }))}
          onClose={() => setAdjusting(false)}
        />
      )}
    </section>
  );
}
