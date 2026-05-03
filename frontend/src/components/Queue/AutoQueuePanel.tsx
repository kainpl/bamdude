import { useMemo, useRef, useState, type DragEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Sparkles, Trash2, Upload, Zap, ChevronRight } from 'lucide-react';
import { api } from '../../api/client';
import type { AutoQueueItem } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { useAuth } from '../../contexts/AuthContext';
import { PrintModal } from '../PrintModal';

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
  const canDrop = hasPermission('queue:create');

  // Drag-drop: drop a sliced file on the panel → upload to library + open
  // PrintModal locked to 'auto' mode (no specific printer; the auto-queue
  // router picks one at dispatch).
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [isDropUploading, setIsDropUploading] = useState(false);
  const [printAfterUpload, setPrintAfterUpload] = useState<{ id: number; filename: string } | null>(null);
  const dragCounterRef = useRef(0);

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

  const handleDragEnter = (e: DragEvent<HTMLDivElement>) => {
    if (!canDrop) return;
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    dragCounterRef.current += 1;
    if (dragCounterRef.current === 1) setIsDraggingFile(true);
  };
  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!canDrop) return;
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  };
  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    if (!canDrop) return;
    e.preventDefault();
    dragCounterRef.current = Math.max(0, dragCounterRef.current - 1);
    if (dragCounterRef.current === 0) setIsDraggingFile(false);
  };
  const handleDrop = async (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    dragCounterRef.current = 0;
    setIsDraggingFile(false);
    if (!canDrop) return;

    const file = e.dataTransfer.files[0];
    if (!file) return;

    const lower = file.name.toLowerCase();
    if (!lower.endsWith('.gcode') && !lower.includes('.gcode.')) {
      showToast(t('printers.dropNotPrintable'), 'error');
      return;
    }

    setIsDropUploading(true);
    try {
      const result = await api.uploadLibraryFile(file, null);
      // No printer compatibility check here — auto-queue router filters
      // by sliced_for_model + target_model at dispatch time, and the
      // operator picks target constraints in the modal anyway.
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      setPrintAfterUpload({ id: result.id, filename: result.filename });
    } catch {
      showToast(t('common.uploadFailed'), 'error');
    } finally {
      setIsDropUploading(false);
    }
  };

  const isEmpty = !items || items.length === 0;

  return (
    <div
      className="relative"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
    <div className="mb-4 bg-bambu-dark-secondary border border-bambu-green/30 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-3">
        <Sparkles className="w-4 h-4 text-bambu-green" />
        <h2 className="text-sm font-semibold text-white">{t('autoQueue.title')}</h2>
        <span className="text-xs text-bambu-gray">
          ({t('autoQueue.itemCount', { count: items?.length ?? 0 })})
        </span>
      </div>

      {isEmpty && (
        <p className="text-xs text-bambu-gray italic">{t('autoQueue.emptyHint')}</p>
      )}

      {!isEmpty && (
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
      )}
    </div>
      {(isDraggingFile || isDropUploading) && (
        <div className="absolute inset-0 z-30 pointer-events-none flex items-center justify-center rounded-lg border-2 border-dashed border-bambu-green bg-bambu-green/10 backdrop-blur-sm">
          <div className="flex flex-col items-center gap-2 text-center px-4">
            {isDropUploading ? (
              <>
                <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
                <p className="text-sm font-medium text-white">{t('common.uploading')}</p>
              </>
            ) : (
              <>
                <Upload className="w-8 h-8 text-bambu-green" />
                <p className="text-sm font-medium text-white">{t('autoQueue.dropToAuto')}</p>
                <p className="text-xs text-bambu-green">{t('autoQueue.dropToAutoHint')}</p>
              </>
            )}
          </div>
        </div>
      )}
      {printAfterUpload && (
        <PrintModal
          mode="add-to-queue"
          libraryFileId={printAfterUpload.id}
          archiveName={printAfterUpload.filename}
          initialDispatchMode="auto"
          lockDispatchMode
          onClose={() => setPrintAfterUpload(null)}
          onSuccess={() => {
            setPrintAfterUpload(null);
            queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
            queryClient.invalidateQueries({ queryKey: ['queue'] });
          }}
        />
      )}
    </div>
  );
}
