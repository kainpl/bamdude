import { useMemo, useRef, useState, type DragEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  ListPlus, Loader2, Sparkles, Trash2, Upload, Zap, ChevronRight,
  ChevronDown, ChevronUp, GripVertical, Pencil,
} from 'lucide-react';
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core';
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { api } from '../../api/client';
import type { AutoQueueItem } from '../../api/client';
import { PrintModal } from '../PrintModal';
import { partitionDroppedFiles, dropRejectionKey } from '../../utils/printableDrop';
import { isPrintable } from '../../lib/fileTags';
import { useToast } from '../../contexts/ToastContext';
import { useAuth } from '../../contexts/AuthContext';
import { LibraryPickerModal } from '../LibraryPickerModal';
import { QueueSequencer } from '../QueueSequencer';
import type { SequencedFile } from '../QueueSequencer';

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
  const canEdit = hasPermission('queue:update_all');
  const canDrop = hasPermission('queue:create');

  // Drag-drop: drop a sliced file on the panel → upload to library + open
  // PrintModal locked to 'auto' mode (no specific printer; the auto-queue
  // router picks one at dispatch).
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [isDropUploading, setIsDropUploading] = useState(false);
  // Files just dropped on the panel, waiting to go through the Schedule dialog
  // one at a time — the same run the printer cards and the library use.
  const [droppedForQueue, setDroppedForQueue] = useState<SequencedFile[] | null>(null);
  // The same batch, chosen instead of dropped. Both end in `droppedForQueue`
  // and the same per-file Schedule dialog — only the way in differs.
  const [pickerOpen, setPickerOpen] = useState(false);
  // Expanded runs (by run key): copies show as individual, individually
  // draggable rows. Collapsed, a run drags as one block.
  const [expandedRuns, setExpandedRuns] = useState<Set<string>>(new Set());
  // Editing target: the copy (or its whole batch) currently in the PrintModal.
  const [editTarget, setEditTarget] = useState<{ item: AutoQueueItem; batchCount: number } | null>(null);
  const dragCounterRef = useRef(0);

  const { data: items } = useQuery({
    queryKey: ['auto-queue', 'pending'],
    queryFn: () => api.getAutoQueue('pending'),
    refetchInterval: 15000,
  });

  // Archive-backed terminal totals — the auto_queue_items row is deleted
  // once its print finishes, so completed/failed/cancelled history lives
  // on print_archives.from_auto_queue. Mirrors the per-printer queue card
  // footer.
  const { data: stats } = useQuery({
    queryKey: ['auto-queue', 'stats'],
    queryFn: () => api.getAutoQueueStats(),
    refetchInterval: 15000,
  });

  // Shortest-job-first overrides manual positions entirely — with it on, a
  // drag would persist an order the distributor then ignores, so the handles
  // hide and a hint says who is in charge.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const sjfActive = settings?.queue_shortest_first === true;

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

  // Order-faithful grouping: the list follows the actual queue order, and
  // only CONSECUTIVE copies of one batch collapse into a xN row. A batch whose
  // copies were spread apart by reordering renders as several runs — the order
  // is always visible and always true, which global batch-grouping could not
  // promise once reordering exists.
  const sortedItems = useMemo(
    () => [...(items ?? [])].sort((a, b) => a.position - b.position || a.id - b.id),
    [items],
  );
  const runs = useMemo(() => {
    const out: Array<{ key: string; batchId: string | null; items: AutoQueueItem[] }> = [];
    for (const it of sortedItems) {
      const last = out[out.length - 1];
      if (last && last.batchId !== null && last.batchId === it.batch_id) last.items.push(it);
      else out.push({ key: `run-${it.id}`, batchId: it.batch_id, items: [it] });
    }
    return out;
  }, [sortedItems]);
  // Whole-batch copy counts — the edit dialog offers "edit all N copies"
  // across runs, not just the contiguous ones.
  const batchTotals = useMemo(() => {
    const m = new Map<string, number>();
    for (const it of sortedItems) {
      if (it.batch_id) m.set(it.batch_id, (m.get(it.batch_id) ?? 0) + 1);
    }
    return m;
  }, [sortedItems]);

  // The sortable list mixes collapsed runs (one block) with the copies of
  // expanded runs (individual rows). Single-copy runs are just rows.
  type DisplayEntry =
    | { kind: 'run'; id: string; run: { key: string; batchId: string | null; items: AutoQueueItem[] } }
    | { kind: 'item'; id: string; item: AutoQueueItem; runKey: string; copyIndex: number; copyTotal: number };
  const displayEntries = useMemo((): DisplayEntry[] => {
    const out: DisplayEntry[] = [];
    for (const run of runs) {
      if (run.items.length > 1 && !expandedRuns.has(run.key)) {
        out.push({ kind: 'run', id: run.key, run });
      } else {
        run.items.forEach((item, i) =>
          out.push({
            kind: 'item',
            id: `item-${item.id}`,
            item,
            runKey: run.key,
            copyIndex: i,
            copyTotal: run.items.length,
          }),
        );
      }
    }
    return out;
  }, [runs, expandedRuns]);

  const reorderMutation = useMutation({
    mutationFn: (entries: { id: number; position: number }[]) => api.reorderAutoQueue(entries),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['auto-queue'] }),
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const dndSensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));
  const dragEnabled = canAssign && !sjfActive && displayEntries.length > 1;

  const handleReorderEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!dragEnabled || !over || active.id === over.id) return;
    const oldIndex = displayEntries.findIndex((e) => e.id === active.id);
    const newIndex = displayEntries.findIndex((e) => e.id === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    const reordered = arrayMove(displayEntries, oldIndex, newIndex);
    // Flatten back to items (a run block carries its copies in order) and
    // renumber the WHOLE pending list 1..N — same contract as the per-printer
    // queue's drag.
    const flat = reordered.flatMap((e) => (e.kind === 'run' ? e.run.items : [e.item]));
    reorderMutation.mutate(flat.map((it, idx) => ({ id: it.id, position: idx + 1 })));
  };

  const deleteRun = (runItems: AutoQueueItem[]) => {
    // Per-copy deletes, not the batch endpoint: a split batch's other run
    // must survive deleting this one.
    for (const it of runItems) cancelMutation.mutate(it.id);
  };

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

    // ⚠️ Every dropped file is answered for. This read files[0] and discarded
    // the rest in silence, so a five-file drop acted on one and said nothing
    // about the other four.
    const { candidates, rejected } = partitionDroppedFiles(Array.from(e.dataTransfer.files));
    for (const { file: bad, rejection } of rejected) {
      showToast(t(dropRejectionKey(rejection), { filename: bad.name }), 'error');
    }
    if (candidates.length === 0) return;

    setIsDropUploading(true);
    try {
      // ⚠️ Every candidate, not just the first — this was the last drop zone
      // still taking files[0]. Each goes through the same deduplication as any
      // upload, so re-dropping a file the library has reuses that row.
      const queued: SequencedFile[] = [];
      for (const candidate of candidates) {
        try {
          const result = await api.uploadLibraryFile(candidate, null);
          if (result.outcome !== 'created') {
            showToast(t('fileManager.dedupUsedExisting', { name: result.filename }), 'info');
          }
          if (!isPrintable(result)) {
            if (result.outcome === 'created') await api.deleteLibraryFile(result.id).catch(() => {});
            showToast(t('printers.dropNoGcodeInside', { filename: candidate.name }), 'error');
            continue;
          }
          // ⚠️ No recorded model means nothing can be verified — not the
          // target for the auto-queue, not the match against the printer this
          // was dropped on. Refused here, before the dialog, rather than
          // queued on a guess: the item would otherwise wait for a machine
          // nobody chose, or print on the wrong one.
          const slicedFor = (result.metadata as Record<string, unknown>)?.sliced_for_model as string | undefined;
          if (!slicedFor) {
            if (result.outcome === 'created') await api.deleteLibraryFile(result.id).catch(() => {});
            showToast(t('printers.dropNoSlicedForModel', { filename: candidate.name }), 'error');
            continue;
          }
          queued.push({ id: result.id, name: result.filename });
        } catch {
          showToast(t('common.uploadFailed'), 'error');
        }
      }
      // ⚠️ Still NO sliced_for_model check against a printer here, and that is
      // right: auto-queue has no target machine to compare against. The model
      // constraint is instead pinned onto each item from its own file, below.
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-stats'] });
      if (queued.length > 0) setDroppedForQueue(queued);
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
        {/* Same permission as the drop zone this panel already is — both add
            items to the auto-queue, and only the gesture differs. */}
        {canDrop && (
          <button
            onClick={() => setPickerOpen(true)}
            className="ml-auto p-1 rounded hover:bg-bambu-dark-tertiary transition-colors"
            title={t('libraryPicker.open')}
          >
            <ListPlus className="w-4 h-4 text-bambu-gray" />
          </button>
        )}
      </div>

      {isEmpty && (
        <p className="text-xs text-bambu-gray italic">{t('autoQueue.emptyHint')}</p>
      )}

      {!isEmpty && (
      <>
      {sjfActive && (
        <p className="text-[11px] text-bambu-gray italic mb-1.5">{t('autoQueue.sjfOrderHint')}</p>
      )}
      <DndContext sensors={dndSensors} collisionDetection={closestCenter} onDragEnd={handleReorderEnd}>
        <SortableContext items={displayEntries.map((e) => e.id)} strategy={verticalListSortingStrategy}>
          <div className="space-y-1.5">
            {displayEntries.map((entry) => {
              if (entry.kind === 'run') {
                const head = entry.run.items[0];
                return (
                  <AutoQueueRow
                    key={entry.id}
                    sortableId={entry.id}
                    item={head}
                    countBadge={entry.run.items.length}
                    draggable={dragEnabled}
                    onExpand={() => setExpandedRuns((prev) => new Set(prev).add(entry.run.key))}
                    onEdit={
                      canEdit
                        ? () => setEditTarget({ item: head, batchCount: head.batch_id ? (batchTotals.get(head.batch_id) ?? 1) : 1 })
                        : undefined
                    }
                    onAssignNow={canAssign ? () => assignNowMutation.mutate(head.id) : undefined}
                    onDelete={canDelete ? () => deleteRun(entry.run.items) : undefined}
                    busy={cancelMutation.isPending || assignNowMutation.isPending}
                    t={t}
                  />
                );
              }
              return (
                <AutoQueueRow
                  key={entry.id}
                  sortableId={entry.id}
                  item={entry.item}
                  copyLabel={entry.copyTotal > 1 ? `${entry.copyIndex + 1}/${entry.copyTotal}` : undefined}
                  draggable={dragEnabled}
                  onCollapse={
                    entry.copyTotal > 1
                      ? () =>
                          setExpandedRuns((prev) => {
                            const next = new Set(prev);
                            next.delete(entry.runKey);
                            return next;
                          })
                      : undefined
                  }
                  onEdit={canEdit ? () => setEditTarget({ item: entry.item, batchCount: 1 }) : undefined}
                  onAssignNow={canAssign ? () => assignNowMutation.mutate(entry.item.id) : undefined}
                  onDelete={canDelete ? () => cancelMutation.mutate(entry.item.id) : undefined}
                  busy={cancelMutation.isPending || assignNowMutation.isPending}
                  t={t}
                />
              );
            })}
          </div>
        </SortableContext>
      </DndContext>
      </>
      )}

      {/* Archive-backed totals — mirrors the per-printer queue card footer.
          Only rendered once at least one auto-queue print has finished. */}
      {stats && stats.total_count > 0 && (
        <div className="text-xs text-bambu-gray pt-2 mt-3 border-t border-bambu-dark-tertiary">
          {t('autoQueue.stats.done', { count: stats.completed_count })}
          {stats.failed_count > 0 && (
            <>{' · '}{t('autoQueue.stats.failed', { count: stats.failed_count })}</>
          )}
          {stats.cancelled_count > 0 && (
            <>{' · '}{t('autoQueue.stats.cancelled', { count: stats.cancelled_count })}</>
          )}
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
      {pickerOpen && (
        <LibraryPickerModal
          // No printer to match against — the auto-queue routes by each file's
          // own model, so every sliced file with a recorded one is offered.
          targetName={t('autoQueue.title')}
          onCancel={() => setPickerOpen(false)}
          onConfirm={(files) => {
            setPickerOpen(false);
            setDroppedForQueue(files);
          }}
        />
      )}
      {editTarget && (
        <PrintModal
          mode="edit-auto-item"
          archiveId={editTarget.item.archive_id ?? undefined}
          libraryFileId={editTarget.item.library_file_id ?? undefined}
          archiveName={
            editTarget.item.archive_name || editTarget.item.library_file_name || `#${editTarget.item.id}`
          }
          autoQueueItem={editTarget.item}
          autoQueueBatchCount={editTarget.batchCount}
          onClose={() => setEditTarget(null)}
          onSuccess={() => setEditTarget(null)}
        />
      )}
      {droppedForQueue && (
        <QueueSequencer
          files={droppedForQueue}
          // The panel IS the auto-queue, so only that form is shown — and each
          // item's target is its own file's model, fixed.
          initialDispatchMode="auto"
          lockDispatchMode
          lockAutoTarget
          onDone={() => {
            setDroppedForQueue(null);
            queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
            queryClient.invalidateQueries({ queryKey: ['queue'] });
          }}
        />
      )}
    </div>
  );
}


// ── Sortable row (a collapsed xN run or a single copy) ───────────────────────

function AutoQueueRow({
  sortableId, item, countBadge, copyLabel, draggable, busy,
  onExpand, onCollapse, onEdit, onAssignNow, onDelete, t,
}: {
  sortableId: string;
  item: AutoQueueItem;
  /** Collapsed run: how many copies this block carries. */
  countBadge?: number;
  /** Expanded copy: its "i/N" position inside the run. */
  copyLabel?: string;
  draggable: boolean;
  busy: boolean;
  onExpand?: () => void;
  onCollapse?: () => void;
  onEdit?: () => void;
  onAssignNow?: () => void;
  onDelete?: () => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: sortableId });
  const label = item.archive_name || item.library_file_name || `#${item.id}`;
  const targetModel = item.target_model || t('autoQueue.anyModel');
  const targetLocation = item.target_location?.name;

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : undefined }}
      className="flex items-center gap-2 p-2.5 bg-bambu-dark rounded border border-bambu-dark-tertiary hover:border-bambu-green/50 transition-colors"
    >
      {draggable && (
        <button
          type="button"
          {...attributes}
          {...listeners}
          className="p-0.5 text-bambu-gray/50 hover:text-white cursor-grab active:cursor-grabbing touch-none shrink-0"
          title={t('queueCard.actions.drag')}
        >
          <GripVertical className="w-3.5 h-3.5" />
        </button>
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-sm text-white truncate">
          <span className="truncate">{label}</span>
          {countBadge != null && countBadge > 1 && (
            <button
              type="button"
              onClick={onExpand}
              className="px-1.5 py-0.5 text-xs bg-bambu-green/20 text-bambu-green rounded shrink-0 inline-flex items-center gap-0.5 hover:bg-bambu-green/30"
              title={t('autoQueue.expandCopies')}
            >
              ×{countBadge}
              <ChevronDown className="w-3 h-3" />
            </button>
          )}
          {copyLabel && (
            <button
              type="button"
              onClick={onCollapse}
              className="px-1.5 py-0.5 text-xs bg-bambu-dark-tertiary text-bambu-gray rounded shrink-0 inline-flex items-center gap-0.5 hover:text-white"
              title={t('autoQueue.collapseCopies')}
            >
              {copyLabel}
              {onCollapse && <ChevronUp className="w-3 h-3" />}
            </button>
          )}
          {item.plate_id != null && (
            <span className="text-xs text-bambu-gray shrink-0">{t('printModal.plateNumber', { number: item.plate_id })}</span>
          )}
        </div>
        <div className="flex items-center gap-2 text-xs text-bambu-gray flex-wrap mt-0.5">
          <span>
            <ChevronRight className="inline w-3 h-3" />
            {targetModel}
          </span>
          {targetLocation && <span>· {targetLocation}</span>}
          {item.force_color_match && <span>· {t('autoQueue.exactColor')}</span>}
          {item.waiting_reason && (
            <span className="text-yellow-700 dark:text-yellow-400">· {item.waiting_reason}</span>
          )}
        </div>
      </div>

      {onEdit && (
        <button
          type="button"
          onClick={onEdit}
          className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded shrink-0"
          title={t('common.edit')}
        >
          <Pencil className="w-3.5 h-3.5" />
        </button>
      )}
      {onAssignNow && (
        <button
          type="button"
          onClick={onAssignNow}
          disabled={busy}
          className="px-2 py-1 text-xs text-bambu-green hover:bg-bambu-green/10 rounded inline-flex items-center gap-1 disabled:opacity-40 shrink-0"
          title={t('autoQueue.assignNow')}
        >
          <Zap className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">{t('autoQueue.assignNow')}</span>
        </button>
      )}
      {onDelete && (
        <button
          type="button"
          onClick={onDelete}
          disabled={busy}
          className="px-2 py-1 text-xs text-red-700 dark:text-red-400 hover:bg-red-500/10 rounded inline-flex items-center gap-1 disabled:opacity-40 shrink-0"
          title={t('common.cancel')}
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      )}
    </div>
  );
}
