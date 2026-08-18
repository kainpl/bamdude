import type { PrinterQueue, PrintQueueItem } from '../api/client';
import type { SequencedFile } from '../components/QueueSequencer';

/** A queue item paired with the shape the Schedule dialog takes it in. */
export interface CopyableItem {
  file: SequencedFile;
  item: PrintQueueItem;
}

/**
 * The queue items that can actually be queued somewhere else.
 *
 * ⚠️ An item with neither backing row cannot be copied — there is nothing to
 * queue elsewhere. In practice every printing item has an archive by then (it
 * is created at print start), so this filters approximately nothing; it is here
 * because "approximately" is not "never", and the alternative is a run that
 * queues a file it cannot name.
 *
 * ⚠️ **The plate travels with the item.** Copying onto another printer of the
 * same model means literally the same file, so the plate it was queued with
 * exists there too — and a copy that forgot which plate was queued would not be
 * one. This is the narrow case where carrying a per-file answer across is
 * right; a bulk selection of arbitrary files must never do it.
 */
export function copyableItems(items: readonly PrintQueueItem[]): CopyableItem[] {
  return items
    .filter((item) => item.library_file_id != null || item.archive_id != null)
    .map((item) => ({
      item,
      file: {
        // The library file wins when both are set: it is the row that outlives
        // this print, and re-queueing from it is what the operator did the
        // first time.
        id: (item.library_file_id ?? item.archive_id) as number,
        source: item.library_file_id != null ? ('library' as const) : ('archive' as const),
        name: item.library_file_name || item.archive_name || `#${item.id}`,
        plateId: item.plate_id,
      },
    }));
}

/**
 * Printers a queue can be copied onto: same model, and not itself.
 *
 * ⚠️ Model equality, not a compatibility judgement. The items were sliced for
 * this machine; another model is a different build volume and a different
 * G-code flavour, and BamDude cannot re-slice. This is the same comparison the
 * drop zones make, on the same field the auto-queue routes by.
 */
export function copyTargets(
  queues: readonly PrinterQueue[] | undefined | null,
  source: PrinterQueue,
): PrinterQueue[] {
  return (queues ?? []).filter(
    (queue) => queue.printer_id !== source.printer_id && queue.printer_model === source.printer_model,
  );
}
