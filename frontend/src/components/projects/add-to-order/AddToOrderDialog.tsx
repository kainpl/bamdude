import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../../../contexts/AuthContext';
import { canReadStock, canTakeStock } from '../../../utils/workshopRights';
import { api } from '../../../api/client';
import type { BatchLine, ProductListItem } from '../../../api/client';
import { useToast } from '../../../contexts/ToastContext';
import { useStockSuggest } from '../../../hooks/useStockSuggest';
import { invalidateOrderViews } from '../../../utils/queryInvalidation';
import { Button } from '../../Button';
import { OrderChoice } from '../../pickers/OrderChoice';
import type { ChosenOrder } from '../../pickers/OrderChoice';
import { WorkshopDialog } from '../../workshop/WorkshopDialog';
import { WorkshopTabPanel, WorkshopTabs } from '../../workshop/WorkshopTabs';
import { PartsTab } from './PartsTab';
import { PlateTab } from './PlateTab';
import { ProductsTab } from './ProductsTab';
import {
  newProductPick,
  partUnits,
  partsLines,
  plateLines,
  productLines,
  productUnits,
  shortfalls,
  shownForLines,
  suggestItems,
} from './addToOrderState';
import type { PartPicks, PlateFile, PlatePick, ProductPicks, Shown } from './addToOrderState';
import { usePlatesOf } from './platesQuery';
import { useTabScroll } from './useTabScroll';
import {
  CHECKING_VIEW,
  COMBINATION_STATE_KEY,
  combinationQuery,
  combinationView,
  isCombinationRefusal,
  reasonsText,
} from '../useCombinationState';
import type { CombinationView } from '../useCombinationState';

type Tab = 'products' | 'parts' | 'plate';
const TABS: Tab[] = ['products', 'parts', 'plate'];

/** What the dialog keeps of a product a page listed: its name, and whether it has groups. */
interface SeenProduct {
  name: string;
  hasGroups: boolean;
}

/** The order the dialog adds to, as its caller knows it. */
export interface AddTarget {
  id: number;
  code: string;
  name: string;
  /** A completed or cancelled order takes nothing from stock (rule 7). */
  active: boolean;
}

/**
 * «Add to order» (spec workshop-add-to-order, rules 19–25; WS-13 E5 B / G):
 * products, parts of a product and a one-off plate, picked across searches,
 * pages and tabs, and added in ONE batch — one transaction on the server, so a
 * refused line adds nothing.
 *
 * Opened from an order (`order`) it adds to that order; opened from a product it
 * asks which ACTIVE order first, opens on that product — searched for by its
 * code, so it is on the page to edit (final review I4) — and after the batch
 * goes to the order the batch was added to (the mockup's `add-go`, E5 K4).
 * Opened from the file manager (`preselectPlate`, WS-13 E13 C02) it asks the same,
 * on «One-off from a file» with that file — and from the plate gallery its plate —
 * already picked, one copy.
 *
 * ⚠️ A tab mounts on its first visit and stays mounted, hidden: its search,
 * filters, page and sideways scroll live with its DOM, and a tab nobody opened
 * asks the server nothing. The body's vertical scroll is not the panel's — it is
 * remembered per tab (`useTabScroll`, E5 R06).
 *
 * ⚠️ While the batch is in flight nothing changes: the X, Escape and «Cancel»
 * are off and every field sits in a disabled fieldset (E5 R09) — the draft that
 * closes on success is exactly the one that was sent.
 *
 * Every picked product with variant groups is asked whether its combination can be
 * ordered (spec product-part-variant-combinations п. 25) — here, for every pick at
 * once, so a pick on another page still counts: «Add» waits for every answer and
 * stays off while one is closed, naming the products. A product without groups, or a
 * caller without the stock's read, is never asked; a product no page has listed yet
 * is waited for. The server refuses a closed combination anyway (409
 * `combination_unavailable`): the dialog says its sentence with the reasons and asks again.
 */
