import { useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Sparkles, Trash2, Zap, ChevronRight } from 'lucide-react';
import { api } from '../../api/client';
import type { AutoQueueItem } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { useAuth } from '../../contexts/AuthContext';

/**
 * Top-of-page panel that surfaces pending auto-queue items — the router
 * layer above per-printer queues. Hidden entirely when nothing is
 * pending so the dashboard stays clean for installs that don't use it.
 */
export function AutoQueuePanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();

  const canAssign = hasPermission('queue:reorder');
  const canDelete = hasPermission('queue:delete_all');

  const { data: items } = useQuery({
    queryKey: ['auto-queue', 'pending'],
    queryFn: () => api.getAutoQueue('pending'),
    refetchInterval: 15000,
  });

  const cancelMutation = useMutation({
    mutationFn: (id: number) => api.removeFromAutoQueue(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
      showToast(t('autoQueue.cancelled'));
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const assignNowMutation = useMutation({
    mutationFn: (id: number) => api.assignAutoQueueNow(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
      queryClient.invalidateQueries({ queryKey: ['queue'] });
      queryClient.invalidateQueries({ queryKey: ['queues'] });
      showToast(t('autoQueue.assigned'));
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const cancelBatchMutation = useMutation({
    mutationFn: (batchId: string) => api.cancelAutoQueueBatch(batchId),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
      showToast(t('autoQueue.batchCancelled', { count: data.affected }));
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  // Group items by batch_id so multi-copy submissions render as one row.
  const grouped = useMemo(() => {
    if (!items) return [] as Array<{ key: string; batchId: string | null; items: AutoQueueItem[] }>;
    const seen = new Map<string, AutoQueueItem[]>();
    for (const it of items) {
      const key = it.batch_id ?? `single-${it.id}`;
      const arr = seen.get(key) ?? [];
      arr.push(it);
      seen.set(key, arr);
    }
    return [...seen.entries()].map(([key, group]) => ({
      key,
      batchId: group[0].batch_id,
      items: group.sort((a, b) => a.position - b.position),
    }));
  }, [items]);

  if (!items || items.length === 0) return null;

  return (
    <div className="mb-4 bg-bambu-dark-secondary border border-bambu-green/30 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-3">
        <Sparkles className="w-4 h-4 text-bambu-green" />
        <h2 className="text-sm font-semibold text-white">{t('autoQueue.title')}</h2>
        <span className="text-xs text-bambu-gray">
          ({t('autoQueue.itemCount', { count: items.length })})
        </span>
      </div>

      <div className="space-y-1.5">
        {grouped.map(({ key, batchId, items: groupItems }) => {
          const head = groupItems[0];
          const isBatch = groupItems.length > 1;
          const label = head.archive_name || head.library_file_name || `#${head.id}`;
          const targetModel = head.target_model || t('autoQueue.anyModel');
          const targetLocation = head.target_location;
          const waitingReason = head.waiting_reason;

          return (
            <div
              key={key}
              className="flex items-center gap-3 p-2.5 bg-bambu-dark rounded border border-bambu-dark-tertiary hover:border-bambu-green/50 transition-colors"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-sm text-white truncate">
                  <span className="truncate">{label}</span>
                  {isBatch && (
                    <span className="px-1.5 py-0.5 text-xs bg-bambu-green/20 text-bambu-green rounded shrink-0">
                      ×{groupItems.length}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2 text-xs text-bambu-gray flex-wrap mt-0.5">
                  <span>
                    <ChevronRight className="inline w-3 h-3" />
                    {targetModel}
                  </span>
                  {targetLocation && <span>· {targetLocation}</span>}
                  {head.force_color_match && <span>· {t('autoQueue.exactColor')}</span>}
                  {waitingReason && (
                    <span className="text-yellow-400">· {waitingReason}</span>
                  )}
                </div>
              </div>

              {canAssign && !isBatch && (
                <button
                  type="button"
                  onClick={() => assignNowMutation.mutate(head.id)}
                  disabled={assignNowMutation.isPending}
                  className="px-2 py-1 text-xs text-bambu-green hover:bg-bambu-green/10 rounded inline-flex items-center gap-1 disabled:opacity-40"
                  title={t('autoQueue.assignNow')}
                >
                  <Zap className="w-3.5 h-3.5" />
                  {t('autoQueue.assignNow')}
                </button>
              )}
              {canDelete && (
                <button
                  type="button"
                  onClick={() => {
                    if (isBatch && batchId) {
                      cancelBatchMutation.mutate(batchId);
                    } else {
                      cancelMutation.mutate(head.id);
                    }
                  }}
                  disabled={cancelMutation.isPending || cancelBatchMutation.isPending}
                  className="px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 rounded inline-flex items-center gap-1 disabled:opacity-40"
                  title={t('common.cancel')}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
