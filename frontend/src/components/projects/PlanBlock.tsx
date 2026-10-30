import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Calculator, Loader2, Send } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { Order, OrderPlan, PlanProposal, PlanSubmission, PlanEnqueueItem, PlanRow as PlanRowData, WorkingPlan } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { canFileFuturePrint } from '../../utils/workshopRights';
import { useToast } from '../../contexts/ToastContext';
import { formatMoney } from '../../utils/currency';
import { formatDateTime, formatDuration } from '../../utils/date';
import { newOperationId } from '../../utils/swapCompatibility';
import { Button } from '../Button';
import { ActionConfirm } from '../workshop/ActionConfirm';
import { FilamentNeeds } from './FilamentNeeds';
import { PlanLine } from './PlanLine';
import { OrderPrintOptions } from './OrderPrintOptions';
import { lineConfigLabel } from './lineConfigLabel';
import { workingEntries, withWorkingEntries } from './workingPlanMath';
import { MAX_PER_PLATE } from './PlanRow';
import { projectPlan, rowDistribution, splitIsOff, type ChosenByRow, type SplitByRow } from './planMath';
import { invalidateOrderViews, invalidateQueueViews } from '../../utils/queryInvalidation';
import { useWorkingPlan, workingSignature } from '../../hooks/useWorkingPlan';
import { useOrderPlan } from '../../hooks/useOrderPlan';
import { useOrderFilament } from '../../hooks/useOrderFilament';
import { useUiPreferences } from '../../hooks/useUiPreferences';
import type { PlateFitDraft } from './PlateFitDialog';
import { useDraftForecast, type DraftForecastState } from '../../hooks/useDraftForecast';
import { WorkingPlanNavigationGuard } from './WorkingPlanNavigationGuard';
import { planErrorText, PlanReadError } from './planErrorText';
import { OrderForecastPanel } from './OrderForecastPanel';

const EMPTY_EDITS = {};

