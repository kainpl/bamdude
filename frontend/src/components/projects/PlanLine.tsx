import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { LinePlan, Order, PlanRow as PlanRowData, PlateRecipe } from '../../api/client';
import { PlanRow } from './PlanRow';
import { PlanUnsatisfiable } from './PlanUnsatisfiable';
import { projectPlan, type YieldByPlate } from './planMath';

interface PlanLineProps {
  order: Order;
  /** The line's plan with the manually added plates already merged into `rows`. */
  line: LinePlan;
  counts: Record<number, number>;
  currency: string | null | undefined;
  showCost: boolean;
  canQueue: boolean;
  canPrint: boolean;
  busy: boolean;
  /** Filament price per gram, recovered from a costed row of the plan, so a
   *  manually added plate is priced the same way the planned ones were. */
  ratePerGram: number | null;
  onCount: (plateId: number, next: number) => void;
  onAddPlate: (row: PlanRowData) => void;
  onEnqueueRow: (plateId: number, count: number) => void;
  onQueued: () => void;
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
    filename: plate.filename,
    count: 1,
    useful: [],
    print_time_seconds: plate.print_time_seconds,
    filament_used_grams: plate.filament_used_grams,
    cost:
      ratePerGram != null && plate.filament_used_grams != null
        ? Math.round(plate.filament_used_grams * ratePerGram * 100) / 100
        : null,
    time_unknown: plate.print_time_seconds == null,
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
  counts,
  currency,
  showCost,
  canQueue,
  canPrint,
  busy,
  ratePerGram,
  onCount,
  onAddPlate,
  onEnqueueRow,
  onQueued,
}: PlanLineProps) {
  const { t } = useTranslation();

  const { data: plates } = useQuery({
    queryKey: ['product-plates', line.product_id],
    queryFn: () => api.getProductPlates(line.product_id),
  });

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

  const projected = projectPlan(line, counts, yields);
  const surplus = projected.surplusAfter ?? line.surplus_after;

  const planned = new Set(line.rows.map((r) => r.plate_id));
  const addable = (plates ?? []).filter((p) => line.candidates.includes(p.id) && !planned.has(p.id));
  // Named or not shown. A bare `#42` is a database id on an operator's screen
  // — it names nothing they can act on, and while the recipes are in flight it
  // would flash up and then be replaced by the real filename.
  const notSliced = line.not_sliced
    .map((id) => plates?.find((p) => p.id === id)?.filename)
    .filter((filename): filename is string => filename != null);

  // plate · covers · count · time · grams · [cost] · actions
  const columns = showCost ? 7 : 6;

  return (
    <div className="rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary" data-testid={`plan-line-${line.line_id}`}>
      <div className="flex items-center justify-between gap-3 flex-wrap p-3 border-b border-bambu-dark-tertiary">
        <div className="min-w-0">
          <p className="text-white font-medium truncate">{line.product_name}</p>
          {line.outstanding_before.length > 0 && (
            <p className="text-xs text-bambu-gray">
              {`${t('orders.plan.outstanding')} ${line.outstanding_before
                .map((o) => `${o.name} × ${o.count}`)
                .join(' · ')}`}
            </p>
          )}
        </div>
        {line.material && (
          <span className="text-xs px-2 py-0.5 rounded-full border border-bambu-dark-tertiary text-bambu-gray-light">
            {line.material}
          </span>
        )}
      </div>

      {(line.rows.length > 0 || line.unsatisfiable.length > 0) && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <tbody>
              {line.rows.map((row) => (
                <PlanRow
                  key={row.plate_id}
                  order={order}
                  lineId={line.line_id}
                  row={row}
                  count={Math.max(0, Math.trunc(counts[row.plate_id] ?? row.count))}
                  currency={currency}
                  showCost={showCost}
                  canQueue={canQueue}
                  canPrint={canPrint}
                  busy={busy}
                  onCount={(next) => onCount(row.plate_id, next)}
                  onEnqueue={() =>
                    onEnqueueRow(row.plate_id, Math.max(0, Math.trunc(counts[row.plate_id] ?? row.count)))
                  }
                  onQueued={onQueued}
                />
              ))}
              {line.unsatisfiable.map((part) => (
                <PlanUnsatisfiable
                  key={part.part_id}
                  productId={line.product_id}
                  material={line.material}
                  part={part}
                  colSpan={columns}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex items-center justify-between gap-3 flex-wrap p-3 border-t border-bambu-dark-tertiary">
        <div className="space-y-1 min-w-0">
          {surplus.length > 0 && (
            <p className="text-xs text-amber-300" data-testid={`plan-line-${line.line_id}-surplus`}>
              {`${t('orders.plan.surplusAfter')} ${surplus.map((s) => `${s.name} +${s.count}`).join(' · ')}`}
            </p>
          )}
          {notSliced.length > 0 && (
            <p className="text-xs text-bambu-gray">{`${t('orders.plan.notSliced')}: ${notSliced.join(' · ')}`}</p>
          )}
        </div>

        {canQueue && addable.length > 0 && (
          <select
            data-testid={`plan-line-${line.line_id}-add`}
            value=""
            aria-label={t('orders.plan.addPlate')}
            onChange={(e) => {
              const plate = addable.find((p) => p.id === Number(e.currentTarget.value));
              if (plate) onAddPlate(rowFromRecipe(plate, ratePerGram));
            }}
            className="px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded text-bambu-gray-light focus:border-bambu-green focus:outline-none"
          >
            <option value="">{t('orders.plan.addPlate')}</option>
            {addable.map((plate) => (
              <option key={plate.id} value={plate.id}>
                {plate.plate_index === 0
                  ? plate.filename
                  : `${plate.filename} · ${t('orders.plan.row.plate', { n: plate.plate_index })}`}
              </option>
            ))}
          </select>
        )}
      </div>
    </div>
  );
}
