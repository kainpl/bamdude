import { useTranslation } from 'react-i18next';
import type { FarmNeeds } from '../../api/client';
import { formatWeight } from '../../utils/weight';
import { ForecastHint } from './ForecastHint';
import { needTestId } from './filamentNeedsHelpers';

/** One chip per (material, colour) every active order still needs, over the orders list. */
export function FilamentStrip({ farm }: { farm: FarmNeeds }) {
  const { t } = useTranslation();
  if (farm.rows.length === 0) return null;
  const anyShort = farm.rows.some((r) => r.short_g != null && r.short_g > 0);
  if (!anyShort && !farm.stock_unavailable) {
    return <p className="text-xs text-bambu-gray" data-testid="filament-strip-covered">{t('orders.filament.allCovered')} <ForecastHint forecast={farm} /></p>;
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="filament-strip">
      <span className="text-bambu-gray">{t('orders.filament.title')} <ForecastHint forecast={farm} /></span>
      {farm.rows.map((row) => {
        const short = row.short_g != null && row.short_g > 0;
        const id = needTestId(row.material, row.colour, 'filament-strip-');
        return (
          <span key={id} data-testid={id} data-short={String(short)} title={t('orders.filament.ordersCount', { count: row.orders_count })}
                className={`rounded-full border px-2 py-0.5 tabular-nums ${short ? 'border-amber-400 text-amber-300' : 'border-bambu-dark-tertiary text-white'}`}>
            {row.colour ? `${row.material} · ${row.colour}` : row.material} {formatWeight(row.need_g)} / {row.have_g == null ? '—' : formatWeight(row.have_g)}
          </span>
        );
      })}
    </div>
  );
}
