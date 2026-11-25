import { useEffect, useId, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type {
  LineConfigurationBody,
  LineConfigurationImpact,
  PartImageRef,
  PartSource,
  Product,
  ProductPart,
  ProjectLine,
} from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useProductDetail } from '../../hooks/useProductDetail';
import { invalidateOrderViews } from '../../utils/queryInvalidation';
import { canReadProducts } from '../../utils/workshopRights';
import { Button } from '../Button';
import { RefreshFailedNote } from '../workshop/RefreshFailedNote';
import { WorkshopDialog } from '../workshop/WorkshopDialog';
import { WorkshopTableScroll } from '../workshop/WorkshopPanel';
import { MODEL_CHIP, VARIANT_CHIP } from './chips';
import { LivePartThumb } from '../PartThumb';
import { bindingByGroup, bindingLabel, type BindingGroup } from '../products/partBinding';
import { CountInput } from './add-to-order/CountInput';
import { saveAllowed } from './lineConfigSave';
import {
  COMBINATION_STATE_KEY,
  choicesKey,
  isCombinationRefusal,
  optionLabel,
  optionState,
  reasonsText,
  useCombinationState,
} from './useCombinationState';

const HEAD = 'px-3 py-2 text-left text-xs font-normal text-bambu-gray whitespace-nowrap';
const CELL = 'px-3 py-2 align-top';
/** The server's cap on one part's count (`line_config.MAX_COUNT`). */
const MAX_COUNT = 9999;
/** At most this many sources per part in the «Source» cell (the mockup shows two). */
const SOURCES_SHOWN = 2;

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

function byOrder(a: ProductPart, b: ProductPart): number {
  return (a.sort_order ?? 0) - (b.sort_order ?? 0) || a.id - b.id;
}

/** Every group's standard option — a group without one is left out, as the server leaves it (E5 R11). */
function defaultsOf(product: Product | undefined): Record<number, number> {
  const out: Record<number, number> = {};
  for (const group of product?.variant_groups ?? []) {
    if (group.default_option_id != null) out[group.id] = group.default_option_id;
  }
  return out;
}

/** A part's count per unit under the chosen options, before the line's own changes: in the kit when,
 *  in every group it is bound in, the chosen option is one of its options («and» across groups, «or»
 *  within one); a part bound to nothing is always in. An option the product's groups do not hold
 *  is a group nobody can choose in — never «always in the kit». */
function basePer(part: ProductPart, chosen: Set<number>, groups: readonly BindingGroup[]): number {
  const known = new Set(groups.flatMap((group) => group.options.map((option) => option.id)));
  if (part.variant_option_ids.some((id) => !known.has(id))) return 0;
  for (const optionIds of bindingByGroup(groups, part.variant_option_ids).values()) {
    if (!optionIds.some((id) => chosen.has(id))) return 0;
  }
  return part.qty_per_unit;
}

/**
 * The body the server gets: every group's choice, and only the counts that
 * differ from what the chosen options give anyway (a parts line: every wanted
 * count). The server keeps the same rule; saying it here keeps the dirty check
 * and the preview honest.
 */
function bodyOf(
  product: Product | undefined,
  isParts: boolean,
  choices: Record<number, number>,
  counts: Record<number, number>,
): LineConfigurationBody {
  if (isParts) {
    return {
      choices: {},
      part_counts: Object.fromEntries(Object.entries(counts).filter(([, qty]) => qty > 0)),
    };
  }
  const chosen = { ...defaultsOf(product), ...choices };
  const set = new Set(Object.values(chosen));
  const parts = new Map((product?.parts ?? []).map((p) => [p.id, p]));
  const changed: Record<number, number> = {};
  for (const [pid, qty] of Object.entries(counts)) {
    const part = parts.get(Number(pid));
    if (part && qty !== basePer(part, set, product?.variant_groups ?? [])) changed[Number(pid)] = qty;
  }
  return { choices: chosen, part_counts: changed };
}

