import { useState } from 'react';
import { NumberInput } from '../NumberInput';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { Order, ProjectLine, ProjectLineUpdate, StockSuggestItem } from '../../api/client';
import { LivePartThumb } from '../PartThumb';
import { useToast } from '../../contexts/ToastContext';
import { useAuth } from '../../contexts/AuthContext';
import { canReadStock, canTakeStock } from '../../utils/workshopRights';
import { useProductDetail } from '../../hooks/useProductDetail';
import { useProductStock } from '../../hooks/useProductStock';
import { useConfigurationKits } from '../../hooks/useConfigurationKits';
import { invalidateOrderViews } from '../../utils/queryInvalidation';
import { getColorName } from '../../utils/colors';
import { Button } from '../Button';
import { ActionConfirm } from '../workshop/ActionConfirm';
import { Select } from '../Select';
import { WorkshopDialog } from '../workshop/WorkshopDialog';
import { WorkshopField, WorkshopFormGrid } from '../workshop/WorkshopFormGrid';
import { lineConfigLabel } from './lineConfigLabel';
import { configBlockedReason, liveKits, stockMoved } from './lineGates';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none disabled:opacity-50';

/** What the dialog is holding, before anything is sent. */
interface Draft {
  quantity: number;
  material: string;
  color: string;
  note: string;
  /** Kits this line takes off the shelf (pass 8, Decision 4). */
  fromStock: number;
  /** Ready units off the finished-goods shelf (spec workshop-add-to-order, rule 13). */
  fromFinished: number;
}

function draftOf(line: ProjectLine): Draft {
  return {
    quantity: line.quantity,
    material: line.material ?? '',
    color: line.color ?? '',
    note: line.note ?? '',
    // ⚠️ The KITS, not `from_stock_units`: in the figures that is every unit
    // from stock, ready units included (rule 14); the request field of the same
    // name is kits alone.
    fromStock: liveKits(line),
    fromFinished: line.from_finished,
  };
}

/** The line as `/stock/suggest` asks about it — its own reservation counts as free for it. */
function suggestItemFor(line: ProjectLine, quantity: number): StockSuggestItem {
  const config = line.configuration;
  const item: StockSuggestItem = {
    product_id: line.product_id,
    options: config ? config.choices.map((c) => c.option_id) : [],
    quantity,
    line_id: line.id,
  };
  if (config && config.changed_parts.length > 0) {
    item.part_counts = Object.fromEntries(config.changed_parts.map((p) => [p.part_id, p.qty]));
  }
  return item;
}

/**
 * Only what the operator actually changed — judged against the draft taken when the
 * dialog OPENED, never against the line as it is now (final review M3).
 *
 * A PATCH carrying every field would overwrite a value somebody else edited
 * between this dialog opening and Save being pressed; so would comparing the
 * opening draft with a line that was re-read meanwhile — a note, a material or a
 * kit assembled elsewhere would be written back for a box the operator never
 * touched. An empty box means null on the wire, never `""`.
 *
 * ⚠️ The material is upper-cased HERE: the server stores it verbatim, so a row
 * that arrived holding a lower-case `petg` (typed before this page existed)
 * would otherwise keep failing to match the plates, which spell theirs
 * upper-case. Saving such a row repairs it — the one field sent although the
 * operator did not move it, and only while nobody else has changed it since.
 */
function changedFields(initial: Draft, draft: Draft, line: ProjectLine): ProjectLineUpdate {
  const patch: ProjectLineUpdate = {};
  if (draft.quantity !== initial.quantity) patch.quantity = draft.quantity;
  const material = draft.material.trim().toUpperCase() || null;
  if (draft.material !== initial.material) patch.material = material;
  else if ((line.material ?? '') === initial.material && material !== (line.material ?? null)) patch.material = material;
  if (draft.color !== initial.color) patch.color = draft.color.trim() || null;
  if (draft.note !== initial.note) patch.note = draft.note.trim() || null;
  // ⚠️ Sent only when the operator MOVED the box. On this field alone, absent
  // means "leave the reservation alone" and a number means "rewrite it"
  // (release + reserve in one server transaction) — so restating the current
  // value would burn a rewrite, and with it the ledger rows that record one,
  // on every save that touched a note.
  if (draft.fromStock !== initial.fromStock) patch.from_stock_units = draft.fromStock;
  // The same rule for the ready units: absent leaves them alone.
  if (draft.fromFinished !== initial.fromFinished) patch.from_finished = draft.fromFinished;
  return patch;
}

