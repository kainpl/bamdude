import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, ClipboardList, Loader2, Send } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, PlanEnqueueItem, PlanRow as PlanRowData } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { formatMoney } from '../../utils/currency';
import { formatDuration } from '../../utils/date';
import { Button } from '../Button';
import { PlanLine } from './PlanLine';
import { MAX_PER_PLATE } from './PlanRow';
import { projectPlan, rowDistribution, splitIsOff, type ChosenByRow, type SplitByRow } from './planMath';
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
 * operator's half-made plan mid-edit. Hence the per-line signatures below: the
 * same payload back is not a change, and a change to one line is not a change
 * to the next.
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
   * `line_id → row plate_id → the plate that row is set to print`, and
   * `line_id → row plate_id → plate_id → prints` beside it.
   *
   * The same part is routinely sliced once per printer model, so a row stands
   * for several files with the identical counted yield (`PlanRow.alternatives`).
   * The first map is which of them a row is showing; the second is how its
   * count is split across them, which is what sends one line's work to two
   * printer MODELS — the auto-queue routes an item by `target_model`.
   *
   * ⚠️ **Both are answers about a PLAN, and are dropped with the counts when
   * the plan moves** — see the signature below, which carries the alternatives
   * for exactly this reason.
   */
  const [chosen, setChosen] = useState<Record<number, ChosenByRow>>({});
  const [split, setSplit] = useState<Record<number, SplitByRow>>({});

  /**
   * `line_id → everything about THAT line's plan the operator's edits answer.`
   *
   * ⚠️ **Content, never the clock.** Anything not in here — the product's name,
   * the totals, the order of two identical reads — leaves the edits alone,
   * which is the whole point: the block is refetched by farm-wide print events
   * that have nothing to do with this order. Anything that IS in here means the
   * plan the operator was editing no longer exists.
   *
   * ⚠️ **Per LINE, not per plan**, so the reseed matches the `added`
   * reconciliation below, which always was. One signature over the whole
   * response meant a count edited on line A reverted the moment line B moved —
   * and with `project-plan` invalidated by every print event on the farm, line
   * B moving is an ordinary event nobody on this page caused.
   */
  const signatures = useMemo(() => {
    const out: Record<number, string> = {};
    for (const line of plan?.lines ?? []) {
      out[line.line_id] = JSON.stringify([
        // ⚠️ The alternatives are IN the signature. A file switch or a split is
        // an answer about the set of plates the row offered when it was made,
        // and a plan that comes back offering a different set has to take it
        // back — otherwise the block sends a plate this row no longer plans.
        line.rows.map((row) => [row.plate_id, row.count, row.alternatives.map((a) => a.plate_id)]),
        line.outstanding_before.map((p) => [p.part_id, p.count]),
        line.surplus_after.map((p) => [p.part_id, p.count]),
        line.unsatisfiable.map((p) => [p.part_id, p.count]),
        line.candidates,
        line.not_sliced,
      ]);
    }
    return out;
  }, [plan]);

  // What each line's plan looked like the last time this ran. A ref, because
  // the comparison is by VALUE: TanStack's structural sharing hands the same
  // object back for an identical payload, but a response that moved one field
  // outside the signature is a new object whose lines have not changed at all.
  const seenSignatures = useRef<Record<number, string>>({});

  useEffect(() => {
    const before = seenSignatures.current;
    seenSignatures.current = signatures;
    // The line is gone from the plan, or it is planning something else: either
    // way the edit has nothing left to mean (decision 9). A line whose
    // signature came back identical keeps every answer untouched. All three
    // maps are keyed by line and reseed together — a count, a file switch and a
    // split are one answer about one plan.
    const keepAnswered = <T,>(prev: Record<number, T>): Record<number, T> => {
      let dropped = false;
      const next: Record<number, T> = {};
      for (const [key, edits] of Object.entries(prev)) {
        const lineId = Number(key);
        if (before[lineId] !== signatures[lineId]) {
          dropped = true;
          continue;
        }
        next[lineId] = edits;
      }
      return dropped ? next : prev;
    };
    setCounts(keepAnswered);
    setChosen(keepAnswered);
    setSplit(keepAnswered);
  }, [signatures]);

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
  }, [plan]);

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
      const projected = projectPlan(line, counts[line.line_id] ?? {}, {}, chosen[line.line_id] ?? {});
      prints += projected.prints;
      seconds = seconds == null || projected.seconds == null ? null : seconds + projected.seconds;
      grams += projected.grams;
      if (projected.cost != null) cost = (cost ?? 0) + projected.cost;
    }
    return { prints, seconds, grams: Math.round(grams * 100) / 100, cost };
  }, [lines, counts, chosen]);

  /** One item per (line, file) with something on it — the row's own plate, the
   *  alternative it was switched to, or every file of a split. */
  const itemsFor = useCallback(
    (line: (typeof lines)[number], only?: number): PlanEnqueueItem[] =>
      line.rows
        .filter((row) => only === undefined || row.plate_id === only)
        .flatMap((row) =>
          rowDistribution(
            row,
            Math.max(0, Math.trunc(counts[line.line_id]?.[row.plate_id] ?? row.count)),
            chosen[line.line_id]?.[row.plate_id],
            split[line.line_id]?.[row.plate_id],
          ).map((entry) => ({ ...entry, line_id: line.line_id })),
        ),
    [counts, chosen, split],
  );

  const items = useMemo(() => lines.flatMap((line) => itemsFor(line)), [lines, itemsFor]);

  const overCap = items.some((item) => item.count > MAX_PER_PLATE);
  // ⚠️ A half-made split blocks the WHOLE-plan button too, not only its own
  // row: sending everything else and silently skipping the row being edited
  // would be the one outcome the operator cannot see coming.
  const splitOff = lines.some((line) =>
    line.rows.some((row) =>
      splitIsOff(
        split[line.line_id]?.[row.plate_id],
        Math.max(0, Math.trunc(counts[line.line_id]?.[row.plate_id] ?? row.count)),
      ),
    ),
  );

  const setCount = (lineId: number, plateId: number, next: number) =>
    setCounts((prev) => ({ ...prev, [lineId]: { ...(prev[lineId] ?? {}), [plateId]: Math.max(0, next) } }));

  const setChoice = (lineId: number, rowPlateId: number, plateId: number) =>
    setChosen((prev) => ({ ...prev, [lineId]: { ...(prev[lineId] ?? {}), [rowPlateId]: plateId } }));

  const setRowSplit = (lineId: number, rowPlateId: number, next: Record<number, number>) =>
    setSplit((prev) => ({ ...prev, [lineId]: { ...(prev[lineId] ?? {}), [rowPlateId]: next } }));

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
      <section id="order-plan" className="space-y-3" data-testid="plan-block">
        {heading}
        <p className="text-sm text-bambu-gray" data-testid="plan-closed">
          {t('orders.plan.closed')}
        </p>
      </section>
    );
  }

  if (isLoading) {
    return (
      <section id="order-plan" className="space-y-3" data-testid="plan-block">
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
      <section id="order-plan" className="space-y-3" data-testid="plan-block">
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

  // ⚠️ **The fifth branch renders too.** `isLoading` is only TRUE on a query
  // that has never resolved AND is fetching; a paused one — the tab was
  // backgrounded, the network dropped, the query was disabled and re-enabled —
  // is `isPending && !isFetching`, which falls through every branch above with
  // no data and used to return `null`. That takes the whole section off the
  // page, which reads as "this order has nothing to print" — the same lie the
  // error branch above exists to avoid, from a state that is not even a
  // failure. The block stays, wearing its own testid and the loading text.
  if (!plan) {
    return (
      <section id="order-plan" className="space-y-3" data-testid="plan-block">
        {heading}
        <div className="flex items-center gap-2 text-bambu-gray text-sm" data-testid="plan-idle">
          <Loader2 className="w-4 h-4 animate-spin" />
          {t('common.loading')}
        </div>
      </section>
    );
  }

  return (
    <section id="order-plan" className="space-y-3" data-testid="plan-block">
      {heading}

      {/* The engine's iteration guard stopped covering, so what follows is a
          PREFIX of the plan — rows, totals and no unsatisfiable part all look
          exactly like a finished one. Said once for the whole order, above the
          lines, because the operator's next move (print all of this, then ask
          again) is the same whichever line was cut short. */}
      {plan.truncated && (
        <div className="flex items-center gap-2 text-sm" data-testid="plan-truncated">
          <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0" />
          <span className="text-amber-300">{t('orders.plan.truncated')}</span>
        </div>
      )}

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
                chosen={chosen[line.line_id] ?? {}}
                split={split[line.line_id] ?? {}}
                currency={settings?.currency}
                showCost={showCost}
                canQueue={canQueue}
                canPrint={canPrint}
                busy={enqueue.isPending}
                ratePerGram={ratePerGram}
                onCount={(plateId, next) => setCount(line.line_id, plateId, next)}
                onChoose={(rowPlateId, plateId) => setChoice(line.line_id, rowPlateId, plateId)}
                onSplit={(rowPlateId, next) => setRowSplit(line.line_id, rowPlateId, next)}
                onAddPlate={(row) =>
                  setAdded((prev) => ({ ...prev, [line.line_id]: [...(prev[line.line_id] ?? []), row] }))
                }
                onEnqueueRow={(rowPlateId) => enqueue.mutate(itemsFor(line, rowPlateId))}
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
                disabled={enqueue.isPending || overCap || splitOff}
                // A disabled button with no reason on it is a dead end. `overCap`
                // is checked first because it names a row's number, which is the
                // more specific of the two complaints.
                title={
                  overCap
                    ? t('orders.plan.row.tooMany')
                    : splitOff
                      ? t('orders.plan.split.off')
                      : undefined
                }
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
