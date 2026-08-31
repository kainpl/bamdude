import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api } from '../api/client';
import type { LibraryGroupingMetadata } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { groupSelection } from '../utils/queueGrouping';
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

/** One PrintModal mount: a file, and which of its plates this group holds. */
interface RunMember {
  file: SequencedFile;
  /** In-group plate indexes, or null to leave the plate question to the file's
   *  own answer (a copy run's ``plateId``, or the dialog's default). */
  plateIds: number[] | null;
}

/** One answer: the dialog that is shown, plus the members it stands for. */
interface RunGroup {
  members: RunMember[];
  /** Plates this group queues in total — what the badge counts. */
  units: number;
}

/** A run over one file at a time: what every caller had before grouping. */
function perFileRun(files: SequencedFile[]): RunGroup[] {
  return files.map((file) => ({ members: [{ file, plateIds: null }], units: 1 }));
}

/**
 * Turn the selection into the groups the run walks.
 *
 * Without metadata this is the old per-file run, which is the honest fallback:
 * grouping is a saving, and losing the saving must never cost the ability to
 * queue.
 */
function buildRun(
  files: SequencedFile[],
  metadata: LibraryGroupingMetadata[] | undefined,
): { grouped: boolean; groups: RunGroup[] } {
  if (!metadata) return { grouped: false, groups: perFileRun(files) };

  const byId = new Map(files.map((file) => [file.id, file]));
  const known = new Set(metadata.map((row) => row.file_id));
  // A file the server could not parse has no plates to preselect: its single
  // unit is a placeholder for "ask about it anyway", not knowledge about it.
  const plateless = new Set(metadata.filter((row) => row.plates.length === 0).map((row) => row.file_id));

  const groups: RunGroup[] = groupSelection(metadata, files.map((file) => file.id)).map((group) => {
    // Units of ONE file are handled by ONE mount — the dialog ticks all of that
    // file's in-group plates, so showing one while the rest queue silently
    // would make it lie about what it is about to do.
    const platesByFile = new Map<number, number[]>();
    for (const unit of group.units) {
      const seen = platesByFile.get(unit.fileId);
      if (seen) seen.push(unit.plateIndex);
      else platesByFile.set(unit.fileId, [unit.plateIndex]);
    }
    return {
      units: group.units.length,
      members: [...platesByFile.entries()].flatMap(([fileId, plateIds]) => {
        const file = byId.get(fileId);
        return file ? [{ file, plateIds: plateless.has(fileId) ? null : plateIds }] : [];
      }),
    };
  });

  // ⚠️ The server skips ids it does not know, and `groupSelection` skips what
  // the server skipped — so a file the library has since lost would drop out of
  // the run without a word. It gets asked about instead, ungrouped.
  for (const file of files) {
    if (!known.has(file.id)) groups.push({ members: [{ file, plateIds: null }], units: 1 });
  }

  return { grouped: true, groups: groups.filter((group) => group.members.length > 0) };
}

