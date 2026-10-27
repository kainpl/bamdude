import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import type { ProjectFigures } from '../../api/client';
import { formatMoney } from '../../utils/currency';
import { formatDateOnly } from '../../utils/date';
import { hoursMinutes } from '../../utils/forecast';
import { formatWeight } from '../../utils/weight';
import { ProgressBar } from './ProgressBar';
import type { ForecastView } from './orderForecastView';
import { useUiPreferences } from '../../hooks/useUiPreferences';

type Tone = 'ok' | 'warn' | 'late';

const TONE_CLASS: Record<Tone, string> = {
  ok: 'text-bambu-green',
  warn: 'text-amber-700 dark:text-amber-400',
  late: 'text-red-600 dark:text-red-500',
};

/** One figure, as the server counted it — this component never adds anything up. */
function Tile({ name, label, children }: { name: string; label: ReactNode; children: ReactNode }) {
  return (
    <div data-testid={`order-tile-${name}`} className="rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3">
      <span className="block text-xs text-bambu-gray">{label}</span>
      {children}
    </div>
  );
}

function Value({ tone, className = 'text-lg leading-7', children }: { tone?: Tone; className?: string; children: ReactNode }) {
  return (
    <b data-tone={tone} className={`block font-semibold tabular-nums ${tone ? TONE_CLASS[tone] : 'text-white'} ${className}`}>
      {children}
    </b>
  );
}

/**
 * The order's summary (WS-13 E3 §E): eight tiles in the mockup's order, the
 * coverage bar, and one line of the other figures the old twelve tiles carried
 * (E07) — time, filament, what can be assembled, other prints, what is held and
 * issued, and what the cost is made of.
 *
 * Every number is displayed, never derived (design decision 8) — the one bit
 * of arithmetic is the known lower bound of a cost whose purchase price is
 * partly unknown, both halves of which the server sends for exactly that (E1 PR5).
 *
 * ⚠️ **«Ready ≈» answers by COVERAGE, not by stage** (E01): the stage is set by
 * hand, and a manual «quality check» with prints still to make has a date.
 */
export function OrderFigures({ figures, forecast }: { figures: ProjectFigures; forecast: ForecastView }) {
  const { t } = useTranslation();
  // The app-wide currency and date format, fetched the way every other screen
  // fetches them; the formatters cover the unresolved first paint.
  const { data: settings } = useUiPreferences();
  const money = (value: number) => formatMoney(value, settings?.currency);

  return (
    <section>
      <div className="mb-3 grid gap-3 grid-cols-[repeat(auto-fill,minmax(130px,1fr))]">
        <Tile name="ordered" label={t('orders.figures.ordered')}>
          <Value>{figures.ordered}</Value>
        </Tile>
        <Tile name="printed" label={t('orders.figures.printed')}>
          <Value>{figures.printed}</Value>
        </Tile>
        {/* Always, and at zero too (spec D04): the tile's place in the row is
            part of the summary's shape, and 0 from stock is a fact. */}
        <Tile name="from-stock" label={t('stock.figures.fromStock')}>
          <Value>{figures.from_stock_units}</Value>
        </Tile>
        <Tile name="remaining" label={t('orders.figures.remaining')}>
          <Value tone={figures.remaining === 0 ? 'ok' : undefined}>{figures.remaining}</Value>
        </Tile>
        <Tile name="printing" label={t('orders.figures.printingQueued')}>
          <Value>
            {figures.prints_in_progress}
            <span className="text-sm font-normal text-bambu-gray"> / {figures.prints_queued}</span>
          </Value>
        </Tile>
        <Tile name="ready" label={t('orders.figures.readyAt')}>
          <ReadyValue figures={figures} forecast={forecast} dateFormat={settings?.date_format} />
        </Tile>
        <Tile name="cost" label={t('orders.figures.cost')}>
          <CostValue figures={figures} money={money} />
        </Tile>
        <Tile name="defects" label={t('orders.figures.defective')}>
          <Value tone={figures.defective > 0 ? 'warn' : undefined}>{figures.defective}</Value>
        </Tile>
      </div>

      {/*
        The bar lives alone in this wrapper so the stray-zero detector can be
        scoped to it: the tiles above legitimately print "0" as labelled
        numbers, while a HIDDEN bar must leave nothing behind at all.
      */}
      <div data-testid="order-progress-area">
        <ProgressBar
          value={figures.covered_units}
          max={figures.ordered}
          progress={figures.progress}
          caption="percent"
          label={`${t('orders.figures.coverage')}: ${
            figures.from_stock_units > 0
              ? t('orders.figures.coverageSources', { printed: figures.printed, stock: figures.from_stock_units })
              : t('orders.figures.coveragePrinted', { printed: figures.printed })
          }`}
          testId="order-progress"
        />
      </div>

      <StatsLine figures={figures} money={money} />
    </section>
  );
}

