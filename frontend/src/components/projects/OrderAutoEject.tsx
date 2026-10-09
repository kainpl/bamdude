import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type Order, type OrderUpdate } from '../../api/client';
import { invalidateOrderViews } from '../../utils/queryInvalidation';
import { ConfirmModal } from '../ConfirmModal';
import { Button } from '../Button';

const PROFILE_SOURCE = 'https://infinityflow3d.com/pages/free-3d-printer-auto-clearing-cad-and-g-code';

function ProfileHelp() {
  const { t } = useTranslation();
  return (
    <div className="space-y-2 text-xs text-bambu-gray">
      <p className="font-medium text-white">{t('autoEject.profileTitle')}</p>
      <p>{t('autoEject.profileExample')}</p>
      <a href={PROFILE_SOURCE} target="_blank" rel="noopener noreferrer" className="text-bambu-green underline">
        {t('autoEject.profileSource')}
      </a>
      <ul className="list-disc pl-4 space-y-1">
        <li>{t('autoEject.profileHeight')}</li>
        <li>{t('autoEject.profileCamera')}</li>
        <li>{t('autoEject.profileOtherPrinters')}</li>
      </ul>
    </div>
  );
}

export function OrderAutoEject({ order, canEdit }: { order: Order; canEdit: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [confirmation, setConfirmation] = useState<{ orderId: number; kind: 'enable' | 'skipFirst' | 'skipFinal' } | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [thresholdDraft, setThresholdDraft] = useState<{ orderId: number; value: string } | null>(null);
  const sending = useRef(false);
  const policy = order.auto_eject_settings ?? { difference_threshold: 1, skip_check: false };
  const thresholdText = thresholdDraft?.orderId === order.id ? thresholdDraft.value : String(policy.difference_threshold);
  const threshold = Number(thresholdText);
  const validThreshold = thresholdText.trim() !== '' && Number.isFinite(threshold) && threshold >= 0.1 && threshold <= 10;
  const toggle = useMutation({
    mutationFn: ({ orderId, data }: { orderId: number; data: OrderUpdate }) => api.updateOrder(orderId, data),
    onSuccess: () => {
      setConfirmation(null);
      setAcknowledged(false);
      setThresholdDraft(null);
      invalidateOrderViews(queryClient);
    },
    onSettled: () => { sending.current = false; },
  });
  const mayEdit = canEdit && order.status === 'active';
  const kind = confirmation?.orderId === order.id && mayEdit
    && (confirmation.kind === 'enable' ? !order.auto_eject_enabled : order.auto_eject_enabled && !policy.skip_check)
    ? confirmation.kind : null;
  const save = (data: OrderUpdate) => {
    if (!mayEdit || sending.current) return;
    sending.current = true;
    toggle.mutate({ orderId: order.id, data });
  };
  const cancel = () => {
    if (sending.current) return;
    setConfirmation(null);
    setAcknowledged(false);
    toggle.reset();
  };
  const open = (next: 'enable' | 'skipFirst') => {
    if (!mayEdit || sending.current) return;
    toggle.reset();
    setAcknowledged(false);
    setConfirmation({ orderId: order.id, kind: next });
  };
  return (
    <div className="rounded-lg border border-bambu-dark-tertiary p-3 space-y-2">
      <label className="flex items-center gap-2 font-medium text-sm">
        <input type="checkbox" className="accent-bambu-green" checked={!!order.auto_eject_enabled}
          disabled={!mayEdit || toggle.isPending}
          onChange={(event) => {
            if (!event.target.checked) save({ auto_eject_enabled: false });
            else open('enable');
          }} />
        {t('autoEject.title')}
      </label>
      <p className="text-xs text-bambu-gray">{t('autoEject.help')}</p>
      {order.auto_eject_enabled && <div className="ml-5 space-y-3 border-l border-bambu-dark-tertiary pl-3">
        <label className="block text-sm">
          {t('autoEject.thresholdLabel')}
          <div className="mt-1 flex items-center gap-2">
            <input type="number" min={0.1} max={10} step={0.1} value={thresholdText}
              disabled={!mayEdit || toggle.isPending || policy.skip_check}
              className="w-24 rounded border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-white"
              aria-invalid={!validThreshold}
              onChange={(event) => setThresholdDraft({ orderId: order.id, value: event.target.value })} />
            <span>%</span>
          </div>
        </label>
        <p className="text-xs text-bambu-gray">{t('autoEject.thresholdHelp')}</p>
        {!validThreshold && <p role="alert" className="text-xs text-red-700 dark:text-red-400">{t('autoEject.thresholdInvalid')}</p>}
        {threshold > 1 && <p className="text-xs text-yellow-700 dark:text-yellow-400">{t('autoEject.thresholdWarning')}</p>}
        {mayEdit && <Button variant="secondary" size="sm"
          disabled={!validThreshold || threshold === policy.difference_threshold || toggle.isPending || policy.skip_check}
          onClick={() => save({ auto_eject_settings: { ...policy, difference_threshold: threshold } })}>
          {t('autoEject.saveThreshold')}
        </Button>}
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-1 accent-red-500" checked={policy.skip_check}
            disabled={!mayEdit || toggle.isPending}
            onChange={(event) => {
              if (event.target.checked) open('skipFirst');
              else save({ auto_eject_settings: { ...policy, skip_check: false } });
            }} />
          {t('autoEject.skipLabel')}
        </label>
        {policy.skip_check && <p role="alert" className="text-sm font-bold text-red-700 dark:text-red-400">{t('autoEject.riskTitle')}</p>}
      </div>}
      <details className="text-xs text-bambu-gray">
        <summary className="cursor-pointer text-bambu-green">{t('autoEject.how')}</summary>
        <p className="mt-2">{t('autoEject.requirements')}</p>
        <ol className="mt-2 list-decimal pl-4 space-y-1">
          <li>{t('autoEject.helpSnapshot')}</li>
          <li>{t('autoEject.helpCheck')}</li>
          <li>{t('autoEject.helpGate')}</li>
          <li>{t('autoEject.helpLighting')}</li>
          <li>{t('autoEject.helpSkip')}</li>
        </ol>
        <div className="mt-3"><ProfileHelp /></div>
      </details>
      {toggle.error && !kind && <p role="alert" className="text-xs text-red-700 dark:text-red-400">{toggle.error.message}</p>}
      {kind === 'enable' && <ConfirmModal
        title={t('autoEject.confirmTitle')}
        message={t('autoEject.confirmMessage')}
        variant="warning"
        confirmText={t('autoEject.confirmEnable')}
        confirmDisabled={!acknowledged}
        isLoading={toggle.isPending}
        onCancel={cancel}
        onConfirm={() => {
          if (acknowledged && kind === 'enable') save(policy.skip_check
            ? { auto_eject_enabled: true, auto_eject_settings: { ...policy, skip_check: false } }
            : { auto_eject_enabled: true });
        }}
      >
        <ProfileHelp />
        <label className="mt-4 flex items-start gap-2 text-sm text-white">
          <input type="checkbox" className="mt-1 accent-bambu-green" checked={acknowledged}
            disabled={toggle.isPending} onChange={(event) => setAcknowledged(event.target.checked)} />
          {t('autoEject.confirmAcknowledgement')}
        </label>
        {toggle.error && <p role="alert" className="mt-2 text-xs text-red-700 dark:text-red-400">{toggle.error.message}</p>}
      </ConfirmModal>}
      {kind === 'skipFirst' && <ConfirmModal
        key="skip-first" title={t('autoEject.skipFirstTitle')} message={t('autoEject.skipFirstMessage')}
        variant="danger" confirmText={t('autoEject.skipContinue')} onCancel={cancel}
        onConfirm={() => {
          if (kind !== 'skipFirst' || sending.current) return;
          setAcknowledged(false);
          setConfirmation({ orderId: order.id, kind: 'skipFinal' });
        }}
      >
        <p className="text-lg font-bold text-red-700 dark:text-red-400">{t('autoEject.riskTitle')}</p>
      </ConfirmModal>}
      {kind === 'skipFinal' && <ConfirmModal
        key="skip-final" title={t('autoEject.skipFinalTitle')} message={t('autoEject.skipFinalMessage')}
        variant="danger" confirmText={t('autoEject.skipConfirm')} confirmDisabled={!acknowledged}
        isLoading={toggle.isPending} onCancel={cancel}
        onConfirm={() => {
          if (acknowledged && kind === 'skipFinal') save({
            auto_eject_settings: { ...policy, skip_check: true }, auto_eject_skip_acknowledged: true,
          });
        }}
      >
        <p className="text-lg font-bold text-red-700 dark:text-red-400">{t('autoEject.riskTitle')}</p>
        <label className="mt-4 flex items-start gap-2 text-sm text-white">
          <input type="checkbox" className="mt-1 accent-red-500" checked={acknowledged}
            disabled={toggle.isPending} onChange={(event) => setAcknowledged(event.target.checked)} />
          {t('autoEject.skipAcknowledgement')}
        </label>
        {toggle.error && <p role="alert" className="mt-2 text-xs text-red-700 dark:text-red-400">{toggle.error.message}</p>}
      </ConfirmModal>}
    </div>
  );
}
