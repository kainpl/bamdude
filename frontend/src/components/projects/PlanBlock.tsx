import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ClipboardList, Loader2, Send } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, PlanEnqueueItem, PlanRow as PlanRowData } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { formatMoney } from '../../utils/currency';
import { formatDuration } from '../../utils/date';
import { Button } from '../Button';
import { PlanLine } from './PlanLine';
import { MAX_PER_PLATE } from './PlanRow';
import { projectPlan } from './planMath';

/**
 * What to print next for this order, per line.
 *
 * The block turns the order page from a ledger into a work plan: the server
 * ranks each line's plates by useful parts per hour, having already subtracted
 * what is printed, in progress and queued, and this is where the operator
 * adjusts the counts and sends the result to the queue.
 *
 * ⚠️ **The counts are a what-if that lives only until the next refetch.** They
 * are seeded from the response — an empty `counts` map means "every row at the
 * count the server planned" — and cleared whenever `dataUpdatedAt` moves, i.e.
 * on every refetch, identical payload included. That is the design's own
 * choice (decision 9): the plan is recomputed from scratch on every read, so an
 * edit made against an older plan has nothing left to mean. Anything the
 * operator wants to keep, they send to the queue.
 *
 * ⚠️ **Nothing here asks whether a printer is ready.** Sending a row to a
 * queue is a routing decision; readiness — plate clear, drying, stagger,
 * filament — stays `check_queue`'s question, asked again at dispatch. There is
 * deliberately no printer hint, badge or ordering anywhere in this block.
 */
