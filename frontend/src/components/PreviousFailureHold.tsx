import { AlertTriangle, Loader2 } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type PrinterQueue } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { invalidateQueueViews } from '../utils/queryInvalidation';

/**
 * The printer's last print failed, and jobs set to run «only if the previous print
 * succeeded» are held for it — its own queue's and AutoQueue work waiting with
 * `previous_print_failed` alike (G03 D4). The note says so wherever the printer's
 * queue widget is; the release is the operator's, under the queue update right.
 *
 * The flag is the server's (`PrinterQueue.previous_print_failed`); nothing here
 * decides whether a failure holds anything.
 */
export function PreviousFailureHold({ queue }: { queue: PrinterQueue | undefined }) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const acknowledge = useMutation({
    mutationFn: (queueId: number) => api.acknowledgeQueueFailure(queueId),
    onSuccess: ({ acknowledged }) => {
      invalidateQueueViews(queryClient);
      // AutoQueue rows wait on this printer's failure; their reasons move with it.
      void queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
      showToast(
        acknowledged ? t('queue.previousFailure.acknowledged') : t('queue.previousFailure.nothingToAcknowledge'),
        acknowledged ? 'success' : 'info',
      );
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  if (!queue?.previous_print_failed) return null;
  return (
    <div className="mb-3 p-3 rounded-lg border border-yellow-400/30 bg-yellow-500/5 flex items-start gap-2 text-xs">
      <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5 text-yellow-600 dark:text-yellow-400" />
      <p className="min-w-0 flex-1 text-yellow-700 dark:text-yellow-400">{t('queue.previousFailure.note')}</p>
      {hasPermission('queue:update_all') && (
        <button
          type="button"
          onClick={() => acknowledge.mutate(queue.id)}
          disabled={acknowledge.isPending}
          className="flex-shrink-0 px-2 py-1 rounded bg-yellow-500/15 text-yellow-700 dark:text-yellow-400 hover:bg-yellow-500/25 disabled:opacity-50 flex items-center gap-1 font-medium"
        >
          {acknowledge.isPending && <Loader2 className="w-3 h-3 animate-spin" />}
          {t('queue.previousFailure.acknowledge')}
        </button>
      )}
    </div>
  );
}
