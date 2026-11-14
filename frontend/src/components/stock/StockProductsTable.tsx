import { Fragment, useId, useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../../contexts/AuthContext';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { StockListItem } from '../../api/client';
import { LivePartThumb } from '../PartThumb';
import { variantsTitle } from '../products/partBinding';
import { Button } from '../Button';
import { SortableHeader } from '../SortableHeader';
import { WorkshopPanel, WorkshopTableScroll } from '../workshop/WorkshopPanel';
import { SectionLink } from '../workshop/SectionLink';

interface StockProductsTableProps {
  products: StockListItem[];
  /** `stock:move`, read once by the page. */
  canEdit: boolean;
  onAdjust: (product: StockListItem) => void;
  /** «Assemble» for this product — its configuration is chosen in the dialog (R01). */
  onAssemble: (product: StockListItem) => void;
  /** The list's `sort_by`; the headers ask the SERVER to sort. */
  sort: string;
  onSortChange: (sortBy: string) => void;
  /** The page bar, drawn inside the same panel under the rows (outside the scroll). */
  footer?: ReactNode;
}

const MARK = 'ml-2 text-xs text-bambu-gray';

/**
 * One row per product with a shelf (WS-13 E12 D03) — the mockup's six columns: the
 * expander, the product, kits by configuration, parts on the shelf, the orders' reservations
 * and the row's actions; a row expands into its counted parts.
 *
 * Every number is the server's — `kits_available` / `kits_by_option` are the ledger's
 * `min` over the counted parts, `parts_on_shelf` its sum, never recomputed from `parts`
 * here (the two would drift the first time a part stopped counting). A product with variants
 * shows one line per option, the other groups at their standard, and the numbers do not add
 * up (as E9). The rows are one page of many, so the headers sort on the server (spec
 * workshop-lists, rules 13, 20). Expansion is per row and local.
 *
 * «Assemble» is not blocked by a zero: the standard may make nothing while another option
 * makes three, and with two groups every row of `kits_by_option` may read 0 while a
 * combination still works — the dialog's own lookup decides (R01). A one-off product is
 * refused by the server, so its button is disabled with the reason on screen.
 */
export function StockProductsTable({ products, canEdit, onAdjust, onAssemble, sort, onSortChange, footer }: StockProductsTableProps) {
  const { t } = useTranslation();
  // A hand correction of the free parts is `stock:adjust` (WS-13 E13 O06); assembling moves goods.
  // Its dialog reads the product's shelf (`/products/{id}/stock`), the catalog's read too.
  const { hasPermission } = useAuth();
  const canAdjust = hasPermission('stock:adjust') && hasPermission('products:read');
  const uid = useId();
  const [open, setOpen] = useState<Set<number>>(() => new Set());
  const withOptions = products.some((p) => p.kits_by_option.length > 0);

  const toggle = (id: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <WorkshopPanel flush footer={footer}>
      <WorkshopTableScroll label={t('stock.tabs.parts')}>
        <table className="w-full text-sm">
          <thead className="text-xs text-bambu-gray bg-bambu-dark-secondary">
            <tr>
              <th className="p-2 w-8" aria-hidden />
              <SortableHeader sortKey="name" label={t('stock.page.product')} sort={sort} onSort={onSortChange} />
              <SortableHeader sortKey="kits" label={t('stock.page.kits')} sort={sort} onSort={onSortChange} descFirst />
              <SortableHeader sortKey="shelf" label={t('stock.page.partsColumn')} sort={sort} onSort={onSortChange} descFirst align="right" />
              <SortableHeader sortKey="reserved" label={t('stock.page.reserved')} sort={sort} onSort={onSortChange} descFirst />
              <th className="p-2" aria-label={t('common.actions')} />
            </tr>
          </thead>
          <tbody>
            {products.map((p) => {
              const expanded = open.has(p.id);
              const oneOff = p.origin !== 'catalog';
              const reasonId = `${uid}-one-off-${p.id}`;
              return (
                <Fragment key={p.id}>
                  <tr className="border-t border-bambu-dark-tertiary text-white align-top" data-testid={`stock-row-${p.id}`}>
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
                      <SectionLink to={`/products/${p.id}`} className="text-white hover:underline">
                        {p.name}
                      </SectionLink>
                      {!p.is_active && <span className={MARK}>{t('stock.page.notInCatalog')}</span>}
                      {oneOff && <span className={MARK}>{t('stock.page.oneOff')}</span>}
                      {p.sku && <small className="block text-xs text-bambu-gray font-mono">{p.sku}</small>}
                    </td>
                    <td className="p-2 tabular-nums" data-testid={`stock-kits-${p.id}`}>
                      {p.kits_by_option.length > 0 ? (
                        p.kits_by_option.map((o) => (
                          <small key={o.option_id} className="block text-xs text-bambu-gray">
                            {o.group_name} {o.option_name}: <b className="text-white font-semibold">{o.kits}</b>
                          </small>
                        ))
                      ) : (
                        <b className="font-semibold">{p.kits_available}</b>
                      )}
                    </td>
                    <td className="p-2 text-right tabular-nums" data-testid={`stock-shelf-${p.id}`}>
                      {p.parts_on_shelf}
                    </td>
                    <td className="p-2 tabular-nums" data-testid={`stock-reserved-${p.id}`}>
                      {p.reservations.length === 0 ? (
                        <small className="text-bambu-gray">—</small>
                      ) : (
                        p.reservations.map((r) => (
                          <span key={r.line_id} className="block whitespace-nowrap">
                            <SectionLink to={`/projects/${r.order_id}`} title={r.order_name} className="text-bambu-green hover:underline">
                              {r.order_code}
                            </SectionLink>
                            {` · ${r.kits}`}
                          </span>
                        ))
                      )}
                    </td>
                    <td className="p-2 text-right">
                      {canEdit && (
                        <>
                          <div className="flex justify-end gap-2">
                            {canAdjust && (
                              <Button size="sm" variant="ghost" onClick={() => onAdjust(p)}>
                                {t('stock.adjust.open')}
                              </Button>
                            )}
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={oneOff}
                              aria-describedby={oneOff ? reasonId : undefined}
                              onClick={() => onAssemble(p)}
                            >
                              {t('stock.page.assemble')}
                            </Button>
                          </div>
                          {oneOff && (
                            <small id={reasonId} className="block mt-1 text-xs text-bambu-gray">
                              {t('stock.page.oneOffAssemble')}
                            </small>
                          )}
                        </>
                      )}
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="border-t border-bambu-dark-tertiary" data-testid={`stock-details-${p.id}`}>
                      <td />
                      <td colSpan={5} className="p-2 pb-4">
                        <table className="w-full max-w-lg text-sm">
                          <thead>
                            <tr className="text-xs text-bambu-gray text-left">
                              <th className="font-normal p-1">{t('stock.part')}</th>
                              <th className="font-normal p-1">{t('stock.perUnit')}</th>
                              <th className="font-normal p-1 text-right">{t('stock.balance')}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {/* Every counted part, a zero per unit too: it has a shelf and makes no kit (R02). */}
                            {p.parts.map((b) => (
                              <tr key={b.part_id} className="text-white">
                                <td className="p-1">
                                  <span className="flex items-center gap-2">
                                    <LivePartThumb partId={b.part_id} image={b.image} name={b.name} size="sm" />
                                    <span className="min-w-0">
                                      {b.name}
                                      {(b.variants ?? []).length > 0 && (
                                        <span
                                          title={variantsTitle(b.variants ?? []) ?? undefined}
                                          className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] bg-bambu-dark-tertiary text-bambu-gray"
                                        >
                                          {t('stock.page.variant')}
                                        </span>
                                      )}
                                    </span>
                                  </span>
                                </td>
                                <td className="p-1 tabular-nums">
                                  {b.qty_per_unit > 0 ? (
                                    `× ${b.qty_per_unit}`
                                  ) : (
                                    <small className="text-xs text-bambu-gray">{t('stock.outOfKit')}</small>
                                  )}
                                </td>
                                <td className="p-1 text-right tabular-nums" data-testid={`stock-balance-${b.part_id}`}>
                                  {b.balance}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
          {withOptions && (
            <tfoot>
              <tr className="border-t border-bambu-dark-tertiary">
                <td />
                <td />
                <td colSpan={4} className="p-2 text-xs text-bambu-gray">
                  {t('products.detail.stockTab.kitsNote')}
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      </WorkshopTableScroll>
    </WorkshopPanel>
  );
}
