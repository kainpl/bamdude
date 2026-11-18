import { Fragment, useEffect, useId, useRef, useState } from 'react';
import { NumberInput } from '../../NumberInput';
import { useMutation, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, WAYBILL_MAX } from '../../../api/client';
import type { FulfilmentLineState, FulfilmentRecipient, FulfilmentResult, FulfilmentState } from '../../../api/client';
import { useAuth } from '../../../contexts/AuthContext';
import { useToast } from '../../../contexts/ToastContext';
import { fulfilmentQuery, useFulfilment } from '../../../hooks/useFulfilment';
import { invalidateOrderViews } from '../../../utils/queryInvalidation';
import { canTakeStock, canWriteOff } from '../../../utils/workshopRights';
import { Button } from '../../Button';
import { LivePartThumb } from '../../PartThumb';
import { DispatchNoteCreated } from '../../stock/DispatchNoteCreated';
import { WorkshopDialog } from '../../workshop/WorkshopDialog';
import { WorkshopFormGrid } from '../../workshop/WorkshopFormGrid';
import { WorkshopTableScroll } from '../../workshop/WorkshopPanel';
import { RefreshFailedNote } from '../../workshop/RefreshFailedNote';
import { CONFIG_ACCENT_CLASS, isNonStandardConfiguration, lineConfigLabel } from '../lineConfigLabel';
import type { OrderRef } from '../orderActions/orderRef';
import { RecipientFields } from './RecipientFields';
import {
  batchTotals,
  clampDraft,
  clampStored,
  completesOrder,
  doneAfter,
  draftFrom,
  emptyDraft,
  issueCeiling,
  partsColumnState,
  partsColumnTotal,
  requestFrom,
  setAllParts,
  withLineWriteOff,
  withoutWriteOffs,
  withPartWriteOff,
} from './fulfilmentState';
import type { Draft, FulfilmentMode, LineDraft, PartDraft, PartsColumn } from './fulfilmentState';

const NUMBER_CLS =
  'w-[68px] px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white disabled:opacity-50';
const FIELD_CLS = 'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white';
const TH = 'px-3 py-2 text-left text-xs font-normal text-bambu-gray whitespace-nowrap';
const TD = 'px-3 py-2.5 align-top';

/**
 * «Склад і видача» (spec workshop-order-issue, rule 27; WS-13 E6 §E): per line — assemble
 * the reserved kits, receive the printed units onto the shelf under the order, write off
 * what broke, issue part of what is there or all; a parts line part by part. One
 * «Execute» is one batch and at most one issue — and every issue is a dispatch note.
 *
 * The numbers are the server's (`getFulfilment` — the same state the POST checks under
 * its locks); the draft only keeps the form inside them. ⚠️ After a refusal nothing is
 * sent again until a state read AFTER it has answered (R04), and a new state trims the
 * STORED draft — a bound that grows back does not return what it took.
 */
export function FulfilmentDialog({
  order,
  mode = 'all',
  complete = false,
  onClose,
  onDone,
}: {
  order: OrderRef;
  mode?: FulfilmentMode;
  /** Opened by a «done» door: an explicit wish to close (E6 B04, R09). */
  complete?: boolean;
  onClose: () => void;
  onDone?: (result: FulfilmentResult) => void;
}) {
  const { t } = useTranslation();
  const query = useFulfilment(order.id);
  // The batch made a dispatch note: the dialog becomes its window (E15).
  const [created, setCreated] = useState<{ id: number; code: string; units: number | null } | null>(null);
  if (created) return <DispatchNoteCreated id={created.id} code={created.code} units={created.units} onClose={onClose} />;

  const subtitle = `${order.code} · ${order.name} · ${order.customer_name ?? t('orders.fulfil.subtitleNoCustomer')}`;
  if (!query.data) {
    return (
      <WorkshopDialog
        onClose={onClose}
        title={t('orders.fulfil.title')}
        subtitle={subtitle}
        size="xl"
        footer={
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button disabled>{t('orders.fulfil.submit')}</Button>
          </>
        }
      >
        {query.isError ? (
          // A failed first read says so and asks again — never «loading» for ever (E02).
          <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-red-600 dark:text-red-500">
            <span>
              {t('orders.fulfil.readFailed')} {(query.error as Error)?.message}
            </span>
            <Button variant="secondary" size="sm" onClick={() => void query.refetch()}>
              {t('common.retry')}
            </Button>
          </div>
        ) : (
          <div role="status" aria-busy className="space-y-2">
            <span className="sr-only">{t('common.loading')}</span>
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-10 rounded-lg bg-bambu-dark-tertiary/60 animate-pulse" />
            ))}
          </div>
        )}
      </WorkshopDialog>
    );
  }

  return (
    <FulfilmentForm
      order={order}
      subtitle={subtitle}
      state={query.data}
      query={query}
      mode={mode}
      complete={complete}
      onClose={onClose}
      onDone={onDone}
      onIssued={setCreated}
    />
  );
}

