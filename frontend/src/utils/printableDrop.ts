/**
 * What a "drop this on a printer / queue" target will accept.
 *
 * ⚠️ **One rule, because there were three.** `QueueCard`, `AutoQueuePanel` and
 * `PrintersPage` each carried the same line verbatim —
 * `!lower.endsWith('.gcode') && !lower.includes('.gcode.')` — which is the
 * pattern this codebase has paid for repeatedly (six readings of the filament
 * cost rate, seven copies of `folder_activity_at`, four of `flattenFolderTree`).
 *
 * ⚠️ **And all three disagreed with the backend.** That rule *accepted* a raw
 * `.gcode`, while `validate_print_file_upload` rejects it outright: Bambu
 * printers in network mode only parse a `.gcode.3mf` zip container, and a raw
 * `.gcode` produces a firmware parse error thirty seconds after Print is
 * pressed. So the zone advertised as "printable files only" let through exactly
 * the format that cannot be printed, and the user met a backend error instead of
 * the friendly refusal standing right there.
 */

/** Why a dropped file cannot be printed, or `null` when it looks like it can. */
export type DropRejection = 'rawGcode' | 'notSliced';

export function dropRejectionFor(filename: string): DropRejection | null {
  const lower = filename.toLowerCase();

  // Raw G-code: the backend's own first rejection, mirrored here so it is
  // answered at the drop rather than after an upload round-trip.
  if (lower.endsWith('.gcode')) return 'rawGcode';

  // Everything printable is a 3MF container. The name alone cannot prove one
  // holds sliced G-code — that is decided by content, and the upload response
  // settles it — but a name that is not a 3MF at all can be turned away here
  // instead of being uploaded to find out.
  if (!lower.endsWith('.3mf')) return 'notSliced';

  return null;
}

/** i18n key for a rejection, so all three zones say the same thing. */
export function dropRejectionKey(rejection: DropRejection): string {
  return rejection === 'rawGcode' ? 'printers.dropRawGcode' : 'printers.dropWrongFormat';
}

/**
 * Split a dropped batch into what is worth uploading and what is not.
 *
 * Every rejected file is returned individually: the three zones used to read
 * `dataTransfer.files[0]` and discard the rest in silence, so a five-file drop
 * queued one and said nothing about the other four.
 */
export function partitionDroppedFiles(files: File[]): {
  candidates: File[];
  rejected: { file: File; rejection: DropRejection }[];
} {
  const candidates: File[] = [];
  const rejected: { file: File; rejection: DropRejection }[] = [];
  for (const file of files) {
    const rejection = dropRejectionFor(file.name);
    if (rejection) rejected.push({ file, rejection });
    else candidates.push(file);
  }
  return { candidates, rejected };
}