export function AddToOrderDialog({
  order,
  preselectProduct,
  preselectPlate,
  onClose,
}: {
  order?: AddTarget;
  preselectProduct?: { id: number; code: string };
  preselectPlate?: { file: PlateFile; plateIndex: number | null };
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const idBase = useId();
  const anchor = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState<Tab>(preselectPlate ? 'plate' : 'products');
  const [visited, setVisited] = useState<Set<Tab>>(() => new Set([preselectPlate ? 'plate' : 'products']));
  const [chosen, setChosen] = useState<ChosenOrder | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [products, setProducts] = useState<ProductPicks>(() =>
    preselectProduct ? new Map([[preselectProduct.id, newProductPick()]]) : new Map(),
  );
  const [seen, setSeen] = useState<Map<number, SeenProduct>>(() => new Map());
  const onProductsSeen = useCallback((items: ProductListItem[]) => {
    setSeen((prev) => {
      let next: Map<number, SeenProduct> | null = null;
      for (const item of items) {
        const fact = { name: item.name, hasGroups: (item.variant_groups ?? []).length > 0 };
        const old = prev.get(item.id);
        if (old && old.name === fact.name && old.hasGroups === fact.hasGroups) continue;
        next ??= new Map(prev);
        next.set(item.id, fact);
      }
      return next ?? prev;
    });
  }, []);
  const [parts, setParts] = useState<PartPicks>(() => new Map());
  const [plate, setPlate] = useState<PlatePick>(() =>
    preselectPlate ? { file: preselectPlate.file, plateIndex: preselectPlate.plateIndex, copies: 1 } : null,
  );
  // E5-V01: a chosen plate a SUCCESSFUL answer no longer has is no longer chosen — the file and
  // the copies stay, and its return chooses nothing. Settled while rendering, so no batch,
  // summary or button ever carries it; a failed re-read is not an empty answer.
  const plates = usePlatesOf(plate?.file ?? null);
  if (
    plate?.plateIndex != null &&
    plates.isSuccess &&
    !plates.data.plates.some((p) => p.index === plate.plateIndex)
  ) {
    setPlate({ ...plate, plateIndex: null });
  }

  const showTab = useCallback((next: Tab) => {
    setVisited((prev) => (prev.has(next) ? prev : new Set(prev).add(next)));
    setTab(next);
  }, []);
  const switchTo = useTabScroll(tab, showTab, anchor);
  // B06: the first focus goes where a person starts typing — the order search from a product,
  // else the open tab's search. Here, in the dialog itself: `Modal` focuses its panel in a
  // passive effect, which runs before this one (a child's effects run first).
  useEffect(() => {
    anchor.current?.querySelector<HTMLInputElement>('input[type="search"]')?.focus();
  }, []);

  const target = order ?? chosen;
  // A chosen order came from the active list; a given one says what it is.
  // …and only with the stock's move and read: without them the lines take nothing and no
  // proposal is asked (WS-13 E13 O06 — «UI шле stock: none»).
  const { hasPermission } = useAuth();
  const takesStock = (order ? order.active : true) && canTakeStock(hasPermission) && canReadStock(hasPermission);
  const items = useMemo(() => suggestItems(products), [products]);
  // Only proposals that answer what each row asks NOW reach the rows and the batch (E5 R02).
  const { current, status, refreshFailed, retry } = useStockSuggest(items, takesStock);

  // Every pick's combination — the route asks the stock's read, as the kits proposal does.
  const checksCombinations = canReadStock(hasPermission);
  const picked = [...products];
  const asks = picked.map(([id]) => checksCombinations && seen.get(id)?.hasGroups === true);
  const combinationResults = useQueries({
    queries: picked.map(([id, pick], i) => combinationQuery(id, pick.choices, asks[i])),
  });
  const combinations = new Map<number, CombinationView>(
    picked.map(([id], i) => [
      id,
      // Not listed yet: whether it has groups at all is not known — wait, never assume.
      checksCombinations && !seen.has(id) ? CHECKING_VIEW : combinationView(combinationResults[i], asks[i]),
    ]),
  );
  const closedNames = picked
    .filter(([id]) => combinations.get(id)?.gate === 'closed')
    .map(([id]) => seen.get(id)?.name ?? `#${id}`);
  const checkingCombinations = picked.some(([id]) => combinations.get(id)?.gate === 'checking');

  // Everything picked on every tab goes in ONE batch — one transaction, so a
  // refused line adds nothing (spec rule 11).
  const productBatch = productLines(products, current, takesStock);
  const partsBatch = partsLines(parts);
  // A plate shows no proposal — the server's `auto` takes what its one-off product holds, which
  // is the stock's move alone (WS-13 E13 O06).
  const plateBatch = plateLines(plate, (order ? order.active : true) && canTakeStock(hasPermission));
  const lines: BatchLine[] = [...productBatch, ...partsBatch, ...plateBatch];

  const add = useMutation({
    // The batch and `shown` ride together: what was sent and what the rows showed, both
    // taken from the render in which «Add» was pressed (C06).
    mutationFn: ({ id, lines: batch }: { id: number; lines: BatchLine[]; shown: Shown[] }) => api.addOrderLines(id, batch),
    onMutate: () => setError(null),
    onSuccess: (result, { id, shown }) => {
      // Both shelves' keys are order views (`ORDER_VIEW_KEYS`), each once.
      invalidateOrderViews(qc, { orderId: id });
      showToast(t('orders.add.added', { count: result.results.length }));
      const short = shortfalls(result, shown);
      if (short.length > 0) {
        // Each line names only the shelf that gave less.
        const detail = short
          .map((s) => {
            const what = [
              s.gotFinished < s.askedFinished &&
                t('orders.add.clampedReady', { got: s.gotFinished, asked: s.askedFinished }),
              s.gotKits < s.askedKits && t('orders.add.clampedKits', { got: s.gotKits, asked: s.askedKits }),
            ]
              .filter(Boolean)
              .join(', ');
            return t('orders.add.clampedLine', { name: s.name, what });
          })
          .join('; ');
        showToast(t('orders.add.clamped', { detail }), 'warning');
      }
      onClose();
      // From a product: the order the batch went to — the mutation's own id, not
      // whatever the field says by now (E5 R09).
      if (!order) navigate(`/projects/${id}`);
    },
    // The server's sentence in the dialog; the controls come back with every pick. A closed
    // combination is read again, so the rows say which and why.
    onError: (err: Error) => {
      setError(err);
      if (isCombinationRefusal(err)) void qc.invalidateQueries({ queryKey: [COMBINATION_STATE_KEY] });
    },
  });
  const pending = add.isPending;

  const canSubmit =
    target != null && lines.length > 0 && !pending && closedNames.length === 0 && !checkingCombinations;
  const summaries = [
    products.size > 0 && t('orders.add.summary.products', { count: products.size, units: productUnits(products) }),
    // B03: a kind after another continues the line («… · деталей: N · …»), alone it opens it.
    parts.size > 0 &&
      t(products.size > 0 ? 'orders.add.summary.partsNext' : 'orders.add.summary.parts', {
        count: parts.size,
        units: partUnits(parts),
      }),
    plate?.plateIndex != null &&
      t(products.size > 0 || parts.size > 0 ? 'orders.add.summary.plateNext' : 'orders.add.summary.plate', {
        n: plate.plateIndex,
        copies: plate.copies,
      }),
  ].filter((s): s is string => Boolean(s));
  // One kind picked names that kind, several are «lines», nothing yet follows the tab.
  const kind: Tab | 'lines' =
    summaries.length > 1
      ? 'lines'
      : productBatch.length > 0
        ? 'products'
        : partsBatch.length > 0
          ? 'parts'
          : plateBatch.length > 0
            ? 'plate'
            : tab;
  const submitLabel = pending
    ? t('orders.add.adding')
    : kind === 'parts'
      ? t('orders.add.submit.parts')
      : kind === 'plate'
        ? t('orders.add.submit.plate')
        : t('orders.add.submit.products', { count: lines.length });

  return (
    <WorkshopDialog
      onClose={onClose}
      title={t('orders.add.title')}
      subtitle={target ? `${target.code} · ${target.name}` : t('orders.add.chooseOrderSubtitle')}
      size="xl"
      pending={pending}
      error={
        error ? (
          <>
            {error.message}
            {isCombinationRefusal(error) && error.reasons && (
              <span className="block">{reasonsText(t, error.reasons)}</span>
            )}
          </>
        ) : null
      }
      summary={summaries.length > 0 ? summaries.join(' · ') : t('orders.add.nothing')}
      footer={
        <>
          {closedNames.length > 0 ? (
            <span data-testid="combination-blocked" className="text-sm text-amber-700 dark:text-amber-400">
              {t('orders.add.combinationClosed', { names: closedNames.join(', ') })}
            </span>
          ) : (
            checkingCombinations && (
              <span data-testid="combination-blocked" className="text-sm text-bambu-gray">
                {t('orders.add.combinationChecking')}
              </span>
            )
          )}
          <Button variant="secondary" onClick={onClose} disabled={pending}>
            {t('common.cancel')}
          </Button>
          <Button
            onClick={() =>
              target != null && !pending && add.mutate({ id: target.id, lines, shown: shownForLines(products, current, takesStock) })
            }
            disabled={!canSubmit}
          >
            {submitLabel}
          </Button>
        </>
      }
    >
      <div ref={anchor} className="space-y-3">
        {!order && (
          <fieldset disabled={pending} className="m-0 min-w-0 border-0 p-0">
            {/* Which ACTIVE order (spec rule 25; WS-13 E5 G02, E13 D01) — the shared choice. */}
            <OrderChoice value={chosen?.id ?? null} onChange={setChosen} label={t('orders.add.toOrder')} />
          </fieldset>
        )}
        <WorkshopTabs
          idBase={idBase}
          ariaLabel={t('orders.add.title')}
          value={tab}
          items={TABS.map((key) => ({ value: key, label: t(`orders.add.tabs.${key}`) }))}
          onChange={switchTo}
          size="detail"
          panels="all"
        />
        {!takesStock && <p className="text-sm text-bambu-gray">{t('orders.add.inactive')}</p>}
        <fieldset disabled={pending} className="m-0 min-w-0 border-0 p-0">
          {TABS.map((key) => (
            <WorkshopTabPanel key={key} idBase={idBase} value={key} hidden={tab !== key}>
              {visited.has(key) && key === 'products' && (
                <ProductsTab
                  picks={products}
                  onPicksChange={setProducts}
                  takesStock={takesStock}
                  stockOf={(id) => ({
                    suggestion: current.get(id),
                    status: status.get(id) ?? 'waiting',
                    refreshFailed: refreshFailed.has(id),
                  })}
                  onRetryStock={retry}
                  combinationOf={(id) => combinations.get(id) ?? CHECKING_VIEW}
                  onProductsSeen={onProductsSeen}
                  initialQuery={preselectProduct?.code}
                />
              )}
              {visited.has(key) && key === 'parts' && <PartsTab picks={parts} onPicksChange={setParts} />}
              {visited.has(key) && key === 'plate' && <PlateTab pick={plate} onPickChange={setPlate} />}
            </WorkshopTabPanel>
          ))}
        </fieldset>
      </div>
    </WorkshopDialog>
  );
}
