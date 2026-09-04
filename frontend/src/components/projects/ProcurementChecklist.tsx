import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ShoppingCart } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, ProcurementRow } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface ProcurementChecklistProps {
  order: Order;
  canEdit: boolean;
}

/**
 * The purchased parts of an order, rolled up across every line.
 *
 * ⚠️ **`remaining` is displayed, never derived** (design decision 8). Typing
 * `25` into "acquired" PATCHes and then waits for the refetch — the remainder
 * on screen stays the server's until the whole order comes back. Subtracting
 * here would be a second place that computes the same number, and the first
 * time the server's rule changed (a part shared by two lines, a line deleted
 * mid-edit) the two would disagree with nothing to say which was right.
 *
 * The input is UNCONTROLLED, keyed on the value the server sent: a fresh
 * `acquired` remounts it with the new number, while an in-flight edit keeps
 * what the operator typed. A controlled input would need a draft map that
 * outlives the refetch, which is the same "second copy of the truth" one
 * level down. ⚠️ A REFUSED patch is therefore the one case the key alone
 * cannot see — the server's number did not change — so a per-row rejection
 * counter joins it; see `rejections` below.
 */
export function ProcurementChecklist({ order, canEdit }: ProcurementChecklistProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // ⚠️ One bump per row per REFUSAL, and it is part of the input's `key`.
  //
  // The box is uncontrolled and keyed on the server's `acquired`, so a rejected
  // PATCH re-renders nothing at all: the number the operator typed stays on
  // screen, looking saved, while the server still holds the old one. Nothing
  // else on the page would ever contradict it — `remaining` is the server's and
  // did not move either. Remounting the input is what puts the truth back, and
  // it is the same remedy the invalid-input branch of `commit` already uses.
  //
  // Accepted trade: a refusal that lands while the operator is already typing
  // the next number throws those keystrokes away with the rest of the DOM value.
  // Showing a saved number that was never saved is the worse of the two, and the
  // toast beside it says what happened.
  const [rejections, setRejections] = useState<Record<number, number>>({});

  const save = useMutation({
    mutationFn: ({ partId, acquired }: { partId: number; acquired: number }) =>
      api.updateOrderProcurement(order.id, partId, acquired),
    onSuccess: () => {
      invalidateOrderViews(queryClient, { orderId: order.id });
    },
    onError: (e: Error, { partId }) => {
      showToast(e.message, 'error');
      setRejections((prev) => ({ ...prev, [partId]: (prev[partId] ?? 0) + 1 }));
    },
  });

  // Nothing bought means nothing to check off — an empty table with three
  // column headers is worse than no section at all.
  if (order.procurement.length === 0) return null;

  const commit = (row: ProcurementRow, field: HTMLInputElement) => {
    const raw = field.value.trim();
    const next = Number(raw);
    // A cleared or nonsense field is not "zero acquired" — it is an edit the
    // operator abandoned, so it patches nothing.
    if (raw !== '' && Number.isInteger(next) && next >= 0 && next !== row.acquired) {
      save.mutate({ partId: row.part_id, acquired: next });
      return;
    }
    // ⚠️ And the box has to say so. The input is uncontrolled and keyed on the
    // server's number, so skipping the patch re-renders NOTHING — a cleared or
    // negative field would have sat there looking saved until some unrelated
    // refetch happened to remount the row.
    field.value = String(row.acquired);
  };

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-white flex items-center gap-2">
        <ShoppingCart className="w-5 h-5" />
        {t('orders.procurement.title')}
      </h2>

      <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-bambu-gray border-b border-bambu-dark-tertiary">
              <th className="px-3 py-2 font-normal">{t('orders.procurement.part')}</th>
              <th className="px-3 py-2 font-normal text-right">{t('orders.procurement.need')}</th>
              <th className="px-3 py-2 font-normal text-right">{t('orders.procurement.acquired')}</th>
              <th className="px-3 py-2 font-normal text-right">{t('orders.procurement.remaining')}</th>
            </tr>
          </thead>
          <tbody>
            {order.procurement.map((row) => (
              <tr key={row.part_id} className="border-b border-bambu-dark-tertiary last:border-0">
                <td className="px-3 py-2 text-white">{row.name}</td>
                <td className="px-3 py-2 text-right text-bambu-gray tabular-nums">{row.need}</td>
                <td className="px-3 py-2 text-right">
                  <input
                    key={`${row.acquired}:${rejections[row.part_id] ?? 0}`}
                    data-testid={`procurement-${row.part_id}-acquired`}
                    type="number"
                    min={0}
                    defaultValue={row.acquired}
                    disabled={!canEdit}
                    onBlur={(e) => commit(row, e.currentTarget)}
                    aria-label={`${row.name} — ${t('orders.procurement.acquired')}`}
                    className="w-20 px-2 py-1 text-right tabular-nums bg-bambu-dark border border-bambu-dark-tertiary rounded text-white focus:border-bambu-green focus:outline-none disabled:opacity-60"
                  />
                </td>
                <td
                  data-testid={`procurement-${row.part_id}-remaining`}
                  className={`px-3 py-2 text-right tabular-nums ${row.remaining > 0 ? 'text-amber-400' : 'text-bambu-gray'}`}
                >
                  {row.remaining}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
