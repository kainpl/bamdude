import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FolderInput, Loader2, X } from 'lucide-react';
import { api, ApiError } from '../api/client';
import { writableFolders } from '../utils/folderTree';
import { Button } from './Button';

/**
 * Copy an archived print's 3MF into the library, into a folder you choose.
 *
 * The work happens server-side through the same helper MakerWorld import and
 * slicer output use, so the metadata parse, the thumbnail, the per-plate cache
 * and the content-hash dedupe are not reimplemented here — this dialog only
 * picks the destination.
 *
 * ⚠️ **Read-only external folders are left out of the list.** The backend
 * answers those with 403, and offering a choice that cannot work is worse than
 * not offering it.
 *
 * ⚠️ **Saving the same print twice does not make a second copy.** The server
 * dedupes on content hash and hands back the row already there; the dialog says
 * so rather than pretending it wrote something.
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
  const [result, setResult] = useState<{ already: boolean } | null>(null);

  const foldersQuery = useQuery({
    queryKey: ['library-folders'],
    queryFn: () => api.getLibraryFolders(),
    enabled: isOpen,
  });

  const save = useMutation({
    mutationFn: () => api.saveArchiveToLibrary(archiveId, folderId),
    onSuccess: (data) => {
      setError(null);
      setResult({ already: data.already_in_library });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
    },
    onError: (e: ApiError) => {
      setResult(null);
      setError(e.message);
    },
  });

  if (!isOpen) return null;

  const folders = writableFolders(foldersQuery.data);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white flex items-center gap-2">
            <FolderInput className="w-4 h-4" />
            {t('archives.saveToLibrary.title')}
          </h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <p className="text-xs text-bambu-gray leading-snug">
            {t('archives.saveToLibrary.description', { name: archiveName })}
          </p>

          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-bambu-gray">
              {t('archives.saveToLibrary.folder')}
            </span>
            <select
              value={folderId ?? ''}
              onChange={(e) => setFolderId(e.target.value ? Number(e.target.value) : null)}
              disabled={save.isPending}
              className="mt-1 w-full px-2 py-1.5 rounded bg-bambu-dark-tertiary text-white text-sm border border-transparent focus:border-bambu-green focus:outline-none"
            >
              <option value="">{t('archives.saveToLibrary.rootFolder')}</option>
              {folders.map(({ folder, depth }) => (
                <option key={folder.id} value={folder.id}>
                  {`${'— '.repeat(depth)}${folder.name}`}
                </option>
              ))}
            </select>
          </label>

          {result && (
            <p className={`text-[11px] leading-snug ${result.already ? 'text-amber-400' : 'text-bambu-green'}`}>
              {result.already ? t('archives.saveToLibrary.alreadyThere') : t('archives.saveToLibrary.saved')}
            </p>
          )}
          {error && <p className="text-[11px] text-red-400 leading-relaxed">{error}</p>}
        </div>

        <div className="px-4 py-3 border-t border-bambu-dark-tertiary flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {result ? t('common.close') : t('common.cancel')}
          </Button>
          <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending || result !== null}>
            {save.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('archives.saveToLibrary.action')}
          </Button>
        </div>
      </div>
    </div>
  );
}
