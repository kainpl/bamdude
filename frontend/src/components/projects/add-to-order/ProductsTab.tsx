import { useCallback, useEffect, useState } from 'react';
import { NumberInput } from '../../NumberInput';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Package } from 'lucide-react';
import { api } from '../../../api/client';
import type { ListVariantGroup, ProductListItem, StockSuggestion } from '../../../api/client';
import { optionLabel, optionState, reasonsText } from '../useCombinationState';
import type { CombinationView } from '../useCombinationState';
import type { SuggestStatus } from '../../../hooks/useStockSuggest';
import { useSearchBox } from '../../../hooks/useSearchBox';
import { getColorName } from '../../../utils/colors';
import { Button } from '../../Button';
import { ListSearchBox } from '../../ListSearchBox';
import { PaginationBar } from '../../PaginationBar';
import { Select } from '../../Select';
import { RefreshFailedNote } from '../../workshop/RefreshFailedNote';
import { WorkshopTableScroll } from '../../workshop/WorkshopPanel';
import { MODEL_CHIP } from '../chips';
import { MAX_LINE_QTY, newProductPick, shownStock, withStock } from './addToOrderState';
import { CountInput } from './CountInput';
import { LoadingRows } from './LoadingRows';
import type { ProductPick, ProductPicks } from './addToOrderState';

const PAGE_SIZE = 24;
const HEAD = 'px-3 py-2 text-left text-xs font-normal text-bambu-gray whitespace-nowrap';
const CELL = 'px-3 py-2.5 align-top';

/** A picked row's stock, as the dialog's `useStockSuggest` tells it. */
export interface RowStock {
  /** The proposal — present only when it answers what the row asks now (E5 R02). */
  suggestion: StockSuggestion | undefined;
  status: SuggestStatus;
  refreshFailed: boolean;
}

/**
 * «Products» of the add-to-order dialog (spec workshop-add-to-order, rule 20;
 * WS-13 E5 C — the mockup's `addProductsTab`).
 *
 * The catalog comes a page at a time from the server — search, category and
 * printer model are request parameters; the picks are the dialog's and outlive
 * the page. A picked row becomes fields in place (no expanding row, E5 K1). Its
 * groups come with the catalog row (H01) — no product is read per row. Stock
 * numbers are the server's proposal; a changed number makes the row the
 * operator's until «pick» hands it back.
 *
 * Whether a picked row's combination can be ordered is the dialog's question, asked
 * of the server (`combinationOf`, spec product-part-variant-combinations п. 25): a
 * value that would close it is marked and stays choosable, a value no available
 * combination holds is off (unless chosen), and a closed choice says why. The tab
 * tells the dialog which products it has listed (`onProductsSeen`) — their names and
 * whether they have groups at all, which is what decides whether one is asked.
 */
