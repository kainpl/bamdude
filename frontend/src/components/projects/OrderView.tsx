import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Loader2 } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { canFileFuturePrint } from '../../utils/workshopRights';
import { OrderHeader } from './OrderHeader';
import { OrderStageStepper } from './OrderStageStepper';
import { CloseSuggestionBanner } from './CloseSuggestionBanner';
import { OrderFigures } from './OrderFigures';
import { OrderLinesTable } from './OrderLinesTable';
import { PlanBlock } from './PlanBlock';
import { ProcurementChecklist } from './ProcurementChecklist';
import { OrderPrints } from './OrderPrints';
import { OrderQueue } from './OrderQueue';
import { OrderTimeline } from './OrderTimeline';
import { OrderAutoEject } from './OrderAutoEject';
import { OrderNotes } from './OrderNotes';
import { OrderAttachments } from './OrderAttachments';
import { TakeStockBanner } from './TakeStockBanner';
import { useFulfilment } from '../../hooks/useFulfilment';
import { useForgetOnUnmount } from '../../hooks/useForgetOnUnmount';
import { useOrderDetail } from '../../hooks/useOrderDetail';
import { DispatchNotesSection } from '../stock/DispatchNotesSection';
import { WorkshopPanel } from '../workshop/WorkshopPanel';
import { forecastView } from './orderForecastView';
import { OrderForecastPanel } from './OrderForecastPanel';
import { OrderFilamentPanel } from './OrderFilamentPanel';
import { OrderPrinterResources } from './OrderPrinterResources';
import { ORDER_SECTIONS, sectionParam, type OrderSection } from './orderSections';
import { WorkshopTabPanel, WorkshopTabs } from '../workshop/WorkshopTabs';
import { useOrderPlan } from '../../hooks/useOrderPlan';
import { toOrderRef } from './orderActions/orderRef';
import type { OrderActions, RunExtra } from './orderActions/useOrderActions';
import type { PlateFitDraft } from './PlateFitDialog';
import type { DraftForecastState } from '../../hooks/useDraftForecast';

/**
 * One order: who it is for, what it asks for, and how much of it is printed.
 * The order page and the orders workspace draw this same component (spec
 * workshop-order-views, rule 12); its root is a size container, so its
 * sections lay out by the room they are given, not by the window.
 *
 * The view composes sections and owns no dialog of the order — those are the
 * page's action host's (WS-13 E6 B01), reached through `actions`. Every figure
 * comes from `GET /projects/{id}` and is shown as sent (design decision 8).
 * `PlanBlock` below the lines answers the other half: what to print next, and
 * how to send it. It owns its own query and its own what-if counts, so the
 * page delegates queue-shortcut review to that same owner.
 */
