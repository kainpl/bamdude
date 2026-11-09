import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api, ApiError, type PrintStateAction, type PrintStateAudit, type PrintStateIssue, type PrintStatePreview } from '../api/client';
import { Button } from './Button';
import { Modal } from './Modal';
import { PrintStatePreheatNote } from './PrintStateRuntimeNotes';
import { formatDateTime } from '../utils/date';
import { failureText } from '../utils/failureText';

/** What the in-memory repair takes from memory, from the server's preview -- nothing else is touched. */
function RuntimeRelease({ preview }: { preview: PrintStatePreview }) {
  const { t } = useTranslation();
  const release = preview.after.runtime;
  if (!release) return null;
  return <div className="space-y-1">
    <p>{t('printStateRecovery.runtime.releases', { archive: release.archive_id })}</p>
    <ul className="list-disc pl-5">
      {release.admission && <li>{t('printStateRecovery.runtime.releaseAdmission')}</li>}
      {release.macro_selection && <li>{t('printStateRecovery.runtime.releaseMacros')}</li>}
      {release.swap_config && <li>{t('printStateRecovery.runtime.releaseSwap')}</li>}
    </ul>
    {preview.before.queue.is_paused === true && <p className="text-amber-700 dark:text-amber-300">{t('printStateRecovery.runtime.alreadyPaused')}</p>}
    {preview.runtime?.plate_gate && <p>{t('printStateRecovery.runtime.plateGate')}</p>}
    <PrintStatePreheatNote pinned={preview.runtime?.preheat_pinned} temperatures={preview.runtime?.temperatures} />
  </div>;
}

/** How the in-memory repair ended -- said as it is, including "not recorded". */
function RuntimeOutcome({ result }: { result: PrintStateAudit }) {
  const { t } = useTranslation();
  const outcome = typeof result.outcome === 'string' ? result.outcome : 'outcome_unconfirmed';
  return <div role="status" className="space-y-1">
    <p className="font-medium">{t(`printStateRecovery.outcome.${outcome}`)}</p>
    {result.reason && <p className="text-xs text-bambu-gray">{t(`printStateRecovery.outcomeReasons.${result.reason}`)}</p>}
    <p>{t('printStateRecovery.runtime.pausedNow')}</p>
    {outcome !== 'applied' && <p>{t('printStateRecovery.runtime.notCancelled')}</p>}
  </div>;
}

