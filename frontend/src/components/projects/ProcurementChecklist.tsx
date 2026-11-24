import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ExternalLink } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, ProcurementRow } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { formatMoney } from '../../utils/currency';
import { invalidateOrderViews } from '../../utils/queryInvalidation';
import { useUiPreferences } from '../../hooks/useUiPreferences';
import { LivePartThumb } from '../PartThumb';

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
/** What one write sent, and against which server value. */
interface SentRecord {
  value: number;
  base: number;
}

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

  // ⚠️ What was last SENT per row, and against which server value (R07). Enter blurs
  // the box and the blur commits; a second blur with the same number — Tab, a click
  // elsewhere — while the first PATCH flies must send nothing. The record is good only
  // while the server still says what it said when the number was sent.
  // ⚠️ **And it lives for ITS write only** (Codex review V01): it is dropped when the
  // write is refused, and when the order has been read again after the write was
  // accepted. Kept past that, a later reversal elsewhere (25 → 10) brought the old
  // record back to life through the matching base, and typing 25 again sent nothing.
  const sent = useRef<Record<number, SentRecord>>({});
  const release = (partId: number, record: SentRecord) => {
    if (sent.current[partId] === record) delete sent.current[partId];
  };

  const { data: settings } = useUiPreferences();

  const save = useMutation({
    mutationFn: ({ partId, partIdentity, acquired }: {
      partId: number; partIdentity?: string; acquired: number; record: SentRecord;
    }) => partIdentity
      ? api.updateOrderProcurement(order.id, partId, acquired, partIdentity)
      : api.updateOrderProcurement(order.id, partId, acquired),
    onSuccess: (_order, { partId, record }) => {
      invalidateOrderViews(queryClient, { orderId: order.id });
      // The record ends with the re-read its write started — the fetch already under
      // way, not a second one (`cancelRefetch: false`).
      void queryClient
        .refetchQueries({ queryKey: ['project', order.id], type: 'active' }, { cancelRefetch: false })
        .finally(() => release(partId, record));
    },
    onError: (e: Error, { partId, record }) => {
      showToast(e.message, 'error');
      release(partId, record);
      setRejections((prev) => ({ ...prev, [partId]: (prev[partId] ?? 0) + 1 }));
    },
  });

  // Nothing bought means nothing to check off — an empty table with three
  // column headers is worse than no section at all.
  // The «Purchased parts» tab is always there, so it never stands blank (WS-13 E3 F05).
  if (order.procurement.length === 0) {
    return (
      <div className="py-8 text-center">
        <p className="text-sm font-medium text-white">{t('orders.procurement.emptyTitle')}</p>
        <p className="mt-1 text-sm text-bambu-gray">{t('orders.procurement.emptyText')}</p>
      </div>
    );
  }

  const commit = (row: ProcurementRow, field: HTMLInputElement) => {
    if (row.part_id < 0) return;
    const raw = field.value.trim();
    const next = Number(raw);
    const last = sent.current[row.part_id];
    const current = last && last.base === row.acquired ? last.value : row.acquired;
    // A cleared or nonsense field is not "zero acquired" — it is an edit the
    // operator abandoned, so it patches nothing.
    if (raw !== '' && Number.isInteger(next) && next >= 0) {
      if (next === current) return;
      const record = { value: next, base: row.acquired };
      sent.current[row.part_id] = record;
      save.mutate({ partId: row.part_id, partIdentity: row.part_identity ?? undefined, acquired: next, record });
      return;
    }
    // ⚠️ And the box has to say so. The input is uncontrolled and keyed on the
    // server's number, so skipping the patch re-renders NOTHING — a cleared or
    // negative field would have sat there looking saved until some unrelated
    // refetch happened to remount the row.
    field.value = String(row.acquired);
  };

  return (
    // No heading and no card of its own — the «Purchased parts» tab names it, and
    // the order's main panel is the frame (WS-13 E3 F05).
    <section className="space-y-3">
      {/* `relative`: the cells' screen-reader text is absolutely positioned, and a box that scrolls
          but positions nothing lets it out — on a phone it pushed the whole order page sideways. */}
      <div className="relative overflow-x-auto" data-testid="procurement-scroll">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-bambu-gray border-b border-bambu-dark-tertiary">
              <th className="px-3 py-2 font-normal">{t('orders.procurement.part')}</th>
              <th className="px-3 py-2 font-normal">{t('orders.procurement.need')}</th>
              <th className="px-3 py-2 font-normal">{t('orders.procurement.acquired')}</th>
              <th className="px-3 py-2 font-normal">{t('orders.procurement.remaining')}</th>
              <th className="px-3 py-2 font-normal">{t('orders.procurement.price')}</th>
            </tr>
          </thead>
          <tbody>
            {order.procurement.map((row) => (
              <tr key={row.part_identity ?? row.part_id} className="border-b border-bambu-dark-tertiary last:border-0">
                <td className="px-3 py-2 align-middle text-white">
                  {/* The part's photo beside its name (spec procurement-stock-images R2): its own door
                      to the larger picture, apart from the supplier's link and from the count. */}
                  <span className="flex items-center gap-2">
                    <LivePartThumb partId={row.part_id} image={row.image ?? null} name={row.name} kind="purchased" size="sm" />
                    <span className="min-w-0 break-words">
                      {row.name}
                      {row.sourcing_url && (
                        <a
                          href={row.sourcing_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          aria-label={t('orders.procurement.openSupplier', { name: row.name })}
                          title={t('orders.procurement.openSupplier', { name: row.name })}
                          className="ml-1.5 inline-flex align-[-2px] text-bambu-gray hover:text-white"
                        >
                          <ExternalLink className="w-3.5 h-3.5" aria-hidden />
                        </a>
                      )}
                    </span>
                  </span>
                </td>
                <td className="px-3 py-2 align-middle text-white tabular-nums">{row.need}</td>
                <td className="px-3 py-2 align-middle">
                  <input
                    key={`${row.acquired}:${rejections[row.part_id] ?? 0}`}
                    data-testid={`procurement-${row.part_id}-acquired`}
                    type="number"
                    min={0}
                    defaultValue={row.acquired}
                    disabled={!canEdit || row.part_id < 0}
                    onBlur={(e) => commit(row, e.currentTarget)}
                    // Enter only lets go of the box; the blur is the one commit (R07).
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') e.currentTarget.blur();
                    }}
                    aria-label={`${row.name} — ${t('orders.procurement.acquired')}`}
                    className="w-[88px] px-3 py-2 tabular-nums bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none disabled:opacity-60"
                  />
                </td>
                <td
                  data-testid={`procurement-${row.part_id}-remaining`}
                  className={`px-3 py-2 align-middle tabular-nums ${row.remaining > 0 ? 'text-amber-700 dark:text-amber-400' : 'text-bambu-green'}`}
                >
                  {row.remaining}
                </td>
                <td className="px-3 py-2 align-middle tabular-nums text-white" data-testid={`procurement-${row.part_id}-price`}>
                  {row.planned_cost != null ? (
                    formatMoney(row.planned_cost, settings?.currency)
                  ) : (
                    <>
                      <span aria-hidden>—</span>
                      <span className="sr-only">{t('orders.procurement.priceUnknown')}</span>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