export function ProductsTab({
  picks,
  onPicksChange,
  takesStock,
  stockOf,
  onRetryStock,
  combinationOf,
  onProductsSeen,
  initialQuery = '',
}: {
  picks: ProductPicks;
  onPicksChange: (next: ProductPicks) => void;
  /** Only an active order takes stock (rule 7) — otherwise there is no stock column. */
  takesStock: boolean;
  stockOf: (productId: number) => RowStock;
  onRetryStock: () => void;
  /** A picked product's combination, as the server answers it. */
  combinationOf: (productId: number) => CombinationView;
  /** Every product a page listed — stable, it runs on every answer. */
  onProductsSeen: (products: ProductListItem[]) => void;
  /** Opens already searched — the product page opens on its own product. */
  initialQuery?: string;
}) {
  const { t } = useTranslation();
  const [q, setQState] = useState(initialQuery);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(PAGE_SIZE);
  const [category, setCategory] = useState('');
  const [model, setModel] = useState('');
  const setQ = useCallback((value: string) => {
    setQState(value);
    setPage(1);
  }, []);
  const { typed, setTyped } = useSearchBox(q, setQ);

  const { data: directory = [] } = useQuery({
    queryKey: ['product-categories'],
    queryFn: () => api.getProductCategories(),
    staleTime: 60_000,
  });
  const { data: facets } = useQuery({
    queryKey: ['product-facets'],
    queryFn: () => api.getProductFacets(),
    staleTime: 60_000,
  });

  const params = {
    page,
    ...(perPage === -1 ? { all: true } : { per_page: perPage }),
    active: true,
    ...(q ? { q } : {}),
    ...(category ? { category } : {}),
    ...(model ? { model } : {}),
  };
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ['products', params],
    queryFn: () => api.getProductsPaged(params),
    placeholderData: keepPreviousData,
  });
  const rows = data?.items ?? [];
  const columns = takesStock ? 6 : 5;
  useEffect(() => {
    if (data) onProductsSeen(data.items);
  }, [data, onProductsSeen]);

  const update = (id: number, pick: ProductPick | null) => {
    const next = new Map(picks);
    if (pick) next.set(id, pick);
    else next.delete(id);
    onPicksChange(next);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <ListSearchBox value={typed} onChange={setTyped} placeholder={t('orders.add.searchProducts')} layout="picker" />
        <Select
          tone="filter"
          active={category !== ''}
          aria-label={t('products.catalog.categories')}
          value={category}
          onChange={(e) => {
            setCategory(e.target.value);
            setPage(1);
          }}
        >
          <option value="">{t('orders.add.allCategories')}</option>
          <option value="none">{t('products.catalog.uncategorized')}</option>
          {directory.map((c) => (
            <option key={c.id} value={String(c.id)}>
              {c.name}
            </option>
          ))}
        </Select>
        <Select
          tone="filter"
          active={model !== ''}
          aria-label={t('products.catalog.model')}
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
        </Select>
      </div>
      <p className="my-2.5 text-[13px] text-bambu-gray">{t('orders.add.help.products')}</p>

      {isError && data && <RefreshFailedNote onRetry={() => refetch()} />}
      <div className="rounded-lg border border-bambu-dark-tertiary">
        <WorkshopTableScroll label={t('orders.add.tabs.products')}>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={`${HEAD} w-8`}>
                  <span className="sr-only">{t('orders.add.pick')}</span>
                </th>
                <th className={`${HEAD} w-[26%]`}>{t('orders.lines.product')}</th>
                <th className={`${HEAD} min-w-[200px]`}>{t('orders.add.configuration')}</th>
                <th className={HEAD}>{t('orders.lines.quantity')}</th>
                {takesStock && <th className={HEAD}>{t('orders.add.stock.title')}</th>}
                <th className={HEAD}>{t('orders.add.materialColor')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((product) => (
                <ProductRow
                  key={product.id}
                  product={product}
                  pick={picks.get(product.id)}
                  stock={picks.has(product.id) ? stockOf(product.id) : undefined}
                  combination={picks.has(product.id) ? combinationOf(product.id) : undefined}
                  onRetryStock={onRetryStock}
                  takesStock={takesStock}
                  onChange={(pick) => update(product.id, pick)}
                />
              ))}
              {isPending && <LoadingRows colSpan={columns} />}
              {isError && !data && (
                <tr>
                  <td colSpan={columns} className="p-6 text-center">
                    <span className="text-amber-700 dark:text-amber-400">{t('orders.add.productsFailed')}</span>{' '}
                    <Button size="sm" variant="ghost" onClick={() => refetch()}>
                      {t('common.retry')}
                    </Button>
                  </td>
                </tr>
              )}
              {data && rows.length === 0 && (
                <tr>
                  <td colSpan={columns} className="p-6 text-center">
                    <p className="text-white">{t('orders.add.noProducts')}</p>
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
          items={t('orders.add.productsNoun')}
        />
      </div>
    </div>
  );
}

/** «Tail: straight / angled; Mount: wall / DIN» — what an unpicked row could be. */
function groupsText(groups: ListVariantGroup[]): string {
  return groups.map((g) => `${g.name}: ${g.options.map((o) => o.name).join(' / ')}`).join('; ');
}

function ProductRow({
  product,
  pick,
  stock,
  combination,
  onRetryStock,
  takesStock,
  onChange,
}: {
  product: ProductListItem;
  pick: ProductPick | undefined;
  stock: RowStock | undefined;
  combination: CombinationView | undefined;
  onRetryStock: () => void;
  takesStock: boolean;
  onChange: (pick: ProductPick | null) => void;
}) {
  const { t } = useTranslation();
  const set = (patch: Partial<ProductPick>) => pick && onChange({ ...pick, ...patch });
  const groups = product.variant_groups ?? [];
  const colours = product.colors.map((c) => getColorName(c)).join(', ');
  return (
    <tr
      data-testid={`add-product-${product.id}`}
      className={`border-t border-bambu-dark-tertiary text-white ${pick ? 'bg-bambu-green/[0.08]' : ''}`}
    >
      <td className={CELL}>
        <input
          type="checkbox"
          checked={pick !== undefined}
          onChange={(e) => onChange(e.target.checked ? newProductPick() : null)}
          aria-label={t('orders.add.pickNamed', { name: product.name })}
          className="mt-3 accent-bambu-green"
        />
      </td>
      <td className={CELL}>
        <div className="flex items-start gap-3">
          {product.has_cover ? (
            <img
              src={api.getProductCoverImageUrl(product.id)}
              alt=""
              className="h-10 w-10 flex-shrink-0 rounded-lg bg-bambu-dark-tertiary object-contain"
            />
          ) : (
            <span className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg bg-bambu-dark-tertiary">
              <Package className="h-[18px] w-[18px] text-bambu-gray" />
            </span>
          )}
          <div className="min-w-0">
            {/* By words: a word never splits, so on a phone the table scrolls instead (the mockup's). */}
            <div className="font-semibold [overflow-wrap:break-word]">{product.name}</div>
            <div className="text-xs text-bambu-gray">
              {product.code}
              {product.sku && (
                <>
                  {' · '}
                  <span className="font-mono">{product.sku}</span>
                </>
              )}
              {product.category && ` · ${product.category.name}`}
              {product.status === 'draft' && (
                <span className="text-amber-700 dark:text-amber-400"> · {t('orders.add.draft')}</span>
              )}
            </div>
            {product.models.length > 0 && (
              <div className="mt-0.5 flex flex-wrap gap-1">
                {product.models.map((m) => (
                  <span key={m} className={MODEL_CHIP}>
                    {m}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      </td>
      <td className={CELL}>
        {/* What the row COULD be reads as the mockup's `small` (12 px, muted). */}
        {groups.length === 0 ? (
          <span className="text-xs text-bambu-gray">{t('orders.add.noVariants')}</span>
        ) : pick ? (
          <PickedConfiguration groups={groups} pick={pick} combination={combination} onChange={onChange} />
        ) : (
          <span className="text-xs text-bambu-gray">{groupsText(groups)}</span>
        )}
      </td>
      <td className={CELL}>
        <CountInput
          value={pick?.qty ?? 1}
          min={1}
          max={MAX_LINE_QTY}
          disabled={!pick}
          onCommit={(qty) => set({ qty })}
          ariaLabel={t('orders.lines.quantity')}
        />
      </td>
      {takesStock && (
        <td className={CELL}>
          {pick && stock ? (
            <StockCell pick={pick} stock={stock} onRetry={onRetryStock} onChange={onChange} />
          ) : (
            <span className="whitespace-nowrap text-xs text-bambu-gray">
              {t(groups.length > 0 ? 'orders.add.stock.listedAll' : 'orders.add.stock.listed', {
                ready: product.finished_available,
                kits: product.kits_available,
              })}
            </span>
          )}
        </td>
      )}
      <td className={CELL}>
        {pick ? (
          <div className="flex flex-col gap-1">
            <Select
              size="sm"
              aria-label={t('products.catalog.material')}
              value={pick.material}
              onChange={(e) => set({ material: e.target.value })}
            >
              <option value="">{t('orders.add.any')}</option>
              {product.materials.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </Select>
            <Select
              size="sm"
              aria-label={t('products.catalog.color')}
              value={pick.color}
              onChange={(e) => set({ color: e.target.value })}
            >
              <option value="">{t('orders.add.anyColor')}</option>
              {product.colors.map((c) => (
                <option key={c} value={c}>
                  {getColorName(c)}
                </option>
              ))}
            </Select>
          </div>
        ) : (
          <>
            <div className="text-xs text-bambu-gray">{product.materials.join(', ') || t('orders.add.any')}</div>
            <div className="text-xs text-bambu-gray">{colours || t('orders.add.anyColor')}</div>
          </>
        )}
      </td>
    </tr>
  );
}

/**
 * A `Select` per variant group, labelled with the group (the mockup's inline label).
 * ⚠️ A group without a standard option stays UNCHOSEN until the operator picks one —
 * the server leaves it so (`line_config._defaults`, E5 R11): the field shows «no
 * choice», and the proposal and the batch carry nothing for it. Clearing it again
 * drops the group from the draft, never sends 0.
 *
 * A value that would close the combination says so in its own text and stays
 * choosable; a value no available combination holds is off, unless it is the one
 * chosen (the field must show what it has). A closed choice says why under the fields.
 */
function PickedConfiguration({
  groups,
  pick,
  combination,
  onChange,
}: {
  groups: ListVariantGroup[];
  pick: ProductPick;
  combination: CombinationView | undefined;
  onChange: (pick: ProductPick) => void;
}) {
  const { t } = useTranslation();
  const choose = (groupId: number, value: string) => {
    const choices = { ...pick.choices };
    if (value === '') delete choices[groupId];
    else choices[groupId] = Number(value);
    onChange({ ...pick, choices });
  };
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap gap-x-2 gap-y-1">
        {groups.map((group) => {
          const current = pick.choices[group.id] ?? group.default_option_id ?? null;
          return (
            <span key={group.id} className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-bambu-gray">
              {group.name}
              <Select
                size="sm"
                aria-label={group.name}
                value={String(current ?? '')}
                onChange={(e) => choose(group.id, e.target.value)}
              >
                {group.default_option_id == null && <option value="">{t('orders.add.noChoice')}</option>}
                {group.options.map((option) => {
                  const { mark, disabled } = optionState(combination, option.id, [current]);
                  return (
                    <option key={option.id} value={String(option.id)} disabled={disabled}>
                      {optionLabel(t, option.name, mark)}
                    </option>
                  );
                })}
              </Select>
            </span>
          );
        })}
      </div>
      {combination?.gate === 'closed' && (
        <p data-testid="combination-note" className="text-xs text-amber-700 dark:text-amber-400">
          {t('orders.combination.closed', { reasons: reasonsText(t, combination.reasons) })}
        </p>
      )}
      {combination?.isError && (
        <p data-testid="combination-note" className="flex flex-wrap items-center gap-1 text-xs">
          <span className="text-amber-700 dark:text-amber-400">{t('orders.combination.failed')}</span>
          <Button size="sm" variant="ghost" onClick={() => combination.refetch()}>
            {t('common.retry')}
          </Button>
        </p>
      )}
    </div>
  );
}

/**
 * A picked row's stock (E5 C06, R02): the proposal's numbers only while they answer
 * what the row asks now. A row the operator set keeps its own numbers through a
 * re-read or a refusal; an automatic row then says only where the proposal stands.
 */
function StockCell({
  pick,
  stock,
  onRetry,
  onChange,
}: {
  pick: ProductPick;
  stock: RowStock;
  onRetry: () => void;
  onChange: (pick: ProductPick) => void;
}) {
  const { t } = useTranslation();
  const { suggestion, status, refreshFailed } = stock;
  const known = status === 'current' && suggestion !== undefined;
  const statusLine =
    status === 'waiting' ? (
      <span className="text-bambu-gray">{t('orders.add.stock.reading')}</span>
    ) : status === 'failed' ? (
      <span className="flex items-center gap-1">
        <span className="text-amber-700 dark:text-amber-400">{t('orders.add.stock.failed')}</span>
        <Button size="sm" variant="ghost" onClick={onRetry}>
          {t('common.retry')}
        </Button>
      </span>
    ) : null;

  // An automatic row without an answer to its own question shows no numbers at all.
  if (pick.auto && !known) return <div className="text-xs">{statusLine}</div>;

  const shown = shownStock(pick, known ? suggestion : undefined);
  const nothing = known && suggestion.finished_free === 0 && suggestion.kits_free === 0;
  const field = (name: 'fromFinished' | 'fromKits', label: string, word: string, free: number | undefined) => (
    <span className="flex items-center gap-1 whitespace-nowrap">
      <NumberInput
        min={0}
        value={shown[name]}
        onChange={(v) => onChange(withStock(pick, known ? suggestion : undefined, name, v))}
        fallback={0}
        aria-label={label}
        className="w-[68px] rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 py-0.5 text-white"
      />
      <span className="text-bambu-gray">{word}</span>
      {free !== undefined && <span className="text-bambu-gray">{t('orders.add.stock.of', { n: free })}</span>}
    </span>
  );
  return (
    <div className="flex flex-col gap-1 text-xs">
      {field('fromFinished', t('orders.add.stock.readyLabel'), t('orders.add.stock.ready'), known ? suggestion.finished_free : undefined)}
      {field('fromKits', t('orders.add.stock.kitsLabel'), t('orders.add.stock.kits'), known ? suggestion.kits_free : undefined)}
      <span className="flex flex-wrap items-center gap-2">
        {statusLine}
        {pick.auto ? (
          <span className={nothing ? 'text-bambu-gray' : 'text-bambu-green'}>
            {t(nothing ? 'orders.add.stock.none' : 'orders.add.stock.auto')}
          </span>
        ) : (
          <button type="button" onClick={() => onChange({ ...pick, auto: true })} className="text-bambu-green hover:underline">
            {t('orders.add.stock.pick')}
          </button>
        )}
        {shown.toPrint > 0 && <span className="text-bambu-gray">{t('orders.add.stock.toPrint', { n: shown.toPrint })}</span>}
      </span>
      {refreshFailed && <span className="text-bambu-gray">{t('orders.add.stock.refreshFailed')}</span>}
    </div>
  );
}