/**
 * Change an order line's configuration (spec workshop-product-variants, rules
 * 11–14, 26; WS-13 E5 F — the mockup's `renderConfig`): the option of each group,
 * and per part whether it is in the kit and how many per unit — or, for a parts
 * line, how many of each.
 *
 * Every change is previewed by the server's dry run before anything is saved:
 * which parts drop out, what of them is already printed or queued (it becomes
 * surplus), and what the line's reservation becomes. The figures are the
 * server's; this dialog only says them.
 *
 * ⚠️ «Reset to standard» is the FULL reset — the standard options AND no changed
 * count (E5 R03; the mockup's resets only the counts). A group without a standard
 * option stays unchosen through it (R11).
 *
 * Whether the chosen combination can be ordered is the server's answer
 * (`useCombinationState`, spec product-part-variant-combinations п. 25): a value that
 * would close it is marked and stays clickable — two groups can be changed one after
 * the other to leave a closed choice; a value no available combination holds is off,
 * unless the line has it or it is chosen. Save waits for that answer and refuses a
 * closed choice — whose dry run is not asked, the server would refuse it as well — but
 * only when the CHOICES differ from the line's own: a saved line
 * whose combination closed later keeps it, and its counts can still be changed (the
 * server checks only a changed choice).
 */
export function LineConfigDialog({
  orderId,
  orderCode,
  line,
  onClose,
}: {
  orderId: number;
  orderCode: string;
  line: ProjectLine;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { hasPermission } = useAuth();
  const markId = useId();
  const productQuery = useProductDetail(line.product_id);
  const product = productQuery.data;
  const isParts = line.mode === 'parts';
  const sources = useQuery({
    queryKey: ['product-part-sources', line.product_id],
    queryFn: () => api.getProductSources(line.product_id),
  });
  const sourcesOf = useMemo(
    () => new Map((sources.data?.parts ?? []).map((p) => [p.part_id, p.sources])),
    [sources.data],
  );

  const initialChoices = useMemo(
    () => Object.fromEntries((line.configuration?.choices ?? []).map((c) => [c.group_id, c.option_id])),
    [line],
  );
  const initialCounts = useMemo(
    () => Object.fromEntries((line.configuration?.changed_parts ?? []).map((p) => [p.part_id, p.qty])),
    [line],
  );
  const [choices, setChoices] = useState<Record<number, number>>(initialChoices);
  const [counts, setCounts] = useState<Record<number, number>>(initialCounts);

  const revision = line.configuration?.revision;
  const saved = new Map([
    ...line.parts.filter((p) => p.qty_per_unit > 0).map((p) => [p.part_id, p.qty_per_unit] as const),
    ...(line.purchased ?? []).filter((p) => p.per > 0).map((p) => [p.part_id, p.per] as const),
  ]);
  const [confirmed, setConfirmed] = useState('');
  const defaults = defaultsOf(product);
  const groups = product?.variant_groups ?? [];
  // The combination as the server reads it — every group's choice; the line's own beside it.
  const chosen = { ...defaults, ...choices };
  const choicesChanged = choicesKey(chosen) !== choicesKey({ ...defaults, ...initialChoices });
  const editingSaved = revision != null && !choicesChanged;
  const body: LineConfigurationBody = editingSaved
    ? {
        choices: {}, part_counts: {},
        part_edits: Object.fromEntries(Object.entries(counts).filter(([id, qty]) => {
          const pid = Number(id);
          return saved.has(pid) && qty !== saved.get(pid);
        })),
      }
    : bodyOf(product, isParts, choices, counts);
  const bodyKey = JSON.stringify(body);
  const initialKey = editingSaved
    ? JSON.stringify({ choices: {}, part_counts: {}, part_edits: {} })
    : JSON.stringify(bodyOf(product, isParts, initialChoices, initialCounts));
  const dirty = product != null && bodyKey !== initialKey;
  const debouncedKey = useDebouncedValue(bodyKey, 300);
  const settled = debouncedKey === bodyKey;
  const combination = useCombinationState(
    line.product_id,
    chosen,
    !isParts && product != null && groups.length > 0 && canReadProducts(hasPermission),
  );
  // Only a changed choice is the server's to refuse; while its answer is not in, nothing is sent.
  const combinationBlocks = choicesChanged && combination.gate !== 'open';
  // A closed choice is said by the combination note; its dry run would only be refused.
  const combinationClosed = choicesChanged && combination.gate === 'closed';

  // The dry run asks about the draft on screen only (settled), and a changed choice only once the
  // combination has answered open — the server refuses a closed one's dry run too.
  const preview = useQuery<LineConfigurationImpact>({
    queryKey: ['line-config-preview', orderId, line.id, debouncedKey],
    queryFn: () => api.previewLineConfiguration(orderId, line.id, JSON.parse(debouncedKey) as LineConfigurationBody),
    enabled: product != null && debouncedKey !== initialKey && settled && !combinationBlocks,
    retry: false,
    staleTime: 30_000,
  });

  const save = useMutation({
    mutationFn: () => api.setLineConfiguration(orderId, line.id, {
      ...body, expected_revision: line.configuration?.revision ?? undefined,
      review_version: preview.data?.review_version ?? undefined,
    }),
    onSuccess: () => {
      invalidateOrderViews(queryClient, { orderId });
      onClose();
    },
    // The combination closed since it was read: read it again, so the marks and reasons say so.
    onError: (err) => {
      if (isCombinationRefusal(err)) {
        void queryClient.invalidateQueries({ queryKey: [COMBINATION_STATE_KEY, line.product_id] });
      }
    },
  });

  const chosenSet = new Set(Object.values(chosen));
  // «Не рахувати» is not a part a line can want (spec workshop-order-issue-followups, rule 34).
  const parts = [...(product?.parts ?? [])].filter((p) => !p.ignored && (!editingSaved || saved.has(p.id))).sort(byOrder);

  /** A count back at what the options give is no longer a change. */
  const setCount = (part: ProductPart, qty: number) =>
    setCounts((prev) => {
      const next = { ...prev };
      if (editingSaved) next[part.id] = qty;
      else if (!isParts && qty === basePer(part, chosenSet, groups)) delete next[part.id];
      else next[part.id] = qty;
      return next;
    });

  const choose = (groupId: number, optionId: number | null) =>
    setChoices((prev) => {
      const next = { ...prev };
      if (optionId == null) delete next[groupId];
      else next[groupId] = optionId;
      return next;
    });

  const reset = () => {
    setChoices(defaultsOf(product));
    setCounts({});
  };

  const nonStandardOptions = (product?.variant_groups ?? []).some(
    (g) => (choices[g.id] ?? g.default_option_id ?? null) !== (g.default_option_id ?? null),
  );
  // An override is what the body carries — a count the chosen options would not give anyway.
  // A draft key the options have since caught up with is no override (review M2).
  const overrides = isParts ? Object.keys(counts).length : Object.keys(body.part_counts).length;
  const canSave =
    saveAllowed({
      productRead: product != null,
      dirty,
      settled,
      fetching: preview.isFetching,
      status: preview.status,
    }) && !combinationBlocks && (!choicesChanged || revision == null || confirmed === bodyKey);
  const pending = save.isPending;

  const subtitle = isParts
    ? t('orders.lineConfig.subtitleParts', { order: orderCode, product: line.product_name })
    : t('orders.lineConfig.subtitle', { order: orderCode, product: line.product_name, qty: line.quantity });

  // Beside Save: why it waits or refuses — or that the line keeps a combination closed since.
  // Save names it as its description (`aria-describedby`), so the reason is read with the button.
  const noteId = `${markId}-note`;
  const combinationNote =
    combination.gate === 'closed' ? (
      <span id={noteId} data-testid="combination-note" className="text-amber-700 dark:text-amber-400">
        {t(choicesChanged ? 'orders.combination.closed' : 'orders.combination.closedKept', {
          reasons: reasonsText(t, combination.reasons),
        })}
      </span>
    ) : combination.gate === 'checking' && choicesChanged ? (
      <span id={noteId} data-testid="combination-note">
        {t('orders.combination.checking')}
      </span>
    ) : combination.isError && choicesChanged ? (
      <span data-testid="combination-note" className="inline-flex flex-wrap items-center gap-2">
        <span id={noteId} className="text-amber-700 dark:text-amber-400">
          {t('orders.combination.failed')}
        </span>
        <Button size="sm" variant="ghost" onClick={() => combination.refetch()}>
          {t('common.retry')}
        </Button>
      </span>
    ) : undefined;

  return (
    <WorkshopDialog
      onClose={onClose}
      title={t('orders.lineConfig.title')}
      subtitle={subtitle}
      size="lg"
      pending={pending}
      error={
        save.isError ? (
          <>
            {save.error.message}
            {isCombinationRefusal(save.error) && save.error.reasons && (
              <span className="block">{reasonsText(t, save.error.reasons)}</span>
            )}
          </>
        ) : undefined
      }
      summary={combinationNote}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={pending}>
            {t('common.cancel')}
          </Button>
          <Button
            onClick={() => (dirty ? save.mutate() : onClose())}
            disabled={!canSave || pending}
            aria-describedby={combinationNote ? noteId : undefined}
          >
            {t('orders.lineConfig.save')}
          </Button>
        </>
      }
    >
      {productQuery.isPending ? (
        <p className="py-6 text-center text-sm text-bambu-gray">{t('common.loading')}</p>
      ) : productQuery.isError && !product ? (
        <div className="space-y-1 py-4 text-sm">
          <p className="text-amber-700 dark:text-amber-400">{t('orders.lineConfig.productFailed')}</p>
          <p className="text-bambu-gray">{productQuery.error.message}</p>
          <Button size="sm" variant="ghost" onClick={() => productQuery.refetch()}>
            {t('common.retry')}
          </Button>
        </div>
      ) : product ? (
        <fieldset disabled={pending} className="m-0 min-w-0 space-y-3 border-0 p-0">
          {productQuery.isError && <RefreshFailedNote onRetry={() => productQuery.refetch()} />}

          {!isParts &&
            (product.variant_groups.length > 0 ? (
              product.variant_groups.map((group) => {
                const current = choices[group.id] ?? group.default_option_id ?? null;
                return (
                  <fieldset
                    key={group.id}
                    className="m-0 flex flex-wrap items-center gap-4 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2.5"
                  >
                    <legend className="float-left mr-2 p-0 text-sm font-semibold text-white">{group.name}</legend>
                    {group.default_option_id == null && (
                      <label className="inline-flex items-center gap-1.5 text-sm text-white">
                        <input
                          type="radio"
                          name={`cfg-group-${group.id}`}
                          checked={current == null}
                          onChange={() => choose(group.id, null)}
                          className="accent-bambu-green"
                        />
                        {t('orders.lineConfig.noChoice')}
                      </label>
                    )}
                    {group.options.map((option) => {
                      // What the line has, and what is chosen, always shows — marked, never off.
                      const { mark, disabled: off } = optionState(combination, option.id, [
                        current,
                        initialChoices[group.id],
                      ]);
                      return (
                        <label
                          key={option.id}
                          title={mark ? t(`orders.combination.option.${mark}`) : undefined}
                          className={`inline-flex items-center gap-1.5 text-sm ${mark ? 'text-amber-700 dark:text-amber-400' : 'text-white'} ${off ? 'cursor-not-allowed opacity-50' : ''}`}
                        >
                          <input
                            type="radio"
                            name={`cfg-group-${group.id}`}
                            checked={current === option.id}
                            disabled={off}
                            aria-describedby={mark ? `${markId}-${mark}` : undefined}
                            onChange={() => choose(group.id, option.id)}
                            className="accent-bambu-green"
                          />
                          {optionLabel(t, option.name, mark)}
                          {option.id === group.default_option_id && (
                            <span className="text-xs text-bambu-gray">{t('orders.lineConfig.standardOption')}</span>
                          )}
                        </label>
                      );
                    })}
                  </fieldset>
                );
              })
            ) : (
              <p className="text-sm text-bambu-gray">{t('orders.lineConfig.noVariants')}</p>
            ))}
          {/* The two descriptions a marked value points at (`aria-describedby`). */}
          {!isParts && product.variant_groups.length > 0 && (
            <>
              <span id={`${markId}-closes`} className="sr-only">
                {t('orders.combination.option.closes')}
              </span>
              <span id={`${markId}-never`} className="sr-only">
                {t('orders.combination.option.never')}
              </span>
            </>
          )}

          {parts.length === 0 ? (
            <p className="text-sm text-bambu-gray">{t('orders.lineConfig.noParts')}</p>
          ) : (
            <div className="rounded-lg border border-bambu-dark-tertiary">
              <WorkshopTableScroll label={t('orders.lineConfig.title')}>
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      {!isParts && <th className={HEAD}>{t('orders.lineConfig.head.inKit')}</th>}
                      <th className={HEAD}>{t('orders.lineConfig.head.part')}</th>
                      <th className={HEAD}>
                        {t(isParts ? 'orders.lineConfig.head.needed' : 'orders.lineConfig.head.perUnit')}
                      </th>
                      {!isParts && (
                        <th className={HEAD}>{t('orders.lineConfig.head.total', { qty: line.quantity })}</th>
                      )}
                      <th className={HEAD}>{t('orders.lineConfig.head.source')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {parts.map((part) => {
                      const base = editingSaved ? (saved.get(part.id) ?? 0) : isParts ? 0 : basePer(part, chosenSet, groups);
                      const shown = (editingSaved && initialCounts[part.id] === counts[part.id]
                        ? undefined
                        : counts[part.id]) ?? base;
                      const off = !isParts && shown === 0;
                      const variant = bindingLabel(groups, part.variant_option_ids);
                      return (
                        <tr
                          key={part.id}
                          data-testid={`config-part-${part.id}`}
                          className={`border-t border-bambu-dark-tertiary text-white ${off ? 'opacity-50' : ''}`}
                        >
                          {!isParts && (
                            <td className={CELL}>
                              <input
                                type="checkbox"
                                checked={shown > 0}
                                aria-label={t('orders.lineConfig.inKitFor', { name: part.name })}
                                onChange={() =>
                                  setCount(part, shown > 0 ? 0 : base > 0 ? base : Math.max(1, part.qty_per_unit))
                                }
                                className="accent-bambu-green"
                              />
                            </td>
                          )}
                          <td className={CELL}>
                            <span className="flex items-start gap-2">
                              <LivePartThumb partId={part.id} image={part.image} name={part.name} kind={part.kind} size="sm" />
                              <span className="min-w-0">
                                <span className="[overflow-wrap:anywhere]">{part.name}</span>
                                {part.kind === 'purchased' && (
                                  <span className={`ml-1.5 ${VARIANT_CHIP}`}>{t('orders.lineConfig.chip.bought')}</span>
                                )}
                                {variant && (
                                  <span className={`ml-1.5 ${VARIANT_CHIP}`}>
                                    {t('orders.lineConfig.chip.variant', { option: variant })}
                                  </span>
                                )}
                                {!isParts && part.id in body.part_counts && (
                                  <span className="ml-1.5 inline-block rounded bg-bambu-green/20 px-1.5 py-px align-[1px] text-[11px] font-medium leading-4 text-bambu-green">
                                    {t('orders.lineConfig.chip.changed')}
                                  </span>
                                )}
                              </span>
                            </span>
                          </td>
                          <td className={`${CELL} whitespace-nowrap`}>
                            <CountInput
                              value={shown}
                              min={0}
                              max={MAX_COUNT}
                              onCommit={(qty) => setCount(part, qty)}
                              ariaLabel={t(isParts ? 'orders.lineConfig.needFor' : 'orders.lineConfig.perUnitFor', {
                                name: part.name,
                              })}
                              className="w-[68px] rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-right tabular-nums text-white focus:border-bambu-green focus:outline-none"
                            />
                            {!isParts && (
                              <span className="ml-2 text-xs text-bambu-gray">
                                {base > 0
                                  ? t('orders.lineConfig.standardN', { n: base })
                                  : t('orders.lineConfig.outsideStandard')}
                              </span>
                            )}
                          </td>
                          {!isParts && (
                            <td className={`${CELL} tabular-nums`}>{shown > 0 ? shown * line.quantity : '—'}</td>
                          )}
                          <td className={CELL}>
                            <SourceCell
                              part={part}
                              sources={sourcesOf.get(part.id)}
                              loading={sources.isPending}
                              failed={sources.isError}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </WorkshopTableScroll>
            </div>
          )}

          {!isParts && parts.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 text-xs text-bambu-gray">
              <span>
                {overrides > 0
                  ? t('orders.lineConfig.hint.changed', { count: overrides })
                  : nonStandardOptions
                    ? t('orders.lineConfig.hint.chosen')
                    : t('orders.lineConfig.hint.standard')}
              </span>
              {(overrides > 0 || nonStandardOptions) && (
                <Button size="sm" variant="ghost" title={t('orders.lineConfig.resetTitle')} onClick={reset}>
                  {t('orders.lineConfig.reset')}
                </Button>
              )}
            </div>
          )}

          {dirty && (
            <>
              <Impact preview={preview} settled={settled} closed={combinationClosed} parts={product?.parts ?? []} />
              {revision != null && choicesChanged && settled && preview.isSuccess && (
                <label className="mt-2 flex items-center gap-2 text-sm text-white">
                  <input type="checkbox" checked={confirmed === bodyKey}
                    onChange={(event) => setConfirmed(event.target.checked ? bodyKey : '')} />
                  {t('orders.lines.confirmVariantKit')}
                </label>
              )}
            </>
          )}
        </fieldset>
      ) : null}
    </WorkshopDialog>
  );
}

/** «Source»: a bought part's purchase; a printed part's first sources, recommended first. */
function SourceCell({
  part,
  sources,
  loading,
  failed,
}: {
  part: ProductPart;
  sources: PartSource[] | undefined;
  loading: boolean;
  failed: boolean;
}) {
  const { t } = useTranslation();
  if (part.kind === 'purchased') return <span className="text-bambu-gray">{t('orders.lineConfig.source.purchase')}</span>;
  if (loading) return <span className="text-bambu-gray">…</span>;
  if (failed) {
    return (
      <span className="text-bambu-gray" title={t('orders.lineConfig.source.failed')}>
        —
      </span>
    );
  }
  // Only what the plan can print from: an unsliced file names a model but is no plate.
  // Two files of one model with the same plate and yield read the same — the second would say
  // nothing, so the cell shows sources a reader can tell apart (recommended first).
  const seen = new Set<string>();
  const list = (sources ?? [])
    .filter((s) => s.sliced)
    .sort((a, b) => Number(b.recommended) - Number(a.recommended))
    .filter((s) => {
      const label = `${s.printer_model ?? ''}|${s.plate_index}|${s.yield}`;
      if (seen.has(label)) return false;
      seen.add(label);
      return true;
    })
    .slice(0, SOURCES_SHOWN);
  if (list.length === 0) return <span className="text-amber-700 dark:text-amber-400">{t('orders.lineConfig.source.none')}</span>;
  return (
    <span className="flex flex-col gap-0.5">
      {list.map((s) => (
        <span key={s.plate_id} className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-bambu-gray">
          {s.printer_model && <span className={MODEL_CHIP}>{s.printer_model}</span>}
          <span>{t('orders.lineConfig.source.plate', { n: s.plate_index, yield: s.yield })}</span>
        </span>
      ))}
    </span>
  );
}

/** The dry run's answer for this draft, said line by line (E5 F07). A part it drops shows the live
 *  product's picture beside its sentence (plan E6, D5): a picture changing does not re-ask the dry run.
 *
 *  A closed choice has no dry run (`closed`: the combination note says why). A dry run the server
 *  refused as `combination_unavailable` — the state said open, failed, or was not asked — is no
 *  failure to retry: asking again can only be refused again, so it says the refusal's own reasons
 *  and offers nothing; Save stays off, as for any dry run without an answer. */
function Impact({
  preview,
  settled,
  closed,
  parts,
}: {
  preview: UseQueryResult<LineConfigurationImpact>;
  settled: boolean;
  closed: boolean;
  parts: ProductPart[];
}) {
  const { t } = useTranslation();
  if (closed) return null;
  if (!settled || preview.isFetching || preview.isPending) {
    return <p className="text-sm text-bambu-gray" data-testid="line-config-impact">{t('orders.lineConfig.checking')}</p>;
  }
  if (preview.isError && isCombinationRefusal(preview.error)) {
    const reasons = preview.error.reasons ?? [];
    return (
      <p className="text-sm text-amber-700 dark:text-amber-400" data-testid="line-config-impact">
        {reasons.length > 0
          ? t('orders.combination.closed', { reasons: reasonsText(t, reasons) })
          : preview.error.message}
      </p>
    );
  }
  if (preview.isError) {
    return (
      <p className="flex flex-wrap items-center gap-2 text-sm" data-testid="line-config-impact">
        <span className="text-amber-700 dark:text-amber-400">{preview.error.message}</span>
        <Button size="sm" variant="ghost" onClick={() => preview.refetch()}>
          {t('common.retry')}
        </Button>
      </p>
    );
  }
  const impact = preview.data;
  const pictureOf = new Map(parts.map((p) => [p.id, p.image]));
  const lines: { key: string; text: string; part?: { id: number; name: string; image?: PartImageRef | null } }[] = [];
  if (impact.reserved_before !== impact.reserved_after) {
    const text = t('orders.lineConfig.impact.reserve', { before: impact.reserved_before, after: impact.reserved_after });
    lines.push({ key: text, text });
  }
  if (impact.finished_before !== impact.finished_after) {
    const text = t('orders.lineConfig.impact.ready', { before: impact.finished_before, after: impact.finished_after });
    lines.push({ key: text, text });
  }
  for (const change of impact.changes ?? []) {
    const text = t('orders.lines.kitChange', { name: change.name, before: change.per_before, after: change.per_after });
    lines.push({ key: `kit-${change.part_id}`, text });
  }
  for (const d of impact.dropping) {
    if (d.printed > 0 || d.queued > 0) {
      const text = t('orders.lineConfig.impact.dropping', { name: d.name, printed: d.printed, queued: d.queued });
      const image = pictureOf.has(d.part_id) ? pictureOf.get(d.part_id) : d.image;
      // Keyed by the part: two parts of one name and the same counts say the same sentence.
      lines.push({ key: `part-${d.part_id}`, text, part: { id: d.part_id, name: d.name, image } });
    }
  }
  return (
    <div className="space-y-0.5 text-sm text-amber-700 dark:text-amber-400" data-testid="line-config-impact">
      {lines.length > 0 ? (
        lines.map((l) =>
          l.part ? (
            <p key={l.key} className="flex items-start gap-1.5">
              <LivePartThumb partId={l.part.id} image={l.part.image} name={l.part.name} size="xs" />
              {l.text}
            </p>
          ) : (
            <p key={l.key}>{l.text}</p>
          ),
        )
      ) : (
        <p>{t('orders.lineConfig.impact.none')}</p>
      )}
    </div>
  );
}
