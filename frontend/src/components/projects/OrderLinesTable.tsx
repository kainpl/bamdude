import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, ClipboardCheck, Clock, ListPlus, Package, Play, Send } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, ProjectLine } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { useAuth } from '../../contexts/AuthContext';
import { getColorName } from '../../utils/colors';
import { ActionConfirm } from '../workshop/ActionConfirm';
import { CardActionMenu, CardActionMenuItem } from '../CardActionMenu';
import { ProgressBar } from './ProgressBar';
import { LinePartsTable } from './LinePartsTable';
import { LineEditDialog } from './LineEditDialog';
import { AddToOrderDialog } from './add-to-order/AddToOrderDialog';
import { Button } from '../Button';
import { LineConfigDialog } from './LineConfigDialog';
import { AddLinePartDialog } from './AddLinePartDialog';
import { CONFIG_ACCENT_CLASS, isNonStandardConfiguration, lineConfigLabel } from './lineConfigLabel';
import { configBlockedReason } from './lineGates';
import { invalidateOrderViews, refreshPartImageRefs } from '../../utils/queryInvalidation';
import { WorkshopTableScroll } from '../workshop/WorkshopPanel';
import { SectionLink } from '../workshop/SectionLink';

const HEAD = 'font-normal text-xs text-bambu-gray px-3 py-2 whitespace-nowrap';
const CELL = 'px-3 py-2.5 align-top';
const HEX = /^#?[0-9a-f]{6}([0-9a-f]{2})?$/i;

/** The configuration caption of B02: what a parts line wants, a non-standard kit, or
 *  «standard configuration» — the first two in amber, the last muted. */
function configCaption(line: ProjectLine, t: ReturnType<typeof useTranslation>['t']) {
  if (line.mode === 'parts') {
    const parts = line.parts.map((p) => `${p.name} × ${p.need}`).join(', ');
    return { text: t('orders.lines.partsOnlyList', { parts }), accent: true };
  }
  const config = line.configuration;
  if (isNonStandardConfiguration(config)) return { text: lineConfigLabel(config, line.mode, t), accent: true };
  return { text: t('orders.lines.standardConfig'), accent: false };
}

interface OrderLinesTableProps {
  order: Order;
  canEdit: boolean;
  /** Delegates to the existing plan editor; never queues from this table. */
  onQueuePlan?: (lineId?: number) => void;
  onRecount?: (line: ProjectLine) => void;
  /** 2 on the order page; 3 in the workspace pane, whose order title is an h2 (E3 B06). */
  headingLevel?: 2 | 3;
}

/**
 * The order's work, one row per line (WS-13 E4 B, C).
 *
 * The table SHOWS: every figure is the server's (design decision 8) and it never
 * counts archives itself. Everything that changes a line opens from the row's menu —
 * the configuration dialog, «Edit line» (`LineEditDialog`, which owns the draft and
 * its rules), reordering and deleting; the expander on the left is the reader's too.
 *
 * ⚠️ **Reordering is TWO patches that swap two `sort_order` values**, not a
 * renumbering of the whole list. There is no bulk-reorder endpoint, and a client
 * that renumbered every row would push its own idea of the order over whatever
 * another session had just done. Sequential rather than parallel: the pair is a
 * swap, and sending both at once means the second can land first.
 *
 * The queue shortcuts delegate to PlanBlock's review and writer. The table never
 * derives a second plan or opens PrintModal with a plate outside that plan.
 */
