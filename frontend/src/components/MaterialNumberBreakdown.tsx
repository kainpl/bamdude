import { useTranslation } from 'react-i18next';
import type { AggregateMaterialNumberRow } from '../api/client';
import { formatWeight } from '../utils/weight';

/**
 * Statistics → Filament trends: consumption and cost grouped by the spools'
 * material number (the operator's article code). The server groups and orders
 * (heaviest first) for the page's period; nothing is summed here. Renders
 * nothing when no numbered spool was used — most farms number nothing.
 */
export function MaterialNumberBreakdown({ rows, currency }: { rows: AggregateMaterialNumberRow[]; currency: string }) {
  const { t } = useTranslation();
  if (rows.length === 0) return null;
  return (
    <div className="bg-bambu-dark rounded-lg p-4">
      <h4 className="text-sm font-medium text-white">{t('stats.byMaterialNumber')}</h4>
      <p className="text-xs text-bambu-gray mt-0.5 mb-3">{t('stats.byMaterialNumberHint')}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-bambu-gray border-b border-bambu-dark-tertiary">
              <th className="py-1.5 pr-3 font-medium">{t('stats.materialNumberColumn')}</th>
              <th className="py-1.5 px-3 font-medium text-right">{t('stats.materialNumberSpools')}</th>
              <th className="py-1.5 px-3 font-medium text-right">{t('stats.materialNumberConsumed')}</th>
              <th className="py-1.5 pl-3 font-medium text-right">{t('stats.materialNumberCost')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.material_number} className="border-b border-bambu-dark-tertiary/50 last:border-0">
                <td className="py-1.5 pr-3 font-mono text-white break-all">{row.material_number}</td>
                <td className="py-1.5 px-3 text-right text-bambu-gray">{row.spools}</td>
                <td className="py-1.5 px-3 text-right text-white">{formatWeight(row.grams, t)}</td>
                <td className="py-1.5 pl-3 text-right text-white">{currency}{row.cost.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