function FulfilmentForm({
  order,
  subtitle,
  state,
  query,
  mode,
  complete: completeAsked,
  onClose,
  onDone,
  onIssued,
}: {
  order: OrderRef;
  subtitle: string;
  state: FulfilmentState;
  query: UseQueryResult<FulfilmentState>;
  mode: FulfilmentMode;
  complete: boolean;
  onClose: () => void;
  onDone?: (result: FulfilmentResult) => void;
  /** The batch opened an issue — its dispatch note replaces the dialog. */
  onIssued: (note: { id: number; code: string; units: number | null }) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { showToast } = useToast();
  const { user, hasPermission } = useAuth();
  // Receiving and issuing move the shelf — `stock:move`; a write-off corrects the books —
  // `stock:adjust` too (WS-13 E13 O06). Without the move the dialog only completes (O23).
  const canMove = canTakeStock(hasPermission);
  const mayWriteOff = canWriteOff(hasPermission);
  const startDraft = (s: FulfilmentState) => (canMove ? draftFrom(s, mode) : emptyDraft(s));
  const writeOffId = useId();
  const root = useRef<HTMLDivElement>(null);

  // The STORED draft (R04): each new state trims it, as a record — never re-derived.
  const [typed, setTyped] = useState<Draft>(() => startDraft(state));
  const [seen, setSeen] = useState(state);
  const [trimmed, setTrimmed] = useState(false);
  if (state !== seen) {
    const next = clampStored(typed, state);
    setSeen(state);
    setTyped(next.draft);
    if (next.changed) setTrimmed(true);
  }
  const draft = clampDraft(typed, state);

  const [recipient, setRecipient] = useState<FulfilmentRecipient>(state.recipient);
  const [waybill, setWaybill] = useState('');
  const [note, setNote] = useState('');
  const [writeOffOpen, setWriteOffOpen] = useState(false);
  const [writeOffNote, setWriteOffNote] = useState('');
  // The close mark is set ONCE, on the first state (R09): asked by a «done» door, or — in
  // the full mode — when the first batch closes the order. Later reads never re-tick it.
  const [closeAsked, setCloseAsked] = useState(
    () => completeAsked || (mode === 'all' && completesOrder(state, startDraft(state))),
  );
  const [error, setError] = useState<string | null>(null);
  // After a refusal: the state read again — `reading` until an answer that came after it.
  const [reread, setReread] = useState<'idle' | 'reading' | 'failed'>('idle');
  const sent = useRef(false);

  // No customer: nothing is issued, the order closes to stock (followups, rules 35–39).
  const issuing = !state.closes_to_stock;
  const completes = completesOrder(state, draft);
  const closing = closeAsked && completes;
  const lines = requestFrom(draft);
  const totals = batchTotals(draft);
  // Two lines of one product read alike to a screen reader: their fields take the
  // configuration into their names (E05 «<column> — <line>»).
  const sharedNames = new Set(
    state.lines
      .filter((l) => l.mode !== 'parts')
      .map((l) => l.product_name)
      .filter((n, i, all) => all.indexOf(n) !== i),
  );
  const noteMissing = totals.writeOff > 0 && writeOffNote.trim() === '';

  const cancelId = useId();
  const submitId = useId();
  const rereadId = useId();
  // The first focus is the first number of the table (the mockup's `openDialog`), else
  // «Cancel» (E16) — after the Modal's own focus of its panel.
  useEffect(() => {
    const first = root.current?.querySelector<HTMLInputElement>('table input[type="number"]:not(:disabled)');
    (first ?? document.getElementById(cancelId))?.focus();
  }, [cancelId]);

  const change = (lineId: number, patch: (d: LineDraft) => LineDraft) =>
    setTyped((prev) => {
      const current = clampDraft(prev, state);
      return clampDraft({ ...current, [lineId]: patch(current[lineId]) }, state);
    });

  const readAgain = async () => {
    setReread('reading');
    try {
      // A read already on its way started before the refusal: fetchQuery would hand it back
      // as «fresh». Cancel it first, so the state the dialog waits for is read after (R04).
      const query = fulfilmentQuery(order.id);
      await qc.cancelQueries({ queryKey: query.queryKey, exact: true });
      await qc.fetchQuery({ ...query, staleTime: 0 });
      setReread('idle');
    } catch {
      setReread('failed');
    }
  };

  const fulfil = useMutation({
    mutationFn: () =>
      api.fulfilOrder(order.id, {
        lines,
        recipient,
        waybill: waybill.trim() || null,
        note: note.trim() || null,
        complete: closing,
        write_off_note: totals.writeOff > 0 ? writeOffNote.trim() : null,
      }),
    onSuccess: (result) => {
      invalidateOrderViews(qc, { orderId: order.id });
      onDone?.(result);
      if (result.issue_id != null && result.issue_code) {
        onIssued({ id: result.issue_id, code: result.issue_code, units: result.issue_units ?? null });
        return;
      }
      showToast(t(closing ? 'orders.fulfil.doneCompleted' : 'orders.fulfil.done'));
      onClose();
    },
    // The server's sentence, and the state read again — nothing is sent until it answers.
    onError: (err: Error) => {
      sent.current = false;
      setError(err.message);
      void readAgain();
    },
  });

  const pending = fulfil.isPending;
  const canSubmit = (lines.length > 0 || closing) && !noteMissing && !pending && reread === 'idle';
  // After a refusal focus never falls to BODY: on «Read again» when the re-read failed, back
  // on «Execute» once the state is read afresh (C08 for this dialog).
  useEffect(() => {
    if (!error) return;
    if (reread === 'failed') document.getElementById(rereadId)?.focus();
    else if (reread === 'idle') document.getElementById(submitId)?.focus();
  }, [error, reread, rereadId, submitId]);
  const submit = () => {
    if (!canSubmit || sent.current) return;
    sent.current = true;
    setTrimmed(false);
    fulfil.mutate();
  };

  // Closing the column takes its numbers back — nothing hidden is written off (final review I2).
  const toggleWriteOff = () => {
    if (writeOffOpen) {
      setTyped((prev) => clampDraft(withoutWriteOffs(state, clampDraft(prev, state)), state));
      setWriteOffNote('');
    }
    setWriteOffOpen(!writeOffOpen);
  };

  const summaryParts = [
    totals.assemble > 0 && t('orders.fulfil.summaryAssemble', { count: totals.assemble }),
    totals.receive > 0 && t('orders.fulfil.summaryReceive', { count: totals.receive }),
    totals.writeOff > 0 && t('orders.fulfil.summaryWriteOff', { count: totals.writeOff }),
    totals.issue > 0 && t('orders.fulfil.summaryIssue', { count: totals.issue }),
  ].filter(Boolean) as string[];
  const summaryText = summaryParts.length ? summaryParts.join(' · ') : t('orders.fulfil.summaryNothing');
  const summary = summaryText.charAt(0).toUpperCase() + summaryText.slice(1);

  const completeLabel = t(issuing ? 'orders.fulfil.complete' : 'orders.fulfil.completeToStock');
  const afterThis = t(issuing ? 'orders.fulfil.afterThis' : 'orders.fulfil.afterThisToStock', {
    done: doneAfter(state, draft),
    ordered: state.ordered,
  });

  const trimmedNote = trimmed ? <p className="text-sm text-bambu-gray">{t('orders.fulfil.trimmed')}</p> : null;
  const errorNode = error ? (
    <div className="space-y-1">
      <p>{error}</p>
      {reread === 'reading' && <p className="text-sm text-bambu-gray">{t('orders.fulfil.rereading')}</p>}
      {reread === 'failed' && (
        <p className="flex flex-wrap items-center gap-2 text-sm">
          {t('orders.fulfil.readFailed')}
          <Button id={rereadId} variant="secondary" size="sm" onClick={() => void readAgain()}>
            {t('orders.fulfil.reread')}
          </Button>
        </p>
      )}
      {trimmedNote}
    </div>
  ) : undefined;

  return (
    <WorkshopDialog
      onClose={onClose}
      title={t('orders.fulfil.title')}
      subtitle={subtitle}
      size="xl"
      pending={pending}
      error={errorNode}
      summary={<span className="text-sm text-bambu-gray-light">{summary}</span>}
      footer={
        <>
          <Button id={cancelId} variant="secondary" onClick={onClose} disabled={pending}>
            {t('common.cancel')}
          </Button>
          <Button id={submitId} onClick={submit} disabled={!canSubmit}>
            {pending ? t('orders.fulfil.submitting') : t('orders.fulfil.submit')}
          </Button>
        </>
      }
    >
      <div ref={root}>
        <fieldset disabled={pending} className="min-w-0 space-y-4">
          {lines.length === 0 && !completes && <p className="text-sm text-bambu-gray">{t('orders.fulfil.nothing')}</p>}
          {!issuing && <p className="text-sm text-bambu-gray">{t('orders.fulfil.noCustomer')}</p>}
          {query.isError && reread === 'idle' && <RefreshFailedNote onRetry={() => void query.refetch()} />}
          {!error && trimmedNote}

          {mayWriteOff && (
            <div className="flex justify-end">
              <Button
                variant="secondary"
                size="sm"
                onClick={toggleWriteOff}
                aria-expanded={writeOffOpen}
                aria-controls={`${writeOffId}-table ${writeOffId}`}
              >
                {t('orders.fulfil.writeOffToggle')}
              </Button>
            </div>
          )}

          {!canMove && <p className="text-sm text-bambu-gray">{t('orders.fulfil.noMoveRight')}</p>}
          {/* Without the stock's move the numbers are read, never typed (WS-13 E13 O06): every number and
              «all parts» box is disabled itself — a disabled fieldset would take the part pictures' buttons
              with it, and seeing a picture is no right of the stock's (plan E6, D6). */}
          <WorkshopTableScroll label={t('orders.fulfil.title')}>
            <table id={`${writeOffId}-table`} className="w-full text-sm">
              <thead>
                <tr>
                  <th className={TH}>{t('orders.fulfil.columns.line')}</th>
                  <th className={TH}>{t('orders.fulfil.columns.ordered')}</th>
                  <th className={TH}>{t('orders.fulfil.columns.assemble')}</th>
                  <th className={TH}>{t('orders.fulfil.columns.receive')}</th>
                  <th className={TH}>{t('orders.fulfil.columns.held')}</th>
                  {writeOffOpen && <th className={TH}>{t('orders.fulfil.columns.writeOff')}</th>}
                  {issuing && <th className={TH}>{t('orders.fulfil.columns.issueNow')}</th>}
                  <th className={TH}>{t('orders.fulfil.columns.issued')}</th>
                </tr>
              </thead>
              <tbody>
                {state.lines.map((line) =>
                  line.mode === 'parts' ? (
                    <PartsLineRows
                      key={line.line_id}
                      line={line}
                      draft={draft[line.line_id]}
                      onChange={change}
                      writeOffOpen={writeOffOpen}
                      issuing={issuing}
                      locked={!canMove}
                    />
                  ) : (
                    <Fragment key={line.line_id}>
                      <ProductLineRow
                        line={line}
                        sharedName={sharedNames.has(line.product_name)}
                        draft={draft[line.line_id]}
                        onChange={change}
                        writeOffOpen={writeOffOpen}
                        issuing={issuing}
                        locked={!canMove}
                      />
                      {line.parts.length > 0 && <PartsLineRows extra
                        line={{ ...line, ordered: line.parts.reduce((s, p) => s + p.wanted, 0),
                          held: line.parts.reduce((s, p) => s + p.held, 0), issued: line.parts.reduce((s, p) => s + p.issued, 0) }}
                        draft={draft[line.line_id]} onChange={change} writeOffOpen={writeOffOpen} issuing={issuing}
                        locked={!canMove} />}
                    </Fragment>
                  ),
                )}
              </tbody>
            </table>
          </WorkshopTableScroll>

          <div id={writeOffId}>
            {writeOffOpen && (
              <label className="block text-sm space-y-1">
                <span className="text-bambu-gray-light">{t('orders.fulfil.writeOffNote')}</span>
                <input
                  value={writeOffNote}
                  maxLength={2000}
                  onChange={(e) => setWriteOffNote(e.target.value)}
                  aria-label={t('orders.fulfil.writeOffNote')}
                  aria-invalid={noteMissing}
                  className={FIELD_CLS}
                />
              </label>
            )}
          </div>

          {issuing && totals.issue > 0 && (
            <section aria-label={t('orders.fulfil.recipient')} className="space-y-1">
              <h3 className="text-sm font-semibold text-white">{t('orders.fulfil.recipient')}</h3>
              <WorkshopFormGrid>
                <RecipientFields value={recipient} onChange={setRecipient} />
                <label className="text-sm space-y-1">
                  <span className="text-bambu-gray">{t('orders.fulfil.waybill')}</span>
                  <input
                    value={waybill}
                    maxLength={WAYBILL_MAX}
                    onChange={(e) => setWaybill(e.target.value)}
                    aria-label={t('orders.fulfil.waybill')}
                    className={FIELD_CLS}
                  />
                </label>
                <label className="text-sm space-y-1">
                  <span className="text-bambu-gray">{t('orders.fulfil.note')}</span>
                  <input
                    value={note}
                    maxLength={2000}
                    onChange={(e) => setNote(e.target.value)}
                    aria-label={t('orders.fulfil.note')}
                    className={FIELD_CLS}
                  />
                </label>
              </WorkshopFormGrid>
              <p className="text-xs text-bambu-gray">{t('orders.fulfil.recipientHint')}</p>
            </section>
          )}

          <WorkshopFormGrid>
            <div data-testid="fulfil-performer" className="flex min-w-0 flex-col gap-1 text-sm">
              <span className="text-bambu-gray-light">{t('orders.fulfil.performer')}</span>
              <span className="text-white">{user?.username ?? '—'}</span>
              <small className="text-xs text-bambu-gray">{t('orders.fulfil.performerHint')}</small>
            </div>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={closing}
                disabled={!completes}
                onChange={(e) => setCloseAsked(e.target.checked)}
                aria-label={completeLabel}
                aria-describedby={completes ? undefined : `${writeOffId}-after`}
              />
              <span className={completes ? 'text-white' : 'text-bambu-gray'}>
                {completeLabel}
                {!completes && (
                  <small id={`${writeOffId}-after`} className="ml-1 text-xs text-bambu-gray">
                    {afterThis}
                  </small>
                )}
              </span>
            </label>
          </WorkshopFormGrid>

          <p className="text-xs leading-5 text-bambu-gray">{t('orders.fulfil.explain')}</p>
        </fieldset>
      </div>
    </WorkshopDialog>
  );
}

function NumberCell({
  value,
  max,
  label,
  onChange,
  disabled = false,
}: {
  value: number;
  max: number;
  label: string;
  onChange: (n: number) => void;
  /** Read, never typed: without the stock's move (WS-13 E13 O06, plan E6 D6). */
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  // Nothing to do in this column: a dash, not a dead field (E05).
  if (max === 0) return <span className="text-bambu-gray">—</span>;
  return (
    <div className="flex items-center gap-2 whitespace-nowrap">
      <NumberInput
        min={0}
        max={max}
        value={value}
        disabled={disabled}
        onChange={onChange}
        fallback={0}
        aria-label={label}
        className={NUMBER_CLS}
      />
      <small className="text-xs text-bambu-gray">{t('orders.fulfil.ofN', { count: max })}</small>
    </div>
  );
}

function ProductLineRow({
  line,
  sharedName = false,
  draft,
  onChange,
  writeOffOpen,
  issuing,
  locked,
}: {
  line: FulfilmentLineState;
  /** Another line of the dialog is of the same product — the field names add the configuration. */
  sharedName?: boolean;
  draft: LineDraft;
  onChange: (lineId: number, patch: (d: LineDraft) => LineDraft) => void;
  writeOffOpen: boolean;
  issuing: boolean;
  /** Without the stock's move every field of the row is read, never typed (WS-13 E13 O06, plan E6 D6). */
  locked: boolean;
}) {
  const { t } = useTranslation();
  const name = line.product_name;
  const config = lineConfigLabel(line.configuration ?? undefined, line.mode, t) || t('orders.fulfil.standardConfig');
  // As the lines table: a kit that differs from the standard in the accent, the standard muted.
  const accent = isNonStandardConfiguration(line.configuration);
  const fieldName = sharedName ? `${name} (${config})` : name;
  return (
    <tr data-testid={`fulfil-line-${line.line_id}`} className="border-t border-bambu-dark-tertiary">
      <td className={TD}>
        <div className="font-semibold text-white">{name}</div>
        {accent ? (
          <small data-config-accent className={`block text-xs ${CONFIG_ACCENT_CLASS}`}>
            {config}
          </small>
        ) : (
          <small className="block text-xs text-bambu-gray">{config}</small>
        )}
        {line.stock_position && (
          <small className="block text-xs text-bambu-gray">
            {t('orders.fulfil.cell', { location: line.stock_position.location || t('orders.fulfil.cellUnassigned') })}
          </small>
        )}
      </td>
      <td className={TD}>{line.ordered}</td>
      <td className={TD}>
        <NumberCell
          disabled={locked}
          value={draft.assemble}
          max={line.can_assemble}
          label={t('orders.fulfil.assembleLabel', { name: fieldName })}
          onChange={(n) => onChange(line.line_id, (d) => ({ ...d, assemble: n }))}
        />
      </td>
      <td className={TD}>
        <NumberCell
          disabled={locked}
          value={draft.receive}
          max={line.can_receive}
          label={t('orders.fulfil.receiveLabel', { name: fieldName })}
          onChange={(n) => onChange(line.line_id, (d) => ({ ...d, receive: n }))}
        />
      </td>
      <td className={TD}>{line.held}</td>
      {writeOffOpen && (
        <td className={TD}>
          <NumberCell
            disabled={locked}
            value={draft.writeOff}
            max={line.held + draft.assemble + draft.receive}
            label={t('orders.fulfil.writeOffLabel', { name: fieldName })}
            onChange={(n) => onChange(line.line_id, (d) => withLineWriteOff(line, d, n))}
          />
        </td>
      )}
      {issuing && (
        <td className={TD}>
          <NumberCell
            disabled={locked}
            value={draft.issue}
            max={issueCeiling(line, draft)}
            label={t('orders.fulfil.issueLabel', { name: fieldName })}
            onChange={(n) => onChange(line.line_id, (d) => ({ ...d, issue: n }))}
          />
        </td>
      )}
      <td className={`${TD} whitespace-nowrap`}>{t('orders.fulfil.issuedOf', { issued: line.issued, ordered: line.ordered })}</td>
    </tr>
  );
}

/** «all N parts» of one column of a parts line (E06): ticked, cleared, or mixed. */
function AllPartsCell({
  line,
  draft,
  column,
  onChange,
  disabled = false,
}: {
  line: FulfilmentLineState;
  draft: LineDraft;
  column: PartsColumn;
  onChange: (lineId: number, patch: (d: LineDraft) => LineDraft) => void;
  /** Read, never ticked: without the stock's move (plan E6, D6). */
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const box = useRef<HTMLInputElement>(null);
  const total = partsColumnTotal(line, draft, column);
  const at = partsColumnState(line, draft, column);
  useEffect(() => {
    if (box.current) box.current.indeterminate = at === 'mixed';
  });
  // Nothing any part can take — no checkbox that would read as «done» (K8).
  if (total === 0) return <span className="text-bambu-gray">—</span>;
  const columnName = t(column === 'receive' ? 'orders.fulfil.columns.receive' : 'orders.fulfil.columns.issueNow');
  return (
    <label className="inline-flex items-center gap-2 whitespace-nowrap text-sm">
      <input
        ref={box}
        type="checkbox"
        checked={at === 'all'}
        disabled={disabled}
        onChange={(e) => onChange(line.line_id, (d) => setAllParts(line, d, column, e.target.checked))}
        aria-label={t('orders.fulfil.allPartsLabel', { column: columnName, name: line.product_name })}
      />
      <span className="text-white">
        {total === 1 ? t('orders.fulfil.allPartsOne') : t('orders.fulfil.allParts', { count: total })}
      </span>
    </label>
  );
}

function PartsLineRows({
  line,
  draft,
  onChange,
  writeOffOpen,
  issuing,
  extra = false,
  locked,
}: {
  extra?: boolean;
  line: FulfilmentLineState;
  draft: LineDraft;
  onChange: (lineId: number, patch: (d: LineDraft) => LineDraft) => void;
  writeOffOpen: boolean;
  issuing: boolean;
  /** Without the stock's move every field of the row is read, never typed (WS-13 E13 O06, plan E6 D6). */
  locked: boolean;
}) {
  const { t } = useTranslation();
  const setPart = (partId: number, field: keyof PartDraft, n: number) =>
    onChange(line.line_id, (d) => ({ ...d, parts: { ...d.parts, [partId]: { ...d.parts[partId], [field]: n } } }));
  const setPartWriteOff = (part: FulfilmentLineState['parts'][number], n: number) =>
    onChange(line.line_id, (d) => ({
      ...d,
      parts: { ...d.parts, [part.part_id]: withPartWriteOff(part, d.parts[part.part_id], n) },
    }));
  return (
    <>
      <tr data-testid={`fulfil-${extra ? 'extras' : 'line'}-${line.line_id}`} className="border-t border-bambu-dark-tertiary">
        <td className={TD}>
          <div className="font-semibold text-white">{line.product_name}</div>
          <small data-config-accent className={`block text-xs ${CONFIG_ACCENT_CLASS}`}>
            {t(extra ? 'orders.fulfil.extraParts' : 'orders.fulfil.partsLine')}
          </small>
        </td>
        <td className={TD}>{line.ordered}</td>
        <td className={`${TD} text-bambu-gray`}>—</td>
        <td className={TD}>
          <AllPartsCell disabled={locked} line={line} draft={draft} column="receive" onChange={onChange} />
        </td>
        <td className={TD}>{line.held}</td>
        {writeOffOpen && <td className={TD} />}
        {issuing && (
          <td className={TD}>
            <AllPartsCell disabled={locked} line={line} draft={draft} column="issue" onChange={onChange} />
          </td>
        )}
        <td className={`${TD} whitespace-nowrap`}>{t('orders.fulfil.issuedOf', { issued: line.issued, ordered: line.ordered })}</td>
      </tr>
      {line.parts.map((part) => {
        const d = draft.parts[part.part_id] ?? { receive: 0, writeOff: 0, issue: 0 };
        return (
          <tr key={part.part_id} className="text-bambu-gray-light">
            <td className={`${TD} pl-8`}>
              {/* The cells are top-aligned: the name starts level with the figures beside it. */}
              <span className="flex items-start gap-2">
                <LivePartThumb partId={part.part_id} image={part.image} name={part.name} size="sm" />
                <span className="min-w-0 break-words">{part.name}</span>
              </span>
            </td>
            <td className={TD}>{part.wanted}</td>
            <td className={TD} />
            <td className={TD}>
              <NumberCell
                disabled={locked}
                value={d.receive}
                max={part.can_receive}
                label={t('orders.fulfil.receiveLabel', { name: part.name })}
                onChange={(n) => setPart(part.part_id, 'receive', n)}
              />
            </td>
            <td className={TD}>{part.held}</td>
            {writeOffOpen && (
              <td className={TD}>
                <NumberCell
                  disabled={locked}
                  value={d.writeOff}
                  max={part.held + d.receive}
                  label={t('orders.fulfil.writeOffLabel', { name: part.name })}
                  onChange={(n) => setPartWriteOff(part, n)}
                />
              </td>
            )}
            {issuing && (
              <td className={TD}>
                <NumberCell
                  disabled={locked}
                  value={d.issue}
                  max={part.held + d.receive - d.writeOff}
                  label={t('orders.fulfil.issueLabel', { name: part.name })}
                  onChange={(n) => setPart(part.part_id, 'issue', n)}
                />
              </td>
            )}
            <td className={`${TD} whitespace-nowrap`}>
              {t('orders.fulfil.issuedOf', { issued: part.issued, ordered: part.wanted })}
            </td>
          </tr>
        );
      })}
    </>
  );
}
