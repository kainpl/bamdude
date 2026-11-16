/**
 * Deleting several files on a printer, one request each, with a report per file.
 *
 * The server answers every file on its own: the file a job is printing from is
 * refused while the others go. So one refusal must not end the batch — the old
 * loop stopped at the first error and never said which files were gone.
 *
 * Two answers do end it, because they are about the PRINTER and would be the
 * same for every file left: its state is not known (`printer_state_unknown`),
 * or the request itself is no longer allowed (401 / 403). The files not reached
 * are reported as not attempted, with the reason.
 */

export interface KeptFile {
  path: string;
  message: string;
  /** The machine code the server sent, when it sent one. */
  code?: string;
}

export interface BulkDeleteReport {
  deleted: string[];
  kept: KeptFile[];
  notAttempted: string[];
  /** Why the batch stopped early; absent when every file was asked. */
  stopMessage?: string;
}

interface RefusalLike {
  message?: string;
  code?: string;
  status?: number;
}

/** Whether a refusal is about the printer as a whole, so asking for the next file is pointless. */
export function stopsTheBatch(error: RefusalLike): boolean {
  return error.code === 'printer_state_unknown' || error.status === 401 || error.status === 403;
}

export async function deletePrinterFilesOneByOne(
  paths: string[],
  deleteOne: (path: string) => Promise<unknown>,
): Promise<BulkDeleteReport> {
  const report: BulkDeleteReport = { deleted: [], kept: [], notAttempted: [] };
  for (let index = 0; index < paths.length; index += 1) {
    const path = paths[index];
    try {
      await deleteOne(path);
      report.deleted.push(path);
    } catch (raw) {
      const error = (raw ?? {}) as RefusalLike;
      const message = typeof error.message === 'string' ? error.message : String(raw);
      if (stopsTheBatch(error)) {
        report.stopMessage = message;
        report.notAttempted = paths.slice(index);
        break;
      }
      report.kept.push({ path, message, code: error.code });
    }
  }
  return report;
}
