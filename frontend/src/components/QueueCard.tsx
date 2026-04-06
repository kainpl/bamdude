import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Pause,
  Play,
  AlertCircle,
  Clock,
  ChevronDown,
  ChevronUp,
  X,
  Loader2,
  CircleCheck,
  Ban,
} from 'lucide-react';
import { api } from '../api/client';
import type { PrinterQueue, PrintQueueItem, Permission } from '../api/client';
import { Card, CardContent } from './Card';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { formatETA } from '../utils/date';

interface QueueCardProps {
  queue: PrinterQueue;
  compact?: boolean;
}

const STATUS_BORDER: Record<string, string> = {
  idle: 'border-bambu-green/30',
  printing: 'border-blue-400/30',
  paused: 'border-yellow-400/30',
  error: 'border-red-400/30',
};

const STATUS_BADGE: Record<string, { bg: string; text: string }> = {
  idle: { bg: 'bg-bambu-green/20', text: 'text-bambu-green' },
  printing: { bg: 'bg-blue-400/20', text: 'text-blue-400' },
  paused: { bg: 'bg-yellow-400/20', text: 'text-yellow-400' },
  error: { bg: 'bg-red-400/20', text: 'text-red-400' },
};

function StatusBadge({ status, t }: { status: string; t: (key: string) => string }) {
  const style = STATUS_BADGE[status] ?? STATUS_BADGE.idle;
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${style.bg} ${style.text}`}>
      {t(`queueCard.status.${status}`)}
    </span>
  );
}

export function QueueCard({ queue, compact = false }: QueueCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const [expanded, setExpanded] = useState(false);

  // Fetch pending items
  const { data: pendingItems } = useQuery({
    queryKey: ['queue', queue.printer_id, 'pending'],
    queryFn: () => api.getQueue(queue.printer_id, 'pending'),
    refetchInterval: 30000,
  });

  // Fetch printer status
  const { data: status } = useQuery({
    queryKey: ['printerStatus', queue.printer_id],
    queryFn: () => api.getPrinterStatus(queue.printer_id),
    refetchInterval: 5000,
  });

  // Pause/Resume queue mutation
  const toggleQueueMutation = useMutation({
    mutationFn: (newStatus: 'idle' | 'paused') => api.updateQueue(queue.id, { status: newStatus }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queues'] });
      queryClient.invalidateQueries({ queryKey: ['queue', queue.printer_id] });
      showToast(t('queueCard.toast.statusUpdated'), 'success');
    },
    onError: (err: Error) => {
      showToast(err.message, 'error');
    },
  });

  // Clear plate mutation
  const clearPlateMutation = useMutation({
    mutationFn: () => api.clearPlate(queue.printer_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue', queue.printer_id] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', queue.printer_id] });
      showToast(t('queue.clearPlateSuccess'), 'success');
    },
    onError: (err: Error) => {
      showToast(err.message, 'error');
    },
  });

  // Start queue item mutation
  const startItemMutation = useMutation({
    mutationFn: (itemId: number) => api.startQueueItem(itemId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue', queue.printer_id] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', queue.printer_id] });
      showToast(t('queueCard.toast.itemStarted'), 'success');
    },
    onError: (err: Error) => {
      showToast(err.message, 'error');
    },
  });

  // Cancel queue item mutation
  const cancelItemMutation = useMutation({
    mutationFn: (itemId: number) => api.cancelQueueItem(itemId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue', queue.printer_id] });
      showToast(t('queue.toast.cancelled'), 'success');
    },
    onError: (err: Error) => {
      showToast(err.message, 'error');
    },
  });

  const borderClass = STATUS_BORDER[queue.status] ?? STATUS_BORDER.idle;
  const pending = pendingItems ?? [];
  const pendingCount = pending.length;

  // --- Compact (S) mode ---
  if (compact) {
    return (
      <Card className={`${borderClass} border`}>
        <CardContent className="!p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-bold text-white truncate">
              {queue.printer_name ?? `Printer #${queue.printer_id}`}
            </span>
            <div className="flex items-center gap-2 flex-shrink-0">
              <StatusBadge status={queue.status} t={t} />
              {queue.status === 'printing' && status?.progress != null && (
                <span className="text-xs text-blue-400 font-medium">{status.progress}%</span>
              )}
              {pendingCount > 0 && (
                <span className="text-xs px-1.5 py-0.5 bg-yellow-400/20 text-yellow-400 rounded">
                  {pendingCount}
                </span>
              )}
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  // --- Full (M) mode ---

  const hasAutoDispatchItems = pendingItems?.some(item => !item.manual_start) ?? false;
  const needsClearPlate =
    (status?.state === 'FINISH' || status?.state === 'FAILED') &&
    !status?.plate_cleared &&
    hasAutoDispatchItems;

  // Find the current printing item (first printing-status item from pending query, or use status info)
  const currentPrintName = status?.subtask_name || status?.current_print;
  const currentThumbnail = status?.cover_url;

  const handlePauseResume = () => {
    if (!hasPermission('queue:update_all')) return;
    if (queue.status === 'idle') {
      toggleQueueMutation.mutate('paused');
    } else if (queue.status === 'paused' || queue.status === 'error') {
      toggleQueueMutation.mutate('idle');
    }
  };

  const visibleItems = expanded ? pending : pending.slice(0, 2);
  const hiddenCount = pendingCount - 2;

  return (
    <Card className={`${borderClass} border`}>
      <CardContent className="!p-4 space-y-3">
        {/* Header */}
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-bold text-white truncate">
            {queue.printer_name ?? `Printer #${queue.printer_id}`}
          </h3>
          <div className="flex items-center gap-2 flex-shrink-0">
            <StatusBadge status={queue.status} t={t} />
            {(queue.status === 'idle' || queue.status === 'paused' || queue.status === 'error') && (
              <button
                onClick={handlePauseResume}
                disabled={toggleQueueMutation.isPending || !hasPermission('queue:update_all')}
                className="p-1 rounded hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
                title={
                  queue.status === 'idle'
                    ? t('queueCard.pauseQueue')
                    : t('queueCard.resumeQueue')
                }
              >
                {toggleQueueMutation.isPending ? (
                  <Loader2 className="w-4 h-4 text-bambu-gray animate-spin" />
                ) : queue.status === 'idle' ? (
                  <Pause className="w-4 h-4 text-bambu-gray" />
                ) : (
                  <Play className="w-4 h-4 text-bambu-gray" />
                )}
              </button>
            )}
          </div>
        </div>

        {/* Error banner */}
        {queue.status === 'error' && (
          <div className="flex items-center gap-2 p-2 rounded-lg bg-red-500/10 border border-red-500/20">
            <AlertCircle className="w-4 h-4 text-red-400 flex-shrink-0" />
            <span className="text-xs text-red-400 flex-1">{t('queueCard.errorState')}</span>
            <button
              onClick={() => {
                if (hasPermission('queue:update_all')) {
                  toggleQueueMutation.mutate('idle');
                }
              }}
              disabled={toggleQueueMutation.isPending || !hasPermission('queue:update_all')}
              className="text-xs px-2 py-0.5 rounded bg-red-500/20 text-red-400 hover:bg-red-500/30 transition-colors disabled:opacity-50"
            >
              {t('queueCard.resumeQueue')}
            </button>
          </div>
        )}

        {/* Current print section */}
        {queue.status === 'printing' && currentPrintName && status && (
          <div className="p-2 rounded-lg bg-bambu-dark">
            <div className="flex items-start gap-3">
              {currentThumbnail && (
                <img
                  src={currentThumbnail}
                  alt=""
                  className="w-10 h-10 rounded object-cover flex-shrink-0"
                />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-xs text-bambu-gray">{t('queueCard.currentPrint')}</p>
                <p className="text-sm text-white truncate">{currentPrintName}</p>
                {/* Progress bar */}
                <div className="flex items-center gap-2 mt-1.5">
                  <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-1.5">
                    <div
                      className="bg-blue-400 h-1.5 rounded-full transition-all"
                      style={{ width: `${status.progress || 0}%` }}
                    />
                  </div>
                  <span className="text-xs text-blue-400 font-medium flex-shrink-0">
                    {status.progress ?? 0}%
                  </span>
                </div>
                {/* ETA / remaining */}
                {status.remaining_time != null && status.remaining_time > 0 && (
                  <div className="flex items-center gap-1 mt-1">
                    <Clock className="w-3 h-3 text-bambu-gray" />
                    <span className="text-xs text-bambu-gray">
                      {formatETA(status.remaining_time)}
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Clear plate section */}
        {needsClearPlate && (
          <div>
            {clearPlateMutation.isSuccess ? (
              <div className="w-full py-2 px-3 rounded-lg bg-bambu-green/10 border border-bambu-green/20 text-bambu-green text-sm flex items-center justify-center gap-2">
                <CircleCheck className="w-4 h-4" />
                {t('queue.plateReady')}
              </div>
            ) : (
              <button
                onClick={() => clearPlateMutation.mutate()}
                disabled={clearPlateMutation.isPending || !hasPermission('printers:clear_plate')}
                className="w-full py-2 px-3 rounded-lg bg-bambu-green/20 border border-bambu-green/40 text-bambu-green hover:bg-bambu-green/30 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-50"
              >
                {clearPlateMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <CircleCheck className="w-4 h-4" />
                )}
                {t('queue.clearPlate')}
              </button>
            )}
          </div>
        )}

        {/* Pending items list */}
        {pendingCount > 0 && (
          <div className="space-y-1">
            {visibleItems.map((item: PrintQueueItem) => (
              <PendingItemRow
                key={item.id}
                item={item}
                onStart={() => startItemMutation.mutate(item.id)}
                onCancel={() => cancelItemMutation.mutate(item.id)}
                startPending={startItemMutation.isPending}
                cancelPending={cancelItemMutation.isPending}
                hasPermission={hasPermission}
                t={t}
              />
            ))}
            {/* Gradient fade + expand button */}
            {hiddenCount > 0 && !expanded && (
              <div className="relative">
                <div className="absolute inset-x-0 -top-6 h-6 bg-gradient-to-t from-bambu-dark-secondary to-transparent pointer-events-none" />
                <button
                  onClick={() => setExpanded(true)}
                  className="w-full flex items-center justify-center gap-1 py-1 text-xs text-bambu-gray hover:text-white transition-colors"
                >
                  <ChevronDown className="w-3 h-3" />
                  {t('queueCard.showMore', { count: hiddenCount })}
                </button>
              </div>
            )}
            {expanded && hiddenCount > 0 && (
              <button
                onClick={() => setExpanded(false)}
                className="w-full flex items-center justify-center gap-1 py-1 text-xs text-bambu-gray hover:text-white transition-colors"
              >
                <ChevronUp className="w-3 h-3" />
                {t('queueCard.showLess')}
              </button>
            )}
          </div>
        )}

        {/* Footer counters */}
        <div className="text-xs text-bambu-gray pt-1 border-t border-bambu-dark-tertiary">
          {t('queueCard.footer.pending', { count: queue.pending_count })}
          {' \u00B7 '}
          {t('queueCard.footer.done', { count: queue.completed_count })}
          {queue.failed_count > 0 && <>{' \u00B7 '}{t('queueCard.footer.failed', { count: queue.failed_count })}</>}
          {queue.cancelled_count > 0 && <>{' \u00B7 '}{t('queueCard.footer.cancelled', { count: queue.cancelled_count })}</>}
        </div>
      </CardContent>
    </Card>
  );
}

interface PendingItemRowProps {
  item: PrintQueueItem;
  onStart: () => void;
  onCancel: () => void;
  startPending: boolean;
  cancelPending: boolean;
  hasPermission: (perm: Permission) => boolean;
  t: (key: string, opts?: Record<string, unknown>) => string;
}

function PendingItemRow({
  item,
  onStart,
  onCancel,
  startPending,
  cancelPending,
  hasPermission,
  t,
}: PendingItemRowProps) {
  const name = item.archive_name || item.library_file_name || `File #${item.archive_id || item.library_file_id}`;

  return (
    <div className="flex items-center gap-2 py-1.5 px-2 rounded hover:bg-bambu-dark transition-colors group">
      <span className="text-xs text-bambu-gray w-5 text-right flex-shrink-0">
        {item.position}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-white truncate">{name}</p>
        {item.waiting_reason && (
          <p className="text-[10px] text-yellow-400 truncate">{item.waiting_reason}</p>
        )}
      </div>
      <div className="flex items-center gap-1 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
        {item.manual_start && (
          <button
            onClick={onStart}
            disabled={startPending || !hasPermission('queue:update_all')}
            className="p-0.5 rounded hover:bg-bambu-green/20 text-bambu-green disabled:opacity-50"
            title={t('queueCard.startItem')}
          >
            <Play className="w-3.5 h-3.5" />
          </button>
        )}
        <button
          onClick={onCancel}
          disabled={cancelPending || !hasPermission('queue:update_all')}
          className="p-0.5 rounded hover:bg-red-500/20 text-red-400 disabled:opacity-50"
          title={t('queueCard.cancelItem')}
        >
          <X className="w-3.5 h-3.5" />
        </button>
        <button
          disabled
          className="p-0.5 rounded text-bambu-gray opacity-50 cursor-not-allowed"
          title={t('queueCard.editItem')}
        >
          <Ban className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  );
}
