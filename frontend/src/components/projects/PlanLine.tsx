import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Shuffle } from 'lucide-react';
import { api } from '../../api/client';
import type { LineForecast, LinePlan, Order, PlanRow as PlanRowData, PlateRecipe } from '../../api/client';
import { etaShort } from '../../utils/forecast';
import { formatDuration } from '../../utils/date';
import { PlanRow } from './PlanRow';
import { PlanUnsatisfiable } from './PlanUnsatisfiable';
import { PartCounts } from './PartCounts';
import { projectPlan, type YieldByPlate } from './planMath';
import { lineConfigLabel } from './lineConfigLabel';
import { Select } from '../Select';
import { useUiPreferences } from '../../hooks/useUiPreferences';

interface PlanLineProps {
  order: Order;
  /** The line's plan with the manually added plates already merged into `rows`. */
  line: LinePlan;
  /** The plate ids of the rows the operator added by hand — they have no planned
   *  count to differ from (WS-13 E4 E06). */
  addedIds: Set<number>;
  /** The farm's own read on this line — when it will be ready, and how it
   *  would split a row with alternatives across the machines. Undefined while
   *  the forecast is unresolved, or when the order is closed. */
  forecast?: LineForecast;
  counts: Record<number, number>;
  /** `row plate_id → the plate that row is set to print`, when the operator has
   *  switched a row to one of its alternatives. */
  chosen: Record<number, number>;
  /** `row plate_id → plate_id → prints`, for the rows the operator has split
   *  across the files the same part is sliced for. */
  split: Record<number, Record<number, number>>;
  currency: string | null | undefined;
  showCost: boolean;
  canQueue: boolean;
  canPrint: boolean;
  busy: boolean;
  /** The operator may rewrite router rows (`orders:update` + `queue:update_all`). */
  canRebalance: boolean;
  onRebalance: () => void;
  /** Filament price per gram, recovered from a costed row of the plan, so a
   *  manually added plate is priced the same way the planned ones were. */
  ratePerGram: number | null;
  onCount: (plateId: number, next: number) => void;
  onChoose: (rowPlateId: number, plateId: number) => void;
  onSplit: (rowPlateId: number, next: Record<number, number>) => void;
  onAddPlate: (row: PlanRowData) => void;
  onEnqueueRow: (rowPlateId: number) => void;
  onQueued: () => void;
  /** The block's own variant, handed to its rows. */
  variant?: 'page' | 'dialog';
}

/** A plate the operator picked by hand, in the shape the plan speaks.
 *
 *  `useful` is empty on purpose: the greedy did not choose this plate, so it
 *  covers nothing "usefully" by the engine's reckoning — the surplus
 *  projection reads the plate's real yield either way. */
function rowFromRecipe(plate: PlateRecipe, ratePerGram: number | null): PlanRowData {
  return {
    plate_id: plate.id,
    library_file_id: plate.library_file_id,
    plate_index: plate.plate_index,
    // A file the caller may not open keeps no name and is marked, exactly as the
    // server marks a planned row — the row labels it and offers nothing that needs
    // the file (WS-13 E1 CL2 / CL6).
    filename: plate.filename,
    hidden: plate.hidden,
    count: 1,
    useful: [],
    print_time_seconds: plate.print_time_seconds,
    filament_used_grams: plate.filament_used_grams,
    cost:
      ratePerGram != null && plate.filament_used_grams != null
        ? Math.round(plate.filament_used_grams * ratePerGram * 100) / 100
        : null,
    time_unknown: plate.print_time_seconds == null,
    // ⚠️ A hand-added plate carries neither. The plate LIST does not say what a
    // file was sliced for and the engine is the only thing that groups plates
    // by their counted yield, so inventing either here would be a guess dressed
    // as an answer: the row prints the plate the operator asked for, and asks
    // nothing about the others.
    printer_model: null,
    alternatives: [],
  };
}

/**
 * One order line's slice of the plan: what is still outstanding, the plates
 * that would cover it, what no plate covers, and what would be left over.
 *
 * ⚠️ **The per-print yields come from the product's plate recipes, not from
 * the plan.** `PlanRow.useful` is clipped to what was outstanding at the pick
 * and aggregated over the row's prints, so it cannot answer "what does one
 * more print of this plate make" — see `planMath.ts`. The recipes query is
 * `['product-plates', id]`, the same key the product page fills, so this is
 * usually a cache read. While it is in flight the surplus shown is the
 * server's `surplus_after`, never a guess.
 *
 * ⚠️ **A yield is restricted to the parts this line COUNTS** (`qty_per_unit >
 * 0`), which is what the engine's `line_yield` does. A shared plate carrying
 * another product's parts would otherwise report them as surplus for a line
 * that never asked for them.
 */
