import { useCallback, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../../api/client';
import type { PartSource, ProductPartRow } from '../../../api/client';
import { useSearchBox } from '../../../hooks/useSearchBox';
import { Button } from '../../Button';
import { LivePartThumb } from '../../PartThumb';
import { ListSearchBox } from '../../ListSearchBox';
import { PaginationBar } from '../../PaginationBar';
import { Select } from '../../Select';
import { RefreshFailedNote } from '../../workshop/RefreshFailedNote';
import { WorkshopTableScroll } from '../../workshop/WorkshopPanel';
import { variantsLabel, variantsTitle } from '../../products/partBinding';
import { MODEL_CHIP, VARIANT_CHIP } from '../chips';
import { MAX_PART_COUNT } from './addToOrderState';
import type { PartPicks } from './addToOrderState';
import { CountInput } from './CountInput';
import { LoadingRows } from './LoadingRows';

const PAGE_SIZE = 24;
const HEAD = 'px-3 py-2 text-left text-xs font-normal text-bambu-gray whitespace-nowrap';
const CELL = 'px-3 py-2.5 align-top';

/**
 * «Parts of a product» (spec workshop-add-to-order, rule 21; WS-13 E5 D — the
 * mockup's `addPartsTab`): the printed parts of catalog products, a server page at
 * a time. Parts of one product become one «parts only» line when added.
 *
 * Each part shows the file and plate the PLAN would print it from — the server's
 * `recommended` source (E1 PS1) — and the yield on that plate. The dialog picks no
 * plate itself (the plan does, E5 K5), so the plate count under a picked part is a
 * preview over the server's yields (PS8): one number when every sliced file yields
 * the same, a range when they differ, «unknown» when nothing is sliced. A file the
 * reader may not open keeps its plate, model and yield, not its name (LV4) — and
 * the tab reads nothing from the library itself.
 */
export function PartsTab({ picks, onPicksChange }: { picks: PartPicks; onPicksChange: (next: PartPicks) => void }) {
  const { t } = useTranslation();
  const [q, setQState] = useState('');
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(PAGE_SIZE);
  const [model, setModel] = useState('');
  const setQ = useCallback((value: string) => {
    setQState(value);
    setPage(1);
  }, []);
  const { typed, setTyped } = useSearchBox(q, setQ);

  const { data: facets } = useQuery({
    queryKey: ['product-facets'],
    queryFn: () => api.getProductFacets(),
    staleTime: 60_000,
  });
  const params = {
    page,
    // The picker is paged — the server refuses the whole list (WS-13 E1 K7).
    per_page: perPage,
    ...(q ? { q } : {}),
    ...(model ? { model } : {}),
  };
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ['product-parts', params],
    queryFn: () => api.getProductParts(params),
    placeholderData: keepPreviousData,
  });
  const rows = data?.items ?? [];

  const toggle = (row: ProductPartRow, on: boolean) => {
    const next = new Map(picks);
    if (on) next.set(row.part_id, { productId: row.product.id, qty: 1 });
    else next.delete(row.part_id);
    onPicksChange(next);
  };
  const setQty = (row: ProductPartRow, qty: number) => {
    const next = new Map(picks);
    next.set(row.part_id, { productId: row.product.id, qty });
    onPicksChange(next);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <ListSearchBox value={typed} onChange={setTyped} placeholder={t('orders.add.parts.search')} layout="picker" />
        <Select
          tone="filter"
          active={model !== ''}
          aria-label={t('products.catalog.model')}
          // PS4: the filter is on the PRODUCT — a product sliced for this printer.
          title={t('orders.add.parts.modelHint')}
          value={model}
          onChange={(e) => {
            setModel(e.target.value);
            setPage(1);
          }}
        >
          <option value="">{t('products.catalog.anyModel')}</option>
          {(facets?.models ?? []).map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
          <option value="none">{t('orders.add.parts.noSlicedFilter')}</option>
        </Select>
      </div>
      <p className="my-2.5 text-[13px] text-bambu-gray">{t('orders.add.help.parts')}</p>

      {isError && data && <RefreshFailedNote onRetry={() => refetch()} />}
      <div className="rounded-lg border border-bambu-dark-tertiary">
        <WorkshopTableScroll label={t('orders.add.tabs.parts')}>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={`${HEAD} w-8`}>
                  <span className="sr-only">{t('orders.add.pick')}</span>
                </th>
                <th className={HEAD}>{t('orders.add.parts.head.part')}</th>
                <th className={HEAD}>{t('orders.add.parts.head.file')}</th>
                <th className={HEAD}>{t('orders.add.parts.head.yield')}</th>
                <th className={HEAD}>{t('orders.add.parts.qty')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <PartRow
                  key={row.part_id}
                  row={row}
                  qty={picks.get(row.part_id)?.qty}
                  onToggle={(on) => toggle(row, on)}
                  onQty={(qty) => setQty(row, qty)}
                />
              ))}
              {isPending && <LoadingRows colSpan={5} />}
              {isError && !data && (
                <tr>
                  <td colSpan={5} className="p-6 text-center">
                    <span className="text-amber-700 dark:text-amber-400">{t('orders.add.parts.failed')}</span>{' '}
                    <Button size="sm" variant="ghost" onClick={() => refetch()}>
                      {t('common.retry')}
                    </Button>
                  </td>
                </tr>
              )}
              {data && rows.length === 0 && (
                <tr>
                  <td colSpan={5} className="p-6 text-center">
                    <p className="text-white">{t('orders.add.parts.none')}</p>
                    <p className="text-sm text-bambu-gray">{t('orders.add.noProductsHint')}</p>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </WorkshopTableScroll>
        <PaginationBar
          page={page}
          totalPages={data?.meta.last_page ?? 1}
          perPage={perPage}
          total={data?.meta.total ?? 0}
          onPageChange={setPage}
          onPerPageChange={(n) => {
            setPerPage(n);
            setPage(1);
          }}
          items={t('orders.add.parts.noun')}
          allowAll={false}
        />
      </div>
    </div>
  );
}

