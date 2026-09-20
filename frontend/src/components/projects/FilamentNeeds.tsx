import { useTranslation } from 'react-i18next';
import type { OrderNeeds } from '../../api/client';
import { formatWeight } from '../../utils/weight';
import { ForecastHint } from './ForecastHint';
import { needTestId } from './filamentNeedsHelpers';

/** «Need vs shelf» per material and line colour, under the plan's totals (spec 2026-09-07, Slice C). */
export function FilamentNeeds({ needs }: { needs: OrderNeeds }) {
  const { t } = useTranslation();
  if (needs.rows.length === 0 && needs.unknown_prints === 0) return null;
  return (
    <div className="rounded-xl border border-bambu-dark-tertiary bg-bambu-dark p-3 text-sm" data-testid="filament-needs">
      <p className="text-xs text-bambu-gray mb-2">
        {t('orders.filament.title')} <ForecastHint forecast={needs} />
      </p>
      <ul className="space-y-1">
        {needs.rows.map((row) => {
          const short = row.short_g != null && row.short_g > 0;
          const shelfUnknown = row.have_g == null;
          return (
            <li key={needTestId(row.material, row.colour)} data-testid={needTestId(row.material, row.colour)} data-short={String(short)}
                className={`flex flex-wrap items-baseline gap-x-3 ${short ? 'text-amber-300' : 'text-white'}`}>
              <span className="font-medium">{row.colour ? `${row.material} · ${row.colour}` : row.material}</span>
              <span className="text-bambu-gray">{t('orders.filament.need')}</span>
              <span className="tabular-nums">{formatWeight(row.need_g)}</span>
              <span className="text-bambu-gray">{t('orders.filament.shelf')}</span>
              {shelfUnknown ? (
                <span title={t('orders.filament.stockUnavailable')}>—</span>
              ) : (
                <span className="tabular-nums">
                  {formatWeight(row.have_g as number)}
                  {row.colour && row.have_type_g != null && row.have_type_g !== row.have_g && (
                    <span className="text-bambu-gray"> ({t('orders.filament.ofType', { amount: formatWeight(row.have_type_g), material: row.material })})</span>
                  )}
                </span>
              )}
              {short && <span>{t('orders.filament.short', { amount: formatWeight(row.short_g as number) })}</span>}
            </li>
          );
        })}
      </ul>
      {needs.unknown_prints > 0 && <p className="text-xs text-amber-300 mt-2">{t('orders.filament.unknownPrints', { count: needs.unknown_prints })}</p>}
    </div>
  );
}
