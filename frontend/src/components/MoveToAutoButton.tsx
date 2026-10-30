import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRightLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api, ApiError, type PrintQueueItem } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { invalidateOrderViews, invalidateQueueViews } from '../utils/queryInvalidation';
import { ConfirmModal } from './ConfirmModal';

export function MoveToAutoButton({ item }: { item: PrintQueueItem }) {
  const { hasPermission, user } = useAuth();
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const mayRemove = hasPermission('queue:delete_all') || (item.created_by_id != null && item.created_by_id === user?.id && hasPermission('queue:delete_own'));
  if (item.status !== 'pending' || !hasPermission('queue:create') || !hasPermission('queue:read') || !mayRemove) return null;
  return <>
    <button type="button" onClick={() => setOpen(true)} title={t('queueTransfer.title')} aria-label={t('queueTransfer.title')}
      className="shrink-0 rounded p-1.5 text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white">
      <ArrowRightLeft className="h-3.5 w-3.5" />
    </button>
    {open && <TransferDialog itemId={item.id} onClose={() => setOpen(false)} />}
  </>;
}

function TransferDialog({ itemId, onClose }: { itemId: number; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { showToast } = useToast();
  const [start, setStart] = useState(false);
  const [filament, setFilament] = useState(false);
  const [profile, setProfile] = useState(false);
  const [failure, setFailure] = useState<string>();
  const preview = useQuery({ queryKey: ['queue', 'auto-transfer-preview', itemId], queryFn: () => api.previewQueueAutoTransfer(itemId), retry: false });
  const proposal = failure ? undefined : preview.data;
  const apply = useMutation({
    mutationFn: () => api.moveQueueToAuto(itemId, { token: proposal!.token!, allow_automatic_start: start,
      allow_automatic_filament: filament, accept_profile_changes: profile }),
    onSuccess: () => {
      invalidateQueueViews(qc);
      qc.invalidateQueries({ queryKey: ['auto-queue'] });
      invalidateOrderViews(qc);
      showToast(t('queueTransfer.success'), 'success');
      onClose();
    },
    onError: (error: Error) => {
      // A sent request with an unknown outcome must never be replayed here.
      const unknown = !(error instanceof ApiError) || error.status >= 500;
      setFailure(t(unknown ? 'queueTransfer.unknown' : 'queueTransfer.stale'));
      invalidateQueueViews(qc);
      qc.invalidateQueries({ queryKey: ['auto-queue'] });
    },
  });
  const optionValue = (value: unknown): string => typeof value === 'boolean' ? t(value ? 'common.enabled' : 'common.disabled')
    : value == null ? '—' : typeof value === 'string' ? t(`queueTransfer.values.${value}`, { defaultValue: value }) : String(value);
  const allowed = proposal?.allowed && !!proposal.token;
  const changes = proposal?.option_changes ?? [];
  const ready = allowed && (!proposal.manual_start || start) && (!proposal.release_pins || filament) && (!changes.length || profile);
  const checkbox = (label: string, checked: boolean, change: (value: boolean) => void) => <label className="flex items-start gap-2 text-sm">
    <input type="checkbox" className="mt-1 shrink-0" checked={checked} onChange={e => change(e.target.checked)} />{label}
  </label>;
  return <ConfirmModal title={t('queueTransfer.title')} message={t('queueTransfer.help')} onCancel={onClose}
    onConfirm={() => apply.mutate()} confirmText={t('queueTransfer.move')} confirmDisabled={!ready} isLoading={apply.isPending}>
    {preview.isPending && <p>{t('common.loading')}</p>}
    {(preview.isError || failure) && <p role="alert">{failure ?? (preview.error instanceof ApiError && preview.error.code
      ? t(`queueTransfer.refusals.${preview.error.code}`, { defaultValue: t('queueTransfer.readFailed') }) : t('queueTransfer.readFailed'))}</p>}
    {proposal && !proposal.allowed && <p role="alert">{t(`queueTransfer.refusals.${proposal.refusal}`, { defaultValue: t('queueTransfer.unavailable') })}</p>}
    {allowed && <div className="space-y-3 text-sm">
      <p className="break-words">{proposal.display_name} · {t('printModal.plateNumber', { number: proposal.plate_id ?? 1 })}</p>
      <p>{proposal.printer_name} → {proposal.target_model} · {proposal.target_location_id == null ? t('queueTransfer.anyLocation') : `#${proposal.target_location_id}`}</p>
      <p>{t('queueTransfer.profileHelp', { model: proposal.comparison_model })}</p>
      {proposal.default_profile && <p>{t('queueTransfer.defaultProfile')}</p>}
      {changes.length > 0 && <>
        <ul className="space-y-1">{changes.map(change => <li key={change.field}>
          {t(`queueTransfer.fields.${change.field}`)}: {optionValue(change.before)} → {optionValue(change.after)}
        </li>)}</ul>
        {checkbox(t('queueTransfer.acceptProfile'), profile, setProfile)}
      </>}
      {proposal.manual_start && checkbox(t('queueTransfer.acceptStart'), start, setStart)}
      {proposal.release_pins && checkbox(t('queueTransfer.acceptFilament'), filament, setFilament)}
      <p className="text-bambu-gray">{t('queueTransfer.reresolve')}</p>
    </div>}
  </ConfirmModal>;
}
