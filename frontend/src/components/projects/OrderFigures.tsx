import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { OrderForecastDetail, ProjectFigures } from '../../api/client';
import { formatMoney } from '../../utils/currency';
import { etaShort, hoursMinutes } from '../../utils/forecast';
import { ProgressBar } from './ProgressBar';
import { ForecastHint } from './ForecastHint';

/** One figure, as the server counted it — this component never adds anything up. */
function Tile({ label, value, detail }: { label: ReactNode; value: string | number; detail?: ReactNode }) {
  return (
    <div className="rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary p-3">
      <p className="text-xs text-bambu-gray">{label}</p>
      <p className="text-lg font-semibold text-white tabular-nums">{value}</p>
      {detail && <p className="mt-1 text-xs text-bambu-gray tabular-nums">{detail}</p>}
    </div>
  );
}

/**
 * The order's counts, exactly as `GET /projects/{id}` sent them.
 *
 * Every number here is displayed, never derived (design decision 8) — the one
 * bit of arithmetic is formatting. Three copies of "printed" disagreeing with
 * each other is what this rule exists to prevent.
 */
export function OrderFigures({ figures, forecast }: { figures: ProjectFigures; forecast?: OrderForecastDetail | null }) {
  const { t } = useTranslation();
  // The app-wide currency, fetched the way every other money-showing screen
  // fetches it; `formatMoney` covers the unresolved first paint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });
  // A current server gives both actual components. The fallback keeps an
  // in-flight upgrade honest: older servers only gave the combined total.
  const filamentCost = figures.total_filament_cost ?? figures.total_cost;
  const energyCost = figures.total_energy_cost ?? 0;

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
        <Tile label={t('orders.figures.printing')} value={figures.prints_in_progress} />
        <Tile label={t('orders.figures.queued')} value={figures.prints_queued} />
        <Tile label={t('orders.figures.time')} value={hoursMinutes(figures.total_time_seconds)} />
        <Tile label={t('orders.figures.grams')} value={figures.total_filament_grams.toFixed(1)} />
        <Tile
          label={t('orders.figures.cost')}
          value={formatMoney(figures.total_cost, settings?.currency)}
          detail={
            <span data-testid="order-cost-breakdown">
              {t('orders.figures.filamentCost')}: {formatMoney(filamentCost, settings?.currency)}
              {' + '}
              {t('orders.figures.energyCost')}: {formatMoney(energyCost, settings?.currency)}
            </span>
          }
        />
        <Tile label={t('orders.figures.defective')} value={figures.defective} />
        <Tile
          label={<>{t('orders.figures.readyAt')} {forecast && <ForecastHint forecast={forecast} />}</>}
          value={forecast ? (forecast.now_eta ? etaShort(forecast.now_eta, settings?.time_format) : t('farmForecast.unavailable')) : '…'}
        />
        <Tile label={t('orders.figures.machineHours')} value={forecast ? hoursMinutes(forecast.machine_seconds) : '…'} />
      </div>

      {forecast && forecast.after_eta && forecast.after_eta !== forecast.now_eta && (
        <p className="text-xs text-bambu-gray">{t('orders.figures.afterAhead', { count: forecast.ahead_count, when: etaShort(forecast.after_eta, settings?.time_format) })}</p>
      )}
      {forecast && forecast.unknown_prints > 0 && <p className="text-xs text-amber-300">{t('orders.figures.unknownPrints', { count: forecast.unknown_prints })}</p>}
      {forecast && forecast.unroutable_prints > 0 && <p className="text-xs text-amber-300">{t('orders.figures.unroutablePrints', { count: forecast.unroutable_prints })}</p>}

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
