import { api } from '../api/client';
import type { PrinterQueue, PrinterStatus, PrintQueueItem } from '../api/client';
import type { SequencedFile } from '../components/QueueSequencer';

/** One row of the copy dialog: what it is, and what a copy of it would queue. */
export interface CopyableItem {
  /**
   * The file a copy would queue — `null` when this row has no original left to
   * re-read, in which case the row is LISTED but cannot be copied.
   *
   * ⚠️ A copy is an ordinary add, so it needs a library file or an archive to
   * read again: m173 saved this job's own bytes, but nothing can queue a
   * snapshot onto another printer yet. Not being copyable is a fact about the
   * row, not a reason to hide it.
   */
  file: SequencedFile | null;
  /** Stable list key — the queue row, or the live print's archive. */
  key: string;
  /** What to call the row, copyable or not. */
  name: string;
  /** The plate the row was queued with. */
  plateId: number | null;
  /** This is the print running right now — worth saying, and it sorts first. */
  printing: boolean;
  /** The order the copy will be filed under, shown in the row so the operator
   *  sees it BEFORE anything queues — the dialog will not ask again. Null for
   *  a row filed under no order, and for the live print, which has no row. */
  orderName?: string | null;
  printTimeSeconds: number | null;
  filamentGrams: number | null;
  /** A picture built from an ORIGINAL row's id, when there is one. */
  thumbnailUrl: string | null;
  /**
   * The queue row whose OWN snapshot renders this plate, or `null`.
   *
   * Preferred over `thumbnailUrl` — the job prints the bytes it accepted, and
   * the original may have been re-sliced since (A03) — and the only picture a
   * job whose originals are gone has at all.
   */
  pictureItemId: number | null;
}

/**
 * Every row of this queue, and for each one whether it can be queued elsewhere.
 *
 * ⚠️ **A row with neither backing id is listed, not dropped** (m173). It used to
 * be filtered out, which meant a job that had outlived its library file — and
 * that prints perfectly from its own saved copy — disappeared from the operator's
 * own queue in this dialog. Vanishing is worse than being un-copyable, so it is
 * shown, with its picture, saying why the copy cannot be made: a copy is an
 * ordinary add and needs an original to read again.
 *
 * ⚠️ **The plate travels with the item.** Copying onto another printer of the
 * same model means literally the same file, so the plate it was queued with
 * exists there too — and a copy that forgot which plate was queued would not be
 * one. This is the narrow case where carrying a per-file answer across is
 * right; a bulk selection of arbitrary files must never do it.
 */
export function copyableItems(items: readonly PrintQueueItem[]): CopyableItem[] {
  return items.map((item) => {
    // The library file wins when both are set: it is the row that outlives
    // this print, and re-queueing from it is what the operator did the first
    // time.
    const fromLibrary = item.library_file_id != null;
    const id = item.library_file_id ?? item.archive_id ?? null;
    const name = item.library_file_name || item.archive_name || `#${item.id}`;
    return {
      file:
        id === null
          ? null
          : {
              id,
              source: fromLibrary ? ('library' as const) : ('archive' as const),
              name,
              plateId: item.plate_id,
              routing: item.filament_routing ?? undefined,
              // What makes two copies of one file and plate two copies, and what
              // tells the run the plate is already decided.
              itemId: item.id,
              batchId: item.batch_id,
              // The order the source row was filed under — "none" included. Always
              // set for a queue row: the question WAS answered when it was queued.
              orderFiling: { projectId: item.project_id ?? null, projectLineId: item.project_line_id ?? null },
            },
      key: `item-${item.id}`,
      name,
      plateId: item.plate_id ?? null,
      orderName: item.project_name,
      printing: item.status === 'printing',
      printTimeSeconds: item.print_time_seconds ?? null,
      filamentGrams: item.filament_used_grams ?? null,
      // The job's own render is asked for by item id; the original's thumbnail is
      // the fallback, and for a row with no ids it is all there ever was.
      pictureItemId: item.source_thumbnail ? item.id : null,
      thumbnailUrl:
        id === null
          ? null
          : fromLibrary
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
    key: `live-archive-${status.current_archive_id}`,
    name,
    plateId: status.current_plate_id ?? null,
    printing: true,
    printTimeSeconds: null,
    filamentGrams: null,
    thumbnailUrl: api.getArchiveThumbnail(status.current_archive_id),
    // The live print is read from the printer, not from a queue row — there is
    // no row whose snapshot could be asked for, and the archive it names has a
    // thumbnail of its own.
    pictureItemId: null,
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
  // ⚠️ `entry.file?`, not `entry.file` — a row whose originals are gone has no
  // file at all now, and it is not the live print either way.
  const alreadyThere = fromQueue.some(
    (entry) =>
      entry.printing ||
      (entry.file?.source === 'archive' && entry.file.id === live.file?.id),
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