function sameDraft(a: Draft, b: Draft): boolean {
  return (
    a.quantity === b.quantity &&
    a.material === b.material &&
    a.color === b.color &&
    a.note === b.note &&
    a.fromStock === b.fromStock &&
    a.fromFinished === b.fromFinished
  );
}

/** The product's values, then the line's own ones the product does not offer. */
function withCurrent(values: string[], ...own: string[]): string[] {
  return own.reduce((list, value) => (value && !list.includes(value) ? [...list, value] : list), values);
}

const HEX = /^#?[0-9a-f]{6}([0-9a-f]{2})?$/i;

interface LineEditDialogProps {
  order: Order;
  line: ProjectLine;
  onClose: () => void;
  /** Open the configuration dialog (F05) for this line — «Change part quantities…». */
  onConfigure: () => void;
}

/**
 * «Edit line» (WS-13 E4 D, F03).
 *
 * The draft, the PATCH of changed fields, the stock ceilings and the locks are the
 * table's former inline editor, moved here WITHOUT changing a rule; the table only
 * shows. The draft is this dialog's own state from the moment it opens: a background
 * re-read of the order or the product never overwrites what the operator typed.
 *
 * ⚠️ **A parts line's quantities are not edited here.** A part count moves the
 * reservation and the surplus, and only the configuration dialog's dry run shows
 * that — so this dialog lists the parts and hands over to it, asking first when the
 * draft would be lost. Material, colour and note stay here for both kinds of line.
 */