/**
 * Queue a set of files by opening the Schedule dialog once per GROUP — files
 * whose dialog would be answered identically are answered once, and the rest of
 * the group queues itself without rendering.
 *
 * There is no bulk dialog because there is nothing a bulk dialog could ask that
 * this one doesn't: printer or auto-queue, plates, AMS mapping, print options,
 * schedule, quantity. The group is exactly the set for which those answers
 * coincide — which is why one dialog can stand for it, and why a selection that
 * disagrees still gets asked file by file.
 *
 * Each member gets a FRESH modal (keyed on its position in the run as well as
 * its id). Plate selection, filament mapping and per-printer config belong to
 * one file, and PrintModal's self-submit uses once-only refs that would leak
 * across members if React reused the mount — the same file can legitimately
 * appear in two groups, so the id alone is not a unique key.
 *
 * ⚠️ **This component never builds a queue payload.** It composes modals;
 * PrintModal owns the payload, and the removed bulk endpoint is why.
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
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [groupIndex, setGroupIndex] = useState(0);
  const [memberIndex, setMemberIndex] = useState(0);

  // PrintModal calls onSuccess and THEN onClose on a successful submit, and
  // only onClose when the operator gives up. So onClose is the single place
  // that decides what happens next, and onSuccess only records which of the two
  // it was. This cannot be state: both fire in one tick, and the second
  // setState would win.
  const queuedRef = useRef(false);

  // The run's tally, all by ref for the same reason: it is written from inside
  // onClose and read in the same tick when the run ends.
  const queuedUnitsRef = useRef(0);
  const queuedByFileRef = useRef(new Map<number, number>());
  const answeredGroupsRef = useRef(new Set<number>());
  const askedRef = useRef(0);

  const ids = useMemo(() => files.map((file) => file.id), [files]);

  // ⚠️ Only a selection of DISTINCT files that said nothing but their id and
  // name can be grouped. A caller that named a `source` or a `plateId` has
  // already answered per file, and a copy run names both:
  //   · an archive id is not a library file id, so the library would answer
  //     about a different row entirely;
  //   · `plateId: null` there means "this item had no plate", not "nobody has
  //     decided" — grouping it would tick every plate of a 3-plate file and
  //     queue three items where the operator asked for one copy;
  //   · and the same file can be in a queue twice, where a repeated id would
  //     collapse two wanted copies into one plate set.
  const groupable = useMemo(
    () =>
      ids.length > 0 &&
      new Set(ids).size === ids.length &&
      files.every((file) => file.source === undefined && file.plateId === undefined),
    [files, ids],
  );

  const grouping = useQuery({
    queryKey: ['queue-grouping', ids],
    queryFn: () => api.getLibraryGroupingMetadata(ids),
    enabled: groupable,
    // No retry: the fallback is a working run, and a retried failure would be
    // seconds of blank screen where the operator expects a dialog.
    retry: false,
    staleTime: 60_000,
  });

  const { grouped, groups } = useMemo(
    () => buildRun(files, grouping.data),
    [files, grouping.data],
  );

  /** Plates each file owes across the whole run. */
  const owedByFile = useMemo(() => {
    const owed = new Map<number, number>();
    for (const group of groups) {
      for (const member of group.members) {
        owed.set(member.file.id, (owed.get(member.file.id) ?? 0) + (member.plateIds?.length ?? 1));
      }
    }
    return owed;
  }, [groups]);

  const totalUnits = useMemo(() => groups.reduce((sum, group) => sum + group.units, 0), [groups]);

  // `isLoading` and not `isPending`: a disabled query is pending forever, and
  // gating on that would render nothing at all for an ungroupable run.
  if (grouping.isLoading) return null;

  const group = groups[groupIndex];
  const member = group?.members[memberIndex];
  if (!group || !member) return null;

  /** Files with at least one plate still undistributed, in the operator's order.
   *
   *  ⚠️ Deliberately over-inclusive: a file whose plates landed in two groups
   *  is handed back when either group is abandoned. The caller re-ticks what
   *  comes back, so an extra tick costs a second look; a missing one silently
   *  drops work the operator still means to distribute. */
  const stillOwed = () =>
    files.filter((file) => (queuedByFileRef.current.get(file.id) ?? 0) < (owedByFile.get(file.id) ?? 1));

  const finish = (remaining: SequencedFile[]) => {
    // One report for the whole run, and only when the run actually queued
    // something the operator did not see a dialog for. A run whose every group
    // held a single unit asked about every plate, so it has nothing to explain.
    const queued = queuedUnitsRef.current;
    if (grouped && queued > 0 && totalUnits > groups.length) {
      const answered = answeredGroupsRef.current.size;
      showToast(
        askedRef.current > 0
          ? t('queue.groupedQueuedWithAsks', { queued, count: answered, asked: askedRef.current })
          : t('queue.groupedQueued', { queued, count: answered }),
      );
    }
    onDone(remaining);
  };

  const showBadge = grouped && (groups.length > 1 || group.units > 1);

  return (
    <PrintModal
      key={`${groupIndex}:${memberIndex}:${member.file.source ?? 'library'}:${member.file.id}`}
      mode={mode}
      libraryFileId={member.file.source === 'archive' ? undefined : member.file.id}
      archiveId={member.file.source === 'archive' ? member.file.id : undefined}
      preselectedPlateId={member.plateIds ? undefined : member.file.plateId}
      preselectedPlateIds={member.plateIds ?? undefined}
      archiveName={member.file.name}
      initialSelectedPrinterIds={initialSelectedPrinterIds}
      initialDispatchMode={initialDispatchMode}
      lockDispatchMode={lockDispatchMode}
      lockPrinterSelection={lockPrinterSelection}
      lockAutoTarget={lockAutoTarget}
      groupBadge={
        showBadge
          ? { current: groupIndex + 1, total: groups.length, units: group.units }
          : undefined
      }
      // The ungrouped run keeps its own "2/5" counter — the group badge would
      // be answering a question that run never asked.
      sequence={!grouped && files.length > 1 ? { current: groupIndex + 1, total: files.length } : undefined}
      // Every member after the group's first one submits itself. It can still
      // decide it has to ask (no filament match, a dead status query, a
      // low-spool warning, a failed dispatch) and render — which is why the
      // branch below treats a refusal exactly like today's abandon.
      autoSubmitWhenUnambiguous={memberIndex > 0}
      onAutoSubmitRefused={() => {
        askedRef.current += 1;
      }}
      onSuccess={() => {
        queuedRef.current = true;
      }}
      onClose={() => {
        const queued = queuedRef.current;
        queuedRef.current = false;

        // Abandoned here: whatever this group and the ones after it still owe
        // is undistributed, and the caller puts it back into the selection.
        if (!queued) {
          finish(stillOwed());
          return;
        }

        const units = member.plateIds?.length ?? 1;
        queuedUnitsRef.current += units;
        queuedByFileRef.current.set(
          member.file.id,
          (queuedByFileRef.current.get(member.file.id) ?? 0) + units,
        );
        answeredGroupsRef.current.add(groupIndex);

        const nextMember = memberIndex + 1;
        if (nextMember < group.members.length) {
          setMemberIndex(nextMember);
          return;
        }
        const nextGroup = groupIndex + 1;
        if (nextGroup < groups.length) {
          setGroupIndex(nextGroup);
          setMemberIndex(0);
          return;
        }
        finish([]);
      }}
    />
  );
}