export function PlanBlock({ order, canEdit }: { order: Order; canEdit: boolean }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const active = order.status === 'active';

  const {
    data: plan,
    isLoading,
    dataUpdatedAt,
  } = useQuery({
    queryKey: ['project-plan', order.id],
    queryFn: () => api.getOrderPlan(order.id),
    enabled: active,
  });

  // The app-wide currency, fetched the way every other money-showing screen
  // fetches it; `formatMoney` covers the unresolved first paint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });

  const [counts, setCounts] = useState<Record<number, Record<number, number>>>({});
  const [added, setAdded] = useState<Record<number, PlanRowData[]>>({});

  useEffect(() => {
    setCounts({});
    setAdded({});
  }, [dataUpdatedAt]);

  // The endpoint demands `projects:update` AND `queue:create`; a user missing
  // either never sees the button rather than being handed a 403 on click.
  const canQueue = canEdit && hasPermission('queue:create');
  const canPrint = hasPermission('printers:control');

  const invalidate = useCallback(() => {
    for (const queryKey of [
      ['project-plan', order.id],
      ['project', order.id],
      ['projects'],
      ['customers'],
      ['customer'],
      ['queue'],
      ['auto-queue'],
    ]) {
      queryClient.invalidateQueries({ queryKey });
    }
  }, [queryClient, order.id]);

  const enqueue = useMutation({
    mutationFn: (items: PlanEnqueueItem[]) =>
      api.enqueueOrderPlan(order.id, { items, target: { kind: 'auto' } }),
    onSuccess: (result) => {
      const created = result.created.reduce((sum, row) => sum + row.queue_item_ids.length, 0);
      showToast(t('orders.plan.toast.enqueued', { count: created }), 'success');
      invalidate();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const lines = useMemo(
    () =>
      (plan?.lines ?? [])
        .map((line) => ({ ...line, rows: [...line.rows, ...(added[line.line_id] ?? [])] }))
        .filter(
          (line) => line.rows.length > 0 || line.unsatisfiable.length > 0 || line.outstanding_before.length > 0,
        ),
    [plan, added],
  );

  // A price per gram recovered from any costed row, so a plate added by hand
  // is priced by the same rate the engine used. `null` when the farm has no
  // filament rate — then no row carries a cost and the column is hidden.
  const ratePerGram = useMemo(() => {
    for (const line of plan?.lines ?? []) {
      for (const row of line.rows) {
        if (row.cost != null && row.filament_used_grams) return row.cost / row.filament_used_grams;
      }
    }
    return null;
  }, [plan]);

  const showCost = ratePerGram != null;

  // Totals need no yields — only the counts and each row's per-print figures —
  // so the empty yield map here is not an omission: `surplusAfter` comes back
  // `null` and is ignored, while each `PlanLine` computes its own from the
  // plate recipes it fetches.
  const totals = useMemo(() => {
    let prints = 0;
    let seconds: number | null = 0;
    let grams = 0;
    let cost: number | null = null;
    for (const line of lines) {
      const projected = projectPlan(line, counts[line.line_id] ?? {}, {});
      prints += projected.prints;
      seconds = seconds == null || projected.seconds == null ? null : seconds + projected.seconds;
      grams += projected.grams;
      if (projected.cost != null) cost = (cost ?? 0) + projected.cost;
    }
    return { prints, seconds, grams: Math.round(grams * 100) / 100, cost };
  }, [lines, counts]);

  const items = useMemo(() => {
    const out: PlanEnqueueItem[] = [];
    for (const line of lines) {
      for (const row of line.rows) {
        const count = Math.max(0, Math.trunc(counts[line.line_id]?.[row.plate_id] ?? row.count));
        if (count > 0) out.push({ plate_id: row.plate_id, count, line_id: line.line_id });
      }
    }
    return out;
  }, [lines, counts]);

  const overCap = items.some((item) => item.count > MAX_PER_PLATE);

  const setCount = (lineId: number, plateId: number, next: number) =>
    setCounts((prev) => ({ ...prev, [lineId]: { ...(prev[lineId] ?? {}), [plateId]: Math.max(0, next) } }));

  const heading = (
    <h2 className="text-lg font-semibold text-white flex items-center gap-2">
      <ClipboardList className="w-5 h-5" />
      {t('orders.plan.title')}
    </h2>
  );

  if (!active) {
    return (
      <section className="space-y-3">
        {heading}
        <p className="text-sm text-bambu-gray" data-testid="plan-closed">
          {t('orders.plan.closed')}
        </p>
      </section>
    );
  }

  if (isLoading) {
    return (
      <section className="space-y-3">
        {heading}
        <div className="flex items-center gap-2 text-bambu-gray text-sm">
          <Loader2 className="w-4 h-4 animate-spin" />
          {t('common.loading')}
        </div>
      </section>
    );
  }

  if (!plan) return null;

  return (
    <section className="space-y-3" data-testid="plan-block">
      {heading}

      {lines.length === 0 ? (
        <p className="text-sm text-bambu-gray" data-testid="plan-empty">
          {t('orders.plan.nothingOutstanding')}
        </p>
      ) : (
        <>
          <div className="space-y-3">
            {lines.map((line) => (
              <PlanLine
                key={line.line_id}
                order={order}
                line={line}
                counts={counts[line.line_id] ?? {}}
                currency={settings?.currency}
                showCost={showCost}
                canQueue={canQueue}
                canPrint={canPrint}
                busy={enqueue.isPending}
                ratePerGram={ratePerGram}
                onCount={(plateId, next) => setCount(line.line_id, plateId, next)}
                onAddPlate={(row) =>
                  setAdded((prev) => ({ ...prev, [line.line_id]: [...(prev[line.line_id] ?? []), row] }))
                }
                onEnqueueRow={(plateId, count) =>
                  enqueue.mutate([{ plate_id: plateId, count, line_id: line.line_id }])
                }
                onQueued={invalidate}
              />
            ))}
          </div>

          <div className="flex items-center justify-between gap-4 flex-wrap rounded-xl border border-bambu-dark-tertiary bg-bambu-dark p-3">
            <div className="flex items-center gap-5 flex-wrap text-sm">
              <Figure
                label={t('orders.plan.totals.prints')}
                testId="plan-totals-prints"
                value={String(totals.prints)}
              />
              <Figure
                label={t('orders.plan.totals.time')}
                testId="plan-totals-time"
                value={totals.seconds == null ? '—' : formatDuration(totals.seconds)}
              />
              <Figure
                label={t('orders.plan.totals.grams')}
                testId="plan-totals-grams"
                value={totals.grams.toFixed(1)}
              />
              {totals.cost != null && (
                <Figure
                  label={t('orders.plan.totals.cost')}
                  testId="plan-totals-cost"
                  value={formatMoney(totals.cost, settings?.currency)}
                />
              )}
            </div>

            {canQueue && (
              <Button
                data-testid="plan-enqueue-all"
                disabled={enqueue.isPending || overCap}
                title={overCap ? t('orders.plan.row.tooMany') : undefined}
                onClick={() =>
                  items.length === 0
                    ? showToast(t('orders.plan.toast.nothingToEnqueue'), 'info')
                    : enqueue.mutate(items)
                }
              >
                {enqueue.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Send className="w-4 h-4" />
                )}
                {t('orders.plan.wholePlanToQueue')}
              </Button>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function Figure({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div>
      <p className="text-xs text-bambu-gray">{label}</p>
      <p className="text-white tabular-nums" data-testid={testId}>
        {value}
      </p>
    </div>
  );
}
