import { useRef, useState } from 'react';

import type { LibraryFileListItem } from '../api/client';
import { PrintModal } from './PrintModal';

interface QueueSequencerProps {
  /** The files to distribute, in the order the operator sees them. */
  files: LibraryFileListItem[];
  /** Called once when the run ends, with the files that were never queued. */
  onDone: (remaining: LibraryFileListItem[]) => void;
}

/**
 * Queue a selection of library files by opening the Schedule dialog once per
 * file, carrying a `2/5` counter.
 *
 * There is no bulk dialog because there is nothing a bulk dialog could ask that
 * this one doesn't: printer or auto-queue, plates, AMS mapping, print options,
 * schedule, quantity. Those are exactly the answers that differ between two
 * files in one selection, so the file is the unit — not the batch.
 *
 * Each file gets a FRESH modal (keyed on its id). Plate selection, filament
 * mapping and per-printer config belong to one file; carrying them over would
 * be wrong rather than convenient — plate 3 of one file need not exist in the
 * next.
 */
export function QueueSequencer({ files, onDone }: QueueSequencerProps) {
  const [index, setIndex] = useState(0);

  // PrintModal calls onSuccess and THEN onClose on a successful submit, and
  // only onClose when the operator gives up. So onClose is the single place
  // that decides what happens next, and onSuccess only records which of the two
  // it was. This cannot be state: both fire in one tick, and the second
  // setState would win.
  const queuedRef = useRef(false);

  const file = files[index];
  if (!file) return null;

  return (
    <PrintModal
      key={file.id}
      mode="add-to-queue"
      libraryFileId={file.id}
      archiveName={file.print_name || file.filename}
      sequence={files.length > 1 ? { current: index + 1, total: files.length } : undefined}
      onSuccess={() => {
        queuedRef.current = true;
      }}
      onClose={() => {
        const queued = queuedRef.current;
        queuedRef.current = false;
        // Abandoned here: this file and everything after it are still
        // undistributed, and the caller puts them back into the selection.
        if (!queued) {
          onDone(files.slice(index));
          return;
        }
        const next = index + 1;
        if (next >= files.length) {
          onDone([]);
          return;
        }
        setIndex(next);
      }}
    />
  );
}