function ReadyValue({
  figures,
  forecast,
  dateFormat,
}: {
  figures: ProjectFigures;
  forecast: ForecastView;
  dateFormat: Parameters<typeof formatDateOnly>[2];
}) {
  const { t } = useTranslation();
  if (forecast.kind === 'closed') return <Value className="text-base leading-7">—</Value>;
  if (figures.remaining === 0) {
    return (
      <Value tone="ok" className="text-base leading-7">
        <CheckCircle2 className="h-5 w-5" aria-hidden />
        <span className="sr-only">{t('orders.figures.readyCovered')}</span>
      </Value>
    );
  }
  if (forecast.kind === 'draft') return <Value className="text-base leading-7">{t('orders.figures.forecastStale')}</Value>;
  if (forecast.kind === 'loading') {
    return (
      <Value className="text-base leading-7">
        …<span className="sr-only">{t('common.loading')}</span>
      </Value>
    );
  }
  if (forecast.kind === 'error') return <Value className="text-base leading-7">—</Value>;
  const { now_eta: eta, late, incomplete_reasons: reasons } = forecast.forecast;
  return (
    <Value tone={eta && late ? 'late' : undefined} className="text-base leading-7">
      {eta ? formatDateOnly(eta, { day: 'numeric', month: 'short' }, dateFormat) : '—'}
      {eta && late && <span className="sr-only"> ({t('orders.forecast.late')})</span>}
      {reasons.length > 0 && (
        <AlertTriangle
          role="img"
          aria-label={t('orders.figures.readyIncomplete')}
          className="ml-1.5 inline h-3.5 w-3.5 align-[-1px] text-amber-600 dark:text-amber-400"
        />
      )}
    </Value>
  );
}

function CostValue({ figures, money }: { figures: ProjectFigures; money: (value: number) => string }) {
  const { t } = useTranslation();
  if (figures.cost_with_procurement != null) return <Value>{money(figures.cost_with_procurement)}</Value>;
  // One bought part has no price (Z8): the known part is a LOWER bound, and with
  // no known part at all there is nothing honest to write but a dash.
  const known = figures.total_cost + figures.procurement_known_cost;
  const why = t('orders.header.marginUnknown');
  return (
    <Value>
      <span title={why}>{known > 0 ? `≥ ${money(known)}` : '—'}</span>
      <span className="sr-only"> ({why})</span>
    </Value>
  );
}

function StatsLine({ figures, money }: { figures: ProjectFigures; money: (value: number) => string }) {
  const { t } = useTranslation();
  const parts: ReactNode[] = [
    t('orders.figures.stats.time', { time: hoursMinutes(figures.total_time_seconds) }),
    t('orders.figures.stats.filament', { weight: formatWeight(figures.total_filament_grams) }),
  ];
  if (figures.complete > 0) parts.push(t('orders.figures.stats.canAssemble', { count: figures.complete }));
  if (figures.other_prints_count > 0) parts.push(t('orders.figures.stats.otherPrints', { count: figures.other_prints_count }));
  if (figures.held_units > 0 || figures.issued_units > 0) {
    parts.push(
      t('orders.figures.stats.heldIssued', { held: figures.held_units, issued: figures.issued_units, ordered: figures.ordered }),
    );
  }

  // What the cost is made of (R07): every non-zero component, the purchases too
  // when nothing was printed — an order of bought parts must not lose its only cost.
  const filament = figures.total_filament_cost ?? figures.total_cost;
  const energy = figures.total_energy_cost ?? 0;
  const components: string[] = [];
  if (filament > 0) components.push(t('orders.figures.stats.costFilament', { amount: money(filament) }));
  // An estimate is inside the electricity figure and said so; an unknown one is outside it and
  // counted, never a zero (spec plug-meter-watchdog W11).
  if (energy > 0) {
    components.push(
      (figures.energy_estimated ?? 0) > 0
        ? t('orders.figures.stats.costEnergyEstimated', { amount: money(energy) })
        : t('orders.figures.stats.costEnergy', { amount: money(energy) }),
    );
  }
  if (figures.procurement_partial) {
    components.push(
      figures.procurement_known_cost > 0
        ? t('orders.figures.stats.costPurchasesAtLeast', { amount: money(figures.procurement_known_cost) })
        : t('orders.figures.stats.costPurchasesUnknown'),
    );
  } else if (figures.procurement_cost != null && figures.procurement_cost > 0) {
    components.push(t('orders.figures.stats.costPurchases', { amount: money(figures.procurement_cost) }));
  }
  if (components.length > 0) {
    parts.push(
      <span key="cost" data-testid="order-cost-breakdown">
        {t('orders.figures.stats.cost', { parts: components.join(' + ') })}
      </span>,
    );
  }
  if ((figures.energy_unavailable ?? 0) > 0) {
    parts.push(
      <span key="energy-unknown" data-testid="order-energy-unknown">
        {t('orders.figures.stats.energyUnknown', { count: figures.energy_unavailable })}
      </span>,
    );
  }

  return (
    <p data-testid="order-stats" className="mt-2 text-xs leading-[18px] text-bambu-gray tabular-nums">
      {parts.map((part, index) => (
        <span key={index}>
          {index > 0 && ' · '}
          {part}
        </span>
      ))}
    </p>
  );
}
