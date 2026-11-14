import { useState, type RefObject } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ExternalLink, GitMerge, Pencil, Plus, Trash2 } from 'lucide-react';
import { api } from '../../../api/client';
import type { PartSource, PartSourcesSummary, Product, ProductPart, ProductSources } from '../../../api/client';
import { useAuth } from '../../../contexts/AuthContext';
import { useToast } from '../../../contexts/ToastContext';
import { useFocusWhenRowLeaves } from '../../../hooks/useFocusWhenRowLeaves';
import { formatMoney } from '../../../utils/currency';
import { Button } from '../../Button';
import { CardActionMenu, CardActionMenuItem } from '../../CardActionMenu';
import { ActionConfirm } from '../../workshop/ActionConfirm';
import { LoadFailedNote } from '../../workshop/LoadFailedNote';
import { RefreshFailedNote } from '../../workshop/RefreshFailedNote';
import { MergePartDialog } from '../MergePartDialog';
import { ProductPartDialog } from '../ProductPartDialog';
import { ProductVariantsDialog } from '../ProductVariantsDialog';
import { deleteProductPart } from '../partMutations';
import { bindingLabel, bindingVariants, variantsTitle } from '../partBinding';
import { PartThumb } from '../../PartThumb';
import { CombinationsTable } from './CombinationsTable';
import { ModelChip } from './ModelChip';
import { useUiPreferences } from '../../../hooks/useUiPreferences';

const HEAD = 'px-3 py-2 text-left text-xs font-normal text-bambu-gray';
const CELL = 'px-3 py-2 align-top';
const CHIP = 'inline-flex items-center rounded px-1.5 text-[11px] leading-5';
const AMBER = 'text-amber-700 dark:text-amber-400';

/** `sort_order` is the authority; `id` only breaks its ties. */
function byOrder(a: ProductPart, b: ProductPart): number {
  return a.sort_order - b.sort_order || a.id - b.id;
}

type SourcesState = { kind: 'loading' } | { kind: 'failed' } | { kind: 'data'; byPart: Map<number, PartSourcesSummary> };

/**
 * Sources that read alike — the same model (or «not sliced»), plate number and yield, the same
 * visibility — in the server's order, each first occurrence keeping its place (the recommended
 * one first). A product linked to a folder of near-identical exports would otherwise repeat one
 * chip dozens of times (measured on the stand: 27 for one part); the chip says how many files
 * give it and names them in its title.
 */
function groupSources(sources: PartSource[]): { source: PartSource; files: (string | null)[] }[] {
  const groups = new Map<string, { source: PartSource; files: (string | null)[] }>();
  for (const source of sources) {
    const key = [source.sliced, source.printer_model, source.plate_index, source.yield, source.hidden].join('|');
    const group = groups.get(key);
    if (group) group.files.push(source.filename);
    else groups.set(key, { source, files: [source.filename] });
  }
  return [...groups.values()];
}

/** «Plates that give it» of one printed part (D02, R10): the server's sources in its order. */
function SourcesCell({ state, partId }: { state: SourcesState; partId: number }) {
  const { t } = useTranslation();
  if (state.kind !== 'data') {
    return <span className="text-bambu-gray">{state.kind === 'loading' ? '…' : '—'}</span>;
  }
  const sources = state.byPart.get(partId)?.sources ?? [];
  if (sources.length === 0) return <span className={`text-xs ${AMBER}`}>{t('products.detail.composition.noSource')}</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {groupSources(sources).map(({ source, files }) => (
        <span
          key={source.plate_id}
          data-testid="part-source"
          // A file the reader may not see is drawn the same, without its name (R10).
          title={source.hidden ? t('products.detail.composition.hiddenSource') : files.filter(Boolean).join(', ') || undefined}
          className="inline-flex items-center gap-1 text-xs text-bambu-gray-light"
        >
          {!source.sliced ? (
            <small className={`text-xs ${AMBER}`}>{t('products.detail.composition.unsliced')}</small>
          ) : (
            source.printer_model && <ModelChip model={source.printer_model} />
          )}
          {t('products.detail.composition.plate', { index: source.plate_index, yield: source.yield })}
          {files.length > 1 && ` · ${t('products.detail.composition.files', { count: files.length })}`}
          {source.hidden && <span className="sr-only">{t('products.detail.composition.hiddenSource')}</span>}
        </span>
      ))}
    </span>
  );
}

