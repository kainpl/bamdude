import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, FileBox, FolderInput, FolderOpen, Loader2, X } from 'lucide-react';
import { api, ApiError } from '../api/client';
import { Button } from './Button';
import { FolderTreePicker } from './FolderTreePicker';

/**
 * Copy an archived print's 3MF into the library, into a folder you choose.
 *
 * The work happens server-side through the same helper MakerWorld import and
 * slicer output use, so the metadata parse, the thumbnail, the per-plate cache
 * and the content-hash dedupe are not reimplemented here — this dialog only
 * picks the destination.
 *
 * The destination comes from the same `FolderTreePicker` the "Move files"
 * dialog uses, so the two read alike — including leaving read-only external
 * folders out, which the backend answers with 403.
 *
 * ⚠️ **Saving the same print twice does not make a second copy.** The server
 * dedupes on content hash and hands back the row already there; this says so
 * rather than claiming to have written something, and still offers the way to
 * go and look at it.
 */

interface Props {
  archiveId: number;
  archiveName: string;
  isOpen: boolean;
  onClose: () => void;
}

export function SaveArchiveToLibraryModal({ archiveId, archiveName, isOpen, onClose }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [folderId, setFolderId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ already: boolean; folderId: number | null } | null>(null);

  const foldersQuery = useQuery({
    queryKey: ['library-folders'],
    queryFn: () => api.getLibraryFolders(),
    enabled: isOpen,
  });

  const save = useMutation({
    mutationFn: () => api.saveArchiveToLibrary(archiveId, folderId),
    onSuccess: (data) => {
      setError(null);
      setResult({ already: data.already_in_library, folderId: data.folder_id });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
    },
    onError: (e: ApiError) => {
      setResult(null);
      setError(e.message);
    },
  });

  if (!isOpen) return null;

  const done = result !== null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white flex items-center gap-2 min-w-0">
            <FolderInput className="w-4 h-4 shrink-0" />
            <span className="truncate">{t('archives.saveToLibrary.title')}</span>
          </h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors shrink-0">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-3">
          {/* What is being saved, named rather than described — the archive's
              own name is the thing you recognise it by. */}
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-bambu-dark text-sm text-white">
            <FileBox className="w-4 h-4 text-bambu-gray shrink-0" />
            <span className="truncate">{archiveName}</span>
          </div>

          {!done && (
            <>
              <div className="text-[10px] uppercase tracking-wider text-bambu-gray">
                {t('archives.saveToLibrary.folder')}
              </div>
              <div className="bg-bambu-dark rounded-lg p-2">
                <FolderTreePicker
                  folders={foldersQuery.data}
                  value={folderId}
                  onChange={setFolderId}
                  rootLabel={t('archives.saveToLibrary.rootFolder')}
                  className="max-h-56"
                />
                {foldersQuery.isLoading && (
                  <p className="text-sm text-bambu-gray text-center py-4">{t('common.loading')}</p>
                )}
              </div>
            </>
          )}

          {done && (
            <div
              className={`flex items-start gap-2 px-3 py-2 rounded-lg text-[11px] leading-snug ${
                result.already ? 'bg-amber-500/10 text-amber-400' : 'bg-bambu-green/10 text-bambu-green'
              }`}
            >
              <Check className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{result.already ? t('archives.saveToLibrary.alreadyThere') : t('archives.saveToLibrary.saved')}</span>
            </div>
          )}
          {error && <p className="text-[11px] text-red-400 leading-relaxed">{error}</p>}
        </div>

        <div className="px-4 py-3 border-t border-bambu-dark-tertiary flex justify-end gap-2">
          {done ? (
            <>
              <Button
                variant="secondary"
                onClick={() =>
                  window.location.assign(result.folderId ? `/files?folder=${result.folderId}` : '/files')
                }
              >
                <FolderOpen className="w-4 h-4" />
                <span className="ml-1.5">{t('archives.saveToLibrary.viewInLibrary')}</span>
              </Button>
              <Button variant="primary" onClick={onClose}>
                {t('common.close')}
              </Button>
            </>
          ) : (
            <>
              <Button variant="secondary" onClick={onClose}>
                {t('common.cancel')}
              </Button>
              <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>
                {save.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
                <span className={save.isPending ? 'ml-1.5' : ''}>{t('archives.saveToLibrary.action')}</span>
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
