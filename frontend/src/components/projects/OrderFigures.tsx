import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { ProjectFigures } from '../../api/client';
import { formatMoney } from '../../utils/currency';
import { ProgressBar } from './ProgressBar';

/** One figure, as the server counted it — this component never adds anything up. */
function Tile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary p-3">
      <p className="text-xs text-bambu-gray">{label}</p>
      <p className="text-lg font-semibold text-white tabular-nums">{value}</p>
    </div>
  );
}

/**
 * Total print time as `h:mm`.
 *
 * Deliberately not `formatDuration` ("2h 30m"): these tiles sit in a row of
 * right-aligned tabular numbers, and a value whose width changes with the
 * words in it breaks the column. The minutes are rounded, so a job of 90
 * seconds reads `0:02` rather than `0:01` plus a hidden remainder.
 */
function hoursMinutes(seconds: number): string {
  const minutes = Math.max(0, Math.round(seconds / 60));
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}`;
}

/**
 * The order's counts, exactly as `GET /projects/{id}` sent them.
 *
 * Every number here is displayed, never derived (design decision 8) — the one
 * bit of arithmetic is formatting. Three copies of "printed" disagreeing with
 * each other is what this rule exists to prevent.
 */
export function OrderFigures({ figures }: { figures: ProjectFigures }) {
  const { t } = useTranslation();
  // The app-wide currency, fetched the way every other money-showing screen
  // fetches it; `formatMoney` covers the unresolved first paint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });

  return (
    <section className="space-y-3">
      <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(120px,1fr))]">
        <Tile label={t('orders.figures.ordered')} value={figures.ordered} />
        <Tile label={t('orders.figures.printed')} value={figures.printed} />
        {/* Beside `printed`, and only when there is any (pass 8, Decision 5).
            `ordered` and `printed` stay literal — the customer asked for that
            many and the farm printed this many — and a permanent "0" tile on
            every order in the farm would be a column of noise. */}
        {figures.from_stock_units > 0 && (
          <Tile label={t('stock.figures.fromStock')} value={figures.from_stock_units} />
        )}
        <Tile label={t('orders.figures.complete')} value={figures.complete} />
        <Tile label={t('orders.figures.remaining')} value={figures.remaining} />
        <Tile label={t('orders.figures.time')} value={hoursMinutes(figures.total_time_seconds)} />
        <Tile label={t('orders.figures.grams')} value={figures.total_filament_grams.toFixed(1)} />
        <Tile label={t('orders.figures.cost')} value={formatMoney(figures.total_cost, settings?.currency)} />
        <Tile label={t('orders.figures.defective')} value={figures.defective} />
      </div>

      {/*
        The bar lives alone in this wrapper so the stray-zero detector can be
        scoped to it: the tiles above legitimately print "0" as labelled
        numbers, while a HIDDEN bar must leave nothing behind at all.
      */}
      <div data-testid="order-progress-area">
        <ProgressBar
          value={figures.printed}
          max={figures.ordered}
          label={t('orders.figures.progress')}
          testId="order-progress"
        />
      </div>

      {figures.other_prints_count > 0 && (
        <p className="text-xs text-bambu-gray">
          {t('orders.figures.otherPrints', { n: figures.other_prints_count })}
        </p>
      )}
    </section>
  );
}
