import { api } from '../api/client';
import type { PrinterQueue, PrinterStatus, PrintQueueItem } from '../api/client';
import type { SequencedFile } from '../components/QueueSequencer';

/** One thing a copy run can queue elsewhere, with what the dialog shows about it. */
export interface CopyableItem {
  file: SequencedFile;
  /** This is the print running right now — worth saying, and it sorts first. */
  printing: boolean;
  printTimeSeconds: number | null;
  filamentGrams: number | null;
  thumbnailUrl: string | null;
}

/**
 * The queue items that can actually be queued somewhere else.
 *
 * ⚠️ An item with neither backing row cannot be copied — there is nothing to
 * queue elsewhere. It is filtered rather than shown greyed out, and the dialog
 * says how many it dropped.
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
    .map((item) => {
      // The library file wins when both are set: it is the row that outlives
      // this print, and re-queueing from it is what the operator did the first
      // time.
      const fromLibrary = item.library_file_id != null;
      const id = (item.library_file_id ?? item.archive_id) as number;
      return {
        file: {
          id,
          source: fromLibrary ? ('library' as const) : ('archive' as const),
          name: item.library_file_name || item.archive_name || `#${item.id}`,
          plateId: item.plate_id,
          // What makes two copies of one file and plate two copies, and what
          // tells the run the plate is already decided.
          itemId: item.id,
          batchId: item.batch_id,
        },
        printing: item.status === 'printing',
        printTimeSeconds: item.print_time_seconds ?? null,
        filamentGrams: item.filament_used_grams ?? null,
        thumbnailUrl: fromLibrary
          ? item.library_file_thumbnail
            ? api.getLibraryFileThumbnailUrl(id)
            : null
          : item.archive_thumbnail
            ? api.getArchiveThumbnail(id)
            : null,
      };
    });
}

/**
 * The print running right now, read from the printer rather than from a queue.
 *
 * ⚠️ **A print started outside BamDude's queue often has no queue row at all**,
 * and the queue card knows it only because the card's "currently printing"
 * block is drawn from live MQTT. The backend does synthesise a virtual item for
 * that case, but only while `_active_prints` still holds the print — a restart
 * empties that, and the card keeps showing the print while the queue list has
 * gone empty. Without this the copy button would vanish on exactly the machine
 * a farm most wants to clone: the one running a job somebody started from the
 * screen.
 *
 * `current_archive_id` is the durable answer — the status endpoint resolves it
 * from `print_archives` by `subtask_id`, not from the in-memory dict, so it
 * survives the restart that loses the virtual item.
 */
export function copyableCurrentPrint(status: PrinterStatus | undefined | null): CopyableItem | null {
  if (!status?.current_archive_id) return null;
  const name = status.subtask_name || status.current_print;
  if (!name) return null;
  return {
    file: {
      id: status.current_archive_id,
      source: 'archive',
      name,
      plateId: status.current_plate_id,
    },
    printing: true,
    printTimeSeconds: null,
    filamentGrams: null,
    thumbnailUrl: api.getArchiveThumbnail(status.current_archive_id),
  };
}

/**
 * The queue's own items, with the running print put back in front of them when
 * the queue does not already know about it.
 *
 * ⚠️ Deduplicated on the archive, not just on "something is already marked
 * printing": a real queue item and the live status describe the same print, and
 * offering it twice would queue two copies of it on every target.
 */
export function withCurrentPrint(
  fromQueue: CopyableItem[],
  status: PrinterStatus | undefined | null,
): CopyableItem[] {
  const live = copyableCurrentPrint(status);
  if (!live) return fromQueue;
  const alreadyThere = fromQueue.some(
    (entry) =>
      entry.printing ||
      (entry.file.source === 'archive' && entry.file.id === live.file.id),
  );
  return alreadyThere ? fromQueue : [live, ...fromQueue];
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
