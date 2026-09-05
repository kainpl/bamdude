import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, ChevronUp, Check, Pencil, Trash2, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, ProjectLine, ProjectLineUpdate } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { useProductStock } from '../../hooks/useProductStock';
import { ConfirmModal } from '../ConfirmModal';
import { ProgressBar } from './ProgressBar';
import { LinePartsTable } from './LinePartsTable';
import { AddLineRow } from './AddLineRow';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

const FIELD_CLASS =
  'px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';
const ICON_BUTTON_CLASS =
  'p-1.5 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors disabled:opacity-40 disabled:hover:bg-transparent';

/** What an inline edit is holding, before anything is sent. */
interface Draft {
  id: number;
  /** Not edited — carried so the row's stock query has a product to ask about
   *  without the hook having to search the order for the line again. */
  productId: number;
  quantity: number;
  material: string;
  color: string;
  note: string;
  /** Kits this line takes off the shelf (pass 8, Decision 4). */
  fromStock: number;
}

function draftOf(line: ProjectLine): Draft {
  return {
    id: line.id,
    productId: line.product_id,
    quantity: line.quantity,
    material: line.material ?? '',
    color: line.color ?? '',
    note: line.note ?? '',
    fromStock: line.from_stock_units,
  };
}

/**
 * Only what the operator actually changed.
 *
 * A PATCH carrying every field would overwrite a value somebody else edited
 * between this row rendering and Save being pressed — and, worse, would write
 * back whatever the row was showing for fields the operator never touched.
 * An empty box means null on the wire, never `""`.
 *
 * ⚠️ The material is upper-cased HERE too, not only on the field's blur: the
 * server stores it verbatim, so a row that arrived holding a lower-case
 * `petg` (typed before this page existed) would otherwise keep failing to
 * match the plates, which spell theirs upper-case. Saving such a row repairs
 * it, which is why the comparison can report a change the operator did not
 * type.
 */
function changedFields(line: ProjectLine, draft: Draft): ProjectLineUpdate {
  const patch: ProjectLineUpdate = {};
  if (draft.quantity !== line.quantity) patch.quantity = draft.quantity;
  const material = draft.material.trim().toUpperCase() || null;
  if (material !== (line.material ?? null)) patch.material = material;
  const color = draft.color.trim() || null;
  if (color !== (line.color ?? null)) patch.color = color;
  const note = draft.note.trim() || null;
  if (note !== (line.note ?? null)) patch.note = note;
  // ⚠️ Sent only when the operator MOVED the box. On this field alone, absent
  // means "leave the reservation alone" and a number means "rewrite it"
  // (release + reserve in one server transaction) — so restating the current
  // value would burn a rewrite, and with it the ledger rows that record one,
  // on every save that touched a note.
  if (draft.fromStock !== line.from_stock_units) patch.from_stock_units = draft.fromStock;
  return patch;
}

interface OrderLinesTableProps {
  order: Order;
  canEdit: boolean;
}

/**
 * The order's work, one row per line.
 *
 * `units_printed` and `progress` are the server's (design decision 8); this
 * table shows them and never counts archives itself.
 *
 * ⚠️ **Reordering is TWO patches that swap two `sort_order` values**, not a
 * renumbering of the whole list. There is no bulk-reorder endpoint, and a
 * client that renumbered every row would push its own idea of the order over
 * whatever another session had just done. Sequential rather than parallel:
 * the pair is a swap, and sending both at once means the second can land
 * first.
 *
 * ⚠️ **Nothing prints from here.** The row's own "print a plate…" picker was
 * the interim answer of pass 2; the plan block below the table replaced it,
 * and a second door onto `PrintModal` from the same page would let an operator
 * queue a plate the plan is not counting.
 */
