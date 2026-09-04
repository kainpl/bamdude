import { useEffect, useId, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { FileArchive, Loader2, Upload, X } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { FolderTreePicker } from '../FolderTreePicker';
import { Button } from '../Button';
import { cardNotesText } from './cardNotes';
import { useDialogFocus } from '../../hooks/useDialogFocus';

interface ImportProductDialogProps {
  onClose: () => void;
}

/**
 * Rebuild a product from an export ZIP.
 *
 * ⚠️ **The folder is a DESTINATION, not a link.** It is where files nobody
 * already has land; the server never joins it to the product, because "every
 * file in here belongs to this product" is not what an operator said by
 * importing into their Downloads folder. Files the library already holds are
 * matched by content hash and reused, so a second import of the same export
 * adds no duplicates — which is also why the picker is optional: with nothing
 * chosen the server reuses, or makes, a root folder named after the product.
 *
 * ⚠️ **The warnings are the point, not decoration.** An import is somebody
 * else's export and half of what it has to say is what it could NOT take — a
 * file the library refused, a plate the 3MF no longer carries, an attachment
 * the manifest names and the archive lacks. They arrive as `CardNote` codes and
 * go through the same `cardNoteText` as every other card answer.
 *
 * ⚠️ **A refusal stays on screen.** 400 (not an export) and 413 (over the
 * ceiling) are shown IN the dialog rather than as a toast, because both are
 * answered by picking a different file — which the operator can only do while
 * the file input is still in front of them.
 */
export function ImportProductDialog({ onClose }: ImportProductDialogProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const titleId = useId();
  // Mounted only while it is open, so "open" is simply `true`.
  const dialog = useDialogFocus<HTMLDivElement>(true);
  const [file, setFile] = useState<File | null>(null);
  const [folderId, setFolderId] = useState<number | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const { data: folders } = useQuery({ queryKey: ['library-folders'], queryFn: api.getLibraryFolders });

  const run = useMutation({
    mutationFn: (chosen: File) => api.importProduct(chosen, folderId),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      // One toast, every warning in it: they are one answer to one question,
      // and five stacked toasts would push the first off screen before it is
      // read. The product page opens behind it either way — a product with a
      // refused file and a warning on screen is worth more than no product.
      showToast(
        result.warnings.length
          ? `${t('products.toast.imported')} · ${cardNotesText(t, result.warnings)}`
          : t('products.toast.imported'),
        result.warnings.length ? 'warning' : 'success',
      );
      navigate(`/products/${result.product.id}`);
    },
    onError: (e: Error) => {
      const status = e instanceof ApiError ? e.status : 0;
      // 413 is the one refusal whose server sentence names a byte count nobody
      // reads; everything else is repeated verbatim, because the server knows
      // what was wrong with the archive and this dialog does not.
      setRefusal(status === 413 ? t('products.import.tooLarge') : t('products.import.failed', { detail: e.message }));
    },
  });

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      {/* ⚠️ The role, the name and the focus, as one unit — see
          `useDialogFocus`, which lists every overlay that uses it and says
          exactly what it does and does not do. Without them the overlay is an
          anonymous `<div>` a screen reader never announces, and a keyboard user
          opening it starts at the top of the PAGE behind. */}
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="bg-bambu-dark-secondary rounded-lg w-full max-w-lg max-h-[85vh] flex flex-col outline-none"
      >
        <div className="flex items-center justify-between gap-3 p-4 border-b border-bambu-dark shrink-0">
          <h2 id={titleId} className="text-lg font-semibold text-white truncate">
            {t('products.import.title')}
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded" aria-label={t('common.close')}>
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          <p className="text-sm text-bambu-gray">{t('products.import.hint')}</p>

          <div className="space-y-2">
            <label className="block text-sm font-medium text-white">{t('products.import.file')}</label>
            <input
              ref={input}
              data-testid="import-file-input"
              type="file"
              accept=".zip,application/zip"
              className="hidden"
              onChange={(e) => {
                setRefusal(null);
                setFile(e.target.files?.[0] ?? null);
              }}
            />
            <div className="flex items-center gap-3">
              <Button type="button" variant="secondary" onClick={() => input.current?.click()} disabled={run.isPending}>
                <FileArchive className="w-4 h-4" />
                {t('products.import.choose')}
              </Button>
              {file && <span className="text-sm text-white truncate">{file.name}</span>}
            </div>
          </div>

          <div className="space-y-2">
            <label className="block text-sm font-medium text-white">{t('products.import.folder')}</label>
            <p className="text-xs text-bambu-gray">{t('products.import.folderHint')}</p>
            <FolderTreePicker
              folders={folders}
              value={folderId}
              onChange={setFolderId}
              rootLabel={t('products.import.newFolder')}
              className="max-h-48 rounded border border-bambu-dark-tertiary p-1"
            />
          </div>

          {refusal && (
            <p role="alert" className="text-sm text-status-error">
              {refusal}
            </p>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 p-4 border-t border-bambu-dark shrink-0">
          <Button type="button" variant="secondary" onClick={onClose} disabled={run.isPending}>
            {t('common.cancel')}
          </Button>
          <Button type="button" onClick={() => file && run.mutate(file)} disabled={!file || run.isPending}>
            {run.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
            {t('products.import.submit')}
          </Button>
        </div>
      </div>
    </div>
  );
}
