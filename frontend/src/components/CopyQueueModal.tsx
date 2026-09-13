import { useId, useMemo, useState } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Check, Copy, FileBox, Printer as PrinterIcon } from 'lucide-react';

import { api } from '../api/client';
import type { PrinterQueue } from '../api/client';
import { Button } from './Button';
import { Modal } from './Modal';
import type { SequencedFile } from './QueueSequencer';
import { copyTargets, type CopyableItem } from '../lib/copyQueue';
import { groupByLocation } from '../utils/locationGroups';
import { readStoredQueueSort, sortQueues } from '../utils/queueOrder';
import { forecastById } from '../utils/etaSort';
import { formatDuration } from '../utils/date';

interface CopyQueueModalProps {
  source: PrinterQueue;
  /** What is on the source queue right now — the running print first, then the
   *  pending items in queue order. Built by the card, because only it knows
   *  whether the running print has a queue row at all. */
  items: CopyableItem[];
  /** How many queue rows had no file behind them and were left out upstream. */
  droppedCount?: number;
  onCancel: () => void;
  /** Never called with an empty side. */
  onConfirm: (files: SequencedFile[], printerIds: number[]) => void;
}

/**
 * Copy one printer's queue onto other printers of the same model.
 *
 * ⚠️ **It picks what and where, and asks nothing else.** The copy itself runs
 * through the ordinary Schedule dialog — one per item, with every chosen
 * printer ticked and locked — so plates, AMS mapping, print options and
 * scheduling are answered where they always are. `PrintModal` already loops
 * over the selected printers and maps filament PER PRINTER, which is why the
 * run is one dialog per item and not one per item per printer: a nested run
 * would ask the same questions many times over and still not know anything the
 * single dialog does not.
 *
 * Items start ticked, printers do not. The queue is what you came to copy; the
 * machines are the decision.
 */
