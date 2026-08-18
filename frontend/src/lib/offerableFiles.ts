import type { LibraryFileListItem } from '../api/client';
import { isPrintable } from './fileTags';
import { mapModelCode } from '../utils/printer';

/**
 * Files a queue can actually be loaded with, out of a library listing.
 *
 * The sibling of `utils/printableDrop.ts`: that one decides what an OS file
 * dropped on a card may be, by name, before it is uploaded; this one decides
 * what a library row may be, by what the backend recorded about it. Both answer
 * the same question — is this a printable file for a queue — and both must keep
 * answering it the same way, or the picker offers a file the Schedule dialog
 * then refuses.
 *
 * Two questions, the same two the drop handlers ask:
 *
 * - does it hold G-code — **content**, via the `gcode` tag (see `isPrintable`),
 *   never the `sliced` tag, which is provenance;
 * - is it sliced for a machine we know — no recorded model means nothing about
 *   it can be verified, so it is refused rather than queued on a guess.
 *
 * ⚠️ An unmappable printer model filters nothing, exactly as on a drop, where
 * the check reads `if (mapModelCode(...) && ...)`. We cannot compare against a
 * code we do not have, and refusing everything on that basis would make an
 * unrecognised machine unloadable.
 *
 * `printerModel` absent is a different thing from unmappable: it means there is
 * no one machine — the auto-queue — where each file's own `sliced_for_model`
 * becomes its own target.
 */
export function offerableFiles(
  files: LibraryFileListItem[] | undefined | null,
  printerModel?: string | null,
): LibraryFileListItem[] {
  const wanted = (printerModel ? mapModelCode(printerModel) : '').toLowerCase() || null;
  return (files ?? []).filter((file) => {
    if (!isPrintable(file)) return false;
    if (!file.sliced_for_model) return false;
    return !wanted || file.sliced_for_model.toLowerCase() === wanted;
  });
}
