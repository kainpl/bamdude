import { useRef, useState, type DragEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Pause,
  Play,
  Square,
  AlertCircle,
  Clock,
  ChevronDown,
  ChevronUp,
  X,
  Loader2,
  CircleCheck,
  ArrowUp,
  ArrowDown,
  ChevronsUp,
  ChevronsDown,
  Layers,
  Pencil,
  MoreVertical,
  Upload,
} from 'lucide-react';
import { BatchActionDialog } from './Queue/BatchActionDialog';
import { PrintModal } from './PrintModal';
import { api, withStreamToken } from '../api/client';
import type { PrinterQueue, PrintQueueItem, Permission } from '../api/client';
import { Card, CardContent } from './Card';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { formatETA, formatDuration } from '../utils/date';
import { mapModelCode } from '../utils/printer';

interface QueueCardProps {
  queue: PrinterQueue;
  compact?: boolean;
  onEditItem?: (item: PrintQueueItem) => void;
}

// 6 distinct hues for batch grouping — intentionally avoids green (success)
// and red (error) so the stripe can't be confused with status colors.
// Border color goes inline via `style` (Tailwind JIT can miss dynamic class
// suffixes); badge classes stay as full literals so the tint is JIT-safe.
const BATCH_PALETTE: { color: string; badge: string }[] = [
  { color: '#60a5fa', badge: 'bg-blue-400/20 text-blue-400' },
  { color: '#c084fc', badge: 'bg-purple-400/20 text-purple-400' },
  { color: '#fbbf24', badge: 'bg-amber-400/20 text-amber-400' },
  { color: '#2dd4bf', badge: 'bg-teal-400/20 text-teal-400' },
  { color: '#f472b6', badge: 'bg-pink-400/20 text-pink-400' },
  { color: '#22d3ee', badge: 'bg-cyan-400/20 text-cyan-400' },
];