export function PlanLine({
  order,
  line,
  addedIds,
  forecast,
  counts,
  chosen,
  split,
  currency,
  showCost,
  canQueue,
  canPrint,
  busy,
  canRebalance,
  onRebalance,
  ratePerGram,
  onCount,
  onChoose,
  onSplit,
  onAddPlate,
  onEnqueueRow,
  onQueued,
  variant = 'page',
}: PlanLineProps) {
  const { t } = useTranslation();

  const { data: plates } = useQuery({
    queryKey: ['product-plates', line.product_id],
    queryFn: () => api.getProductPlates(line.product_id),
  });

  // The user's own time format, the way every other ETA-showing screen reads
  // it; `etaShort` covers the unresolved first paint with its own default.
  const { data: settings } = useUiPreferences();

  const counted = useMemo(() => {
    const source = order.lines.find((l) => l.id === line.line_id);
    if (!source) return null;
    return new Set(source.parts.filter((p) => p.qty_per_unit > 0).map((p) => p.part_id));
  }, [order.lines, line.line_id]);

  // ⚠️ No line, no yields — NOT an unrestricted one. When the line has gone
  // from the order between the two reads there is nothing to say which parts it
  // counts, and a plate's full yield would report another product's parts as
  // this line's surplus. An empty map makes `projectPlan` answer
  // `surplusAfter: null`, and the server's own `surplus_after` is shown.
  const yields = useMemo(() => {
    const out: YieldByPlate = {};
    if (counted === null) return out;
    for (const plate of plates ?? []) {
      out[plate.id] = plate.yield.filter((y) => counted.has(y.part_id));
    }
    return out;
  }, [plates, counted]);

  const projected = projectPlan(line, counts, yields, chosen, split);
  const surplus = projected.surplusAfter ?? line.surplus_after;
  const orderLine = order.lines.find((l) => l.id === line.line_id);
  const configLabel = orderLine ? lineConfigLabel(orderLine.configuration, orderLine.mode, t) : '';

  // ⚠️ A row's ALTERNATIVES are as planned as the row itself — the row already
  // offers each of them as a file switch, so offering one here too would put
  // the same work on screen twice and count it twice in every total.
  const planned = new Set(line.rows.flatMap((r) => [r.plate_id, ...r.alternatives.map((a) => a.plate_id)]));
  const addable = (plates ?? []).filter((p) => line.candidates.includes(p.id) && !planned.has(p.id));
  // Named or not shown. A bare `#42` is a database id on an operator's screen
  // — it names nothing they can act on, and while the recipes are in flight it
  // would flash up and then be replaced by the real filename.
  const hiddenLabel = t('products.plates.hiddenFile');
  const plateName = (plate: PlateRecipe) => plate.filename ?? hiddenLabel;
  const notSliced = line.not_sliced
    .map((id) => plates?.find((p) => p.id === id))
    .filter((plate): plate is PlateRecipe => plate != null)
    .map(plateName);

  const head = 'px-2.5 py-2 text-left font-normal whitespace-nowrap';
  // ⚠️ Gated on EITHER permission, because adding a row writes nothing: it is a
  // client-side what-if that puts a plate on screen at count 1. Somebody who holds
  // only `printers:control` reaches "to printer…" through this menu.
  const showAdd = (canQueue || canPrint) && addable.length > 0;
  // With a no-plate block the not-sliced files are said inside it (E10).
  const slicedInFooter = line.unsatisfiable.length === 0 && notSliced.length > 0;
  const hasFooter = surplus.length > 0 || slicedInFooter || showAdd;

  return (
    <div
      className="rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3.5"
      data-testid={`plan-line-${line.line_id}`}
    >
      <div className="flex items-start justify-between gap-3 flex-wrap" data-testid={`plan-line-${line.line_id}-head`}>
        <div className="min-w-0">
          <p className="text-white">
            <span className="font-semibold">{line.product_id > 0 && !line.rows.some(row => row.issue === 'line_unavailable')
              ? line.product_name : t('workingPlan.unavailableLine')}</span>
            {configLabel && <small className="ml-1.5 text-xs text-bambu-gray">{configLabel}</small>}
          </p>
          {line.outstanding_before.length > 0 && (
            <small className="block mt-0.5 text-xs text-bambu-gray">
              {t('orders.plan.outstanding')}{' '}
              <PartCounts parts={line.outstanding_before} format={(o) => `${o.name} × ${o.count}`} separator=" · " />
            </small>
          )}
          {forecast?.eta_complete && forecast.now_eta && (
            <small className="block text-xs text-bambu-gray" data-testid={`plan-line-${line.line_id}-ready`}>
              {t('orders.plan.readyAt', { when: etaShort(forecast.now_eta, settings?.time_format) })}
            </small>
          )}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {line.material && (
            <span className="text-xs px-2.5 py-1 rounded-full border border-bambu-dark-tertiary bg-bambu-dark text-bambu-gray-light">
              {line.material}
            </span>
          )}
          {projected.seconds != null && projected.seconds > 0 && (
            <small className="text-xs text-bambu-gray-light" data-testid={`plan-line-${line.line_id}-machine`}>
              {t('orders.plan.machineTime', { time: formatDuration(projected.seconds) })}
            </small>
          )}
          {canRebalance && (line.pending_auto_prints ?? 0) > 0 && (
            <button
              type="button"
              onClick={onRebalance}
              disabled={busy}
              data-testid={`plan-line-${line.line_id}-rebalance`}
              title={t('orders.plan.rebalance.title')}
              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-lg text-bambu-gray-light hover:text-white hover:bg-bambu-dark-tertiary disabled:opacity-40 whitespace-nowrap"
            >
              <Shuffle className="w-3.5 h-3.5" />
              {t('orders.plan.rebalance.button')}
            </button>
          )}
        </div>
      </div>

      {line.rows.length > 0 && (
        // `relative`: the sr-only «Actions» header is positioned inside this scroll, not
        // outside it, where it would widen the page.
        <div className="relative mt-2.5 overflow-x-auto" data-testid={`plan-line-${line.line_id}-scroll`}>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-bambu-gray">
                <th scope="col" className={head}>
                  {t('orders.plan.col.plate')}
                </th>
                <th scope="col" className={head}>
                  {t('orders.plan.col.covers')}
                </th>
                <th scope="col" className={head}>
                  {t('orders.plan.col.prints')}
                </th>
                <th scope="col" className={head}>
                  {t('orders.plan.col.timeFilament')}
                </th>
                {/* No width of its own: with room the buttons stand in one row (the
                    mockup's), in a narrow table the cell wraps them — never their text. */}
                <th scope="col" className={head}>
                  <span className="sr-only">{t('orders.plan.col.actions')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {line.rows.map((row) => {
                // ⚠️ ONE expression, computed once. The count the row SHOWS and
                // the count its button SENDS have to be the same number, and
                // two copies of the arithmetic are two chances for them not to
                // be.
                const count = Math.max(0, Math.trunc(counts[row.plate_id] ?? row.count));
                return (
                  <PlanRow
                    key={row.plate_id}
                    order={order}
                    lineId={line.line_id}
                    row={row}
                    count={count}
                    planned={addedIds.has(row.plate_id) ? null : row.count}
                    chosen={chosen[row.plate_id]}
                    split={split[row.plate_id]}
                    proposal={forecast?.rows.find((r) => r.plate_id === row.plate_id)?.proposed_split ?? null}
                    currency={currency}
                    showCost={showCost}
                    canQueue={canQueue}
                    canPrint={canPrint}
                    busy={busy}
                    onCount={(next) => onCount(row.plate_id, next)}
                    onChoose={(plateId) => onChoose(row.plate_id, plateId)}
                    onSplit={(next) => onSplit(row.plate_id, next)}
                    onEnqueue={() => onEnqueueRow(row.plate_id)}
                    onQueued={onQueued}
                    variant={variant}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* E10: what no plate makes, under the table — with the files that are linked
          but not sliced beside the link, since that is usually why. */}
      {line.unsatisfiable.map((part) => (
        <PlanUnsatisfiable
          key={part.part_id}
          lineId={line.line_id}
          productId={line.product_id}
          material={line.material}
          part={part}
          notSliced={notSliced}
        />
      ))}

      {hasFooter && (
      <div className="mt-2.5 flex items-center justify-between gap-3 flex-wrap">
        <div className="space-y-1 min-w-0">
          {surplus.length > 0 && (
            <p className="text-xs text-amber-700 dark:text-amber-400" data-testid={`plan-line-${line.line_id}-surplus`}>
              {t('orders.plan.surplusAfter')}{' '}
              <PartCounts parts={surplus} format={(s) => `${s.name} +${s.count}`} separator=" · " />
            </p>
          )}
          {slicedInFooter && (
            <p className="text-xs text-bambu-gray">{`${t('orders.plan.notSliced')}: ${notSliced.join(' · ')}`}</p>
          )}
        </div>

        {showAdd && (
          <Select
            size="sm"
            tone="muted"
            data-testid={`plan-line-${line.line_id}-add`}
            value=""
            aria-label={t('orders.plan.addPlate')}
            onChange={(e) => {
              const plate = addable.find((p) => p.id === Number(e.currentTarget.value));
              if (plate) onAddPlate(rowFromRecipe(plate, ratePerGram));
            }}
          >
            <option value="">{t('orders.plan.addPlate')}</option>
            {addable.map((plate) => (
              <option key={plate.id} value={plate.id}>
                {plate.plate_index === 0
                  ? plateName(plate)
                  : `${plateName(plate)} · ${t('orders.plan.row.plate', { n: plate.plate_index })}`}
              </option>
            ))}
          </Select>
        )}
      </div>
      )}
    </div>
  );
}
