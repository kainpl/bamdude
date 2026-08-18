import { useTranslation } from 'react-i18next';
import { AlertTriangle, X } from 'lucide-react';

import type { TrashRestoreConflict } from '../api/client';
import { Button } from './Button';

interface RestoreDuplicateDialogProps {
  conflicts: TrashRestoreConflict[];
  /** How many of the selected files have no conflict at all. */
  cleanCount: number;
  onRestoreAll: () => void;
  onSkipDuplicates: () => void;
  onCancel: () => void;
  busy?: boolean;
}

/**
 * Asks before restoring a file whose content the library already holds.
 *
 * ⚠️ It asks — it does not refuse. Every ingest path now declines to create a
 * byte-identical duplicate, and restoring from the trash is the one way left to
 * make one; but a duplicate can be deliberate (two MakerWorld profiles produce
 * identical 3MFs), and the person restoring is the one who put the file there.
 *
 * "Skip the duplicates" only appears when there is something to skip — offering
 * it for a single conflicting file would mean offering "do nothing" twice.
 */
export function RestoreDuplicateDialog({
  conflicts,
  cleanCount,
  onRestoreAll,
  onSkipDuplicates,
  onCancel,
  busy = false,
}: RestoreDuplicateDialogProps) {
  const { t } = useTranslation();

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-lg">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <AlertTriangle className="w-5 h-5 text-yellow-500" />
            {t('libraryTrash.restoreDuplicate.title')}
          </h2>
          <button onClick={onCancel} className="p-1 hover:bg-bambu-dark rounded" aria-label={t('common.close')}>
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <p className="text-sm text-white">
            {t('libraryTrash.restoreDuplicate.intro', { count: conflicts.length })}
          </p>

          <ul className="max-h-48 overflow-y-auto space-y-1 text-xs">
            {conflicts.map((c) => (
              <li key={c.id} className="p-2 rounded bg-bambu-dark/50">
                <span className="text-white break-words">{c.filename}</span>
                <span className="text-bambu-gray"> — {t('libraryTrash.restoreDuplicate.sameAs')} </span>
                <span className="text-white break-words">{c.existing_filename}</span>
              </li>
            ))}
          </ul>

          {cleanCount > 0 && (
            <p className="text-xs text-bambu-gray">
              {t('libraryTrash.restoreDuplicate.othersRestoreAnyway', { count: cleanCount })}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {t('common.cancel')}
          </Button>
          {cleanCount > 0 && (
            <Button variant="ghost" onClick={onSkipDuplicates} disabled={busy}>
              {t('libraryTrash.restoreDuplicate.skipDuplicates', { count: cleanCount })}
            </Button>
          )}
          <Button onClick={onRestoreAll} disabled={busy}>
            {t('libraryTrash.restoreDuplicate.restoreAll')}
          </Button>
        </div>
      </div>
    </div>
  );
}