export function OrderLinesTable({ order, canEdit }: OrderLinesTableProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [draft, setDraft] = useState<Draft | null>(null);
  const [deleting, setDeleting] = useState<ProjectLine | null>(null);

  // `sort_order` is the authority and `id` only breaks its ties, so two lines
  // that share a position still come out in a stable order rather than
  // swapping places on every render.
  const lines = [...order.lines].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);

  const invalidate = () => {
    // ⚠️ The whole set: a line's quantity, material or colour is what the
    // plan block plans, so an edit here restates it.
    invalidateOrderViews(queryClient, { orderId: order.id });
  };

  // The shelf of the product being edited. `null` while nothing is open, which
  // in TanStack v5 is a DISABLED query — pending, not fetching, asking nothing.
  // The hook owns the key so this and the add-line row below cannot end up
  // fighting over one query's options; see `useProductStock`.
  const { data: editStock } = useProductStock(draft ? draft.productId : null);

  const save = useMutation({
    mutationFn: ({ lineId, data }: { lineId: number; data: ProjectLineUpdate }) =>
      api.updateOrderLine(order.id, lineId, data),
    onSuccess: (saved, { lineId, data }) => {
      invalidate();
      if (data.from_stock_units != null) {
        // The reservation moved parts, so the product's shelf and the catalog
        // card that shows `kits_available` are both stale.
        queryClient.invalidateQueries({ queryKey: ['product-stock'] });
        queryClient.invalidateQueries({ queryKey: ['product'] });
        queryClient.invalidateQueries({ queryKey: ['products'] });
        // What was ACTUALLY reserved can be less than what was asked — the
        // shelf may have emptied since the row was opened. The row is closing,
        // so the honest number is said rather than shown.
        const line = saved.lines.find((l) => l.id === lineId);
        if (line && line.from_stock_units < data.from_stock_units) {
          showToast(t('stock.line.clamped', { n: line.from_stock_units }), 'warning');
        }
      }
      setDraft(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const swap = useMutation({
    mutationFn: async ({ a, b }: { a: ProjectLine; b: ProjectLine }) => {
      await api.updateOrderLine(order.id, a.id, { sort_order: b.sort_order });
      await api.updateOrderLine(order.id, b.id, { sort_order: a.sort_order });
    },
    // ⚠️ `onSettled`, NOT `onSuccess`: a swap is two writes and the first can
    // land while the second does not (a dropped connection, a 404 on a line
    // another session just deleted). The server is then holding two lines with
    // the SAME `sort_order` — and on `onSuccess` alone the cache would never be
    // invalidated, so the table would keep drawing the pre-swap order. Both
    // buttons then look broken for ever: swapping equal values is a no-op, and
    // the `id` tiebreak keeps the wrong display perfectly stable. Refetching
    // whatever happened is the only way the operator sees the real state.
    onSettled: invalidate,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (lineId: number) => api.deleteOrderLine(order.id, lineId),
    onSuccess: () => {
      invalidate();
      setDeleting(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  /**
   * Save, unless there is nothing to save.
   *
   * A `PATCH {}` is a request that asks the server to change nothing, bumps the
   * order's `updated_at` and invalidates two query keys for a row the operator
   * only opened and closed. Pressing Save on an untouched row is the same
   * gesture as Cancel, so it does the same thing.
   */
  const commitDraft = (line: ProjectLine, editing: Draft) => {
    const data = changedFields(line, editing);
    if (Object.keys(data).length === 0) {
      setDraft(null);
      return;
    }
    save.mutate({ lineId: line.id, data });
  };

  const toggleExpanded = (lineId: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(lineId)) next.delete(lineId);
      else next.add(lineId);
      return next;
    });

  const busy = save.isPending || swap.isPending || remove.isPending;

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-medium text-white">{t('orders.lines.title')}</h2>

      <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-bambu-gray text-left">
              <th className="font-normal p-2">{t('orders.lines.product')}</th>
              <th className="font-normal p-2">{t('orders.lines.quantity')}</th>
              <th className="font-normal p-2">{t('orders.lines.material')}</th>
              <th className="font-normal p-2">{t('orders.lines.color')}</th>
              <th className="font-normal p-2">{t('orders.lines.note')}</th>
              <th className="font-normal p-2 min-w-[8rem]">{t('orders.lines.progress')}</th>
              <th className="font-normal p-2 text-right">{t('orders.lines.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {lines.length === 0 && (
              <tr>
                <td colSpan={7} className="p-4 text-bambu-gray">
                  {t('orders.lines.empty')}
                </td>
              </tr>
            )}

            {lines.map((line, index) => {
              const editing = draft?.id === line.id ? draft : null;
              const open = expanded.has(line.id);
              return [
                <tr key={line.id} data-line={line.id} className="border-t border-bambu-dark-tertiary text-white">
                  <td className="p-2">
                    <Link to={`/products/${line.product_id}`} className="hover:text-bambu-green transition-colors">
                      {line.product_name}
                    </Link>
                  </td>
                  <td className="p-2 tabular-nums">
                    {editing ? (
                      <input
                        type="number"
                        min={1}
                        aria-label={t('orders.lines.quantity')}
                        value={editing.quantity}
                        onChange={(e) =>
                          setDraft({ ...editing, quantity: Math.max(1, Number(e.target.value) || 1) })
                        }
                        className={`${FIELD_CLASS} w-20`}
                      />
                    ) : (
                      line.quantity
                    )}
                    {/* Kits off the shelf, under the quantity because they are
                        the same unit: the line asks for N, and some of those N
                        come from stock instead of a printer. A column of its own
                        would push the table past the width it already has.
                        ⚠️ The editable ceiling is what is FREE plus what this
                        line already holds — an edit releases its own reservation
                        before making the new one, so the line's own kits are back
                        in the pool by the time the server clamps. */}
                    {editing
                      ? (() => {
                          const pool = (editStock?.kits_available ?? 0) + line.from_stock_units;
                          if (pool <= 0) return null;
                          return (
                            <div className="mt-1">
                              <label className="block text-xs text-bambu-gray" htmlFor={`line-${line.id}-from-stock`}>
                                {t('stock.line.label')}
                              </label>
                              <input
                                id={`line-${line.id}-from-stock`}
                                data-testid={`line-${line.id}-from-stock`}
                                type="number"
                                min={0}
                                max={Math.min(pool, editing.quantity)}
                                value={editing.fromStock}
                                onChange={(e) =>
                                  setDraft({ ...editing, fromStock: Math.max(0, Number(e.target.value) || 0) })
                                }
                                className={`${FIELD_CLASS} w-20`}
                              />
                            </div>
                          );
                        })()
                      : line.from_stock_units > 0 && (
                          <p className="text-xs text-bambu-gray" data-testid={`line-${line.id}-from-stock-shown`}>
                            {t('stock.line.reserved', { n: line.from_stock_units })}
                          </p>
                        )}
                  </td>
                  <td className="p-2">
                    {editing ? (
                      <input
                        type="text"
                        aria-label={t('orders.lines.material')}
                        value={editing.material}
                        onChange={(e) => setDraft({ ...editing, material: e.target.value })}
                        onBlur={() => setDraft({ ...editing, material: editing.material.trim().toUpperCase() })}
                        className={`${FIELD_CLASS} w-24`}
                      />
                    ) : (
                      line.material || <span className="text-bambu-gray">—</span>
                    )}
                  </td>
                  <td className="p-2">
                    {editing ? (
                      <input
                        type="text"
                        aria-label={t('orders.lines.color')}
                        value={editing.color}
                        onChange={(e) => setDraft({ ...editing, color: e.target.value })}
                        className={`${FIELD_CLASS} w-24`}
                      />
                    ) : (
                      line.color || <span className="text-bambu-gray">—</span>
                    )}
                  </td>
                  <td className="p-2">
                    {editing ? (
                      <input
                        type="text"
                        aria-label={t('orders.lines.note')}
                        value={editing.note}
                        onChange={(e) => setDraft({ ...editing, note: e.target.value })}
                        className={FIELD_CLASS}
                      />
                    ) : (
                      line.note || <span className="text-bambu-gray">—</span>
                    )}
                  </td>
                  <td className="p-2">
                    <ProgressBar value={line.units_printed} max={line.quantity} testId={`line-${line.id}-progress`} />
                  </td>
                  <td className="p-2">
                    <div className="flex items-center justify-end gap-0.5">
                      <button
                        type="button"
                        data-testid={`line-${line.id}-expand`}
                        onClick={() => toggleExpanded(line.id)}
                        title={open ? t('orders.lines.collapse') : t('orders.lines.expand')}
                        aria-label={open ? t('orders.lines.collapse') : t('orders.lines.expand')}
                        className={ICON_BUTTON_CLASS}
                      >
                        {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                      </button>

                      {canEdit && editing && (
                        <>
                          <button
                            type="button"
                            data-testid={`line-${line.id}-save`}
                            onClick={() => commitDraft(line, editing)}
                            disabled={busy}
                            title={t('orders.lines.save')}
                            aria-label={t('orders.lines.save')}
                            className={ICON_BUTTON_CLASS}
                          >
                            <Check className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            onClick={() => setDraft(null)}
                            title={t('orders.lines.cancel')}
                            aria-label={t('orders.lines.cancel')}
                            className={ICON_BUTTON_CLASS}
                          >
                            <X className="w-4 h-4" />
                          </button>
                        </>
                      )}

                      {canEdit && !editing && (
                        <>
                          <button
                            type="button"
                            data-testid={`line-${line.id}-edit`}
                            onClick={() => setDraft(draftOf(line))}
                            title={t('orders.lines.edit')}
                            aria-label={t('orders.lines.edit')}
                            className={ICON_BUTTON_CLASS}
                          >
                            <Pencil className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            data-testid={`line-${line.id}-up`}
                            onClick={() => swap.mutate({ a: line, b: lines[index - 1] })}
                            disabled={index === 0 || busy}
                            title={t('orders.lines.moveUp')}
                            aria-label={t('orders.lines.moveUp')}
                            className={ICON_BUTTON_CLASS}
                          >
                            <ChevronUp className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            data-testid={`line-${line.id}-down`}
                            onClick={() => swap.mutate({ a: line, b: lines[index + 1] })}
                            disabled={index === lines.length - 1 || busy}
                            title={t('orders.lines.moveDown')}
                            aria-label={t('orders.lines.moveDown')}
                            className={ICON_BUTTON_CLASS}
                          >
                            <ChevronDown className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            data-testid={`line-${line.id}-delete`}
                            onClick={() => setDeleting(line)}
                            title={t('orders.lines.delete')}
                            aria-label={t('orders.lines.delete')}
                            className={`${ICON_BUTTON_CLASS} hover:text-red-500`}
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>,
                open ? (
                  <tr key={`${line.id}-parts`} className="bg-bambu-dark/40">
                    <td colSpan={7} className="px-4 pb-3">
                      <LinePartsTable parts={line.parts} />
                    </td>
                  </tr>
                ) : null,
              ];
            })}

            {canEdit && <AddLineRow orderId={order.id} />}
          </tbody>
        </table>
      </div>

      {deleting && (
        <ConfirmModal
          title={t('orders.lines.confirmDeleteTitle')}
          message={t('orders.lines.confirmDelete')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(deleting.id)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </section>
  );
}
