import { Fragment, useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { StockProduct } from '../../api/client';
import { Button } from '../Button';

interface StockProductsTableProps {
  products: StockProduct[];
  /** `projects:update`, read once by the page. */
  canEdit: boolean;
  onAdjust: (product: StockProduct) => void;
}

/**
 * One row per product with a shelf; a row expands into its counted parts and
 * the active orders holding its kits.
 *
 * Every number is the server's — `kits_available` is the ledger's `min` over
 * the counted parts, never recomputed from `parts` here (the two would drift
 * the first time a part stopped counting). Expansion is per row and local:
 * nothing about which rows are open is worth a URL or storage.
 */
export function StockProductsTable({ products, canEdit, onAdjust }: StockProductsTableProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<Set<number>>(() => new Set());

  const toggle = (id: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-bambu-gray text-left">
            <th className="font-normal p-2 w-8" />
            <th className="font-normal p-2">{t('stock.page.product')}</th>
            <th className="font-normal p-2">{t('stock.page.kits')}</th>
            <th className="font-normal p-2">{t('stock.page.partsColumn')}</th>
            <th className="font-normal p-2" />
          </tr>
        </thead>
        <tbody>
          {products.map((p) => {
            const expanded = open.has(p.id);
            return (
              <Fragment key={p.id}>
                <tr className="border-t border-bambu-dark-tertiary text-white" data-testid={`stock-row-${p.id}`}>
                  <td className="p-2">
                    <button
                      type="button"
                      onClick={() => toggle(p.id)}
                      aria-expanded={expanded}
                      aria-label={`${t(expanded ? 'stock.page.collapse' : 'stock.page.expand')} — ${p.name}`}
                      className="p-1 rounded hover:bg-bambu-dark-tertiary text-bambu-gray"
                    >
                      {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                    </button>
                  </td>
                  <td className="p-2">
                    <Link to={`/products/${p.id}`} className="text-white hover:underline">
                      {p.name}
                    </Link>
                    {!p.is_active && <span className="ml-2 text-xs text-bambu-gray">{t('stock.page.notInCatalog')}</span>}
                    {p.origin !== 'catalog' && <span className="ml-2 text-xs text-bambu-gray">{t('stock.page.oneOff')}</span>}
                  </td>
                  <td className="p-2 tabular-nums font-medium" data-testid={`stock-kits-${p.id}`}>
                    {p.kits_available}
                  </td>
                  <td className="p-2 text-bambu-gray">{t('stock.page.partsCount', { count: p.parts.length })}</td>
                  <td className="p-2 text-right">
                    {canEdit && (
                      <Button size="sm" variant="secondary" onClick={() => onAdjust(p)}>
                        {t('stock.adjust.open')}
                      </Button>
                    )}
                  </td>
                </tr>
                {expanded && (
                  <tr className="border-t border-bambu-dark-tertiary" data-testid={`stock-details-${p.id}`}>
                    <td />
                    <td colSpan={4} className="p-2 pb-4">
                      <table className="w-full max-w-lg text-sm mb-3">
                        <thead>
                          <tr className="text-xs text-bambu-gray text-left">
                            <th className="font-normal p-1">{t('stock.part')}</th>
                            <th className="font-normal p-1">{t('stock.perUnit')}</th>
                            <th className="font-normal p-1">{t('stock.balance')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {p.parts.map((b) => (
                            <tr key={b.part_id} className="text-white">
                              <td className="p-1">{b.name}</td>
                              <td className="p-1 tabular-nums">{b.qty_per_unit}</td>
                              <td className="p-1 tabular-nums" data-testid={`stock-balance-${b.part_id}`}>{b.balance}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <p className="text-xs text-bambu-gray mb-1">{t('stock.page.reservations')}</p>
                      {p.reservations.length === 0 ? (
                        <p className="text-sm text-bambu-gray">{t('stock.page.noReservations')}</p>
                      ) : (
                        <ul className="text-sm text-white space-y-1">
                          {p.reservations.map((r) => (
                            <li key={r.line_id}>
                              <Link to={`/projects/${r.order_id}`} className="text-bambu-green hover:underline">
                                {r.order_name}
                              </Link>
                              <span className="ml-2 tabular-nums">{t('stock.kits', { count: r.kits })}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
