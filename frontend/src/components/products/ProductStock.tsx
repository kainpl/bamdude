import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, PackageCheck, X } from 'lucide-react';
import { api, STOCK_NOTE_TOKENS } from '../../api/client';
import type { StockMovement } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { useProductStock } from '../../hooks/useProductStock';
import { formatDateOnly } from '../../utils/date';
import type { DateFormat } from '../../utils/date';
import { Button } from '../Button';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';

/** The seven tokens, as a set, for the one question this file asks of them. */
const NOTE_TOKENS: ReadonlySet<string> = new Set(STOCK_NOTE_TOKENS);

/**
 * Whether a note is one the SERVER wrote, and therefore one to translate.
 *
 * ⚠️ **The token set is closed and the fallback is verbatim, not blank.** The
 * backend writes tokens (Ruling 17) precisely so its half can be read in the
 * operator's language; the other half is a hand correction, whose whole value
 * is the sentence the person typed. Translating by prefix or dropping an
 * unknown note would lose exactly the notes that matter.
 */
function isNoteToken(note: string): boolean {
  return NOTE_TOKENS.has(note);
}

/** `+5` / `−3`. Signed on purpose: a reversal is a movement too, and a column
 *  of unsigned numbers cannot be read as a ledger. The ledger never writes a
 *  zero, so there is no third case. */
function signed(delta: number): string {
  return delta > 0 ? `+${delta}` : `−${Math.abs(delta)}`;
}

/**
 * Where a movement came from: its order, or the print that made it.
 *
 * ⚠️ **The archive is text, not a link.** There is no per-archive route in this
 * app — `/archives` is a filtered list and takes `printer`, `file` and `search`
 * params, none of which addresses one row — so a link would have to invent a
 * destination. The id is what the operator searches with; the order, which does
 * have a page, is a real link.
 */
function MovementSource({ movement }: { movement: StockMovement }) {
  const { t } = useTranslation();
  if (movement.order_id != null) {
    return (
      <Link to={`/projects/${movement.order_id}`} className="text-bambu-green hover:underline">
        {movement.order_name ?? `#${movement.order_id}`}
      </Link>
    );
  }
  if (movement.archive_id != null) {
    return <span>{t('stock.archiveRef', { n: movement.archive_id })}</span>;
  }
  return <span className="text-bambu-gray">—</span>;
}

interface AdjustDialogProps {
  productId: number;
  parts: { part_id: number; name: string }[];
  onClose: () => void;
}

/**
 * The hand correction: the operator counted the shelf and it disagreed with us.
 *
 * Only COUNTED parts are offered, because they are the only ones that hold a
 * balance — the server answers 422 for any other, and offering a part whose
 * only possible outcome is an error is worse than not offering it.
 */
function AdjustDialog({ productId, parts, onClose }: AdjustDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [partId, setPartId] = useState<number>(parts[0]?.part_id ?? 0);
  const [delta, setDelta] = useState('1');
  const [note, setNote] = useState('');

  const adjust = useMutation({
    mutationFn: () => api.adjustProductStock(productId, { part_id: partId, delta: Number(delta), note: note.trim() }),
    onSuccess: () => {
      // The shelf, the product's own `kits_available`, and the catalog card
      // that shows it. No order view moves: a hand correction changes what is
      // free, never what a line has already reserved.
      queryClient.invalidateQueries({ queryKey: ['product-stock', productId] });
      queryClient.invalidateQueries({ queryKey: ['product', productId] });
      queryClient.invalidateQueries({ queryKey: ['products'] });
      showToast(t('stock.adjust.saved'));
      onClose();
    },
    // 409 (would go below zero) and 422 (not a counted part) both arrive as the
    // server's own sentence in `detail`, which is what `ApiError.message` is.
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const parsed = Number(delta);
  const valid = Number.isInteger(parsed) && parsed !== 0 && note.trim().length > 0 && partId > 0;

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md border border-bambu-dark-tertiary">
        <div className="p-4 border-b border-bambu-dark-tertiary flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">{t('stock.adjust.title')}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('common.cancel')}
            className="p-1 hover:bg-bambu-dark rounded"
          >
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <div>
            <label htmlFor="stock-adjust-part" className="block text-sm text-bambu-gray mb-1">
              {t('stock.adjust.part')}
            </label>
            <select
              id="stock-adjust-part"
              value={partId}
              onChange={(e) => setPartId(Number(e.target.value))}
              className={FIELD_CLASS}
            >
              {parts.map((p) => (
                <option key={p.part_id} value={p.part_id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="stock-adjust-delta" className="block text-sm text-bambu-gray mb-1">
              {t('stock.adjust.delta')}
            </label>
            <input
              id="stock-adjust-delta"
              type="number"
              value={delta}
              onChange={(e) => setDelta(e.target.value)}
              className={FIELD_CLASS}
            />
          </div>

          <div>
            <label htmlFor="stock-adjust-note" className="block text-sm text-bambu-gray mb-1">
              {t('stock.adjust.note')}
            </label>
            <input
              id="stock-adjust-note"
              type="text"
              maxLength={500}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t('stock.adjust.notePlaceholder')}
              className={FIELD_CLASS}
            />
          </div>
        </div>

        <div className="p-4 border-t border-bambu-dark-tertiary flex gap-3">
          <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
            {t('common.cancel')}
          </Button>
          <Button
            type="button"
            onClick={() => adjust.mutate()}
            disabled={!valid || adjust.isPending}
            className="flex-1"
            data-testid="stock-adjust-submit"
          >
            {t('stock.adjust.submit')}
          </Button>
        </div>
      </div>
    </div>
  );
}

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
 */
export function ProductStock({ productId, canEdit }: ProductStockProps) {
  const { t } = useTranslation();
  const [adjusting, setAdjusting] = useState(false);

  const { data, isPending, isError } = useProductStock(productId);
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
  if (isPending || isError) {
    return (
      <section className="space-y-3" data-testid="product-stock">
        <h2 className="text-lg font-medium text-white flex items-center gap-2">
          <PackageCheck className="w-5 h-5 text-bambu-green" />
          {t('stock.title')}
        </h2>
        {isPending ? (
          <p className="flex items-center gap-2 text-sm text-bambu-gray">
            <Loader2 className="w-4 h-4 animate-spin" />
            {t('common.loading')}
          </p>
        ) : (
          <p className="text-sm text-red-500" data-testid="stock-error">
            {t('stock.error')}
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

      {balances.length === 0 ? (
        <p className="text-sm text-bambu-gray">{t('stock.empty')}</p>
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

      {adjusting && (
        <AdjustDialog
          productId={productId}
          parts={balances.map((b) => ({ part_id: b.part_id, name: b.name }))}
          onClose={() => setAdjusting(false)}
        />
      )}
    </section>
  );
}
