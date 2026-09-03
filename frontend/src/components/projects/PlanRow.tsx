import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { Order, PlanRow as PlanRowData } from '../../api/client';
import { formatMoney } from '../../utils/currency';
import { formatDuration } from '../../utils/date';
import { Button } from '../Button';
import { PrintModal } from '../PrintModal';

/** The server's own ceiling on one enqueue item (`PlanEnqueueItem.count`). */
export const MAX_PER_PLATE = 999;

interface PlanRowProps {
  order: Order;
  lineId: number;
  row: PlanRowData;
  count: number;
  currency: string | null | undefined;
  showCost: boolean;
  canQueue: boolean;
  canPrint: boolean;
  busy: boolean;
  onCount: (next: number) => void;
  onEnqueue: () => void;
  onQueued: () => void;
}

/**
 * One recommended plate, printed `count` times.
 *
 * ⚠️ **The figures on the row are PER PRINT** — `count` is the multiplier, and
 * multiplying is this component's whole job. That is also why the count editor
 * is here rather than in a modal: the operator changes a number and reads the
 * consequence in the same line.
 *
 * ⚠️ **`plate_id` is `ProductPlate.id`, `plate_index` is the slicer's.** The
 * queue and `PrintModal` speak the second one, where 0 means "no plate pinned",
 * i.e. the whole file — hence `plate_index || undefined` below and nowhere a
 * bare `plate_index`.
 *
 * The `+` is deliberately uncapped: a count over the server's 999 disables the
 * *queue* button with the reason in its title, rather than silently refusing a
 * click or clamping a number the operator typed on purpose.
 *
 * ⚠️ **The test ids carry the LINE id beside the plate's.** `ProductPlate.id`
 * is unique per product, not per order — two lines of the same product put the
 * same plate on screen twice, and a bare `plan-row-100` would match both. The
 * `counts` state upstream has always been keyed `lineId → plateId`; this is the
 * ids catching up with it.
 */
export function PlanRow({
  order,
  lineId,
  row,
  count,
  currency,
  showCost,
  canQueue,
  canPrint,
  busy,
  onCount,
  onEnqueue,
  onQueued,
}: PlanRowProps) {
  const { t } = useTranslation();
  const [printing, setPrinting] = useState(false);

  const tooMany = count > MAX_PER_PLATE;
  const step = 'px-2 py-1 rounded border border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary disabled:opacity-40 disabled:hover:bg-transparent';

  return (
    <tr
      data-testid={`plan-row-${lineId}-${row.plate_id}`}
      className={`border-b border-bambu-dark-tertiary last:border-0 ${count === 0 ? 'opacity-50' : ''}`}
    >
      <td className="px-3 py-2 min-w-0">
        <p className="text-white truncate">{row.filename}</p>
        <p className="text-xs text-bambu-gray">
          {row.plate_index === 0 ? t('orders.plan.row.wholeFile') : t('orders.plan.row.plate', { n: row.plate_index })}
        </p>
      </td>

      <td className="px-3 py-2 text-xs text-bambu-gray">
        {row.useful.length > 0
          ? `${t('orders.plan.row.covers')} ${row.useful.map((u) => `${u.name} × ${u.count}`).join(' · ')}`
          : '—'}
      </td>

      <td className="px-3 py-2">
        <div className="inline-flex items-center gap-1">
          <button
            type="button"
            data-testid={`plan-row-${lineId}-${row.plate_id}-dec`}
            className={step}
            disabled={count <= 0}
            onClick={() => onCount(count - 1)}
          >
            −
          </button>
          <input
            type="number"
            min={0}
            data-testid={`plan-row-${lineId}-${row.plate_id}-count`}
            value={count}
            aria-label={t('orders.plan.row.count')}
            onChange={(e) => onCount(Math.max(0, Math.trunc(Number(e.currentTarget.value) || 0)))}
            className="w-16 px-2 py-1 text-right tabular-nums bg-bambu-dark border border-bambu-dark-tertiary rounded text-white focus:border-bambu-green focus:outline-none"
          />
          <button
            type="button"
            data-testid={`plan-row-${lineId}-${row.plate_id}-inc`}
            className={step}
            onClick={() => onCount(count + 1)}
          >
            +
          </button>
        </div>
      </td>

      <td className="px-3 py-2 text-right text-bambu-gray tabular-nums whitespace-nowrap">
        {row.print_time_seconds == null ? '—' : formatDuration(row.print_time_seconds * count)}
      </td>

      <td className="px-3 py-2 text-right text-bambu-gray tabular-nums whitespace-nowrap">
        {row.filament_used_grams == null ? '—' : (row.filament_used_grams * count).toFixed(1)}
      </td>

      {showCost && (
        <td className="px-3 py-2 text-right text-bambu-gray tabular-nums whitespace-nowrap">
          {row.cost == null ? '—' : formatMoney(row.cost * count, currency)}
        </td>
      )}

      <td className="px-3 py-2">
        <div className="flex items-center justify-end gap-2 flex-wrap">
          {canQueue && (
            <Button
              size="sm"
              variant="outline"
              data-testid={`plan-row-${lineId}-${row.plate_id}-queue`}
              disabled={busy || count === 0 || tooMany}
              title={tooMany ? t('orders.plan.row.tooMany') : undefined}
              onClick={onEnqueue}
            >
              {t('orders.plan.row.toQueue', { count })}
            </Button>
          )}
          {canPrint && (
            <Button
              size="sm"
              variant="ghost"
              data-testid={`plan-row-${lineId}-${row.plate_id}-printer`}
              onClick={() => setPrinting(true)}
            >
              {t('orders.plan.row.toPrinter')}
            </Button>
          )}
        </div>

        {printing && (
          <PrintModal
            mode="add-to-queue"
            libraryFileId={row.library_file_id}
            archiveName={row.filename}
            preselectedPlateId={row.plate_index || undefined}
            projectId={order.id}
            projectLineId={lineId}
            // Routing, not dispatching: the modal is opened on the printer leg
            // and kept there, because "to printer…" already answered the only
            // question the toggle asks.
            initialDispatchMode="specific"
            lockDispatchMode
            onClose={() => setPrinting(false)}
            onSuccess={() => {
              setPrinting(false);
              onQueued();
            }}
          />
        )}
      </td>
    </tr>
  );
}