function getBatchAccent(batchId: string): (typeof BATCH_PALETTE)[number] {
  // djb2 hash — deterministic, spreads short strings well enough for 6 buckets.
  let h = 5381;
  for (let i = 0; i < batchId.length; i++) {
    h = ((h << 5) + h + batchId.charCodeAt(i)) | 0;
  }
  return BATCH_PALETTE[Math.abs(h) % BATCH_PALETTE.length];
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

export function QueueCard({ queue, compact = false, onEditItem }: QueueCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const [expanded, setExpanded] = useState(false);

  // Drag-drop: drop a sliced file on the queue card → upload to library +
  // open PrintModal locked to this printer in 'specific' (add-to-queue)
  // mode. Mirrors the printer-card direct-print flow but lands the job in
  // the queue instead of starting it immediately, and ignores printer
  // status (idle/printing/paused) — adding to a queue is always allowed.
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [isDropUploading, setIsDropUploading] = useState(false);
  const [printAfterUpload, setPrintAfterUpload] = useState<{ id: number; filename: string } | null>(null);
  const dragCounterRef = useRef(0);

  // Pull system time-format so ETA respects the user's 12h/24h choice.
  // Cached globally — shared with everywhere else that reads settings.
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 60_000,
  });
  const timeFormat = (settings?.time_format ?? 'system') as 'system' | '12h' | '24h';

  // Fetch pending items
  const { data: pendingItems } = useQuery({
    queryKey: ['queue', queue.printer_id, 'pending'],
    queryFn: () => api.getQueue(queue.printer_id, 'pending'),
    refetchInterval: 30000,
  });

  // Fetch failed + skipped items for the "Issues" section (retry / unskip).
  // Combined into one query to avoid double-requests; server returns them
  // in creation order — we slice in the client.
  const { data: failedItems } = useQuery({
    queryKey: ['queue', queue.printer_id, 'failed'],
    queryFn: () => api.getQueue(queue.printer_id, 'failed'),
    refetchInterval: 30000,
  });
  const { data: skippedItems } = useQuery({
    queryKey: ['queue', queue.printer_id, 'skipped'],
    queryFn: () => api.getQueue(queue.printer_id, 'skipped'),
    refetchInterval: 30000,
  });

  // Fetch printing item (real or virtual) — used to get source badge for
  // external / direct-dispatch prints.  Virtual items have is_virtual=true.
  const { data: printingItems } = useQuery({
    queryKey: ['queue', queue.printer_id, 'printing'],
    queryFn: () => api.getQueue(queue.printer_id, 'printing'),
    refetchInterval: 10000,
  });
  const currentItem = printingItems?.[0];

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

  // Printer-control mutations for the current-print card (works for
  // real queue items AND virtual external/direct-dispatch items).
  const pausePrintMutation = useMutation({
    mutationFn: () => api.pausePrint(queue.printer_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', queue.printer_id] });
      showToast(t('queueCard.toast.paused'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });
  const resumePrintMutation = useMutation({
    mutationFn: () => api.resumePrint(queue.printer_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', queue.printer_id] });
      showToast(t('queueCard.toast.resumed'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });
  const stopPrintMutation = useMutation({
    mutationFn: () => api.stopPrint(queue.printer_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', queue.printer_id] });
      queryClient.invalidateQueries({ queryKey: ['queue', queue.printer_id] });
      showToast(t('queueCard.toast.stopped'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  // ── Queue item commands (reorder / bump / clone / skip / toggle / retry) ──
  const invalidateQueue = () =>
    queryClient.invalidateQueries({ queryKey: ['queue', queue.printer_id] });

  const reorderMutation = useMutation({
    mutationFn: ({ id, direction }: { id: number; direction: 'up' | 'down' }) =>
      api.reorderQueueItem(id, direction),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.moved'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const bumpMutation = useMutation({
    mutationFn: (id: number) => api.bumpQueueItem(id),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.bumped'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const bumpBottomMutation = useMutation({
    mutationFn: (id: number) => api.bumpQueueItemBottom(id),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.bumpedBottom'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const cloneMutation = useMutation({
    mutationFn: ({ id, scope }: { id: number; scope: 'single' | 'batch' }) =>
      api.cloneQueueItem(id, scope),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.cloned'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const skipMutation = useMutation({
    mutationFn: (id: number) => api.skipQueueItem(id),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.skipped'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const toggleManualStartMutation = useMutation({
    mutationFn: (id: number) => api.toggleManualStart(id),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.manualStartToggled'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const cancelBatchMutation = useMutation({
    mutationFn: (batchId: string) => api.cancelBatch(batchId),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.batchCancelled'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const cloneBatchMutation = useMutation({
    mutationFn: (batchId: string) => api.cloneBatch(batchId, 'batch'),
    onSuccess: () => {
      invalidateQueue();
      showToast(t('queueCard.toast.batchCloned'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const borderClass = STATUS_BORDER[queue.status] ?? STATUS_BORDER.idle;
  const pending = pendingItems ?? [];
  const pendingCount = pending.length;

  const canDrop = hasPermission('queue:create');

  const handleCardDragEnter = (e: DragEvent<HTMLDivElement>) => {
    if (!canDrop) return;
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    dragCounterRef.current += 1;
    if (dragCounterRef.current === 1) setIsDraggingFile(true);
  };
  const handleCardDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!canDrop) return;
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  };
  const handleCardDragLeave = (e: DragEvent<HTMLDivElement>) => {
    if (!canDrop) return;
    e.preventDefault();
    dragCounterRef.current = Math.max(0, dragCounterRef.current - 1);
    if (dragCounterRef.current === 0) setIsDraggingFile(false);
  };
  const handleCardDrop = async (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    dragCounterRef.current = 0;
    setIsDraggingFile(false);
    if (!canDrop) return;

    const file = e.dataTransfer.files[0];
    if (!file) return;

    // Only sliced/printable formats — same gate as the printer-card direct-print drop.
    const lower = file.name.toLowerCase();
    if (!lower.endsWith('.gcode') && !lower.includes('.gcode.')) {
      showToast(t('printers.dropNotPrintable'), 'error');
      return;
    }

    setIsDropUploading(true);
    try {
      const result = await api.uploadLibraryFile(file, null);

      // Compatibility check against printer model — abort + delete the
      // transient upload if mismatched, same UX as the printer-card flow.
      const slicedFor = (result.metadata as Record<string, unknown>)?.sliced_for_model as string | undefined;
      const printerModel = mapModelCode(queue.printer_model);
      if (slicedFor && printerModel && slicedFor.toLowerCase() !== printerModel.toLowerCase()) {
        await api.deleteLibraryFile(result.id).catch(() => {});
        showToast(
          t('printers.incompatibleFile', { slicedFor, printerModel }),
          'error',
        );
        return;
      }

      // Surface the new library file in File Manager immediately —
      // without this the cached list would stay stale for up to the
      // global 60s staleTime and the operator would think the upload
      // never happened.
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      setPrintAfterUpload({ id: result.id, filename: result.filename });
    } catch {
      showToast(t('common.uploadFailed'), 'error');
    } finally {
      setIsDropUploading(false);
    }
  };

  const dropOverlay = (isDraggingFile || isDropUploading) ? (
    <div className="absolute inset-0 z-30 pointer-events-none flex items-center justify-center rounded-xl border-2 border-dashed border-bambu-green bg-bambu-green/10 backdrop-blur-sm">
      <div className="flex flex-col items-center gap-2 text-center px-4">
        {isDropUploading ? (
          <>
            <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
            <p className="text-sm font-medium text-white">{t('common.uploading')}</p>
          </>
        ) : (
          <>
            <Upload className="w-8 h-8 text-bambu-green" />
            <p className="text-sm font-medium text-white">{t('queueCard.dropToQueue')}</p>
            <p className="text-xs text-bambu-green">{t('queueCard.dropToQueueHint')}</p>
          </>
        )}
      </div>
    </div>
  ) : null;

  const dropPrintModal = printAfterUpload ? (
    <PrintModal
      mode="add-to-queue"
      libraryFileId={printAfterUpload.id}
      archiveName={printAfterUpload.filename}
      initialSelectedPrinterIds={[queue.printer_id]}
      lockDispatchMode
      onClose={() => setPrintAfterUpload(null)}
      onSuccess={() => {
        setPrintAfterUpload(null);
        queryClient.invalidateQueries({ queryKey: ['queue', queue.printer_id] });
        queryClient.invalidateQueries({ queryKey: ['queues'] });
      }}
    />
  ) : null;

  // --- Compact (S) mode ---
  if (compact) {
    return (
      <div
        className="relative"
        onDragEnter={handleCardDragEnter}
        onDragOver={handleCardDragOver}
        onDragLeave={handleCardDragLeave}
        onDrop={handleCardDrop}
      >
        <Card className={`${borderClass} border`}>
          <CardContent className="!p-3">
            <div className="flex items-center justify-between gap-2">
              <Link
                to={`/#printer-${queue.printer_id}`}
                className="text-sm font-bold text-white truncate hover:text-bambu-green transition-colors"
                title={t('queueCard.goToPrinter')}
              >
                {queue.printer_name ?? `Printer #${queue.printer_id}`}
              </Link>
              <div className="flex items-center gap-2 flex-shrink-0">
                <StatusBadge status={queue.status} t={t} />
                {queue.status === 'printing' && status?.progress != null &&
                  (status.state === 'RUNNING' || status.state === 'PAUSE') && (
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
        {dropOverlay}
        {dropPrintModal}
      </div>
    );
  }

  // --- Full (M) mode ---

  const hasAutoDispatchItems = pendingItems?.some(item => !item.manual_start) ?? false;
  const needsClearPlate =
    (status?.state === 'FINISH' || status?.state === 'FAILED') &&
    !!status?.awaiting_plate_clear &&
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
    <div
      className="relative"
      onDragEnter={handleCardDragEnter}
      onDragOver={handleCardDragOver}
      onDragLeave={handleCardDragLeave}
      onDrop={handleCardDrop}
    >
    <Card className={`${borderClass} border`}>
      <CardContent className="!p-4 space-y-3">
        {/* Header */}
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-bold truncate">
            <Link
              to={`/#printer-${queue.printer_id}`}
              className="text-white hover:text-bambu-green transition-colors"
              title={t('queueCard.goToPrinter')}
            >
              {queue.printer_name ?? `Printer #${queue.printer_id}`}
            </Link>
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
          <div className="p-3 rounded-lg bg-bambu-dark">
            <div className="flex items-start gap-3">
              {currentThumbnail ? (
                <img
                  src={withStreamToken(currentThumbnail)}
                  alt=""
                  className="w-20 h-20 rounded-lg object-cover flex-shrink-0 bg-bambu-dark-tertiary"
                />
              ) : (
                <div className="w-20 h-20 rounded-lg bg-bambu-dark-tertiary flex-shrink-0" />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5 mb-1">
                  <p className="text-sm text-bambu-gray">{t('queueCard.currentPrint')}</p>
                  {currentItem?.source && currentItem.source !== 'bamdude_queue' && (
                    <span className="text-[10px] px-1 py-0.5 rounded bg-amber-500/20 text-amber-400 font-medium">
                      {t(`queue.source.${currentItem.source}`)}
                    </span>
                  )}
                </div>
                <p className="text-sm text-white truncate mb-2">{currentPrintName}</p>
                {/* Progress / ETA / layer fields are only live while the printer
                    is actually RUNNING or PAUSE. Between dispatch and the first
                    push_status tick (FINISH from the prior print, or PREPARE
                    while heating) the stale mc_percent would otherwise flash —
                    100% from the previous job for a few seconds before snapping
                    back to 0 (upstream #950286ad). */}
                {(status.state === 'RUNNING' || status.state === 'PAUSE') ? (
                  <>
                    <div className="flex items-center gap-2">
                      <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-2">
                        <div
                          className="bg-blue-400 h-2 rounded-full transition-all"
                          style={{ width: `${status.progress || 0}%` }}
                        />
                      </div>
                      <span className="text-sm text-white font-medium flex-shrink-0">
                        {Math.round(status.progress ?? 0)}%
                      </span>
                    </div>
                    <div className="flex items-center gap-3 mt-2 text-xs text-bambu-gray">
                      {status.remaining_time != null && status.remaining_time > 0 && (
                        <>
                          <span className="flex items-center gap-1">
                            <Clock className="w-3 h-3" />
                            {formatDuration(status.remaining_time * 60)}
                          </span>
                          <span className="text-bambu-green font-medium">
                            ETA {formatETA(status.remaining_time, timeFormat, t)}
                          </span>
                        </>
                      )}
                      {status.layer_num != null && status.total_layers != null && status.total_layers > 0 && (
                        <span className="flex items-center gap-1">
                          <Layers className="w-3 h-3" />
                          {status.layer_num}/{status.total_layers}
                        </span>
                      )}
                    </div>
                  </>
                ) : (
                  <div className="flex items-center gap-2">
                    <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-2">
                      <div className="bg-blue-400 h-2 rounded-full" style={{ width: '0%' }} />
                    </div>
                    <span className="text-sm text-white font-medium flex-shrink-0">0%</span>
                  </div>
                )}
              </div>
            </div>

            {/* Printer control buttons — work for both real queue items
                 AND virtual external/direct-dispatch items.  Printer state
                 (RUNNING / PAUSE) decides which button shows. */}
            {hasPermission('printers:control') && (
              <div className="flex items-center gap-1.5 mt-2 pt-2 border-t border-bambu-dark-tertiary">
                {status.state === 'PAUSE' || status.state === 'PAUSED' ? (
                  <button
                    onClick={() => resumePrintMutation.mutate()}
                    disabled={resumePrintMutation.isPending}
                    className="flex-1 py-1.5 px-2 rounded bg-bambu-green/10 hover:bg-bambu-green/20 text-bambu-green text-xs font-medium transition-colors disabled:opacity-50 flex items-center justify-center gap-1"
                    title={t('queueCard.resumePrint')}
                  >
                    {resumePrintMutation.isPending ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Play className="w-3.5 h-3.5" />
                    )}
                    <span>{t('queueCard.resumePrint')}</span>
                  </button>
                ) : (
                  <button
                    onClick={() => pausePrintMutation.mutate()}
                    disabled={pausePrintMutation.isPending}
                    className="flex-1 py-1.5 px-2 rounded bg-yellow-500/10 hover:bg-yellow-500/20 text-yellow-400 text-xs font-medium transition-colors disabled:opacity-50 flex items-center justify-center gap-1"
                    title={t('queueCard.pausePrint')}
                  >
                    {pausePrintMutation.isPending ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Pause className="w-3.5 h-3.5" />
                    )}
                    <span>{t('queueCard.pausePrint')}</span>
                  </button>
                )}
                <button
                  onClick={() => {
                    if (window.confirm(t('queueCard.confirmStopPrint'))) {
                      stopPrintMutation.mutate();
                    }
                  }}
                  disabled={stopPrintMutation.isPending}
                  className="flex-1 py-1.5 px-2 rounded bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs font-medium transition-colors disabled:opacity-50 flex items-center justify-center gap-1"
                  title={t('queueCard.stopPrint')}
                >
                  {stopPrintMutation.isPending ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <Square className="w-3.5 h-3.5" />
                  )}
                  <span>{t('queueCard.stopPrint')}</span>
                </button>
              </div>
            )}
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
            {visibleItems.map((item: PrintQueueItem) => {
              // Detect if this item is part of an active batch (≥2 pending siblings).
              const batchSize = item.batch_id
                ? pending.filter(p => p.batch_id === item.batch_id).length
                : 0;
              const isInBatch = batchSize >= 2;
              // Display number: 1-based sequential index within the full
              // pending list (not the DB position — raw positions can grow
              // unbounded over time as items are added/cloned/reordered).
              const displayNumber = pending.indexOf(item) + 1;
              // Position-based top/bottom detection — used to hide no-op
              // reorder buttons.  Based on the full `pending` list (not
              // `visibleItems`, which is sliced for the collapsed view).
              // For batched items, the entire block counts: the batch is
              // "at top" only when every sibling sits at the top.
              const siblingIds = item.batch_id
                ? new Set(pending.filter(p => p.batch_id === item.batch_id).map(p => p.id))
                : new Set([item.id]);
              const firstNonBlock = pending.find(p => !siblingIds.has(p.id));
              const lastNonBlock = [...pending].reverse().find(p => !siblingIds.has(p.id));
              const blockPositions = pending.filter(p => siblingIds.has(p.id)).map(p => p.position);
              const minBlockPos = Math.min(...blockPositions);
              const maxBlockPos = Math.max(...blockPositions);
              const isFirst = !firstNonBlock || minBlockPos < firstNonBlock.position;
              const isLast = !lastNonBlock || maxBlockPos > lastNonBlock.position;
              return (
                <PendingItemRow
                  key={item.id}
                  item={item}
                  onStart={() => startItemMutation.mutate(item.id)}
                  onCancel={() => cancelItemMutation.mutate(item.id)}
                  onMove={(direction) => reorderMutation.mutate({ id: item.id, direction })}
                  onBump={() => bumpMutation.mutate(item.id)}
                  onBumpBottom={() => bumpBottomMutation.mutate(item.id)}
                  onClone={(scope) => cloneMutation.mutate({ id: item.id, scope })}
                  onSkip={() => skipMutation.mutate(item.id)}
                  onToggleManualStart={() => toggleManualStartMutation.mutate(item.id)}
                  onEdit={onEditItem ? () => onEditItem(item) : undefined}
                  onCancelBatch={
                    item.batch_id ? () => cancelBatchMutation.mutate(item.batch_id!) : undefined
                  }
                  onCloneBatch={
                    item.batch_id ? () => cloneBatchMutation.mutate(item.batch_id!) : undefined
                  }
                  batchSize={batchSize}
                  isInBatch={isInBatch}
                  isFirst={isFirst}
                  isLast={isLast}
                  displayNumber={displayNumber}
                  startPending={startItemMutation.isPending}
                  cancelPending={cancelItemMutation.isPending}
                  anyPending={
                    reorderMutation.isPending ||
                    bumpMutation.isPending ||
                    bumpBottomMutation.isPending ||
                    cloneMutation.isPending ||
                    skipMutation.isPending ||
                    toggleManualStartMutation.isPending
                  }
                  hasPermission={hasPermission}
                  t={t}
                />
              );
            })}
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

        {/* Issues section — failed + skipped items with retry / unskip */}
        {((failedItems?.length ?? 0) > 0 || (skippedItems?.length ?? 0) > 0) && (
          <IssuesSection
            failedItems={failedItems ?? []}
            skippedItems={skippedItems ?? []}
            queueKey={['queue', queue.printer_id]}
            hasPermission={hasPermission}
            t={t}
          />
        )}

        {/* Footer counters — clickable shortcut to archives filtered by this printer */}
        <Link
          to={`/archives?printer=${queue.printer_id}`}
          className="block text-xs text-bambu-gray hover:text-white pt-1 border-t border-bambu-dark-tertiary transition-colors"
          title={t('queueCard.footer.viewArchivesTitle')}
        >
          {t('queueCard.footer.pending', { count: queue.pending_count })}
          {' \u00B7 '}
          {t('queueCard.footer.done', { count: queue.completed_count })}
          {queue.failed_count > 0 && <>{' \u00B7 '}{t('queueCard.footer.failed', { count: queue.failed_count })}</>}
          {queue.cancelled_count > 0 && <>{' \u00B7 '}{t('queueCard.footer.cancelled', { count: queue.cancelled_count })}</>}
        </Link>
      </CardContent>
    </Card>
      {dropOverlay}
      {dropPrintModal}
    </div>
  );
}

interface PendingItemRowProps {
  item: PrintQueueItem;
  onStart: () => void;
  onCancel: () => void;
  onMove: (direction: 'up' | 'down') => void;
  onBump: () => void;
  onBumpBottom: () => void;
  onClone: (scope: 'single' | 'batch') => void;
  onSkip: () => void;
  onToggleManualStart: () => void;
  onEdit?: () => void;
  onCancelBatch?: () => void;
  onCloneBatch?: () => void;
  batchSize: number;
  isInBatch: boolean;
  isFirst: boolean;
  isLast: boolean;
  displayNumber: number;
  startPending: boolean;
  cancelPending: boolean;
  anyPending: boolean;
  hasPermission: (perm: Permission) => boolean;
  t: (key: string, opts?: Record<string, unknown>) => string;
}

function PendingItemRow({
  item,
  onStart,
  onCancel,
  onMove,
  onBump,
  onBumpBottom,
  onClone,
  onSkip,
  onToggleManualStart,
  onEdit,
  onCancelBatch,
  onCloneBatch,
  batchSize,
  isInBatch,
  isFirst,
  isLast,
  displayNumber,
  startPending,
  cancelPending,
  anyPending,
  hasPermission,
  t,
}: PendingItemRowProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [batchDialog, setBatchDialog] = useState<null | 'cancel' | 'clone'>(null);
  const navigate = useNavigate();
  const canUpdate = hasPermission('queue:update_all');
  const canCreate = hasPermission('queue:create');
  const name = item.archive_name || item.library_file_name || `File #${item.archive_id || item.library_file_id}`;

  // Deterministic per-batch accent: hash batch_id → pick one of 6 hues.
  // All siblings of a batch get the same stripe + badge tint, different
  // batches in the same queue get different colors — grouping is visible
  // at a glance even when rows get reordered.
  const batchAccent = isInBatch && item.batch_id ? getBatchAccent(item.batch_id) : null;

  const handleCancel = () => {
    if (isInBatch && onCancelBatch) {
      setBatchDialog('cancel');
    } else {
      onCancel();
    }
  };

  const handleClone = () => {
    if (isInBatch && onCloneBatch) {
      setBatchDialog('clone');
    } else {
      onClone('single');
    }
  };

  return (
    <>
      <div
        className="flex items-center gap-2 py-1.5 px-2 rounded hover:bg-bambu-dark transition-colors group"
        style={
          batchAccent
            ? { borderLeft: `3px solid ${batchAccent.color}`, paddingLeft: '0.5rem' }
            : undefined
        }
      >
        <span className="text-xs text-bambu-gray w-5 text-right flex-shrink-0">
          {displayNumber}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <p className="text-xs text-white truncate flex-1">{name}</p>
            {isInBatch && batchAccent && (
              <span className={`text-[9px] px-1 rounded ${batchAccent.badge} font-medium`}>
                {t('queueCard.batch.label', { count: batchSize })}
              </span>
            )}
            {item.manual_start && (
              <span className="text-[9px] px-1 rounded bg-yellow-400/20 text-yellow-400 font-medium">
                M
              </span>
            )}
          </div>
          {item.waiting_reason && (
            <p className="text-[10px] text-yellow-400 truncate">{item.waiting_reason}</p>
          )}
        </div>
        <div className="flex items-center gap-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
          {item.manual_start && (
            <button
              onClick={onStart}
              disabled={startPending || !canUpdate}
              className="p-0.5 rounded hover:bg-bambu-green/20 text-bambu-green disabled:opacity-50"
              title={t('queueCard.startItem')}
            >
              <Play className="w-3.5 h-3.5" />
            </button>
          )}
          {!isFirst && (
            <button
              onClick={() => onMove('up')}
              disabled={anyPending || !canUpdate}
              className="p-0.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray-light disabled:opacity-30"
              title={t('queueCard.actions.moveUp')}
            >
              <ArrowUp className="w-3.5 h-3.5" />
            </button>
          )}
          {!isLast && (
            <button
              onClick={() => onMove('down')}
              disabled={anyPending || !canUpdate}
              className="p-0.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray-light disabled:opacity-30"
              title={t('queueCard.actions.moveDown')}
            >
              <ArrowDown className="w-3.5 h-3.5" />
            </button>
          )}
          {!isFirst && (
            <button
              onClick={onBump}
              disabled={anyPending || !canUpdate}
              className="p-0.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray-light disabled:opacity-30"
              title={t('queueCard.actions.bumpTop')}
            >
              <ChevronsUp className="w-3.5 h-3.5" />
            </button>
          )}
          {!isLast && (
            <button
              onClick={onBumpBottom}
              disabled={anyPending || !canUpdate}
              className="p-0.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray-light disabled:opacity-30"
              title={t('queueCard.actions.bumpBottom')}
            >
              <ChevronsDown className="w-3.5 h-3.5" />
            </button>
          )}
          {onEdit && (
            <button
              onClick={onEdit}
              disabled={!canUpdate}
              className="p-0.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray-light disabled:opacity-30"
              title={t('queueCard.editItem')}
            >
              <Pencil className="w-3.5 h-3.5" />
            </button>
          )}
          <div className="relative">
            <button
              onClick={() => setMenuOpen(v => !v)}
              disabled={anyPending}
              className="p-0.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray-light disabled:opacity-30"
              title={t('queueCard.actions.more')}
            >
              <MoreVertical className="w-3.5 h-3.5" />
            </button>
            {menuOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
                <div className="absolute right-0 top-full mt-1 z-20 min-w-[180px] rounded-md bg-bambu-dark-secondary border border-bambu-dark-tertiary shadow-xl py-1 text-xs">
                  {item.archive_id && (
                    <>
                      <button
                        onClick={() => {
                          setMenuOpen(false);
                          navigate(
                            `/archives?search=${encodeURIComponent(item.archive_name || '')}`,
                          );
                        }}
                        className="w-full text-left px-3 py-1.5 text-white hover:bg-bambu-dark"
                      >
                        {t('queueCard.actions.viewArchive')}
                      </button>
                      <div className="border-t border-bambu-dark-tertiary my-1" />
                    </>
                  )}
                  <button
                    disabled={!canCreate}
                    onClick={() => {
                      setMenuOpen(false);
                      handleClone();
                    }}
                    className="w-full text-left px-3 py-1.5 text-white hover:bg-bambu-dark disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    {t('queueCard.actions.clone')}
                  </button>
                  <button
                    disabled={!canUpdate}
                    onClick={() => {
                      setMenuOpen(false);
                      onSkip();
                    }}
                    className="w-full text-left px-3 py-1.5 text-white hover:bg-bambu-dark disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    {t('queueCard.actions.skip')}
                  </button>
                  <button
                    disabled={!canUpdate}
                    onClick={() => {
                      setMenuOpen(false);
                      onToggleManualStart();
                    }}
                    className="w-full text-left px-3 py-1.5 text-white hover:bg-bambu-dark disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    {item.manual_start
                      ? t('queueCard.actions.unsetManualStart')
                      : t('queueCard.actions.setManualStart')}
                  </button>
                  {isInBatch && (
                    <>
                      <div className="border-t border-bambu-dark-tertiary my-1" />
                      <button
                        disabled={!canCreate}
                        onClick={() => {
                          setMenuOpen(false);
                          if (onCloneBatch) onCloneBatch();
                        }}
                        className="w-full text-left px-3 py-1.5 text-white hover:bg-bambu-dark disabled:opacity-40 disabled:hover:bg-transparent"
                      >
                        {t('queueCard.batch.cloneBatch')}
                      </button>
                      <button
                        disabled={!canUpdate}
                        onClick={() => {
                          setMenuOpen(false);
                          if (onCancelBatch) onCancelBatch();
                        }}
                        className="w-full text-left px-3 py-1.5 text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:hover:bg-transparent"
                      >
                        {t('queueCard.batch.cancelBatch')}
                      </button>
                    </>
                  )}
                </div>
              </>
            )}
          </div>
          <button
            onClick={handleCancel}
            disabled={cancelPending || !canUpdate}
            className="p-0.5 rounded hover:bg-red-500/20 text-red-400 disabled:opacity-50"
            title={t('queueCard.cancelItem')}
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
      {batchDialog === 'cancel' && (
        <BatchActionDialog
          open
          onClose={() => setBatchDialog(null)}
          batchSize={batchSize}
          title={t('queueCard.batch.cancelTitle')}
          applyAllLabel={t('queueCard.batch.cancelAll', { count: batchSize })}
          applyOneLabel={t('queueCard.batch.cancelOne')}
          applyAllDanger
          onApplyAll={() => {
            setBatchDialog(null);
            if (onCancelBatch) onCancelBatch();
          }}
          onApplyOne={() => {
            setBatchDialog(null);
            onCancel();
          }}
        />
      )}
      {batchDialog === 'clone' && (
        <BatchActionDialog
          open
          onClose={() => setBatchDialog(null)}
          batchSize={batchSize}
          title={t('queueCard.batch.cloneTitle')}
          applyAllLabel={t('queueCard.batch.cloneAll', { count: batchSize })}
          applyOneLabel={t('queueCard.batch.cloneOne')}
          onApplyAll={() => {
            setBatchDialog(null);
            if (onCloneBatch) onCloneBatch();
          }}
          onApplyOne={() => {
            setBatchDialog(null);
            onClone('single');
          }}
        />
      )}
    </>
  );
}


interface IssuesSectionProps {
  failedItems: PrintQueueItem[];
  skippedItems: PrintQueueItem[];
  queueKey: (string | number)[];
  hasPermission: (perm: Permission) => boolean;
  t: (key: string, opts?: Record<string, unknown>) => string;
}

/**
 * Collapsible section under pending items showing failed + skipped
 * items with retry / unskip / remove-from-queue affordances.
 *
 * Collapsed by default — summary only.  Expands on click.  Each row
 * has its own mutation state; no bulk actions here.
 */
function IssuesSection({ failedItems, skippedItems, queueKey, hasPermission, t }: IssuesSectionProps) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: queueKey });

  const retryMutation = useMutation({
    mutationFn: (id: number) => api.retryQueueItem(id),
    onSuccess: () => {
      invalidate();
      showToast(t('queueCard.toast.retrying'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });
  const unskipMutation = useMutation({
    mutationFn: (id: number) => api.unskipQueueItem(id),
    onSuccess: () => {
      invalidate();
      showToast(t('queueCard.toast.unskipped'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });
  const removeMutation = useMutation({
    mutationFn: (id: number) => api.removeFromQueue(id),
    onSuccess: () => {
      invalidate();
      showToast(t('queue.toast.cancelled'), 'success');
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const total = failedItems.length + skippedItems.length;
  const canUpdate = hasPermission('queue:update_all');

  return (
    <div className="space-y-1 pt-1">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-1 text-xs text-bambu-gray hover:text-white transition-colors"
      >
        {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <AlertCircle className="w-3 h-3 text-yellow-400" />
        <span>{t('queueCard.issues.header', { count: total })}</span>
      </button>
      {open && (
        <div className="space-y-1">
          {failedItems.map(item => {
            const name = item.archive_name || item.library_file_name || `File #${item.archive_id || item.library_file_id}`;
            return (
              <div key={item.id} className="flex items-center gap-2 py-1 px-2 rounded bg-red-500/5 group">
                <X className="w-3 h-3 text-red-400 flex-shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-white truncate">{name}</p>
                  {item.error_message && (
                    <p className="text-[10px] text-red-400 truncate">{item.error_message}</p>
                  )}
                </div>
                <div className="flex items-center gap-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={() => retryMutation.mutate(item.id)}
                    disabled={retryMutation.isPending || !canUpdate}
                    className="p-0.5 rounded hover:bg-bambu-green/20 text-bambu-green disabled:opacity-50"
                    title={t('queueCard.actions.retry')}
                  >
                    <Play className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => removeMutation.mutate(item.id)}
                    disabled={removeMutation.isPending}
                    className="p-0.5 rounded hover:bg-red-500/20 text-red-400 disabled:opacity-50"
                    title={t('queue.removeFromQueue')}
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            );
          })}
          {skippedItems.map(item => {
            const name = item.archive_name || item.library_file_name || `File #${item.archive_id || item.library_file_id}`;
            return (
              <div key={item.id} className="flex items-center gap-2 py-1 px-2 rounded bg-yellow-500/5 group">
                <Pause className="w-3 h-3 text-yellow-400 flex-shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-white truncate">{name}</p>
                </div>
                <div className="flex items-center gap-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={() => unskipMutation.mutate(item.id)}
                    disabled={unskipMutation.isPending || !canUpdate}
                    className="p-0.5 rounded hover:bg-bambu-green/20 text-bambu-green disabled:opacity-50"
                    title={t('queueCard.actions.unskip')}
                  >
                    <Play className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => removeMutation.mutate(item.id)}
                    disabled={removeMutation.isPending}
                    className="p-0.5 rounded hover:bg-red-500/20 text-red-400 disabled:opacity-50"
                    title={t('queue.removeFromQueue')}
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
