import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { splitBySelfContained } from '../utils/queueSource';

interface QueueSpoolDeleteNoteProps {
  /** Archives about to be deleted / purged. */
  archiveIds?: readonly number[];
  /** Library files about to be trashed / purged. */
  libraryFileIds?: readonly number[];
  /** Ask only while the confirmation is on screen. */
  enabled?: boolean;
}

/**
 * What happens to the QUEUE when the original is deleted (spec §10).
 *
 * ⚠️ **"It will also remove N queued prints" stopped being true with m173.** A
 * queued job that already keeps its own copy of the file survives the archive or
 * library row it came from — deleting the source detaches the navigation link and
 * nothing more — while a job that is still reading the original goes with it. A
 * confirmation that claims either half for the whole set is a lie in every mixed
 * selection, which is the normal case for a bulk delete.
 *
 * So the split is COUNTED, from the rows themselves. The delete-impact endpoint
 * answers with one flat number, and inventing a breakdown from the kind of
 * source would be a guess; the queue list already carries each row's
 * `source_storage`, and that is the only proof there is.
 *
 * ⚠️ **PENDING rows only.** Both delete paths cancel exactly the pending ones
 * (`archive_purge` / `library_trash`); a terminal row is print history — it will
 * not print and it will not be cancelled — so counting it inflated
 * "still print" and misdescribed "are cancelled with them".
 *
 * ⚠️ **It never stays silent about a question it could not answer.** Nothing on
 * screen can distinguish "no queued print is affected" from "the queue could not
 * be read", and this sentence sits on a destructive confirmation, so a failed
 * query says so and repeats the consequence in words.
 */
export function QueueSpoolDeleteNote({
  archiveIds = [],
  libraryFileIds = [],
  enabled = true,
}: QueueSpoolDeleteNoteProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const asked = archiveIds.length > 0 || libraryFileIds.length > 0;
  // ⚠️ A reader without `queue:read_all` is answered with THEIR OWN rows only
  // (`print_queue.list_queue` filters by `created_by_id`), so the count below is
  // a part of the picture by construction. That is a fact about the permission,
  // not a guess from a number — the pre-flight's own total counts every status
  // and cannot be compared against a pending-only list.
  const seesEveryRow = hasPermission('queue:read_all');

  // Shares the `queue` prefix every queue view uses, so a queue mutation
  // refreshes it and an open confirmation cannot answer from stale rows. Only
  // pending rows are asked for — the rest are history, and it keeps this off the
  // full unpaginated listing with five eager-loaded relations per row.
  const { data: items, isLoading, isError } = useQuery({
    queryKey: ['queue', 'all', 'pending'],
    queryFn: () => api.getQueue(undefined, 'pending'),
    enabled: enabled && asked,
    staleTime: 5_000,
  });

  if (!asked || !enabled) return null;
  if (isLoading) return <p className="text-xs text-bambu-gray italic">{t('queueSpool.deleteNote.checking')}</p>;
  if (isError) {
    return <p className="text-xs text-yellow-700 dark:text-yellow-400">{t('queueSpool.deleteNote.couldNotCheck')}</p>;
  }

  const archives = new Set(archiveIds);
  const files = new Set(libraryFileIds);
  // ⚠️ Virtual rows are excluded: an external print's synthesised row is not a
  // job anybody queued and there is nothing to cancel or to keep.
  const affected = (items ?? []).filter(
    (item) =>
      !item.is_virtual &&
      ((item.archive_id != null && archives.has(item.archive_id)) ||
        (item.library_file_id != null && files.has(item.library_file_id))),
  );
  if (affected.length === 0) return null;

  const { selfContained, needsOriginal } = splitBySelfContained(affected.map((item) => item.source_storage));

  return (
    <div className="text-xs text-bambu-gray space-y-1">
      {selfContained > 0 && (
        <p>
          {t('queueSpool.deleteNote.selfContained', { count: selfContained })}{' '}
          {t('queueSpool.deleteNote.removeHint')}
        </p>
      )}
      {needsOriginal > 0 && <p>{t('queueSpool.deleteNote.needsOriginal', { count: needsOriginal })}</p>}
      {!seesEveryRow && (
        <p className="text-yellow-700 dark:text-yellow-400">{t('queueSpool.deleteNote.ownRowsOnly')}</p>
      )}
    </div>
  );
}