export function PrintStateRecoveryDialog({ issue, action, onClose, onApplied }: {
  issue: PrintStateIssue; action: PrintStateAction; onClose: () => void; onApplied: () => void;
}) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<PrintStatePreview | null>(null);
  const [completed, setCompleted] = useState(false);
  const [idle, setIdle] = useState(false);
  const [manualSwap, setManualSwap] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PrintStateAudit | null>(null);
  const estimate = action === 'fill_estimated_end_and_align';
  const close = action === 'close_interrupted_attempt';
  const runtime = action === 'clear_terminal_runtime_context';
  const group = action === 'cancel_terminal_claims';
  const settlement = action === 'settle_swap_by_operator';
  const shown = (key: string, value: unknown): string => {
    if (value === null || value === undefined) return '—';
    if ((key === 'error_code' || key === 'failure_reason') && typeof value === 'string') return failureText(t, value, null, issue.archive_id);
    if (key === 'waiting_reason' && value === true) return t('printStateRecovery.recordedReason');
    if (typeof value === 'boolean') return t(value ? 'common.yes' : 'common.no');
    if (key === 'completed_at' && typeof value === 'string') return formatDateTime(value);
    if (key === 'status') return t(`printStateRecovery.states.${String(value)}`);
    if (key === 'time_source') return t('printStateRecovery.slicerEstimate');
    return String(value);
  };

  const prepare = async () => {
    setBusy(true); setError(null); setPreview(null); setConfirmed(false);
    try {
      const answer = await api.previewPrintState(issue.printer_id, {
        kind: issue.kind as 'item' | 'header' | 'runtime' | 'archive', target_id: issue.target_id, action,
        confirmed_completed: completed, confirmed_idle: idle, confirmed_manual_swap: manualSwap, note,
      });
      setPreview(answer);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { setIdle(false); setManualSwap(false); setCompleted(false); }
      setError(e instanceof ApiError && e.status === 409 ? t('printStateRecovery.changed') : t('printStateRecovery.failed'));
    } finally { setBusy(false); }
  };
  const apply = async () => {
    if (!preview || !confirmed || busy) return;
    setBusy(true); setError(null);
    try {
      const answer = await api.applyPrintState(issue.printer_id, preview.preview_token);
      onApplied();
      // The in-memory repair answers with its outcome, which the operator must read before leaving:
      // the queue is paused whatever it was, and an unrecorded result needs the history.
      if (runtime) setResult(answer);
      else onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setIdle(false); setManualSwap(false); setCompleted(false);
        setPreview(null); setConfirmed(false); setError(t('printStateRecovery.changed'));
      } else {
        // Retain the same token after a lost response: the server's audit makes
        // retry idempotent. A conflict instead needs a fresh explicit preview.
        setError(t('printStateRecovery.failed'));
      }
    } finally { setBusy(false); }
  };

  return <Modal title={t(`printStateRecovery.actions.${action}`)} onClose={onClose} closeDisabled={busy} size="lg"
    alert={error || busy ? <div className="px-4 pb-3 space-y-1 text-sm">
      {error && <p role="alert" className="text-red-600 dark:text-red-300">{error}</p>}
      {busy && <p role="status">{t('common.loading')}</p>}
    </div> : undefined}
    footer={<>
      <Button variant="secondary" onClick={onClose} disabled={busy}>{t(result ? 'common.close' : 'common.cancel')}</Button>
      {!result && (preview
        ? <Button variant={action === 'delete_stale_item' ? 'danger' : 'primary'} onClick={() => void apply()}
          disabled={busy || !confirmed}>{t('printStateRecovery.apply')}</Button>
        : <Button onClick={() => void prepare()} disabled={busy || (estimate && !completed) || ((group || settlement) && !idle) || (settlement && !manualSwap)}>{t('printStateRecovery.preview')}</Button>)}
    </>}>
    <div className="p-4 space-y-4 text-sm">
      <p className="break-words">{issue.printer_name} · {t('printStateRecovery.target', {
        item: issue.item_id ?? issue.context?.queue_item_id ?? '—', archive: issue.archive_id ?? '—',
      })}</p>
      <p>{t('printStateRecovery.preserved')}</p>
      {estimate && <p className="text-amber-600 dark:text-amber-300">{t('printStateRecovery.estimate')}</p>}
      {close && <p className="text-amber-600 dark:text-amber-300">{t('printStateRecovery.interruptedUncertain')}</p>}
      {runtime && <div className="space-y-1 text-amber-700 dark:text-amber-300">
        <p>{t('printStateRecovery.runtime.willPause')}</p>
        <p>{t('printStateRecovery.runtime.notPlate')}</p>
      </div>}
      {result && <RuntimeOutcome result={result} />}
      {!preview && !result && <>
        {(group || settlement) && <label className="flex gap-2 items-start"><input type="checkbox" checked={idle} onChange={e => setIdle(e.target.checked)} /><span>{t('printStateRecovery.idleAssertion')}</span></label>}
        {settlement && <label className="flex gap-2 items-start"><input type="checkbox" checked={manualSwap} onChange={e => setManualSwap(e.target.checked)} /><span>{t('printStateRecovery.manualSwapAssertion')}</span></label>}
        {estimate && <label className="flex gap-2 items-start">
          <input type="checkbox" checked={completed} onChange={e => setCompleted(e.target.checked)} />
          <span>{t('printStateRecovery.completedConfirmation')}</span>
        </label>}
        <label className="block">{t('printStateRecovery.note')}
          <textarea className="mt-1 block w-full bg-bambu-dark border border-bambu-dark-tertiary rounded p-2"
            maxLength={500} value={note} onChange={e => setNote(e.target.value)} />
        </label>
      </>}
      {preview && !result && <>
        {runtime && <RuntimeRelease preview={preview} />}
        {group && <div className="overflow-x-auto"><p>{t('printStateRecovery.groupExplanation')}</p><table className="w-full text-left"><thead><tr><th>{t('printStateRecovery.fields.item')}</th><th>{t('printStateRecovery.before')}</th><th>{t('printStateRecovery.after')}</th></tr></thead><tbody>{preview.members?.map(m => <tr key={m.id}><td>#{m.id}</td><td>{t(`printStateRecovery.states.${m.status}`)}</td><td>{t('printStateRecovery.states.cancelled')}</td></tr>)}</tbody></table></div>}
        {settlement && <><p>{t('printStateRecovery.settlementChange')}</p><p>{t('printStateRecovery.swapModeFact')}: {t(preview.swap_mode_enabled ? 'common.yes' : 'common.no')}</p><p>{t('printStateRecovery.settlementNext')}</p></>}
        {preview.estimate && <p className="text-amber-700 dark:text-amber-300">{t('printStateRecovery.estimatedLabel')}: {' '}
          {formatDateTime(preview.estimate.started_at)} + {preview.estimate.duration_seconds}s → {formatDateTime(preview.estimate.completed_at)}
        </p>}
        <div className="overflow-x-auto">
          <table className="w-full text-left"><thead><tr><th>{t('printStateRecovery.field')}</th>
            <th>{t('printStateRecovery.before')}</th><th>{t('printStateRecovery.after')}</th></tr></thead>
            <tbody>{(['queue', 'item', 'archive'] as const).flatMap(kind =>
              Object.entries(preview.after[kind]).filter(([key]) => key !== 'extra_data').map(([key, value]) =>
                <tr key={`${kind}.${key}`}><td className="py-2 pr-2">{t(`printStateRecovery.fields.${kind}`)}: {t(`printStateRecovery.fields.${key}`)}</td>
                  <td className="pr-2 break-words">{shown(key, preview.before[kind][key])}</td><td className="break-words">{shown(key, value)}</td></tr>))}
              {preview.after.delete_item && <tr><td>{t('printStateRecovery.fields.item')}</td>
                <td>#{issue.item_id}</td><td>{t('printStateRecovery.deleted')}</td></tr>}
            </tbody></table>
        </div>
        {close
          ? <p>{t('printStateRecovery.interruptedNext')}</p>
          : !runtime && Object.keys(preview.after.queue).length > 0 && <p>{t('printStateRecovery.paused')}</p>}
        <label className="flex gap-2 items-start"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />
          <span>{t('printStateRecovery.confirm')}</span></label>
      </>}
    </div>
  </Modal>;
}