export function CopyQueueModal({ source, items, droppedCount = 0, onCancel, onConfirm }: CopyQueueModalProps) {
  const { t } = useTranslation();
  const headingId = useId();

  const [pickedItems, setPickedItems] = useState<Set<number>>(
    () => new Set(items.map((_, index) => index)),
  );
  const [pickedPrinters, setPickedPrinters] = useState<Set<number>>(new Set());

  const { data: queues } = useQuery({ queryKey: ['queues'], queryFn: api.getQueues });
  const targets = useMemo(() => copyTargets(queues, source), [queues, source]);

  // Shares its keys with the cards on the page behind, so this costs no extra
  // polling — it reads the same cache and re-renders when it moves. Asked for
  // the unsorted targets: the order below is computed FROM these statuses.
  const statuses = useQueries({
    queries: targets.map((queue) => ({
      queryKey: ['printerStatus', queue.printer_id],
      queryFn: () => api.getPrinterStatus(queue.printer_id),
      refetchInterval: 10_000,
    })),
  });
  const statusOf = (printerId: number) => statuses[targets.findIndex((queue) => queue.printer_id === printerId)]?.data;

  // The order the Queues screen is in — the cards behind this dialog. The two
  // ETA orders need what the screen reads too: the live statuses above and,
  // for «free at», the server's forecast (the stats bar's query, so cached
  // whenever the dialog opens from the queue page).
  const [{ sortBy, sortAsc }] = useState(readStoredQueueSort);
  const { data: forecast } = useQuery({ queryKey: ['queue-forecast'], queryFn: api.getQueueForecast, enabled: sortBy === 'freeAt' });
  const forecastRows = useMemo(() => forecastById(forecast), [forecast]);
  const sortedTargets = useMemo(
    () => sortQueues(targets, sortBy, sortAsc, { statusOf, forecastOf: (printerId) => forecastRows.get(printerId) }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- statusOf closes over `statuses`, a fresh array each render; the list is a dozen rows
    [targets, sortBy, sortAsc, statuses, forecastRows],
  );
  // Headers only when the screen behind is grouped by location; otherwise one
  // unlabelled group, so the list renders through the same branch either way.
  const groups: { key: string; label: string | null; items: PrinterQueue[] }[] = useMemo(
    () =>
      sortBy === 'location'
        ? groupByLocation(sortedTargets, (queue) => queue.printer_location, t('queueCard.ungrouped')).map(
            (group) => ({ key: String(group.locationId ?? 'ungrouped'), label: group.label, items: group.items }),
          )
        : [{ key: 'all', label: null, items: sortedTargets }],
    [sortBy, sortedTargets, t],
  );

  const toggle = <T,>(set: Set<T>, value: T): Set<T> => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  };

  const bulkButtons = (allOn: () => void, allOff: () => void, allSelected: boolean) => (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={allOn}
        disabled={allSelected}
        className="text-xs text-bambu-gray hover:text-white disabled:opacity-40 disabled:hover:text-bambu-gray transition-colors"
      >
        {t('copyQueue.selectAll')}
      </button>
      <span className="text-bambu-gray/40">|</span>
      <button
        type="button"
        onClick={allOff}
        className="text-xs text-bambu-gray hover:text-white transition-colors"
      >
        {t('copyQueue.clear')}
      </button>
    </div>
  );

  const tick = (checked: boolean) => (
    <span
      className={`w-4 h-4 shrink-0 rounded border flex items-center justify-center ${
        checked ? 'bg-bambu-green border-bambu-green' : 'border-bambu-gray/50'
      }`}
    >
      {checked && <Check className="w-3 h-3 text-black" strokeWidth={3} />}
    </span>
  );

  const canCopy = pickedItems.size > 0 && pickedPrinters.size > 0;

  return (
    <Modal
      onClose={onCancel}
      labelledBy={headingId}
      size="5xl"
      panelClassName="h-[80vh]"
      bodyClassName="flex flex-col"
      header={
        <div className="min-w-0">
          <h2 id={headingId} className="text-lg font-semibold text-white truncate">{t('copyQueue.title')}</h2>
          <p className="text-xs text-bambu-gray truncate">
            {t('copyQueue.subtitle', {
              printer: source.printer_name ?? `#${source.printer_id}`,
              model: source.printer_model ?? '',
            })}
          </p>
        </div>
      }
    >
      <div className="flex-1 min-h-0 flex flex-col md:flex-row">
        {/* What to copy */}
        <div className="flex-1 min-w-0 flex flex-col border-b md:border-b-0 md:border-r border-bambu-dark">
          <div className="flex items-center justify-between gap-2 p-3 border-b border-bambu-dark shrink-0">
            <h3 className="text-sm font-medium text-white">
              {t('copyQueue.whatCount', { count: pickedItems.size })}
            </h3>
            {bulkButtons(
              () => setPickedItems(new Set(items.map((_, index) => index))),
              () => setPickedItems(new Set()),
              pickedItems.size === items.length,
            )}
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {items.length === 0 ? (
              <p className="text-sm text-bambu-gray italic p-4 text-center">{t('copyQueue.nothingToCopy')}</p>
            ) : (
              items.map(({ file, printing, orderName, printTimeSeconds, filamentGrams, thumbnailUrl }, index) => {
                const checked = pickedItems.has(index);
                return (
                  <button
                    key={`${file.source}-${file.id}-${index}`}
                    type="button"
                    aria-pressed={checked}
                    onClick={() => setPickedItems((prev) => toggle(prev, index))}
                    className={`w-full flex items-center gap-3 p-2 rounded border text-left transition-colors ${
                      checked
                        ? 'border-bambu-green bg-bambu-green/10'
                        : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green/50'
                    }`}
                  >
                    {tick(checked)}
                    <span className="w-12 h-12 shrink-0 rounded bg-bambu-dark-tertiary overflow-hidden flex items-center justify-center">
                      {thumbnailUrl ? (
                        <img
                          src={thumbnailUrl}
                          alt=""
                          className="w-full h-full object-contain"
                        />
                      ) : (
                        <FileBox className="w-5 h-5 text-bambu-gray/50" />
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm text-white truncate">{file.name}</span>
                      <span className="block text-xs text-bambu-gray truncate">
                        {[
                          printing ? t('copyQueue.printingNow') : null,
                          file.plateId != null ? t('copyQueue.plate', { n: file.plateId }) : null,
                          // The copy files under this order without asking —
                          // so it is said here, where the item can still be unticked.
                          orderName ? t('copyQueue.forOrder', { name: orderName }) : null,
                          printTimeSeconds ? formatDuration(printTimeSeconds) : null,
                          filamentGrams ? `${Math.round(filamentGrams)} g` : null,
                        ]
                          .filter(Boolean)
                          .join(' · ')}
                      </span>
                    </span>
                  </button>
                );
              })
            )}
            {droppedCount > 0 && (
              <p className="text-xs text-bambu-gray italic pt-1">
                {t('copyQueue.notCopyable', { count: droppedCount })}
              </p>
            )}
          </div>
        </div>

        {/* Where to copy it */}
        <div className="flex-1 min-w-0 flex flex-col">
          <div className="flex items-center justify-between gap-2 p-3 border-b border-bambu-dark shrink-0">
            <h3 className="text-sm font-medium text-white">
              {t('copyQueue.whereCount', { count: pickedPrinters.size })}
            </h3>
            {bulkButtons(
              () => setPickedPrinters(new Set(sortedTargets.map((queue) => queue.printer_id))),
              () => setPickedPrinters(new Set()),
              pickedPrinters.size === sortedTargets.length && sortedTargets.length > 0,
            )}
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {sortedTargets.length === 0 ? (
              <p className="text-sm text-bambu-gray italic p-4 text-center">
                {t('copyQueue.noOtherPrinters', { model: source.printer_model ?? '' })}
              </p>
            ) : (
              groups.map((group) => (
                <div key={group.key} className="space-y-2">
                  {group.label && (
                    <p className="text-xs font-medium text-bambu-gray uppercase tracking-wide pt-1">
                      {group.label}
                    </p>
                  )}
                  {group.items.map((queue) => {
                    const checked = pickedPrinters.has(queue.printer_id);
                    const status = statusOf(queue.printer_id);
                    const printing = queue.status === 'printing';
                    return (
                      <button
                        key={queue.printer_id}
                        type="button"
                        aria-pressed={checked}
                        onClick={() => setPickedPrinters((prev) => toggle(prev, queue.printer_id))}
                        className={`w-full flex items-center gap-3 p-2 rounded border text-left transition-colors ${
                          checked
                            ? 'border-bambu-green bg-bambu-green/10'
                            : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green/50'
                        }`}
                      >
                        {tick(checked)}
                        <PrinterIcon
                          className={`w-4 h-4 shrink-0 ${
                            status?.connected === false ? 'text-bambu-gray/40' : 'text-bambu-gray'
                          }`}
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm text-white truncate">
                            {queue.printer_name ?? `#${queue.printer_id}`}
                          </span>
                          <span className="block text-xs text-bambu-gray truncate">
                            {[
                              status?.connected === false
                                ? t('copyQueue.offline')
                                : printing
                                  ? t('copyQueue.busy', { percent: Math.round(status?.progress ?? 0) })
                                  : t('copyQueue.idle'),
                              queue.pending_count > 0
                                ? t('copyQueue.pending', { count: queue.pending_count })
                                : null,
                              queue.printer_location?.name ?? null,
                            ]
                              .filter(Boolean)
                              .join(' · ')}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2 p-4 border-t border-bambu-dark shrink-0">
        {/* Says where the copies land before you press it — appending is what
            everything else in BamDude does with a busy printer, and a copy
            that jumped the running queue would be the surprise.

            ⚠️ And what a copy IS (m173): an ordinary add, which reads the
            original file again and saves its own copy of it. So a job that is
            self-contained here can still refuse to be copied when its original
            is gone — said before the button rather than discovered after it. */}
        <span className="text-xs text-bambu-gray">
          {t('copyQueue.appendsHint')}
          {' '}
          {t('copyQueue.readsOriginalHint')}
        </span>
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={onCancel}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={!canCopy}
            onClick={() =>
              onConfirm(
                items.filter((_, index) => pickedItems.has(index)).map((entry) => entry.file),
                [...pickedPrinters],
              )
            }
          >
            <Copy className="w-4 h-4" />
            {t('copyQueue.copy')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