export function OrderLinesTable({ order, canEdit, headingLevel = 2, onQueuePlan, onRecount }: OrderLinesTableProps) {
  const { t } = useTranslation();
  const Heading = headingLevel === 3 ? 'h3' : 'h2';
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<ProjectLine | null>(null);
  const [deleting, setDeleting] = useState<ProjectLine | null>(null);
  const [configuringId, setConfiguringId] = useState<number | null>(null);
  const configuring = order.lines.find((line) => line.id === configuringId);
  const [addingPart, setAddingPart] = useState<ProjectLine | null>(null);
  const [adding, setAdding] = useState(false);
  const orderActive = order.status === 'active';

  // `sort_order` is the authority and `id` only breaks its ties, so two lines that
  // share a position still come out in a stable order.
  const lines = [...order.lines].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);

  const invalidate = () => {
    // ⚠️ The whole set: a line is what the plan block plans, and a released
    // reservation moved the product's shelf (Ruling 29).
    invalidateOrderViews(queryClient, { orderId: order.id });
  };

  const swap = useMutation({
    mutationFn: async ({ a, b }: { a: ProjectLine; b: ProjectLine }) => {
      await api.updateOrderLine(order.id, a.id, { sort_order: b.sort_order });
      await api.updateOrderLine(order.id, b.id, { sort_order: a.sort_order });
    },
    // ⚠️ `onSettled`, NOT `onSuccess`: a swap is two writes and the first can land
    // while the second does not. The server then holds two lines with the SAME
    // `sort_order` — on `onSuccess` alone the table would keep drawing the pre-swap
    // order for ever. Refetching whatever happened is the only honest answer.
    onSettled: invalidate,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (lineId: number) => api.deleteOrderLine(order.id, lineId),
    onSuccess: (_answer, lineId) => {
      invalidate();
      // A one-off product goes with its last line, and SQLite gives its parts' ids to the next parts:
      // its cached pictures go now, not when the socket says so (spec procurement-stock-images A16).
      const productId = order.lines.find((line) => line.id === lineId)?.product_id;
      refreshPartImageRefs(queryClient, productId == null ? 'all' : [productId]);
      setDeleting(null);
    },
    // A refusal is the confirmation's to show, in its own dialog (WS-13 E13 E02).
  });

  const toggleExpanded = (lineId: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(lineId)) next.delete(lineId);
      else next.add(lineId);
      return next;
    });

  const busy = swap.isPending || remove.isPending;
  const canQueue = orderActive && onQueuePlan != null;

  return (
    <section className="space-y-3">
      {/* WS-13 E3 E06: the heading carries the count; the table has no card of its
          own inside the order's main panel — only its own scroll region. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Heading className="text-lg leading-7 font-semibold text-white">
          {t('orders.lines.title')} <small className="text-sm font-normal text-bambu-gray">({order.lines.length})</small>
        </Heading>
        <div className="flex flex-wrap items-center gap-2">
          {canQueue && (
            <Button size="sm" variant="secondary" onClick={() => onQueuePlan?.()}>
              <Send className="w-4 h-4" />
              {t('orders.plan.queueOrder')}
            </Button>
          )}
          {canEdit && (
            <Button size="sm" onClick={() => setAdding(true)}>
              <ListPlus className="w-4 h-4" />
              {t('orders.lines.addToOrder')}
            </Button>
          )}
        </div>
      </div>

      <WorkshopTableScroll label={t('orders.lines.title')}>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left">
              <th className={`${HEAD} w-8 pr-0`} />
              <th className={HEAD}>{t('orders.lines.productConfig')}</th>
              <th className={HEAD}>{t('orders.lines.quantity')}</th>
              <th className={HEAD}>{t('orders.lines.materialColor')}</th>
              <th className={`${HEAD} min-w-[160px]`}>{t('orders.lines.progress')}</th>
              <th className={`${HEAD} w-px`}>
                <span className="sr-only">{t('orders.lines.actions')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {lines.length === 0 && (
              <tr>
                <td colSpan={6} className="p-4 text-bambu-gray border-t border-bambu-dark-tertiary">
                  {t('orders.lines.empty')}
                </td>
              </tr>
            )}

            {lines.map((line, index) => {
              const open = expanded.has(line.id);
              const caption = configCaption(line, t);
              const blocked = configBlockedReason(order, line, t);
              const colorKnown = line.color != null && HEX.test(line.color);
              return [
                <tr key={line.id} data-line={line.id} className="border-t border-bambu-dark-tertiary text-white">
                  <td className={`${CELL} w-8 pr-0`}>
                    <button
                      type="button"
                      data-testid={`line-${line.id}-expand`}
                      onClick={() => toggleExpanded(line.id)}
                      aria-expanded={open}
                      aria-label={open ? t('orders.lines.collapse') : t('orders.lines.expand')}
                      title={open ? t('orders.lines.collapse') : t('orders.lines.expand')}
                      className="p-1 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
                    >
                      {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                    </button>
                  </td>
                  <td className={CELL}>
                    <div className="flex items-center gap-3">
                      <span
                        data-testid={`line-${line.id}-thumb`}
                        className="w-10 h-10 flex-shrink-0 rounded-lg bg-bambu-dark-tertiary text-bambu-gray grid place-items-center overflow-hidden"
                      >
                        {line.product_has_cover ? (
                          <img
                            src={api.getProductCoverImageUrl(line.product_id)}
                            alt=""
                            className="w-full h-full object-cover"
                          />
                        ) : (
                          <Package className="w-[18px] h-[18px]" aria-hidden />
                        )}
                      </span>
                      <div className="min-w-0">
                        <SectionLink to={`/products/${line.product_id}`} className="font-medium text-white hover:underline">
                          {line.product_name}
                        </SectionLink>
                        {(line.product_origin ?? 'catalog') !== 'catalog' && (
                          <span className="ml-1.5 inline-flex items-center rounded px-2 py-0.5 text-xs font-medium whitespace-nowrap bg-gray-200 dark:bg-gray-500/20 text-gray-600 dark:text-gray-400">
                            {t('orders.lines.oneOff')}
                          </span>
                        )}
                        {line.configuration?.origin && (
                          <small className="block text-xs text-bambu-gray">{t(
                            line.configuration.origin === 'migration'
                              ? 'orders.lines.compositionMigrated'
                              : 'orders.lines.compositionAtOrder',
                          )}</small>
                        )}
                        <small className="block mt-0.5 text-xs text-bambu-gray" data-testid={`line-${line.id}-config`}>
                          {line.product_sku && (
                            <>
                              <span className="font-mono text-xs">{line.product_sku}</span>
                              {' · '}
                            </>
                          )}
                          {caption.accent ? (
                            <span data-config-accent className={CONFIG_ACCENT_CLASS}>
                              {caption.text}
                            </span>
                          ) : (
                            caption.text
                          )}
                        </small>
                        {line.note && (
                          <small className="block mt-0.5 text-xs text-bambu-gray" data-testid={`line-${line.id}-note`}>
                            {`«${line.note}»`}
                          </small>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className={`${CELL} tabular-nums`}>
                    <span data-testid={`line-${line.id}-quantity`}>
                      {line.mode === 'parts'
                        ? t('orders.lines.partsQty', { count: line.quantity })
                        : t('orders.lines.unitsQty', { count: line.quantity })}
                    </span>
                    {(line.from_finished > 0 || line.from_kit_units > 0) && (
                      <small className="block mt-0.5 text-xs text-bambu-gray" data-testid={`line-${line.id}-from-stock-shown`}>
                        {t('orders.lines.fromStock', {
                          list: [
                            line.from_finished > 0 && t('orders.lines.fromStockReady', { count: line.from_finished }),
                            line.from_kit_units > 0 && t('orders.lines.fromStockKits', { count: line.from_kit_units }),
                          ]
                            .filter(Boolean)
                            .join(' · '),
                        })}
                      </small>
                    )}
                    {(line.held > 0 || line.issued > 0) && (
                      <small className="block mt-0.5 text-xs text-bambu-green" data-testid={`line-${line.id}-issued`}>
                        {t('orders.lines.heldIssued', { held: line.held, issued: line.issued })}
                      </small>
                    )}
                  </td>
                  <td className={CELL} data-testid={`line-${line.id}-material`}>
                    {line.material || <span className="text-bambu-gray">{t('orders.lines.anyMaterial')}</span>}
                    <small className="block mt-0.5 text-xs" data-testid={`line-${line.id}-color`}>
                      {line.color ? (
                        colorKnown ? (
                          <>
                            <span
                              data-swatch
                              aria-hidden
                              className="inline-block w-2.5 h-2.5 rounded-full border border-bambu-gray mr-1.5 align-[-1px]"
                              style={{ backgroundColor: line.color.startsWith('#') ? line.color.slice(0, 7) : `#${line.color.slice(0, 6)}` }}
                            />
                            {getColorName(line.color)}
                          </>
                        ) : (
                          line.color
                        )
                      ) : (
                        <span className="text-bambu-gray">{t('orders.lines.anyColor')}</span>
                      )}
                    </small>
                  </td>
                  <td className={`${CELL} min-w-[160px]`}>
                    <ProgressBar
                      value={line.covered_units}
                      max={line.quantity}
                      progress={line.progress}
                      label={`${line.covered_units} / ${line.quantity}`}
                      caption="percent"
                      testId={`line-${line.id}-progress`}
                    />
                    <small className="block mt-1 text-xs text-bambu-gray tabular-nums">
                      <span data-testid={`line-${line.id}-coverage-sources`}>
                        {line.mode === 'parts' || line.from_stock_units === 0
                          ? t('orders.lines.printedOnly', { printed: line.units_printed })
                          : t('orders.lines.coverageSources', { printed: line.units_printed, stock: line.from_stock_units })}
                      </span>
                      {(line.prints_in_progress > 0 || line.prints_queued > 0) && (
                        <span data-testid={`line-${line.id}-live`}>
                          <span className="sr-only">
                            {t('orders.lines.live', { printing: line.prints_in_progress, queued: line.prints_queued })}
                          </span>
                          <span aria-hidden className="inline-flex items-center gap-1">
                            {' · '}
                            <Play className="w-3 h-3" />
                            {line.prints_in_progress}
                            <Clock className="w-3 h-3 ml-1" />
                            {line.prints_queued}
                          </span>
                        </span>
                      )}
                    </small>
                  </td>
                  <td className={`${CELL} w-px text-right whitespace-nowrap`}>
                    {(canEdit || canQueue) && (
                      <CardActionMenu
                        label={t('orders.lines.menu', { product: line.product_name })}
                        width="max-content"
                        estimatedHeight={240}
                      >
                        {(close) => (
                          <>
                            {canQueue && (
                              <CardActionMenuItem onSelect={() => { close(); onQueuePlan?.(line.id); }}>
                                {t('orders.plan.queueLine')}
                              </CardActionMenuItem>
                            )}
                            {canEdit && (
                              <>
                                <CardActionMenuItem
                                  disabled={blocked != null}
                                  title={blocked ?? undefined}
                                  onSelect={() => {
                                    close();
                                    setConfiguringId(line.id);
                                  }}
                                >
                                  {t('orders.lines.menuConfigure')}
                                </CardActionMenuItem>
                                {orderActive && hasPermission('products:read') && (
                                  <CardActionMenuItem
                                    disabled={blocked != null}
                                    title={blocked ?? undefined}
                                    onSelect={() => {
                                      close();
                                      setAddingPart(line);
                                    }}
                                  >
                                    {t('orders.lines.addLinePart')}
                                  </CardActionMenuItem>
                                )}
                                <CardActionMenuItem
                                  onSelect={() => {
                                    close();
                                    setEditing(line);
                                  }}
                                >
                                  {t('orders.lines.menuEdit')}
                                </CardActionMenuItem>
                                <CardActionMenuItem
                                  disabled={index === 0 || busy}
                                  onSelect={() => {
                                    close();
                                    swap.mutate({ a: line, b: lines[index - 1] });
                                  }}
                                >
                                  {t('orders.lines.moveUp')}
                                </CardActionMenuItem>
                                <CardActionMenuItem
                                  disabled={index === lines.length - 1 || busy}
                                  onSelect={() => {
                                    close();
                                    swap.mutate({ a: line, b: lines[index + 1] });
                                  }}
                                >
                                  {t('orders.lines.moveDown')}
                                </CardActionMenuItem>
                                <div role="separator" className="my-1 border-t border-bambu-dark-tertiary" />
                                <CardActionMenuItem
                                  danger
                                  onSelect={() => {
                                    close();
                                    setDeleting(line);
                                  }}
                                >
                                  {t('orders.lines.delete')}
                                </CardActionMenuItem>
                              </>
                            )}
                          </>
                        )}
                      </CardActionMenu>
                    )}
                  </td>
                </tr>,
                open ? (
                  <tr key={`${line.id}-parts`}>
                    <td className="bg-bambu-dark-tertiary/30" />
                    <td colSpan={5} className="bg-bambu-dark-tertiary/30 px-3 py-1">
                      <LinePartsTable parts={line.parts} purchased={line.purchased ?? []} mode={line.mode} />
                      {onRecount && orderActive && line.parts.length > 0 && <div className="py-3">
                        <Button variant="secondary" size="sm" className="border-bambu-green/50" onClick={() => onRecount(line)}>
                          <ClipboardCheck className="h-4 w-4" />{t('orderRecount.action')}
                        </Button>
                      </div>}
                    </td>
                  </tr>
                ) : null,
              ];
            })}
          </tbody>
        </table>
      </WorkshopTableScroll>

      {adding && (
        <AddToOrderDialog
          order={{ id: order.id, code: order.code, name: order.name, active: orderActive }}
          onClose={() => setAdding(false)}
        />
      )}

      {editing && (
        <LineEditDialog
          order={order}
          // The CURRENT line; the dialog's draft is its own from the moment it opened.
          line={order.lines.find((l) => l.id === editing.id) ?? editing}
          onClose={() => setEditing(null)}
          onConfigure={() => {
            setConfiguringId(editing.id);
            setEditing(null);
          }}
        />
      )}

      {configuring && (
        <LineConfigDialog orderId={order.id} orderCode={order.code} line={configuring} onClose={() => setConfiguringId(null)} />
      )}
      {addingPart && (
        <AddLinePartDialog
          orderId={order.id}
          line={addingPart}
          onClose={() => setAddingPart(null)}
          onSaved={() => {
            setExpanded((prev) => new Set([...prev, addingPart.id]));
            invalidate();
            setAddingPart(null);
          }}
        />
      )}

      {deleting && (
        <ActionConfirm
          title={t('orders.lines.confirmDeleteTitle', { product: deleting.product_name })}
          body={<p className="text-sm text-bambu-gray">{t('orders.lines.confirmDelete')}</p>}
          primaryLabel={t('orders.lines.confirmDeleteAction')}
          danger
          send={() => remove.mutateAsync(deleting.id)}
          onClose={() => setDeleting(null)}
        />
      )}
    </section>
  );
}
