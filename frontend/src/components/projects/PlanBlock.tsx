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
import { invalidateOrderViews } from '../../utils/queryInvalidation';

/**
 * What to print next for this order, per line.
 *
 * The block turns the order page from a ledger into a work plan: the server
 * ranks each line's plates by useful parts per hour, having already subtracted
 * what is printed, in progress and queued, and this is where the operator
 * adjusts the counts and sends the result to the queue.
 *
 * ⚠️ **The counts are a what-if that lives until the PLAN changes — not until
 * the next refetch.** They are seeded from the response (an empty `counts` map
 * means "every row at the count the server planned") and cleared when the
 * plan's own content moves: decision 9 says an edit made against an older plan
 * has nothing left to mean, and that is still true. But `project-plan` is
 * invalidated by every `print_complete` / `archive_created` on the FARM —
 * archive events carry no `project_id`, so the whole prefix goes — and clearing
 * on the clock meant a print finishing on an unrelated printer wiped the
 * operator's half-made plan mid-edit. Hence the signature below: the same
 * payload back is not a change.
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
    isError,
    refetch,
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

  /**
   * Everything about the plan the operator's edits are an answer TO.
   *
   * ⚠️ **Content, never the clock.** Anything not in here — the product's name,
   * the totals, the order of two identical reads — leaves the edits alone,
   * which is the whole point: the block is refetched by farm-wide print events
   * that have nothing to do with this order. Anything that IS in here means the
   * plan the operator was editing no longer exists.
   */
  const signature = useMemo(
    () =>
      JSON.stringify(
        (plan?.lines ?? []).map((line) => [
          line.line_id,
          line.rows.map((row) => [row.plate_id, row.count]),
          line.outstanding_before.map((p) => [p.part_id, p.count]),
          line.surplus_after.map((p) => [p.part_id, p.count]),
          line.unsatisfiable.map((p) => [p.part_id, p.count]),
          line.candidates,
          line.not_sliced,
        ]),
      ),
    [plan],
  );

  useEffect(() => {
    setCounts({});
  }, [signature]);

  // ⚠️ A hand-added plate is not seeded from anything, so a reseed cannot
  // restore it — it survives every refetch instead, and is dropped only when it
  // has stopped meaning what it meant: the line is gone, the plate is no longer
  // a candidate, or the server has planned that plate itself (keeping it would
  // then show one plate on two rows).
  useEffect(() => {
    setAdded((prev) => {
      const lines = plan?.lines;
      if (!lines) return prev;
      const next: Record<number, PlanRowData[]> = {};
      for (const line of lines) {
        const before = prev[line.line_id];
        if (!before?.length) continue;
        const planned = new Set(line.rows.map((row) => row.plate_id));
        const kept = before.filter((row) => line.candidates.includes(row.plate_id) && !planned.has(row.plate_id));
        if (kept.length > 0) next[line.line_id] = kept.length === before.length ? before : kept;
      }
      // Same rows, same arrays: hand back the state that is already there, so a
      // refetch that drops nothing does not re-render the block either.
      const same =
        Object.keys(next).length === Object.keys(prev).length &&
        Object.entries(next).every(([lineId, rows]) => prev[Number(lineId)] === rows);
      return same ? prev : next;
    });
  }, [signature, plan]);

  // The endpoint demands `projects:update` AND `queue:create`; a user missing
  // either never sees the button rather than being handed a 403 on click.
  const canQueue = canEdit && hasPermission('queue:create');
  const canPrint = hasPermission('printers:control');

  const invalidate = useCallback(() => {
    invalidateOrderViews(queryClient, { orderId: order.id });
    // ⚠️ The two queue keys are NOT order views and stay here: enqueueing
    // puts rows in a printer's queue and in the auto-queue distributor, which
    // no order page reads and the helper therefore knows nothing about.
    queryClient.invalidateQueries({ queryKey: ['queue'] });
    queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
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

  // ⚠️ `plan-block` marks THE BLOCK, on every branch — it answers "is the plan
  // on this page", not "has the plan arrived". Carrying it on the loaded and
  // failed branches only made "the page mounts the block" a question that could
  // be asked of the page just before the plan resolved, and answered no.
  if (!active) {
    return (
      <section className="space-y-3" data-testid="plan-block">
        {heading}
        <p className="text-sm text-bambu-gray" data-testid="plan-closed">
          {t('orders.plan.closed')}
        </p>
      </section>
    );
  }

  if (isLoading) {
    return (
      <section className="space-y-3" data-testid="plan-block">
        {heading}
        <div className="flex items-center gap-2 text-bambu-gray text-sm">
          <Loader2 className="w-4 h-4 animate-spin" />
          {t('common.loading')}
        </div>
      </section>
    );
  }

  // ⚠️ The heading stays, and so does a way back. Returning null on a failed
  // fetch removed the whole section from the page, and a block that is simply
  // absent reads as "this order has nothing to print" — the one thing a failed
  // plan must not say.
  //
  // Data first, then `isError`: the plan is re-read after every enqueue and on
  // every print event, so a refetch that blips must not replace a plan still on
  // screen with an error.
  if (isError && !plan) {
    return (
      <section className="space-y-3" data-testid="plan-block">
        {heading}
        <div className="flex items-center gap-3 flex-wrap text-sm">
          <p className="text-red-400" data-testid="plan-error">
            {t('orders.plan.loadFailed')}
          </p>
          <Button size="sm" variant="outline" data-testid="plan-retry" onClick={() => refetch()}>
            {t('orders.plan.retry')}
          </Button>
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