export function OrderView({
  id,
  onDeleted,
  embedded = false,
  section: sectionProp,
  onSectionChange,
  actions,
}: {
  id: number;
  /** The owner's way out once THIS order is deleted — the route leaves, the workspace moves on. */
  onDeleted: () => void;
  /** The page's order action host (E6 B01) — the view mounts none of the order's dialogs. */
  actions: OrderActions;
  /** Inside another page (the workspace's pane): no breadcrumb out of it, and not the page's heading. */
  embedded?: boolean;
  /** The open section — the owner keeps it in its URL (WS-13 E3 F02); absent, the view keeps its own. */
  section?: OrderSection;
  onSectionChange?: (section: OrderSection) => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const forgetOrder = useForgetOnUnmount(['project', id]);

  const [planDraftChanged, setPlanDraftChanged] = useState(false);
  const [planActionRevision, setPlanActionRevision] = useState(0);
  const preparePlanAction = useRef<(() => Promise<boolean>) | null>(null);
  const registerPlanPreparation = useCallback((prepare: (() => Promise<boolean>) | null) => {
    preparePlanAction.current = prepare;
  }, []);
  const [draftForecast, setDraftForecast] = useState<DraftForecastState>();
  const [fitDraft, setFitDraft] = useState<PlateFitDraft | null>(null);
  const plateSelection = useRef<Record<number, number[]>>({});
  const rememberPlates = useCallback((selected: Record<number, number[]>) => { plateSelection.current = selected; }, []);
  const canFit = (['orders:read', 'orders:update', 'products:read', 'stock:read'] as const).every(p => hasPermission(p));
  const openFit = () => {
    if (!canFit) return;
    actions.plateFit?.({ orderId: id, selectedPlates: plateSelection.current, onApplied: setFitDraft });
  };
  const [queueRequest, setQueueRequest] = useState<{
    orderId: number; lineId?: number; requestedAt: number; refreshing?: boolean; refreshFailed?: boolean;
  } | null>(null);
  // When the plan was last sent: until the forecast is read again after it, the
  // cached answer is the previous plan's and is not shown as current (R03).
  const [enqueuedAt, setEnqueuedAt] = useState<number | null>(null);

  // The six sections (WS-13 E3 §F). Controlled by the owner's URL; a view with no
  // owner keeps its own. A section is mounted on its first visit and KEPT — hidden
  // — from then on, so a draft in the notes or the plan survives a look at another
  // tab, and a section nobody opened asks the server nothing (F04).
  const [ownSection, setOwnSection] = useState<OrderSection>('plan');
  const section = sectionProp ?? ownSection;
  const selectSection = (next: OrderSection) => {
    if (onSectionChange) onSectionChange(next);
    else setOwnSection(next);
  };
  const [visited, setVisited] = useState<ReadonlySet<OrderSection>>(() => new Set([section]));
  if (!visited.has(section)) setVisited(new Set([...visited, section]));
  const reviewQueuePlan = (lineId?: number) => {
    const request = { orderId: id, lineId, requestedAt: Date.now(), refreshing: true };
    setQueueRequest(request);
    selectSection('plan');
    // Captions belong to the order, not the plan. Read that first, then cancel
    // any earlier plan read and ask again. Neither an old response nor one
    // successful half of the review may enable Send. Keep the existing editor.
    const orderKey = ['project', id];
    const planKey = ['project-plan', id];
    void (async () => {
      let refreshFailed = false;
      try {
        await Promise.all([
          queryClient.cancelQueries({ queryKey: orderKey, exact: true }),
          queryClient.cancelQueries({ queryKey: planKey, exact: true }),
        ]);
        await queryClient.refetchQueries({ queryKey: orderKey, exact: true, type: 'active' }, { throwOnError: true });
        await queryClient.cancelQueries({ queryKey: planKey, exact: true });
        await queryClient.invalidateQueries({ queryKey: planKey, exact: true }, { throwOnError: true });
      } catch {
        refreshFailed = true;
      }
      // A cancelled review, another shortcut or navigation owns its own reads.
      setQueueRequest((current) => current === request ? { ...request, refreshing: false, refreshFailed } : current);
    })();
  };
  const tabsId = useId();
  // The plan tab's count READS the plan block's query and never fetches for itself
  // (E1 CN2): no parentheses until the plan has been asked for, «(—)» while it is read.
  const planForCount = useOrderPlan(id, false);

  useEffect(() => {
    setPlanDraftChanged(false);
    setDraftForecast(undefined);
    setQueueRequest(null);
  }, [id]);

  const {
    data: order,
    isLoading,
    isError,
    error,
    isFetching: orderFetching,
    dataUpdatedAt: orderUpdatedAt,
  } = useOrderDetail(id);

  // Only an active order can still be simulated forward — a completed or
  // cancelled one has nothing left to schedule.
  // What the order could assemble, receive and issue now — the header button, the
  // banner's counts. Only an active order has anything to issue.
  const fulfilment = useFulfilment(
    Number.isFinite(id) ? id : null,
    order?.status === 'active' && hasPermission('orders:update'),
  );

  const forecast = useQuery({
    queryKey: ['order-forecast', id],
    queryFn: () => api.getOrderForecast(id),
    enabled: Number.isFinite(id) && order?.status === 'active',
    staleTime: 30_000,
  });

  // After a send the forecast is READ AGAIN (E3-V02). The invalidation that follows
  // the send cannot promise it: a query still waiting for its first answer hands that
  // in-flight read back instead of starting one, and its late answer — the previous
  // plan's — would land after the send and pass for fresh. Dropping the in-flight read
  // and asking anew makes every answer that lands after `enqueuedAt` one asked after it.
  const onPlanSent = useCallback(() => {
    setEnqueuedAt(Date.now());
    const queryKey = ['order-forecast', id];
    void queryClient
      .cancelQueries({ queryKey })
      .then(() => queryClient.refetchQueries({ queryKey, type: 'active' }));
  }, [queryClient, id]);

  // The page's way back to the list (spec B03): above the header, outside it, and
  // there in the loading and error states too (B05). The workspace has none — its
  // list is beside it, and a crumb would lead to a bare /projects.
  const crumbs = embedded ? null : (
    <nav aria-label={t('orders.header.breadcrumbLabel')} className="mb-3 flex min-w-0 items-center gap-1 text-sm text-bambu-gray">
      <Link to="/projects" className="shrink-0 hover:text-white transition-colors">
        {t('orders.header.breadcrumb')}
      </Link>
      {order && (
        <>
          <ChevronRight className="w-4 h-4 shrink-0" aria-hidden />
          <span className="min-w-0 truncate text-white">{order.name}</span>
        </>
      )}
    </nav>
  );

  if (isLoading) {
    return (
      <div className={embedded ? '' : 'p-4'}>
        {crumbs}
        <div className="flex items-center gap-2 text-bambu-gray">
          <Loader2 className="w-4 h-4 animate-spin" />
          {t('common.loading')}
        </div>
      </div>
    );
  }
  // ⚠️ Data presence first, then `isError` — see the long note on ProductPage.
  // TanStack v5 flips `status` to "error" on any failed fetch, a background
  // REFETCH of a query still holding good data included, and this page
  // invalidates `['project', id]` on every mutation its sections make. An
  // `isError`-first check would throw the whole rendered order away because a
  // refetch blipped. With no data the two cases still read apart: a fetch that
  // FAILED is not an order that was deleted.
  if (!order) {
    // A 404 is an order that is not there (deleted, or a wrong link): «not found», under the
    // crumbs back to the list — as a product and a stock position say it (WS-13 E13 H05).
    const missing = !isError || (error instanceof ApiError && error.status === 404);
    return (
      <div className={embedded ? '' : 'p-4'}>
        {crumbs}
        {missing ? (
          <div className="text-bambu-gray text-sm">{t('orders.page.notFound')}</div>
        ) : (
          <div className="text-sm text-red-600 dark:text-red-500">
            {t('orders.page.loadFailed')} {(error as Error)?.message}
          </div>
        )}
      </div>
    );
  }

  const canEdit = hasPermission('orders:update');
  const ref = toOrderRef(order);
  // What every run from this view adds: the full order, and — for a delete — the
  // forget-on-unmount of THIS view's query, then the owner's way out (E6 B08).
  const extra: RunExtra = {
    order,
    onDeleted: () => {
      forgetOrder();
      onDeleted();
    },
  };
  const legacyForecast = forecastView({
    active: order.status === 'active',
    draft: planDraftChanged,
    sentAt: enqueuedAt,
    dataUpdatedAt: forecast.dataUpdatedAt,
    errorUpdatedAt: forecast.errorUpdatedAt,
    data: forecast.data,
    isError: forecast.isError,
  });
  const exactResult = draftForecast?.result;
  const forecastNow = draftForecast
    ? draftForecast.pending || draftForecast.invalid ? { kind: 'loading' as const }
      : exactResult?.forecast ? { kind: 'data' as const, refreshFailed: draftForecast.error,
        forecast: { ...exactResult.forecast,
          now_eta: exactResult.coverage_complete ? exactResult.order_ready_at : null,
          after_eta: exactResult.coverage_complete ? exactResult.forecast.after_eta : null } }
        : { kind: 'error' as const }
    : legacyForecast;

  function sectionBody(value: OrderSection) {
    if (!order) return null;
    switch (value) {
      case 'plan':
        return (
          <div className="space-y-4">
          {actions.resources && <OrderPrinterResources orderId={order.id}
            editable={order.status === 'active' && hasPermission('orders:update')}
            prepareChange={async () => (await preparePlanAction.current?.()) ?? false}
            onChanged={() => { setFitDraft(null); setPlanActionRevision(value => value + 1); }}
            onConfigure={() => { void (async () => {
              if (!await preparePlanAction.current?.()) return;
              actions.resources?.({ orderId: order.id, onChanged: () => {
                setFitDraft(null); setPlanActionRevision(value => value + 1);
              } });
            })(); }} />}
          {order.status === 'active' && draftForecast && (
            <OrderForecastPanel
              view={forecastNow}
              draft={draftForecast}
              remaining={order.figures.remaining}
              onRetry={() => forecast.refetch()}
              headingLevel={embedded ? 3 : 2}
            />
          )}
          <PlanBlock
            key={`${order.id}-${planActionRevision}`}
            onPreparePlanAction={registerPlanPreparation}
            order={order}
            fitDraft={fitDraft?.orderId === id ? fitDraft : null}
            onPlateSelection={rememberPlates}
            onPlateFit={canFit && actions.plateFit ? openFit : undefined}
            // The plan files new work under the order — Fф, not only `orders:update` (WS-13 E13 O21).
            canEdit={canFileFuturePrint(hasPermission)}
            onDraftChanged={setPlanDraftChanged}
            onForecast={setDraftForecast}
            visible={section === 'plan'}
            queueRequest={queueRequest?.orderId === id ? queueRequest : null}
            queueOrderReady={!orderFetching && !isError && orderUpdatedAt >= (queueRequest?.requestedAt ?? 0)}
            onQueueRequestRetry={() => reviewQueuePlan(queueRequest?.lineId)}
            onQueueRequestClose={() => setQueueRequest(null)}
            onEnqueued={onPlanSent}
          />
          </div>
        );
      case 'prints':
        return <OrderPrints order={order} canEdit={canEdit} />;
      case 'procurement':
        return <ProcurementChecklist order={order} canEdit={canEdit} />;
      case 'issues':
        return <DispatchNotesSection projectId={order.id} canEdit={canEdit} inTab />;
      case 'notes':
        return <OrderNotes order={order} canEdit={canEdit} />;
      case 'files':
        return <OrderAttachments order={order} canEdit={canEdit} />;
    }
  }

  // Zones in reading order (WS-13 E3 B01): head (title, facts, actions, the stage
  // row) → banners → the grid of ONE main panel and the side column. The grid's
  // columns are the named container's call (`.order-view*` in index.css), not
  // the window's.
  return (
    // The page's own padding is the view's (the mockup's #app 16); inside the
    // workspace the list page has one already (H01).
    <div data-testid="order-view" className={embedded ? 'order-view' : 'order-view p-4'}>
      {crumbs}
      <div data-testid="order-head" className="border-b border-bambu-dark-tertiary pb-3 mb-4">
          <OrderHeader
            order={order}
            actions={actions}
            extra={extra}
            fulfilment={
              fulfilment.data &&
              (fulfilment.data.can_issue > 0 || fulfilment.data.can_receive > 0 || fulfilment.data.can_assemble > 0)
                ? { primary: order.stage === 'qc' }
                : undefined
            }
            embedded={embedded}
            openHref={`/projects/${order.id}${sectionParam(section) ? `?section=${sectionParam(section)}` : ''}`}
          />

      <OrderStageStepper order={order} canEdit={canEdit} />
      </div>

      <div data-testid="order-banners" className="space-y-4 [&:not(:empty)]:mb-4">
        {canEdit && (
          <CloseSuggestionBanner
            order={order}
            state={fulfilment.data}
            // A banner door: «Mark completed» / «Close to stock» ask to close, the rest do not (E6 B04).
            onFulfil={(mode, complete) => actions.run('fulfil', ref, { ...extra, mode, complete })}
          />
        )}
        {canEdit && order.status === 'active' && <TakeStockBanner orderId={order.id} lines={order.lines} />}
      </div>

      <div data-testid="order-grid" className="order-view-grid">
        <WorkshopPanel data-testid="order-main" className="min-w-0">
          <div className="space-y-6">
            <OrderAutoEject key={order.id} order={order} canEdit={canEdit} />
            <OrderFigures figures={order.figures} forecast={forecastNow} />

            <OrderLinesTable order={order} canEdit={canEdit} headingLevel={embedded ? 3 : 2}
              onRecount={canEdit && (['orders:read', 'products:read', 'stock:read', 'stock:adjust'] as const).every(p => hasPermission(p)) && actions.recount
                ? line => { void (async () => {
                  if (!await preparePlanAction.current?.()) return;
                  actions.recount?.({ orderId: order.id, lineId: line.id, product: line.product_name, onChanged: () => {
                    setFitDraft(null); setDraftForecast(undefined); setPlanActionRevision(value => value + 1);
                  } });
                })(); } : undefined}
              onQueuePlan={order.status === 'active' && canFileFuturePrint(hasPermission) && hasPermission('queue:create')
                ? reviewQueuePlan : undefined} />

            <div className="!mt-5">
              <WorkshopTabs
                idBase={tabsId}
                ariaLabel={t('orders.detail.tabsLabel')}
                value={section}
                items={ORDER_SECTIONS.map((value) => ({
                  value,
                  label: t(`orders.detail.tabs.${value}`),
                  count:
                    value === 'plan'
                      ? planForCount.data
                        ? planForCount.data.totals.rows
                        : planForCount.isFetching
                          ? null
                          : undefined
                      : value === 'prints'
                        ? order.counts.prints
                        : value === 'issues'
                          ? order.counts.issues
                          : undefined,
                }))}
                onChange={selectSection}
                size="detail"
                panels="all"
              />
              {ORDER_SECTIONS.map((value) => (
                <WorkshopTabPanel key={value} idBase={tabsId} value={value} hidden={value !== section} className="pt-4">
                  {visited.has(value) && sectionBody(value)}
                </WorkshopTabPanel>
              ))}
            </div>
          </div>
        </WorkshopPanel>

        {/* The forecast stays above the plan, before the table on narrow screens. */}
        <div data-testid="order-side" className="order-view-side">
          <OrderFilamentPanel orderId={order.id} active={order.status === 'active'} headingLevel={embedded ? 3 : 2}
            draft={planDraftChanged ? draftForecast : undefined} />
          <OrderQueue orderId={order.id} headingLevel={embedded ? 3 : 2} />
          <OrderTimeline orderId={order.id} headingLevel={embedded ? 3 : 2} />
        </div>
      </div>

    </div>
  );
}