/**
 * The «Composition» tab of the product page (WS-13 E9 D01–D04, E10 C10).
 *
 * It has no fields: the variants card, the printed parts with the plates that give them
 * (`GET /products/{id}/sources`, the key `LineConfigDialog` shares), the purchased parts.
 * Every edit is a dialog, and the tab holds its doors:
 *
 * - «Add part» and a row's «Edit» open `ProductPartDialog` — one part, one request;
 * - a printed row's «Merge into…» (with another printed part to go into) opens
 *   `MergePartDialog`; the merged row leaves and the page's heading takes the focus;
 * - «Delete» asks with what the server really does (C09): the part goes with the history
 *   of its movements (a purchased one with its purchases), and the number on the shelf is
 *   said — no write-off movement is recorded;
 * - «Manage variants…» opens `ProductVariantsDialog` — the whole set as one draft, one
 *   request; the card above stays a read summary, with the server's table of which
 *   combinations of the options an order can take (`CombinationsTable`).
 */
export function CompositionTab({
  product,
  headingRef,
}: {
  product: Product;
  headingRef: RefObject<HTMLHeadingElement | null>;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const canEdit = hasPermission('products:update');
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // The part dialog: `part: null` is a new part.
  const [partDialog, setPartDialog] = useState<{ part: ProductPart | null } | null>(null);
  const [merging, setMerging] = useState<ProductPart | null>(null);
  const [deleting, setDeleting] = useState<ProductPart | null>(null);
  const [variantsOpen, setVariantsOpen] = useState(false);
  const keepFocusWhenRowLeaves = useFocusWhenRowLeaves(headingRef);
  const rowOf = (part: ProductPart) => document.querySelector(`[data-testid="part-${part.id}-row"]`);

  const { data: settings } = useUiPreferences();
  const sources = useQuery<ProductSources>({
    queryKey: ['product-part-sources', product.id],
    queryFn: () => api.getProductSources(product.id),
    retry: false,
  });
  const sourcesState: SourcesState = sources.data
    ? { kind: 'data', byPart: new Map(sources.data.parts.map((p) => [p.part_id, p])) }
    : sources.isError
      ? { kind: 'failed' }
      : { kind: 'loading' };

  const groups = product.variant_groups ?? [];
  const printed = product.parts.filter((p) => p.kind === 'printed').sort(byOrder);
  const purchased = product.parts.filter((p) => p.kind === 'purchased').sort(byOrder);

  const perUnit = (part: ProductPart) =>
    part.qty_per_unit > 0 ? (
      <>{t('products.detail.composition.times', { count: part.qty_per_unit })}
        {(part.extra_percent ?? 0) > 0 && <small className="ml-2 text-amber-700 dark:text-amber-400">{t('products.partDialog.extraBadge', { percent: part.extra_percent })}</small>}
      </>
    ) : (
      <small className={`text-xs ${part.ignored ? AMBER : 'text-bambu-gray'}`}>
        {t(part.ignored ? 'products.composition.notCounted' : 'products.composition.outOfKit')}
      </small>
    );
  // The binding's condition in short — «400 · сплошна», «закрутка · 400, 600» — or «always» (п. 23).
  const variantOf = (part: ProductPart) => {
    const label = bindingLabel(groups, part.variant_option_ids);
    return label ? (
      <span
        className={`${CHIP} bg-amber-500/15 ${AMBER}`}
        title={variantsTitle(bindingVariants(groups, part.variant_option_ids)) ?? undefined}
      >
        {label}
      </span>
    ) : (
      <small className="text-xs text-bambu-gray">{t('products.detail.composition.always')}</small>
    );
  };
  const rowMenu = (part: ProductPart) =>
    canEdit && (
      <CardActionMenu label={t('common.actions')} width="max-content">
        {(close) => (
          <>
            <CardActionMenuItem
              onSelect={() => {
                close();
                setPartDialog({ part });
              }}
            >
              <Pencil className="h-4 w-4" />
              {t('common.edit')}
            </CardActionMenuItem>
            {part.kind === 'printed' && printed.length > 1 && (
              <CardActionMenuItem
                onSelect={() => {
                  close();
                  setMerging(part);
                }}
              >
                <GitMerge className="h-4 w-4" />
                {t('products.composition.mergeInto')}
              </CardActionMenuItem>
            )}
            <CardActionMenuItem
              danger
              onSelect={() => {
                close();
                setDeleting(part);
              }}
            >
              <Trash2 className="h-4 w-4" />
              {t('common.delete')}
            </CardActionMenuItem>
          </>
        )}
      </CardActionMenu>
    );

  return (
    <div className="space-y-5">
      {/* D01 — the variants card. */}
      <div data-testid="composition-variants" className="space-y-2 rounded-xl border border-bambu-dark-tertiary p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm">
            <b className="font-semibold text-white">{t('products.detail.composition.variants')}</b>{' '}
            <small className="text-xs text-bambu-gray">{t('products.detail.composition.variantsHint')}</small>
          </p>
          {canEdit && (
            <Button size="sm" variant="ghost" onClick={() => setVariantsOpen(true)}>
              {t('products.detail.composition.manageVariants')}
            </Button>
          )}
        </div>
        {groups.length === 0 ? (
          <small className="text-xs text-bambu-gray">{t('products.detail.composition.noVariants')}</small>
        ) : (
          groups.map((group) => (
            <div key={group.id} className="flex flex-wrap items-center gap-2 text-sm">
              <b className="font-semibold text-white">{group.name}</b>
              {group.options.map((option) => {
                const standard = option.id === group.default_option_id;
                const names = product.parts.filter((p) => p.variant_option_ids.includes(option.id)).map((p) => p.name);
                return (
                  <span
                    key={option.id}
                    data-testid={`variant-chip-${option.id}`}
                    data-standard={standard ? 'true' : undefined}
                    className={`inline-flex flex-wrap items-baseline gap-x-1 rounded-full border px-2.5 py-0.5 text-xs ${
                      standard ? 'border-bambu-green/60 bg-bambu-green/10 text-white' : 'border-bambu-dark-tertiary text-bambu-gray-light'
                    }`}
                  >
                    {option.name}
                    {standard && ` · ${t('products.detail.composition.standard')}`}{' '}
                    <small className="text-[11px] text-bambu-gray">
                      {names.length > 0 ? names.join(', ') : t('products.detail.composition.noParts')}
                    </small>
                  </span>
                );
              })}
            </div>
          ))
        )}
        {/* п. 24 — which combinations of the options an order can take, and what the others lack. */}
        <CombinationsTable product={product} />
      </div>

      {/* D02 / D04 — the printed parts, with the doors on the heading's right. */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-base font-semibold text-white">{t('products.composition.printed')}</h3>
          {canEdit && (
            <Button size="sm" onClick={() => setPartDialog({ part: null })}>
              <Plus className="h-4 w-4" />
              {t('products.detail.composition.addPart')}
            </Button>
          )}
        </div>

        {sources.isError && (
          sources.data ? (
            <RefreshFailedNote onRetry={() => sources.refetch()} />
          ) : (
            <LoadFailedNote
              role="status"
              message={t('products.detail.composition.sourcesFailed')}
              onRetry={() => sources.refetch()}
            />
          )
        )}
        {printed.length === 0 ? (
          <small className="block text-xs text-bambu-gray">{t('products.detail.composition.printedEmpty')}</small>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary">
                  <th className={HEAD}>{t('products.composition.name')}</th>
                  <th className={HEAD}>{t('products.composition.perUnit')}</th>
                  <th className={HEAD}>{t('products.composition.variant')}</th>
                  <th className={HEAD}>{t('products.composition.aliases')}</th>
                  <th className={HEAD}>{t('products.detail.composition.sources')}</th>
                  {canEdit && <th className={HEAD} aria-label={t('common.actions')} />}
                </tr>
              </thead>
              <tbody>
                {printed.map((part) => (
                  <tr key={part.id} data-testid={`part-${part.id}-row`} className="border-b border-bambu-dark-tertiary last:border-0">
                    <td className={CELL}>
                      <span className="flex items-start gap-2">
                        <PartThumb partId={part.id} image={part.image} name={part.name} kind="printed" size="sm" />
                        <span className="min-w-0">
                          <b className="font-medium text-white break-words">{part.name}</b>
                          {part.auto && (
                            <span className={`${CHIP} ml-1.5 bg-bambu-dark-tertiary text-bambu-gray`}>
                              {t('products.composition.fromFile')}
                            </span>
                          )}
                        </span>
                      </span>
                    </td>
                    <td className={`${CELL} whitespace-nowrap tabular-nums`}>{perUnit(part)}</td>
                    <td className={CELL}>{variantOf(part)}</td>
                    <td className={CELL}>
                      <span className="flex flex-wrap gap-1">
                        {part.aliases.map((alias) => (
                          <span key={alias} className={`${CHIP} bg-bambu-dark-tertiary font-mono text-bambu-gray-light`}>
                            {alias}
                          </span>
                        ))}
                      </span>
                    </td>
                    <td className={CELL} data-testid="part-sources">
                      <SourcesCell state={sourcesState} partId={part.id} />
                    </td>
                    {canEdit && <td className={`${CELL} text-right`}>{rowMenu(part)}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* D03 — the purchased parts. */}
        <h3 className="pt-3 text-base font-semibold text-white">{t('products.composition.purchased')}</h3>
        {purchased.length === 0 ? (
          <small className="block text-xs text-bambu-gray">{t('products.detail.composition.purchasedEmpty')}</small>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary">
                  <th className={HEAD}>{t('products.composition.name')}</th>
                  <th className={HEAD}>{t('products.composition.perUnit')}</th>
                  <th className={HEAD}>{t('products.composition.variant')}</th>
                  <th className={HEAD}>{t('products.detail.composition.price')}</th>
                  <th className={HEAD}>{t('products.composition.sourcingUrl')}</th>
                  <th className={HEAD}>{t('products.composition.remarks')}</th>
                  {canEdit && <th className={HEAD} aria-label={t('common.actions')} />}
                </tr>
              </thead>
              <tbody>
                {purchased.map((part) => (
                  <tr key={part.id} data-testid={`part-${part.id}-row`} className="border-b border-bambu-dark-tertiary last:border-0">
                    <td className={`${CELL} text-white`}>
                      <span className="flex items-start gap-2">
                        <PartThumb partId={part.id} image={part.image} name={part.name} kind="purchased" size="sm" />
                        <span className="min-w-0 break-words">{part.name}</span>
                      </span>
                    </td>
                    <td className={`${CELL} whitespace-nowrap tabular-nums`}>{perUnit(part)}</td>
                    <td className={CELL}>{variantOf(part)}</td>
                    <td className={`${CELL} whitespace-nowrap tabular-nums`} data-testid="part-price">
                      {part.unit_price != null ? formatMoney(part.unit_price, settings?.currency) : '—'}
                    </td>
                    <td className={CELL} data-testid="part-url">
                      {part.sourcing_url ? (
                        <a
                          href={part.sourcing_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          title={part.sourcing_url}
                          className="inline-flex items-center gap-1 text-bambu-gray-light hover:text-white"
                        >
                          {t('products.detail.composition.link')}
                          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                        </a>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className={CELL}>
                      <small className="text-xs text-bambu-gray-light wrap-anywhere">{part.remarks ?? ''}</small>
                    </td>
                    {canEdit && <td className={`${CELL} text-right`}>{rowMenu(part)}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-bambu-gray">{t('products.detail.composition.footnote')}</p>
      </div>

      {partDialog && (
        <ProductPartDialog
          key={partDialog.part?.id ?? 'new'}
          product={product}
          part={partDialog.part}
          onClose={() => setPartDialog(null)}
        />
      )}

      {merging && (
        <MergePartDialog
          product={product}
          source={merging}
          // The merged row — and its menu, where the focus goes back — leaves with the re-read.
          beforeSend={() => keepFocusWhenRowLeaves(rowOf(merging))}
          onClose={() => setMerging(null)}
        />
      )}

      {deleting && (
        <ActionConfirm
          title={t('products.partDelete.title')}
          body={
            <div className="space-y-2 text-sm">
              {/* C09: what the server really does — the history goes; no write-off is recorded. */}
              <p>
                {t(deleting.kind === 'purchased' ? 'products.partDelete.bodyPurchased' : 'products.partDelete.body', {
                  name: deleting.name,
                })}
              </p>
              {deleting.kind === 'printed' && (deleting.stock_balance ?? 0) > 0 && (
                <p>{t('products.partDelete.onShelf', { count: deleting.stock_balance ?? 0 })}</p>
              )}
            </div>
          }
          primaryLabel={t('common.delete')}
          danger
          send={async () => {
            // The row — and its menu, where the focus goes back — leaves with the re-read;
            // watched before the request, which may land while this is still open.
            keepFocusWhenRowLeaves(rowOf(deleting));
            await deleteProductPart(queryClient, product.id, deleting);
            showToast(t('products.partDelete.deleted'));
          }}
          onClose={() => setDeleting(null)}
        />
      )}

      {variantsOpen && <ProductVariantsDialog product={product} onClose={() => setVariantsOpen(false)} />}
    </div>
  );
}
