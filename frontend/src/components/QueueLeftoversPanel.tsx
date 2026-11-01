import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type QueueLeftover } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime } from '../utils/date';
import { invalidateQueueViews } from '../utils/queryInvalidation';
import { Card } from './Card';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';

/**
 * Queue rows nothing will ever clean up (backend ``services/queue_leftovers.py``): finished rows a later print
 * has superseded -- e.g. kept by a held table swap -- and AutoQueue assignments whose printer job is gone. The
 * server decides what is retirable and asks again under the lane; this panel only lists and confirms.
 */
export function QueueLeftoversPanel() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const canRead = hasPermission('queue:read_all');
  const canRetire = hasPermission('queue:delete_all');
  const query = useQuery({ queryKey: ['queue-leftovers'], queryFn: api.getQueueLeftovers, enabled: canRead });
  const items = query.data?.items ?? [];
  const retirable = items.filter(item => item.retirable);
  const retire = useMutation({
    mutationFn: () => api.retireQueueLeftovers({
      item_ids: retirable.filter(item => item.kind === 'finished_row').map(item => item.item_id!),
      orphan_ids: retirable.filter(item => item.kind === 'orphan_assignment').map(item => item.auto_item_id!),
    }),
    onSuccess: result => {
      const removed = result.retired_item_ids.length + result.retired_orphan_ids.length;
      showToast(result.skipped.length
        ? `${t('queueLeftovers.done', { count: removed })} · ${t('queueLeftovers.skipped', { count: result.skipped.length })}`
        : t('queueLeftovers.done', { count: removed }), result.skipped.length ? 'warning' : 'success');
    },
    onError: () => showToast(t('queueLeftovers.retireFailed'), 'error'),
    onSettled: () => {
      setConfirming(false);
      void queryClient.invalidateQueries({ queryKey: ['queue-leftovers'] });
      void queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
      invalidateQueueViews(queryClient);
    },
  });
  if (!canRead) return null;
  const describe = (item: QueueLeftover) => item.kind === 'orphan_assignment'
    ? t('queueLeftovers.orphan', { id: item.auto_item_id })
    : t('queueLeftovers.finishedRow', {
      printer: item.printer_name ?? '—', name: item.name ?? `#${item.item_id}`,
      when: item.completed_at ? formatDateTime(item.completed_at) : '—',
    });
  return <section id="queue-leftovers" aria-label={t('queueLeftovers.title')}>
    <Card className="p-4 space-y-4">
      <div className="flex flex-wrap justify-between items-center gap-3">
        <h2 className="text-lg font-semibold">{t('queueLeftovers.title')}</h2>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>{t('queueLeftovers.refresh')}</Button>
          {canRetire && retirable.length > 0 && <Button variant="danger" size="sm" onClick={() => setConfirming(true)}
            disabled={retire.isPending}>{t('queueLeftovers.retire', { count: retirable.length })}</Button>}
        </div>
      </div>
      <p className="text-sm text-bambu-gray">{t('queueLeftovers.description')}</p>
      {query.isPending && <p role="status">{t('common.loading')}</p>}
      {query.isError && <p role="alert">{t('queueLeftovers.failed')}</p>}
      {query.data && items.length === 0 && <p>{t('queueLeftovers.empty')}</p>}
      {items.length > 0 && <ul className="space-y-2">{items.map(item => {
        const hold = item.hold_reason ? t(`archives.swapHold.reason.${item.hold_reason}`, { defaultValue: '' }) : '';
        return <li key={`${item.kind}:${item.item_id ?? item.auto_item_id}`} data-testid="queue-leftover"
          className="rounded border border-bambu-dark-tertiary p-3 space-y-1">
          <p className="text-sm break-words">{describe(item)}</p>
          {hold && <p className="text-xs text-bambu-gray break-words">{hold}</p>}
          {item.keep_reason && <p className="text-xs text-amber-700 dark:text-amber-400 break-words">
            {t(`queueLeftovers.keep.${item.keep_reason}`, { defaultValue: t('queueLeftovers.keep.unknown') })}</p>}
        </li>;
      })}</ul>}
    </Card>
    {confirming && <ConfirmModal title={t('queueLeftovers.confirmTitle')} variant="danger"
      message={t('queueLeftovers.confirmMessage', { count: retirable.length })} confirmText={t('queueLeftovers.retire', { count: retirable.length })}
      isLoading={retire.isPending} onConfirm={() => retire.mutate()} onCancel={() => setConfirming(false)} />}
  </section>;
}