export function LineEditDialog({ order, line, onClose, onConfigure }: LineEditDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [initial] = useState<Draft>(() => draftOf(line));
  const [draft, setDraft] = useState<Draft>(initial);
  const [error, setError] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);
  // ⚠️ **What is typed is text until the box is left** (final review I1). The quantity's
  // rule — a floor, and «lowering takes kits first» — applied per keystroke turned
  // retyping 10 → 12 into «1» and lowered the reservation to one kit for good; on a
  // moved line «12» became «42». The rule runs when the box is left, and on Save.
  const [quantityText, setQuantityText] = useState<string | null>(null);
  const [finishedText, setFinishedText] = useState<string | null>(null);

  // Only an active order takes ready units (spec workshop-add-to-order, rule 7).
  const orderActive = order.status === 'active';
  const partsMode = line.mode === 'parts';
  const moved = stockMoved(line);
  const quantityFloor = moved ? Math.max(1, line.issued + line.held) : 1;
  const configLabel = lineConfigLabel(line.configuration, line.mode, t);
  const blocked = configBlockedReason(order, line, t);

  const product = useProductDetail(line.product_id);
  // The shelf's figures are the stock's read, taking from it the stock's move (WS-13 E13 O06):
  // without them the dialog asks no figure and offers no stock field — a lower quantity still
  // gives the line's kits back (O23).
  const { hasPermission } = useAuth();
  const readsStock = canReadStock(hasPermission);
  const takes = readsStock && canTakeStock(hasPermission);
  // The shelf of the product. A configured line holds its own kit, not the product's
  // standard one — its kits are asked of the configuration.
  const config = line.configuration;
  const nonStandard =
    config != null && (config.choices.some((c) => !c.is_default) || config.changed_parts.length > 0);
  const stockQuery = useProductStock(partsMode || !readsStock ? null : line.product_id);
  const kitsQuery = useConfigurationKits(
    partsMode || !readsStock ? null : line.product_id,
    nonStandard && config
      ? {
          options: config.choices.map((c) => c.option_id),
          counts: Object.fromEntries(config.changed_parts.map((p) => [p.part_id, p.qty])),
        }
      : null,
  );
  const kitsSource = nonStandard ? kitsQuery : stockQuery;
  const freeKits = kitsSource.data?.kits_available;
  // Ready units free in the line's own position, its own reservation included —
  // asked of the ONE picking function, as «Pick from stock» is.
  const takesFinished = !partsMode && orderActive;
  const suggest = useQuery({
    queryKey: ['stock-suggest', 'line', line.id, line.config_key],
    queryFn: () => api.suggestStock([suggestItemFor(line, line.quantity)]),
    enabled: takesFinished && readsStock,
  });
  const freeFinished = suggest.data?.items?.[0]?.finished_free;
  // While a figure is unknown the ceiling is what the line itself holds — the box never
  // offers what nobody has confirmed; the hint says the figure is still coming.
  const editFreeKits = freeKits ?? 0;
  const editFreeFinished = freeFinished ?? line.from_finished;

  const pick = useMutation({
    mutationFn: (quantity: number) => api.suggestStock([suggestItemFor(line, quantity)]),
    onSuccess: (answer) => {
      const s = answer.items?.[0];
      if (!s) return;
      setDraft((d) => ({ ...d, fromFinished: s.from_finished, fromStock: s.from_kits }));
    },
    onError: (e: Error) => setError(e.message),
  });

  const save = useMutation({
    mutationFn: (data: ProjectLineUpdate) => api.updateOrderLine(order.id, line.id, data),
    onSuccess: (saved, data) => {
      // ⚠️ The whole set: a line's quantity, material or colour is what the plan block
      // plans, and a rewritten reservation moved the product's shelf — `ORDER_VIEW_KEYS`
      // carries those keys (Ruling 29), so they are not invalidated a second time here.
      invalidateOrderViews(queryClient, { orderId: order.id });
      const after = saved.lines.find((l) => l.id === line.id);
      if (after && data.from_finished != null && after.from_finished < data.from_finished) {
        showToast(t('stock.line.clampedFinished', { count: after.from_finished }), 'warning');
      }
      // What was ACTUALLY reserved can be less than what was asked — the shelf may have
      // emptied since the dialog opened. The dialog is closing, so the number is said.
      if (after && data.from_stock_units != null && after.from_kit_units < data.from_stock_units) {
        showToast(t('stock.line.clamped', { n: after.from_kit_units }), 'warning');
      }
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  const busy = save.isPending || pick.isPending;

  /** The quantity's rule, applied to a typed value. */
  const withQuantity = (d: Draft, raw: string): Draft => {
    if (moved) {
      // Moved stock is not rewritten: the quantity alone changes, never below what
      // went out and what waits on the shelf; the server fits the kits under it.
      return { ...d, quantity: Math.max(quantityFloor, Number(raw) || quantityFloor) };
    }
    // ⚠️ Lowering the quantity lowers the reservation with it. A line for two units
    // holding five kits is not a reservation, it is stock taken out of circulation —
    // the server clamps it away anyway (Ruling 16). Kits go back first, then ready
    // units (spec rule 6).
    const quantity = Math.max(1, Number(raw) || 1);
    // ⚠️ Only an ACTIVE order's ready units shrink with it: a completed one shipped
    // them, and a PATCH naming them there is refused (final review I3).
    const fromFinished = orderActive ? Math.min(d.fromFinished, quantity) : d.fromFinished;
    return { ...d, quantity, fromFinished, fromStock: Math.min(d.fromStock, Math.max(0, quantity - fromFinished)) };
  };

  /** The ready units' rule: within what is free and the quantity; kits fit under them. */
  const withFinished = (d: Draft, raw: string): Draft => {
    const fromFinished = Math.min(Math.max(0, Number(raw) || 0), Math.min(editFreeFinished, d.quantity));
    return { ...d, fromFinished, fromStock: Math.min(d.fromStock, d.quantity - fromFinished) };
  };

  /** The draft with every box that is still being typed in settled by its rule. */
  const settle = (): Draft => {
    let d = draft;
    if (quantityText != null) d = withQuantity(d, quantityText);
    if (finishedText != null) d = withFinished(d, finishedText);
    if (quantityText != null || finishedText != null) {
      setQuantityText(null);
      setFinishedText(null);
      setDraft(d);
    }
    return d;
  };

  /**
   * Save, unless there is nothing to save. A `PATCH {}` asks the server to change
   * nothing and bumps the order's `updated_at`; pressing Save on an untouched draft
   * is the same gesture as Cancel, so it does the same thing.
   */
  const submit = () => {
    const data = changedFields(initial, settle(), line);
    if (Object.keys(data).length === 0) {
      onClose();
      return;
    }
    setError(null);
    save.mutate(data);
  };

  /** D03 / R01: to the configuration — straight from a clean draft, asked from a dirty one. */
  const toConfiguration = () => {
    if (sameDraft(settle(), initial)) onConfigure();
    else setLeaving(true);
  };

  // D02: the product's values, the line's own (as it opened) and the draft's (final review M4).
  const materials = withCurrent(product.data?.materials ?? [], initial.material, draft.material);
  const colors = withCurrent(product.data?.colors ?? [], initial.color, draft.color);
  const optionsNote = product.isPending
    ? t('orders.lineEdit.optionsLoading')
    : product.isError && !product.data
      ? t('orders.lineEdit.optionsFailed')
      : null;

  /** The availability hint of a stock box: unknown is never shown as a zero. */
  const availability = (
    source: { isPending: boolean; isError: boolean; data?: unknown },
    known: () => string,
  ): string => {
    if (source.data !== undefined) return known();
    if (source.isError) return t('orders.lineEdit.availabilityFailed');
    return t('orders.lineEdit.availabilityLoading');
  };

  const subtitle = `${line.product_name} · ${configLabel || t('orders.lines.standardConfig')}`;

  return (
    <>
      <WorkshopDialog
        title={t('orders.lineEdit.title')}
        subtitle={subtitle}
        size="lg"
        pending={save.isPending}
        error={error ?? undefined}
        onClose={onClose}
        footer={
          <>
            <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
              {t('orders.lineEdit.cancel')}
            </Button>
            <Button onClick={submit} disabled={busy}>
              {t('orders.lineEdit.save')}
            </Button>
          </>
        }
      >
        <WorkshopFormGrid>
          {partsMode ? (
            <div className="col-span-full flex flex-col gap-2">
              <p className="text-sm text-bambu-gray-light">{t('orders.lineEdit.parts')}</p>
              <ul className="text-sm text-white space-y-0.5">
                {line.parts.map((p) => (
                  <li key={p.part_id} className="flex items-center gap-2">
                    <LivePartThumb partId={p.part_id} image={p.image} name={p.name} size="sm" />
                    {`${p.name} × ${p.qty_per_unit}`}
                  </li>
                ))}
              </ul>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={toConfiguration}
                  disabled={blocked != null}
                  title={blocked ?? undefined}
                >
                  {t('orders.lineEdit.changeParts')}
                </Button>
                {blocked && <span className="text-xs text-bambu-gray">{blocked}</span>}
              </div>
            </div>
          ) : (
            <>
              <WorkshopField
                label={t('orders.lineEdit.quantity')}
                htmlFor={`line-${line.id}-quantity`}
                // M9: a moved line's floor is said, not only enforced.
                hint={moved ? t('orders.lineEdit.quantityFloor', { count: quantityFloor }) : undefined}
              >
                <input
                  id={`line-${line.id}-quantity`}
                  type="number"
                  min={quantityFloor}
                  value={quantityText ?? draft.quantity}
                  aria-describedby={moved ? `line-${line.id}-quantity-hint` : undefined}
                  onChange={(e) => setQuantityText(e.target.value)}
                  onBlur={() => settle()}
                  className={FIELD_CLASS}
                />
              </WorkshopField>
              <div aria-hidden className="max-[761px]:hidden" />
              {!takes ? null : moved ? (
                <MovedKits line={line} draft={draft} setDraft={setDraft} />
              ) : (
                <>
                  <WorkshopField
                    label={t('orders.lineEdit.fromFinished')}
                    htmlFor={`line-${line.id}-from-finished`}
                    hint={
                      !orderActive
                        ? t('stock.line.finishedActiveOnly')
                        : availability(suggest, () =>
                            t('orders.lineEdit.finishedAvailable', {
                              count: editFreeFinished,
                              config: configLabel || t('orders.lineConfig.standard'),
                            }),
                          )
                    }
                  >
                    <input
                      id={`line-${line.id}-from-finished`}
                      type="number"
                      min={0}
                      max={Math.min(editFreeFinished, draft.quantity)}
                      value={finishedText ?? draft.fromFinished}
                      disabled={!orderActive}
                      aria-describedby={`line-${line.id}-from-finished-hint`}
                      onChange={(e) => setFinishedText(e.target.value)}
                      onBlur={() => settle()}
                      className={FIELD_CLASS}
                    />
                  </WorkshopField>
                  <KitsField
                    line={line}
                    draft={draft}
                    setDraft={setDraft}
                    // ⚠️ The ceiling is what is FREE plus what this line already holds —
                    // an edit releases its own reservation before making the new one.
                    pool={editFreeKits + line.from_kit_units}
                    hint={availability(kitsSource, () =>
                      t('orders.lineEdit.kitsAvailable', { count: editFreeKits + line.from_kit_units }),
                    )}
                  />
                  {orderActive && (
                    <div className="col-span-full flex flex-wrap items-center gap-2">
                      <Button variant="ghost" size="sm" onClick={() => pick.mutate(settle().quantity)} disabled={busy}>
                        {t('stock.line.pick')}
                      </Button>
                      <span className="text-xs text-bambu-gray">{t('orders.lineEdit.pickHint')}</span>
                    </div>
                  )}
                </>
              )}
            </>
          )}

          <WorkshopField
            label={t('orders.lineEdit.material')}
            htmlFor={`line-${line.id}-material`}
            hint={optionsNote ?? t('orders.lineEdit.materialHint')}
          >
            <Select
              id={`line-${line.id}-material`}
              className="w-full"
              value={draft.material}
              aria-describedby={`line-${line.id}-material-hint`}
              onChange={(e) => setDraft({ ...draft, material: e.target.value })}
            >
              {materials.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
              <option value="">{t('orders.lineEdit.any')}</option>
            </Select>
          </WorkshopField>
          <WorkshopField
            label={t('orders.lineEdit.color')}
            htmlFor={`line-${line.id}-color`}
            hint={optionsNote ?? t('orders.lineEdit.colorHint')}
          >
            <Select
              id={`line-${line.id}-color`}
              className="w-full"
              value={draft.color}
              aria-describedby={`line-${line.id}-color-hint`}
              onChange={(e) => setDraft({ ...draft, color: e.target.value })}
            >
              {colors.map((c) => (
                <option key={c} value={c}>
                  {HEX.test(c) ? getColorName(c) : c}
                </option>
              ))}
              <option value="">{t('orders.lineEdit.any')}</option>
            </Select>
          </WorkshopField>
          <WorkshopField label={t('orders.lineEdit.note')} htmlFor={`line-${line.id}-note`} full>
            <input
              id={`line-${line.id}-note`}
              type="text"
              value={draft.note}
              onChange={(e) => setDraft({ ...draft, note: e.target.value })}
              className={FIELD_CLASS}
            />
          </WorkshopField>
        </WorkshopFormGrid>
      </WorkshopDialog>

      {leaving && (
        <ActionConfirm
          title={t('orders.lineEdit.discardTitle')}
          body={<p className="text-sm text-bambu-gray">{t('orders.lineEdit.discardMessage')}</p>}
          primaryLabel={t('orders.lineEdit.discardConfirm')}
          cancelLabel={t('orders.lineEdit.stay')}
          danger
          send={async () => {
            setLeaving(false);
            onConfigure();
          }}
          onClose={() => setLeaving(false)}
        />
      )}
    </>
  );
}

/** The kits box of a line whose stock has NOT moved — up to the pool, under the ready units. */
function KitsField({
  line,
  draft,
  setDraft,
  pool,
  hint,
}: {
  line: ProjectLine;
  draft: Draft;
  setDraft: (d: Draft) => void;
  pool: number;
  hint: string;
}) {
  const { t } = useTranslation();
  // Ready units come first; kits fit under what is left (spec workshop-add-to-order, rule 6).
  const kitsRoom = Math.max(0, draft.quantity - draft.fromFinished);
  return (
    <WorkshopField label={t('orders.lineEdit.fromKits')} htmlFor={`line-${line.id}-from-stock`} hint={hint}>
      <NumberInput
        id={`line-${line.id}-from-stock`}
        min={0}
        max={Math.min(pool, kitsRoom)}
        value={draft.fromStock}
        aria-describedby={`line-${line.id}-from-stock-hint`}
        // ⚠️ **The DRAFT is clamped, not the display** (finding I2): while the ceiling was
        // applied only to `value`, the box showed 2 and the draft held the 7 that was
        // typed — the save sent 7 and the server clamped it back. The field reports only
        // a number inside its bounds and settles an out-of-range one on blur, so the draft
        // never holds more than the ceiling. `changedFields` still sends the field only
        // when it MOVED.
        onChange={(v) => setDraft({ ...draft, fromStock: v })}
        fallback={0}
        className={FIELD_CLASS}
      />
    </WorkshopField>
  );
}

/** A moved line's kits: they only come DOWN — back onto the shelf (followups, rule 42). */
function MovedKits({ line, draft, setDraft }: { line: ProjectLine; draft: Draft; setDraft: (d: Draft) => void }) {
  const { t } = useTranslation();
  const live = liveKits(line);
  if (live === 0) {
    return <p className="col-span-full text-xs text-bambu-gray">{t('orders.lineEdit.movedNoKits')}</p>;
  }
  return (
    <WorkshopField
      label={t('orders.lineEdit.fromKits')}
      htmlFor={`line-${line.id}-from-stock`}
      hint={t('orders.lines.movedKitsDown')}
    >
      <NumberInput
        id={`line-${line.id}-from-stock`}
        min={0}
        max={live}
        value={draft.fromStock}
        aria-describedby={`line-${line.id}-from-stock-hint`}
        onChange={(v) => setDraft({ ...draft, fromStock: v })}
        fallback={0}
        className={FIELD_CLASS}
      />
    </WorkshopField>
  );
}