function PartRow({
  row,
  qty,
  onToggle,
  onQty,
}: {
  row: ProductPartRow;
  qty: number | undefined;
  onToggle: (on: boolean) => void;
  onQty: (qty: number) => void;
}) {
  const { t } = useTranslation();
  const picked = qty !== undefined;
  const best: PartSource | undefined = row.sources.find((s) => s.recommended);
  const fileName = best ? (best.hidden || best.filename == null ? t('products.plates.hiddenFile') : best.filename) : null;
  const plate = best
    ? best.plate_index === 0
      ? t('orders.plan.row.wholeFile')
      : t('orders.plan.row.plate', { n: best.plate_index })
    : null;
  return (
    <tr
      data-testid={`add-part-${row.part_id}`}
      className={`border-t border-bambu-dark-tertiary text-white ${picked ? 'bg-bambu-green/[0.08]' : ''}`}
    >
      <td className={CELL}>
        <input
          type="checkbox"
          checked={picked}
          onChange={(e) => onToggle(e.target.checked)}
          aria-label={t('orders.add.pickNamed', { name: row.name })}
          className="mt-1 accent-bambu-green"
        />
      </td>
      <td className={CELL}>
        <div className="flex items-start gap-2">
          <LivePartThumb partId={row.part_id} image={row.image} name={row.name} size="sm" />
          <div className="min-w-0">
            <div className="font-semibold [overflow-wrap:break-word]">
              {row.name}
              {row.variants.length > 0 && (
                <span className={`ml-1.5 ${VARIANT_CHIP}`} title={variantsTitle(row.variants) ?? undefined}>
                  {variantsLabel(row.variants)}
                </span>
              )}
            </div>
            <div className="text-xs text-bambu-gray">
              {row.product.name}
              {row.product.sku && (
                <>
                  {' · '}
                  <span className="font-mono">{row.product.sku}</span>
                </>
              )}
              {` · ${row.product.code}`}
            </div>
          </div>
        </div>
      </td>
      <td className={CELL}>
        {/* The mockup's `small` (12 px, muted); a file name wraps whole, so on a phone the
            table scrolls instead of splitting it mid-token. */}
        {best && row.has_sliced_source ? (
          <div className="text-xs text-bambu-gray [overflow-wrap:break-word]">
            {fileName} · {plate}
          </div>
        ) : (
          <div className="text-xs text-amber-700 dark:text-amber-400">{t('orders.add.parts.noSliced')}</div>
        )}
        {row.models.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-1">
            {row.models.map((m) => (
              <span key={m} className={MODEL_CHIP}>
                {m}
              </span>
            ))}
          </div>
        )}
      </td>
      <td className={`${CELL} whitespace-nowrap`}>
        {best && row.has_sliced_source ? t('orders.add.parts.perPlate', { n: best.yield }) : '—'}
      </td>
      <td className={CELL}>
        <CountInput
          value={qty ?? 1}
          min={1}
          max={MAX_PART_COUNT}
          disabled={!picked}
          onCommit={onQty}
          ariaLabel={t('orders.add.parts.qty')}
        />
        {picked && <PlatesPreview qty={qty} row={row} />}
      </td>
    </tr>
  );
}

/**
 * How many plates `qty` parts take (E1 PS8) — over the server's yields, never over a
 * source this dialog chose: the plan chooses. It promises no dispatch.
 */
function PlatesPreview({ qty, row }: { qty: number; row: ProductPartRow }) {
  const { t } = useTranslation();
  let text: string;
  if (!row.has_sliced_source || row.yield_min == null || row.yield_max == null || row.yield_min <= 0) {
    text = t('orders.add.parts.platesUnknown');
  } else {
    const from = Math.ceil(qty / row.yield_max);
    const to = Math.ceil(qty / row.yield_min);
    text = from === to ? t('orders.add.parts.plates', { count: from }) : t('orders.add.parts.platesRange', { from, to });
  }
  return <div className="mt-1 text-xs text-bambu-gray">{text}</div>;
}