/** Exact working draft. Autosave never moves the explicitly saved baseline. */
export function PlanBlock({
  order,
  canEdit,
  variant = 'page',
  onEnqueued,
  onDraftChanged,
  onForecast,
  visible = true,
  queueRequest,
  onQueueRequestClose,
  queueOrderReady = true,
  onQueueRequestRetry,
  fitDraft,
  onPlateSelection,
  onPlateFit,
  onLeaveGuard,
  onPreparePlanAction,
}: {
  order: Order;
  canEdit: boolean;
  /** `dialog` drops the page heading — the wizard has its own title. */
  variant?: 'page' | 'dialog';
  /** Fired once per successful enqueue, after the invalidations. */
  onEnqueued?: () => void;
  /** The page uses this to keep its order-level ETA from describing an edited
   * draft. The dialog relies on this block's own notice. */
  onDraftChanged?: (changed: boolean) => void;
  onForecast?: (state: DraftForecastState) => void;
  visible?: boolean;
  /** A shortcut requests a fresh review of this editor's current plan, optionally one line. */
  queueRequest?: { lineId?: number; requestedAt: number; refreshing?: boolean; refreshFailed?: boolean } | null;
  onQueueRequestClose?: () => void;
  /** The view owns order captions and must finish its fresh detail read too. */
  queueOrderReady?: boolean;
  onQueueRequestRetry?: () => void;
  fitDraft?: PlateFitDraft | null;
  onPlateSelection?: (selected: Record<number, number[]>) => void;
  onPlateFit?: () => void;
  onLeaveGuard?: (guard: (() => boolean) | null) => void;
  /** Persist this editor before its owner opens an order-changing action. */
  onPreparePlanAction?: (prepare: (() => Promise<boolean>) | null) => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const active = order.status === 'active';
  const [optionsBlocked, setOptionsBlocked] = useState(false);
  const [uncertainRequest, setUncertainRequest] = useState<typeof queueRequest>(null);
  const [previewRevision, setPreviewRevision] = useState(0);

  const { data: receivedPlan, isLoading, isFetching, isError, dataUpdatedAt, refetch } = useOrderPlan(order.id, active);

  const editBase = useRef<OrderPlan | undefined>(undefined);
  const [frozenPlan, setFrozenPlan] = useState<OrderPlan>();
  const onDraftCommitted = useCallback(() => invalidateOrderViews(queryClient, { orderId: order.id }), [queryClient, order.id]);
  const working = useWorkingPlan(order.id, order.id > 0, onDraftCommitted);
  const latestWorking = useRef(working.state);
  latestWorking.current = working.state;
  const localGeneration = useRef(working.generation);
  const localEditsCurrent = localGeneration.current === working.generation;
  const plan = useMemo(() => localEditsCurrent && frozenPlan ? { ...frozenPlan, lines: frozenPlan.lines.map(line => {
    const fresh = receivedPlan?.lines.find(current => current.line_id === line.line_id);
    return { ...line, product_name: fresh?.product_name ?? line.product_name };
  }) } : withWorkingEntries(receivedPlan, working.state), [frozenPlan, receivedPlan, working.state, localEditsCurrent]);
  const { refreshInputs, change: changeWorking } = working;
  const [planAction, setPlanAction] = useState(false);
  const [confirmPlanAction, setConfirmPlanAction] = useState<'reload' | 'rebuild'>();
  const [planNotice, setPlanNotice] = useState<string>();
  const [needsPlanRead, setNeedsPlanRead] = useState(false);
  const [proposal, setProposal] = useState<{ kind: 'remainder' | 'rebuild'; value: PlanProposal }>();
  const [submission, setSubmission] = useState<PlanSubmission>();
  const [adminCancel, setAdminCancel] = useState(false);
  const [cancelReason, setCancelReason] = useState('');
  const [submissionUncertain, setSubmissionUncertain] = useState(false);
  const submissionAttempt = useRef<{ id: string; registered: boolean } | undefined>(undefined);
  const requestedContext = useRef<string | undefined>(undefined);
  const contextBusy = useRef(false);
  useEffect(() => () => { requestedContext.current = undefined; }, [order.id]);
  useEffect(() => {
    const operation = working.state?.active_operation;
    if (!operation) return;
    let alive = true;
    void api.getPlanSubmission(order.id, operation).then(value => { if (alive) setSubmission(value); }).catch(() => undefined);
    return () => { alive = false; };
  }, [order.id, working.state?.active_operation]);

  // The app-wide currency, fetched the way every other money-showing screen
  // fetches it; `formatMoney` covers the unresolved first paint.
  const { data: settings } = useUiPreferences();

  // The line ETA and the farm's proposed split. Keyed the same way `OrderPage`
  // already queries it (`['order-forecast', id]`) — TanStack dedupes the two
  // mounts, it is not a second fetch.
  const forecast = useQuery({
    queryKey: ['order-forecast', order.id],
    queryFn: () => api.getOrderForecast(order.id),
    enabled: active,
    staleTime: 30_000,
  });

  // The filament this plan still needs against what is on the shelf — in the
  // dialog only: on the order page it is the side panel's (WS-13 E3 F06).
  const filament = useOrderFilament(order.id, active && variant === 'dialog');

  const [localCounts, setCounts] = useState<Record<number, Record<number, number>>>({});
  const [localAdded, setAdded] = useState<Record<number, PlanRowData[]>>({});
  const counts: Record<number, Record<number, number>> = localEditsCurrent ? localCounts : EMPTY_EDITS;
  const added: Record<number, PlanRowData[]> = localEditsCurrent ? localAdded : EMPTY_EDITS;
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
   * Their exact distribution is autosaved. Current demand and recipes can
   * flag issues, but do not replace these operator choices.
   */
  const [localChosen, setChosen] = useState<Record<number, ChosenByRow>>({});
  const [localSplit, setSplit] = useState<Record<number, SplitByRow>>({});
  const chosen: Record<number, ChosenByRow> = localEditsCurrent ? localChosen : EMPTY_EDITS;
  const split: Record<number, SplitByRow> = localEditsCurrent ? localSplit : EMPTY_EDITS;
  const clearLocal = useCallback(() => {
    editBase.current = undefined;
    setFrozenPlan(undefined);
    setCounts({}); setChosen({}); setSplit({}); setAdded({});
    localGeneration.current = working.generation;
  }, [working.generation]);
  useEffect(() => { if (!localEditsCurrent) clearLocal(); }, [localEditsCurrent, clearLocal]);
  const freeze = useCallback(() => {
    if (localGeneration.current !== working.generation) clearLocal();
    if (!editBase.current && plan) { editBase.current = plan; setFrozenPlan(plan); }
  }, [plan, working.generation, clearLocal]);

  // Untouched recommendations may refresh normally. Once the operator edits,
  // freeze the shown rows; current server inputs are reconciled separately.
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
    if (editBase.current) return;
    const before = seenSignatures.current;
    seenSignatures.current = signatures;
    // This branch is only for an untouched recommendation. A local working
    // draft is never reseeded by a new demand or recipe signature.
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
    if (editBase.current) return;
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

  // Every door of the plan files new work under the order — Fф, `orders:update` or
  // `orders:file_prints` (WS-13 E13 O21) — beside the queue's or the printer's own right; a
  // user missing either never sees the button rather than being handed a 403 on click.
  // `canEdit` is the caller's say for the view (Fф on the order page and the plan-from-files
  // dialog); the printer asks Fф itself — a read-only view still prints for whoever may file.
  const filesFuture = canFileFuturePrint(hasPermission);
  const missingContext = !!working.state && working.state.revision > 0 && working.state.entries.some(e => !receivedPlan?.lines.some(l => l.line_id === e.line_id && l.rows.some(r => [r, ...r.alternatives].some(p => p.plate_id === e.plate_id))));
  const contextBehind = missingContext || !!working.state && !!receivedPlan && ((receivedPlan.working_revision ?? 0) < working.state.revision || receivedPlan.inputs_token !== working.state.inputs_token);
  const frozen = !!working.state?.active_operation || submissionUncertain || !!working.error || working.recovering || needsPlanRead || planAction;
  const canQueue = canEdit && filesFuture && hasPermission('queue:create') && !!working.state && !frozen;
  const canPrint = filesFuture && hasPermission('printers:control');
  const canRebalance = canEdit && filesFuture && hasPermission('queue:update_all');

  const invalidate = useCallback(() => {
    invalidateOrderViews(queryClient, { orderId: order.id });
    // ⚠️ The queue keys are NOT order views and stay here: enqueueing puts
    // rows in a printer's queue and in the auto-queue distributor, which no
    // order page reads and `invalidateOrderViews` therefore knows nothing
    // about. The queue's own helper covers the tile that dates them.
    invalidateQueueViews(queryClient);
    queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
  }, [queryClient, order.id]);

  const resetEditor = async (refresh = true) => {
    requestedContext.current = undefined;
    try {
      await working.reload();
    } catch (error) {
      const message = planErrorText(error, t, 'read_failed');
      setNeedsPlanRead(true);
      setPlanNotice(message);
      throw new PlanReadError(message);
    }
    // reload's generation deactivates overlays atomically with its snapshot.
    // A failed read never reaches this cleanup and preserves the local input.
    editBase.current = undefined;
    setFrozenPlan(undefined);
    seenSignatures.current = {};
    setCounts({}); setChosen({}); setSplit({}); setAdded({});
    setProposal(undefined);
    setPlanNotice(undefined);
    setNeedsPlanRead(false);
    if (refresh) await refetch();
    setPreviewRevision(value => value + 1);
  };
  const enqueue = useMutation({
    mutationFn: async (items: PlanEnqueueItem[]) => {
      submissionAttempt.current = undefined;
      if (working.state?.active_operation) throw new Error(t('workingPlan.pending'));
      working.change(canonicalRef.current);
      const state = await working.flush();
      const parts = state.entries.filter(e => e.count > 0 && items.some(i => i.line_id === e.line_id && i.plate_id === e.plate_id))
        .map(e => ({ entry_id: e.entry_id, count: e.count }));
      const operation = newOperationId();
      submissionAttempt.current = { id: operation, registered: false };
      const registered = await api.registerPlanSubmission(order.id, { operation_id: operation, expected_revision: state.revision, parts });
      submissionAttempt.current = { id: operation, registered: true };
      setSubmission(registered);
      // If the response is lost, GET working-plan still exposes the frozen operation.
      await working.reload();
      return api.actPlanSubmission(order.id, operation, 'execute');
    },
    onSuccess: async (result) => {
      setSubmission(result);
      const created = result.created_count ?? result.created.reduce((sum, row) => sum + row.queue_item_ids.length, 0);
      showToast(t(result.error ? 'workingPlan.partial' : 'orders.plan.toast.enqueued', { count: created }) + (result.error ? ` ${planErrorText(result.error, t)}` : ''), result.error ? 'warning' : 'success');
      await resetEditor(false);
      submissionAttempt.current = undefined;
      setSubmissionUncertain(false);
      invalidate(); onEnqueued?.();
    },
    onError: async (e: Error) => {
      const attempt = submissionAttempt.current;
      // A lost execution response may have consumed the draft. Never reload
      // just its revision and then write the pre-submission counts back to it.
      if (attempt && (attempt.registered || !(e instanceof ApiError) || e.status >= 500)) {
        setSubmissionUncertain(true);
        try {
          const status = await api.getPlanSubmission(order.id, attempt.id);
          setSubmission(status);
          if (status.created.length) onEnqueued?.();
          await resetEditor(false);
          invalidate();
          setSubmissionUncertain(false);
        } catch (statusError) {
          if (!attempt.registered && statusError instanceof ApiError && statusError.status === 404) {
            // No execute was sent. A late registration can only freeze a plan,
            // never publish it; a subsequent write/register still has CAS.
            submissionAttempt.current = undefined;
            setSubmissionUncertain(false);
            try { await working.refreshInputs(); } catch { /* Keep local input. */ }
          }
        }
      } else {
        submissionAttempt.current = undefined;
        try { await working.refreshInputs(); } catch { /* Keep local input. */ }
      }
      if (!queueRequest) showToast(planErrorText(e, t), 'error');
    },
  });

  contextBusy.current = frozen || enqueue.isPending;
  useEffect(() => {
    if (!working.state || !receivedPlan || contextBusy.current ||
      queryClient.getQueryState(['project-plan', order.id])?.fetchStatus === 'fetching') return;
    const revision = receivedPlan.working_revision ?? 0;
    const mismatch = revision !== working.state.revision || receivedPlan.inputs_token && receivedPlan.inputs_token !== working.state.inputs_token;
    if ((!mismatch && !missingContext) || isFetching) return;
    const key = JSON.stringify([working.state.revision, working.state.inputs_token, revision, receivedPlan.inputs_token]);
    if (requestedContext.current === key) return;
    requestedContext.current = key;
    const currentAttempt = () => requestedContext.current === key && !contextBusy.current;
    const readFailed = (error: unknown) => {
      if (requestedContext.current !== key) return;
      setNeedsPlanRead(true);
      setPlanNotice(planErrorText(error, t, 'read_failed'));
    };
    const readPlan = async (expected?: WorkingPlan) => {
      const result = await refetch({ cancelRefetch: false });
      if (!currentAttempt()) return;
      if (result.isError) { readFailed(result.error); return; }
      const current = latestWorking.current;
      if (expected && current?.revision === expected.revision && current.inputs_token === expected.inputs_token &&
        ((result.data?.working_revision ?? 0) !== current.revision || result.data?.inputs_token !== current.inputs_token)) {
        setNeedsPlanRead(true); // A bounded read did not reconcile the context.
      }
    };
    // Equal revisions don't date the tokens: focus may have refreshed either
    // resource. Read working first, then metadata once if working stayed put.
    if (revision === working.state.revision && mismatch) {
      const before = working.state;
      void refreshInputs().then(next => {
        if (!next || !currentAttempt()) return;
        if (next.revision === revision && next.inputs_token === before.inputs_token &&
          next.inputs_token !== receivedPlan.inputs_token) return readPlan(next);
      }).catch(readFailed);
    } else if (revision > working.state.revision) {
      void refreshInputs().catch(readFailed);
    } else void readPlan().catch(readFailed);
  }, [receivedPlan, working.state, working.error, working.recovering, needsPlanRead, isFetching, refreshInputs, refetch, enqueue.isPending, planAction, queryClient, order.id, missingContext, t]);

  // The line's Rebalance button (spec 2026-09-10): the same procedure the tick
  // runs behind its setting, forced for this one line. Router rows move, so the
  // queue views and the order views are both re-read.
  const rebalance = useMutation({
    mutationFn: (lineId: number) => api.rebalanceOrderLine(order.id, lineId),
    onSuccess: (result) => {
      if (result.converted > 0) {
        showToast(
          t('orders.plan.rebalance.moved', {
            parts: result.moved_parts,
            converted: result.converted,
            created: result.created,
          }),
          'success',
        );
      } else if (result.skipped.length > 0) {
        // `pending_auto_prints` counts rows the procedure itself refuses —
        // pinned, staged, scheduled — so the button shows for a line of them,
        // and "nothing would finish sooner" would be a claim about the farm
        // that was never asked. The reason the server gave says the truth.
        showToast(t(`autoQueue.rebalance.skipped.${result.skipped[0].reason}`), 'info');
      } else {
        showToast(t('orders.plan.rebalance.nothing'), 'info');
      }
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
      const projected = projectPlan(
        line,
        counts[line.line_id] ?? {},
        {},
        chosen[line.line_id] ?? {},
        split[line.line_id] ?? {},
      );
      prints += projected.prints;
      seconds = seconds == null || projected.seconds == null ? null : seconds + projected.seconds;
      grams += projected.grams;
      if (projected.cost != null) cost = (cost ?? 0) + projected.cost;
    }
    return { prints, seconds, grams: Math.round(grams * 100) / 100, cost };
  }, [lines, counts, chosen, split]);

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

  useEffect(() => {
    const selected: Record<number, number[]> = {};
    for (const line of lines) {
      const ids = new Set<number>();
      for (const row of line.rows) {
        const count = Math.max(0, Math.trunc(counts[line.line_id]?.[row.plate_id] ?? row.count));
        if (!count) continue;
        const picked = chosen[line.line_id]?.[row.plate_id];
        const distribution = split[line.line_id]?.[row.plate_id];
        // An untouched grouped row offers ALL equivalent files to the fleet.
        // Explicit file/split choices still narrow that set; zero rows stay out.
        if (picked == null && distribution == null) {
          [row.plate_id, ...row.alternatives.map(alt => alt.plate_id)].forEach(id => ids.add(id));
        } else {
          rowDistribution(row, count, picked, distribution).forEach(item => ids.add(item.plate_id));
        }
      }
      if (ids.size) selected[line.line_id] = [...ids];
    }
    onPlateSelection?.(selected);
  }, [lines, counts, chosen, split, onPlateSelection]);

  const appliedFit = useRef<PlateFitDraft | null>(null);
  useEffect(() => {
    if (!fitDraft || fitDraft === appliedFit.current || fitDraft.orderId !== order.id || !plan) return;
    if (order.lines.find(l => l.id === fitDraft.lineId)?.quantity !== fitDraft.quantity) return;
    const baseline = plan.lines.find(l => l.line_id === fitDraft.lineId);
    if (!baseline) return;
    appliedFit.current = fitDraft;
    if (JSON.stringify(baseline) !== fitDraft.planSignature) {
      showToast(t('plateFit.errors.stale'), 'warning');
      return;
    }
    const byPlate = new Map(fitDraft.rows.map(row => [row.plate_id, row.count]));
    const represented = new Set<number>();
    const nextCounts: Record<number, number> = {};
    const nextSplit: SplitByRow = {};
    for (const row of baseline.rows) {
      const ids = [row.plate_id, ...row.alternatives.map(alt => alt.plate_id)];
      ids.forEach(id => represented.add(id));
      nextCounts[row.plate_id] = ids.reduce((sum, id) => sum + (byPlate.get(id) ?? 0), 0);
      // The fleet may use equal-yield files on several models. Preserve those
      // exact runs in the existing split editor, rather than merging everything
      // onto the first file or adding a duplicate row for an alternative.
      if (ids.slice(1).some(id => (byPlate.get(id) ?? 0) > 0)) {
        nextSplit[row.plate_id] = Object.fromEntries(ids.map(id => [id, byPlate.get(id) ?? 0]));
      }
    }
    const extraRows = fitDraft.rows.filter(row => !represented.has(row.plate_id));
    for (const row of extraRows) nextCounts[row.plate_id] = row.count;
    freeze();
    setCounts(prev => ({ ...prev, [fitDraft.lineId]: nextCounts }));
    setAdded(prev => ({ ...prev, [fitDraft.lineId]: extraRows }));
    setChosen(prev => ({ ...prev, [fitDraft.lineId]: {} }));
    setSplit(prev => ({ ...prev, [fitDraft.lineId]: nextSplit }));
  }, [fitDraft, order.id, order.lines, plan, showToast, t, freeze]);

  // A forecast belongs to the exact server plan. Compare effective enqueue
  // items rather than the editor's state maps: setting a count back to its
  // planned value is fresh again, and an added row is naturally included.
  const draftChanged = useMemo(() => {
    if (!plan) return false;
    const baseline = plan.lines.flatMap((line) =>
      line.rows.flatMap((row) =>
        rowDistribution(row, row.count, undefined, undefined).map((entry) => ({ ...entry, line_id: line.line_id })),
      ),
    );
    return baseline.length !== items.length || baseline.some((item, index) => {
      const actual = items[index];
      return actual?.line_id !== item.line_id || actual?.plate_id !== item.plate_id || actual?.count !== item.count;
    });
  }, [plan, items]);

  useEffect(() => {
    onDraftChanged?.(draftChanged);
    return () => onDraftChanged?.(false);
  }, [draftChanged, onDraftChanged]);

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
  const canonicalEntries = useMemo(() => workingEntries(lines, counts, chosen, split, working.state?.entries, !!working.state?.revision), [lines, counts, chosen, split, working.state?.entries, working.state?.revision]);
  const canonicalRef = useRef(canonicalEntries);
  canonicalRef.current = canonicalEntries;
  const canonicalSignature = workingSignature(canonicalEntries);
  const workingLoaded = !!working.state;
  const planLoading = active && !isError && !working.error && (!plan || !workingLoaded);
  // An empty draft alone is not completion: missing demand, failed reads and
  // unfinished submissions must retain their review/recovery controls.
  const nothingToPlan = active && !!plan && !isError && !plan.truncated &&
    workingLoaded && !working.error && !working.saving && !working.dirty &&
    working.state!.entries.length === 0 && working.state!.issues.length === 0 &&
    !frozen && !proposal && !queueRequest && lines.length === 0;
  useEffect(() => {
    if (canEdit && localEditsCurrent && editBase.current && latestWorking.current && !latestWorking.current.active_operation && !overCap && !splitOff) changeWorking(canonicalRef.current);
    // A server acknowledgment cannot turn the previous displayed payload into
    // a new edit (notably after accepting changed source metadata).
  }, [canonicalSignature, canEdit, workingLoaded, changeWorking, overCap, splitOff, localEditsCurrent]);

  const runPlanAction = async (action: () => Promise<void>) => {
    setPlanAction(true);
    try { await action(); } catch (error) { showToast(planErrorText(error, t), 'error'); }
    finally { setPlanAction(false); }
  };
  const flushPlan = async () => {
    working.change(canonicalRef.current);
    return working.flush();
  };
  useEffect(() => {
    onPreparePlanAction?.(async () => {
      if (!plan || isFetching || !localEditsCurrent || !working.state || frozen || splitOff || overCap) return false;
      try {
        working.change(canonicalRef.current);
        await working.flush();
        return true;
      } catch (error) {
        showToast(planErrorText(error, t), 'error');
        return false;
      }
    });
    return () => onPreparePlanAction?.(null);
  }, [onPreparePlanAction, plan, isFetching, localEditsCurrent, working, frozen, splitOff, overCap, showToast, t]);
  const savePlan = () => runPlanAction(async () => {
    working.change(canonicalRef.current);
    await working.save(canonicalRef.current);
    showToast(t('workingPlan.saved'), 'success');
  });
  const discardPlan = () => runPlanAction(async () => {
    if (!window.confirm(t('workingPlan.discardConfirm'))) return;
    const state = await flushPlan();
    await api.actWorkingPlan(order.id, 'discard', state);
    await resetEditor();
  });
  const prepareReplan = (kind: 'remainder' | 'rebuild') => runPlanAction(async () => {
    setPlanNotice(undefined);
    try {
      const state = await flushPlan();
      const value = await api.replanWorkingPlan(order.id, { expected_revision: state.revision, kind, inputs_token: state.inputs_token }) as PlanProposal;
      setProposal({ kind, value });
    } catch (error) {
      if (error instanceof ApiError && error.code === 'source_review_required') setPlanNotice(t('workingPlan.reviewRemainder'));
      else throw error;
    }
  });
  const applyReplan = async () => {
    if (!proposal) return;
    try {
      const state = await flushPlan();
      await api.replanWorkingPlan(order.id, { expected_revision: state.revision, kind: proposal.kind,
        inputs_token: proposal.value.inputs_token, apply: true, proposal_token: proposal.value.proposal_token, proposal_entries: proposal.value.entries });
      await resetEditor();
    } catch (error) {
      if (error instanceof PlanReadError) {
        // Apply succeeded; only its subsequent refresh failed. Keep the read
        // recovery action, rather than offering to apply the same proposal.
        setProposal(undefined);
        setConfirmPlanAction(undefined);
        return;
      }
      const stale = error instanceof ApiError ? ['inputs_changed', 'plan_conflict'].includes(error.code ?? '')
        : error instanceof Error && error.message === 'workingPlan.conflict';
      const uncertain = !stale && (!(error instanceof ApiError) || error.status >= 500);
      if (!stale && !uncertain) throw error;
      setProposal(undefined);
      setConfirmPlanAction(undefined);
      setNeedsPlanRead(uncertain);
      setPlanNotice(t(uncertain ? 'workingPlan.applyUncertain' : 'workingPlan.proposalStale'));
    }
  };
  const resumeSubmission = (action: 'execute' | 'cancel') => runPlanAction(async () => {
    const operation = working.state?.active_operation ?? submissionAttempt.current?.id ?? submission?.operation_id;
    if (!operation) return;
    const status = await api.actPlanSubmission(order.id, operation, action);
    setSubmission(status);
    if (status.created.length) onEnqueued?.();
    await resetEditor(false); invalidate();
    setSubmissionUncertain(false);
  });
  const readSubmission = () => runPlanAction(async () => {
    const operation = working.state?.active_operation ?? submissionAttempt.current?.id ?? submission?.operation_id;
    if (operation) {
      const status = await api.getPlanSubmission(order.id, operation);
      setSubmission(status);
      if (status.created.length) onEnqueued?.();
    }
    await resetEditor(false); invalidate();
    setSubmissionUncertain(false);
  });
  const exact = useDraftForecast(order.id, items, working.state?.inputs_token ?? plan?.inputs_token,
    active && !!plan && visible, overCap || splitOff, previewRevision,
    working.state ? { entries: canonicalEntries, saved_revision: working.state.saved_revision, epoch: working.state.epoch } : undefined);
  const presentedForecast = useMemo(() => ({ ...exact, nothingToPlan, planLoading }), [exact, nothingToPlan, planLoading]);
  useEffect(() => { onForecast?.(presentedForecast); }, [presentedForecast, onForecast]);
  const lineForecast = (lineId: number) => {
    const previous = forecast.data?.lines.find(line => line.line_id === lineId);
    const current = exact.result?.forecast?.lines.find(line => line.line_id === lineId);
    if (!current) return draftChanged ? undefined : previous;
    return {
      ...current,
      eta_complete: current.eta_complete && !exact.result?.coverage.some(p => p.line_id === lineId && p.missing > 0),
      // A fleet proposal is an explicit editing action, separate from the date
      // of the selected files. Do not replace exact placement with its advice.
      rows: draftChanged ? current.rows : previous?.rows ?? current.rows,
    };
  };

  const setCount = (lineId: number, plateId: number, next: number) => { freeze();
    setCounts((prev) => ({ ...prev, [lineId]: { ...(prev[lineId] ?? {}), [plateId]: Math.max(0, next) } })); };

  const setChoice = (lineId: number, rowPlateId: number, plateId: number) => { freeze();
    setChosen((prev) => ({ ...prev, [lineId]: { ...(prev[lineId] ?? {}), [rowPlateId]: plateId } })); };

  const setRowSplit = (lineId: number, rowPlateId: number, next: Record<number, number>) => { freeze();
    setSplit((prev) => ({ ...prev, [lineId]: { ...(prev[lineId] ?? {}), [rowPlateId]: next } })); };

  // Shortcuts use the SAME edited rows and writer as the plan, scoped by line id:
  // two lines may name the same product but ask for different configurations.
  const selectedLines = queueRequest
    ? lines.filter((line) => queueRequest.lineId == null || line.line_id === queueRequest.lineId)
    : [];
  const selectedItems = selectedLines.flatMap((line) => itemsFor(line));
  const selectedSplitOff = selectedLines.some((line) => line.rows.some((row) => splitIsOff(
    split[line.line_id]?.[row.plate_id],
    Math.max(0, Math.trunc(counts[line.line_id]?.[row.plate_id] ?? row.count)),
  )));
  const waitingForPlan = queueRequest?.refreshing || !queueOrderReady
    || isLoading || isFetching || !plan || dataUpdatedAt < (queueRequest?.requestedAt ?? 0);
  const reviewMismatch = selectedLines.some((line) => !order.lines.some(
    (detail) => detail.id === line.line_id && detail.product_id === line.product_id,
  ));
  const invalidDraft = selectedItems.some((item) => item.count > MAX_PER_PLATE) || selectedSplitOff;
  const queueBlocked = !active || !canQueue || waitingForPlan || queueRequest?.refreshFailed || reviewMismatch || isError || optionsBlocked || invalidDraft
    || selectedItems.length === 0 || enqueue.isPending || uncertainRequest === queueRequest;
  const queueConfirmation = queueRequest && onQueueRequestClose ? (
    <ActionConfirm
      title={t('orders.plan.queueConfirm.title')}
      subtitle={order.name}
      primaryLabel={t('orders.plan.queueConfirm.send')}
      primaryDisabled={queueBlocked}
      onClose={onQueueRequestClose}
      send={async () => {
        if (queueBlocked) throw new Error(t('orders.plan.queueConfirm.notReady'));
        try {
          await enqueue.mutateAsync(selectedItems);
        } catch (error) {
          // A writer commits each item. A 5xx or lost response may have filed some
          // work: do not resend this review. Reopening reads the remaining plan.
          if (!(error instanceof ApiError) || error.status >= 500) {
            setUncertainRequest(queueRequest);
            invalidate();
          }
          throw new Error(planErrorText(error, t));
        }
      }}
      body={<div className="space-y-3" data-testid="plan-queue-review">
        <p>{t(queueRequest.lineId == null ? 'orders.plan.queueConfirm.orderScope' : 'orders.plan.queueConfirm.lineScope')}</p>
        {queueRequest.refreshFailed || (!waitingForPlan && reviewMismatch) ? <div role="alert">
          <p>{t('orders.plan.queueConfirm.refreshFailed')}</p>
          <Button size="sm" variant="secondary" onClick={onQueueRequestRetry}>{t('orders.plan.retry')}</Button>
        </div> : isError ? <div role="alert">
          <p>{t('orders.plan.loadFailed')}</p>
          <Button size="sm" variant="secondary" onClick={() => onQueueRequestRetry ? onQueueRequestRetry() : void refetch()}>{t('orders.plan.retry')}</Button>
        </div> : waitingForPlan ? <p role="status">{t('common.loading')}</p> : <>
          <ul className="space-y-1">
            {[...order.lines].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id)
              .map((line, index) => ({ line, position: index + 1 }))
              .filter(({ line }) => selectedItems.some((item) => item.line_id === line.id))
              .map(({ line, position }) => <li key={line.id}>
                {t('orders.plan.queueConfirm.position', { position })} · {line.product_name} — {t('orders.plan.queueConfirm.runs', {
                  count: selectedItems.filter((item) => item.line_id === line.id).reduce((sum, item) => sum + item.count, 0),
                })}
                <small className="block text-bambu-gray">{lineConfigLabel(line.configuration, line.mode, t) || t('orders.lines.standardConfig')}</small>
              </li>)}
          </ul>
          <p className="font-medium">{t('orders.plan.queueConfirm.total', {
            count: selectedItems.reduce((sum, item) => sum + item.count, 0),
          })}</p>
          {selectedItems.length === 0 && <p>{t('orders.plan.nothingOutstanding')}</p>}
          {(plan?.truncated || selectedLines.some((line) => line.unsatisfiable.length > 0 || line.not_sliced.length > 0))
            && <p className="text-amber-700 dark:text-amber-300">{t('orders.plan.queueConfirm.incomplete')}</p>}
        </>}
        {optionsBlocked && <p role="alert">{t('orderPrintOptions.unsaved')}</p>}
        {uncertainRequest === queueRequest && <p role="status">{t('orders.plan.queueConfirm.uncertain')}</p>}
        {invalidDraft && <p role="alert">{t('orders.plan.queueConfirm.invalidDraft')}</p>}
        {!active && <p role="alert">{t('orders.plan.closedText')}</p>}
        {!canQueue && <p role="alert">{t('orders.plan.queueConfirm.notReady')}</p>}
        <p className="text-xs text-bambu-gray">{t('orders.plan.queueConfirm.explanation')}</p>
      </div>}
    />
  ) : null;
  const intro = (
    <p className="text-xs leading-[18px] text-bambu-gray" data-testid="plan-intro">
      {t('orders.plan.intro')}
    </p>
  );

  const navigationGuard = <WorkingPlanNavigationGuard orderId={order.id} dirty={working.dirty || working.saving || splitOff ||
    !!editBase.current && workingSignature(canonicalEntries) !== workingSignature(working.state?.entries)} onLeaveGuard={onLeaveGuard} />;
  const entryCaption = (entry: { line_id: number; plate_id: number }) => {
    const line = receivedPlan?.lines.find(line => line.line_id === entry.line_id);
    const row = line?.rows.flatMap(row => [row, ...row.alternatives]).find(row => row.plate_id === entry.plate_id);
    const unavailable = line?.rows.some(group => group.issue === 'line_unavailable'
      && [group, ...group.alternatives].some(plate => plate.plate_id === entry.plate_id));
    const name = line && line.product_id > 0 && !unavailable
      ? line.product_name ?? t('workingPlan.unavailableLine') : t('workingPlan.unavailableLine');
    return [name, row?.filename || t('workingPlan.unavailableSource')].join(' · ');
  };

  // ⚠️ `plan-block` marks THE BLOCK, on every branch — it answers "is the plan
  // on this page", not "has the plan arrived". Carrying it on the loaded and
  // failed branches only made "the page mounts the block" a question that could
  // be asked of the page just before the plan resolved, and answered no.
  if (!active) {
    return (
      <section id="order-plan" className="space-y-3" data-testid="plan-block">
        {navigationGuard}
        {queueConfirmation}
        <div className="py-8 text-center" data-testid="plan-closed">
          <p className="text-sm font-medium text-white">{t('orders.plan.closedTitle')}</p>
          <p className="mt-1 text-sm text-bambu-gray">{t('orders.plan.closedText')}</p>
          {working.state && working.state.revision > 0 && <details className="mt-3 text-sm text-bambu-gray-light">
            <summary>{t('workingPlan.closedDraft')}</summary>
            <ul>{working.state.entries.map(e => <li key={e.entry_id}>{entryCaption(e)}: {e.count}</li>)}</ul>
            {working.state.saved_at && <p>{t('workingPlan.baseline', { who: working.state.saved_by, when: formatDateTime(working.state.saved_at, settings?.time_format, settings?.date_format) })}</p>}
          </details>}
        </div>
      </section>
    );
  }

  if (isLoading || (plan && planLoading)) {
    return (
      <section id="order-plan" className="space-y-3" data-testid="plan-block">
        {navigationGuard}
        {queueConfirmation}
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
        {navigationGuard}
        {queueConfirmation}
        <div className="flex items-center gap-3 flex-wrap text-sm">
          <p className="text-red-600 dark:text-red-400" data-testid="plan-error">
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
        {navigationGuard}
        {queueConfirmation}
        <div className="flex items-center gap-2 text-bambu-gray text-sm" data-testid="plan-idle">
          <Loader2 className="w-4 h-4 animate-spin" />
          {t('common.loading')}
        </div>
      </section>
    );
  }

  if (nothingToPlan) {
    return <section id="order-plan" className="space-y-3" data-testid="plan-block">
      <p className="rounded-xl border border-bambu-green/30 bg-bambu-green/10 px-4 py-3 text-sm text-white"
        role="status" data-testid="plan-empty">{t('orders.plan.nothingOutstanding')}</p>
      {variant === 'dialog' && (exact.filament ?? (!draftChanged ? filament.data : undefined)) &&
        <FilamentNeeds needs={(exact.filament ?? filament.data)!} />}
    </section>;
  }

  const fitAction = onPlateFit && order.lines.some(l => l.mode !== 'parts') ? (
    <Button variant="secondary" className="ring-1 ring-bambu-green/60" onClick={onPlateFit} disabled={enqueue.isPending || rebalance.isPending || frozen}>
      <Calculator className="w-5 h-5 shrink-0 text-bambu-green" aria-hidden="true" />
      {t('plateFit.nearest')}
    </Button>
  ) : null;

  return (
    <section id="order-plan" className="space-y-3" data-testid="plan-block">
      {navigationGuard}
      {queueConfirmation}
      {confirmPlanAction && <ActionConfirm title={t(confirmPlanAction === 'reload' ? 'workingPlan.reload' : 'workingPlan.rebuild')}
        body={t(confirmPlanAction === 'reload' ? 'workingPlan.reloadConfirm' : 'workingPlan.rebuildConfirm')}
        primaryLabel={t(confirmPlanAction === 'reload' ? 'workingPlan.reload' : 'workingPlan.apply')}
        onClose={() => setConfirmPlanAction(undefined)} send={async () => {
          setPlanAction(true);
          try {
            if (confirmPlanAction === 'reload') await resetEditor();
            else await applyReplan();
          } catch (error) { throw new Error(planErrorText(error, t)); }
          finally { setPlanAction(false); }
        }} />}
      {intro}
      <div className="rounded-xl border border-bambu-dark-tertiary p-3 space-y-2" data-testid="working-plan-controls">
        <p className="text-sm text-bambu-gray-light" role="status">
          {t(working.recovering ? 'workingPlan.reading' : working.saving ? 'workingPlan.saving' : working.dirty ? 'workingPlan.unsaved' : !working.state ? 'common.loading' : working.state.revision ? 'workingPlan.autosaved' : 'workingPlan.notSaved')}
        </p>
        <p className="text-xs text-bambu-gray">{working.state?.saved_at
          ? t('workingPlan.baseline', { who: working.state.saved_by, when: formatDateTime(working.state.saved_at, settings?.time_format, settings?.date_format) })
          : t('workingPlan.noBaseline')}</p>
        {contextBehind && <div className="text-sm text-bambu-gray" role="status">
          {t(isError || !isFetching ? 'workingPlan.contextFailed' : 'workingPlan.contextChanged')}
          {!isFetching && <Button size="sm" variant="secondary" onClick={() => void refetch()}>{t('orders.plan.retry')}</Button>}
        </div>}
        {working.state?.inputs_changed && <p className="text-sm text-amber-700 dark:text-amber-400">{t('workingPlan.inputsChanged')}</p>}
        {working.baselineChanged && <p role="alert" className="text-sm text-amber-700 dark:text-amber-400">{t('workingPlan.baselineChanged')}</p>}
        {working.state?.saved_entries && <details className="text-xs text-bambu-gray-light">
          <summary>{t('workingPlan.viewBaseline')}</summary>
          <p>{t('orderDraft.machineHours')}: {working.state.saved_machine_seconds == null ? '—' : formatDuration(working.state.saved_machine_seconds)}</p>
          <ul>{working.state.saved_entries.map(e => <li key={e.entry_id}>{entryCaption(e)}: {e.count}</li>)}</ul>
        </details>}
        {working.state?.saved_epoch != null && working.state.saved_epoch !== working.state.epoch &&
          <p className="text-sm text-amber-700 dark:text-amber-400">{t('workingPlan.scopeChanged')}</p>}
        {planNotice && <p role="alert" className="text-sm text-amber-700 dark:text-amber-400">{planNotice}</p>}
        {(working.error || needsPlanRead) && <div role="alert" className="text-sm text-amber-700 dark:text-amber-400">
          {working.error && planErrorText(working.error, t)} <Button size="sm" disabled={planAction || working.recovering} onClick={() => setConfirmPlanAction('reload')}>{t('workingPlan.reload')}</Button>
        </div>}
        {proposal && <div className="text-sm space-y-2" data-testid="working-plan-proposal">
          <p>{t('workingPlan.proposal', { before: proposal.value.previous_count ?? '—', after: proposal.value.count ?? '—' })}</p>
          {proposal.value.removed_issue_entry_count != null && <p>{t('workingPlan.removedIssues', { count: proposal.value.removed_issue_entry_count })}</p>}
          <Button size="sm" disabled={frozen} onClick={() => proposal.kind === 'rebuild' ? setConfirmPlanAction('rebuild') : void runPlanAction(applyReplan)}>{t('workingPlan.apply')}</Button>
          <Button size="sm" variant="secondary" disabled={planAction} onClick={() => setProposal(undefined)}>{t('common.cancel')}</Button>
          <details><summary>{t('workingPlan.proposalDetails')}</summary><ul>{proposal.value.entries.map(e => <li key={e.entry_id}>{entryCaption(e)}: {e.count} ({t('workingPlan.protected', { count: e.protected_count })})</li>)}</ul></details>
        </div>}
        {canEdit && <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={frozen || !working.state || overCap || splitOff} onClick={() => void savePlan()}>{t('workingPlan.save')}</Button>
          <Button size="sm" variant="secondary" disabled={frozen || !working.state?.saved_revision || working.state.saved_epoch !== working.state.epoch} onClick={() => void discardPlan()}>{t('workingPlan.discard')}</Button>
          <Button size="sm" variant="secondary" disabled={frozen || !working.state || overCap || splitOff} onClick={() => void prepareReplan('remainder')}>{t('workingPlan.remainder')}</Button>
          <Button size="sm" variant="secondary" disabled={frozen || !working.state || overCap || splitOff} onClick={() => void prepareReplan('rebuild')}>{t('workingPlan.rebuild')}</Button>
        </div>}
        {(!!working.state?.active_operation || submissionUncertain) && <div className="text-sm space-y-2" role="alert">
          <p>{t('workingPlan.pending')}</p>
          {submission && <p>{t('workingPlan.receipts', { done: submission.created_count ?? submission.created.reduce((n, r) => n + r.count, 0), remaining: submission.remaining_count ?? submission.remaining.reduce((n, r) => n + r.count, 0) })}</p>}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" disabled={planAction} onClick={() => void readSubmission()}>{t('workingPlan.readStatus')}</Button>
            {filesFuture && hasPermission('queue:create') && submission?.can_execute !== false && <>
              <Button size="sm" disabled={planAction || !submission} onClick={() => void resumeSubmission('execute')}>{t('workingPlan.continue')}</Button>
              <Button size="sm" variant="secondary" disabled={planAction || !submission} onClick={() => void resumeSubmission('cancel')}>{t('workingPlan.cancel')}</Button>
            </>}
            {submission?.is_foreign && submission.can_cancel && <Button size="sm" variant="secondary" disabled={planAction} onClick={() => { setCancelReason(''); setAdminCancel(true); }}>{t('workingPlan.adminCancel')}</Button>}
          </div>
        </div>}
        {working.state?.issues.map(issue => <div key={`${issue.entry_id}-${issue.code}`} className="text-sm text-amber-700 dark:text-amber-400">
          <p>{t(issue.code === 'line_unavailable' ? 'workingPlan.lineIssue' : issue.code === 'source_unavailable' ? 'workingPlan.unavailableIssue' : issue.code === 'source_changed' ? 'workingPlan.changedIssue' : issue.code === 'order_resources_changed' ? 'orderResources.errors.order_resources_changed' : issue.code.startsWith('target_') ? 'workingPlan.targetIssue' : 'workingPlan.sourceIssue')} ({entryCaption(working.state!.entries.find(e => e.entry_id === issue.entry_id) ?? { line_id: -1, plate_id: -1 })})</p>
          {canEdit && !frozen && <div className="flex flex-wrap gap-2">
            {issue.code.startsWith('target_') && <Button size="sm" variant="secondary" onClick={() => void runPlanAction(async () => {
              working.change(canonicalRef.current.map(e => e.entry_id === issue.entry_id ? { ...e, target: { kind: 'auto' }, manual: true, protected_count: e.count } : e));
              await working.flush(); await resetEditor();
            })}>{t('workingPlan.useAuto')}</Button>}
            {issue.current_source_token && <Button size="sm" variant="secondary" onClick={() => void runPlanAction(async () => {
              working.change(canonicalRef.current.map(e => e.entry_id === issue.entry_id ? { ...e, source_token: issue.current_source_token } : e));
              await working.flush(); await resetEditor();
            })}>{t('workingPlan.acceptSource')}</Button>}
            <Button size="sm" variant="secondary" onClick={() => void runPlanAction(async () => {
              if (!window.confirm(t('workingPlan.removeConfirm'))) return;
              working.change(canonicalRef.current.filter(e => e.entry_id !== issue.entry_id));
              await working.flush(); await resetEditor();
            })}>{t('workingPlan.removeEntry')}</Button>
          </div>}
        </div>)}
      </div>
      {adminCancel && submission && <ActionConfirm title={t('workingPlan.adminCancel')}
        body={<div className="space-y-3"><p>{t('workingPlan.adminCancelDescription')}</p><label className="block">{t('workingPlan.cancelReason')}<textarea value={cancelReason} maxLength={500} onChange={e => setCancelReason(e.target.value)} className="block w-full bg-bambu-dark border border-bambu-gray rounded p-2" /></label></div>}
        primaryLabel={t('workingPlan.adminCancel')} primaryDisabled={!cancelReason.trim()} danger
        onClose={() => setAdminCancel(false)} send={async () => {
          setSubmission(await api.cancelPlanSubmission(order.id, submission.operation_id, cancelReason.trim()));
          await resetEditor(false); invalidate(); setAdminCancel(false);
        }} />}
      <OrderPrintOptions key={order.id} orderId={order.id}
        canEdit={canEdit && hasPermission('orders:update')} busy={enqueue.isPending}
        onBlocked={setOptionsBlocked} />

      {/* The engine's iteration guard stopped covering, so what follows is a
          PREFIX of the plan — rows, totals and no unsatisfiable part all look
          exactly like a finished one. Said once for the whole order, above the
          lines, because the operator's next move (print all of this, then ask
          again) is the same whichever line was cut short. */}
      {plan.truncated && (
        <div className="flex items-center gap-2 text-sm" data-testid="plan-truncated">
          <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-500 shrink-0" />
          <span className="text-amber-700 dark:text-amber-300">{t('orders.plan.truncated')}</span>
        </div>
      )}

      {variant === 'dialog' && <OrderForecastPanel view={{ kind: 'loading' }} draft={exact}
        remaining={order.figures?.remaining ?? 0} onRetry={exact.retry} headingLevel={3} />}

      {lines.length === 0 ? (<>
        <p
          className="rounded-xl border border-bambu-green/30 bg-bambu-green/10 px-4 py-3 text-sm text-white"
          data-testid="plan-empty"
        >
          {t('orders.plan.nothingOutstanding')}
        </p>
        {fitAction && <div className="flex justify-end">{fitAction}</div>}
      </>) : (
        <>
          <fieldset disabled={frozen} className="min-w-0 space-y-3">
            {lines.map((line) => (
              <PlanLine
                key={line.line_id}
                order={order}
                line={line}
                addedIds={new Set((added[line.line_id] ?? []).map((row) => row.plate_id))}
                forecast={lineForecast(line.line_id)}
                counts={counts[line.line_id] ?? {}}
                chosen={chosen[line.line_id] ?? {}}
                split={split[line.line_id] ?? {}}
                currency={settings?.currency}
                showCost={showCost}
                canQueue={canQueue}
                canPrint={canPrint}
                busy={enqueue.isPending || rebalance.isPending || optionsBlocked || frozen}
                canRebalance={canRebalance}
                onRebalance={() => rebalance.mutate(line.line_id)}
                ratePerGram={ratePerGram}
                onCount={(plateId, next) => setCount(line.line_id, plateId, next)}
                onChoose={(rowPlateId, plateId) => setChoice(line.line_id, rowPlateId, plateId)}
                onSplit={(rowPlateId, next) => setRowSplit(line.line_id, rowPlateId, next)}
                onAddPlate={(row) => { freeze(); setAdded((prev) => ({ ...prev, [line.line_id]: [...(prev[line.line_id] ?? []), row] })); }}
                onEnqueueRow={(rowPlateId) => enqueue.mutate(itemsFor(line, rowPlateId))}
                onQueued={invalidate}
                variant={variant}
              />
            ))}
          </fieldset>

          {/* E11: one line — prints, time, weight and, when it is known, the filament's
              cost — and the whole-plan button beside it. */}
          <div
            className="flex items-center justify-between gap-3 flex-wrap border-t border-bambu-dark-tertiary pt-3 px-1 text-sm"
            data-testid="plan-total"
          >
            <p className="text-bambu-gray-light tabular-nums">
              {t('orders.plan.totals.label')}{' '}
              <b className="text-white font-semibold" data-testid="plan-totals-prints">
                {t('orders.plan.totals.prints', { count: totals.prints })}
              </b>
              {' · '}
              <span data-testid="plan-totals-time">{totals.seconds == null ? '—' : formatDuration(totals.seconds)}</span>
              {' · '}
              <span data-testid="plan-totals-grams">{t('orders.plan.row.grams', { grams: totals.grams.toFixed(1) })}</span>
              {totals.cost != null && (
                <>
                  {' · '}
                  <span data-testid="plan-totals-cost">
                    {t('orders.plan.totals.cost', { cost: formatMoney(totals.cost, settings?.currency) })}
                  </span>
                </>
              )}
            </p>

            {fitAction}
            {canQueue && (
              <Button
                data-testid="plan-enqueue-all"
                disabled={enqueue.isPending || optionsBlocked || overCap || splitOff}
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

      {/* ⚠️ OUTSIDE the ternary above, deliberately. The need is the plan's
          remainder PLUS the order's pending queue rows, so «nothing
          outstanding» is exactly the state in which the whole need lives in
          the queue — and that was the state the table hid itself in (final
          review I2). The query is already gated on the order being active,
          which is the only gate this has. */}
      {variant === 'dialog' && (exact.filament ?? (!draftChanged ? filament.data : undefined)) &&
        <FilamentNeeds needs={(exact.filament ?? filament.data)!} />}
    </section>
  );
}
