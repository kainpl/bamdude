import { useRef, useState } from 'react';

import { PrintModal } from './PrintModal';
import type { PrintModalMode } from './PrintModal';

/** The least a file must say about itself to be scheduled. */
export interface SequencedFile {
  id: number;
  /** What the dialog calls it — a print name where there is one, else the filename. */
  name: string;
  /** Which side of PrintModal's either/or the id belongs to. Defaults to the
   *  library file, which is what every caller had until a queue could be copied
   *  — a queue item can be backed by an archive instead. */
  source?: 'library' | 'archive';
  /** Pre-select this plate instead of letting the dialog default to the first.
   *  Only a caller that KNOWS the file's plates may set it — copying a queue
   *  does, because it is literally the same file. A general bulk selection must
   *  not: plate 3 of one file need not exist in the next. */
  plateId?: number | null;
}

interface QueueSequencerProps {
  /** The files to distribute, in the order the operator sees them. */
  files: SequencedFile[];
  /** Called once when the run ends, with the files that were never queued. */
  onDone: (remaining: SequencedFile[]) => void;
  /** Defaults to `add-to-queue`, which is what every caller wants today. */
  mode?: PrintModalMode;
  /** Pin the run to one printer — the drop target already chose it. Pair with
   *  ``lockPrinterSelection`` so the dialog still SHOWS which printer rather
   *  than omitting the question. */
  initialSelectedPrinterIds?: number[];
  lockPrinterSelection?: boolean;
  initialDispatchMode?: 'specific' | 'auto';
  /** Hide the specific/auto toggle when the drop target already implies it. */
  lockDispatchMode?: boolean;
  /** Pin each file's auto-queue target to its own ``sliced_for_model``. Per
   *  file, not per run — two dropped files can be sliced for two machines. */
  lockAutoTarget?: boolean;
}

/**
 * Queue a set of files by opening the Schedule dialog once per file, carrying a
 * `2/5` counter.
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
 *
 * Used by the library's bulk Schedule and by dropping files onto a printer or a
 * printer's queue. The drop targets pass a pinned printer; the library passes
 * none and lets the dialog ask.
 */
export function QueueSequencer({
  files,
  onDone,
  mode = 'add-to-queue',
  initialSelectedPrinterIds,
  initialDispatchMode,
  lockDispatchMode,
  lockPrinterSelection,
  lockAutoTarget,
}: QueueSequencerProps) {
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
      key={`${file.source ?? 'library'}-${file.id}`}
      mode={mode}
      libraryFileId={file.source === 'archive' ? undefined : file.id}
      archiveId={file.source === 'archive' ? file.id : undefined}
      preselectedPlateId={file.plateId}
      archiveName={file.name}
      initialSelectedPrinterIds={initialSelectedPrinterIds}
      initialDispatchMode={initialDispatchMode}
      lockDispatchMode={lockDispatchMode}
      lockPrinterSelection={lockPrinterSelection}
      lockAutoTarget={lockAutoTarget}
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
