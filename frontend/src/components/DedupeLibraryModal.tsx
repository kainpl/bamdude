import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Copy, Loader2, X } from 'lucide-react';

import { api } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

interface DedupeLibraryModalProps {
  onClose: () => void;
}

/**
 * Move byte-identical duplicates already in the library to the trash.
 *
 * New arrivals never create a duplicate any more, so this is a one-time
 * cleanup for what accumulated before — which is why it is a button rather
 * than something that runs at startup: there would be nobody to show the
 * result to, while the trash retention clock is already running.
 *
 * ⚠️ It asks the backend first. A dialog that says "this will trash N files"
 * has to know N, and the same endpoint answers it without writing.
 */
export function DedupeLibraryModal({ onClose }: DedupeLibraryModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const preview = useQuery({
    queryKey: ['library-dedupe-preview'],
    queryFn: () => api.dedupeLibraryFiles(true),
    // Always re-ask: the answer changes with every upload, and a stale count on
    // a destructive-looking button is worse than a spinner.
    staleTime: 0,
    gcTime: 0,
  });

  const run = useMutation({
    mutationFn: () => api.dedupeLibraryFiles(false),
    onSuccess: (res) => {
      showToast(t('libraryDedupe.toast.success', { count: res.trashed }), 'success');
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      queryClient.invalidateQueries({ queryKey: ['library-folders'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash'] });
      queryClient.invalidateQueries({ queryKey: ['library-trash-count'] });
      onClose();
    },
    onError: () => showToast(t('libraryDedupe.toast.failed'), 'error'),
  });

  const trashed = preview.data?.trashed ?? 0;
  const groups = preview.data?.groups ?? 0;

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-md">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <Copy className="w-5 h-5 text-blue-600 dark:text-blue-400" />
            {t('libraryDedupe.title')}
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded" aria-label={t('common.close')}>
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        <div className="p-4 space-y-3">
          {preview.isLoading ? (
            <div className="flex justify-center py-6">
              <Loader2 className="w-5 h-5 animate-spin text-bambu-green" />
            </div>
          ) : trashed === 0 ? (
            <p className="text-sm text-bambu-gray">{t('libraryDedupe.nothingToDo')}</p>
          ) : (
            <>
              <p className="text-sm text-white">{t('libraryDedupe.summary', { count: trashed, groups })}</p>
              {/* The two things that make this safe are the two things worth
                  saying out loud: the copy that is referenced stays, and
                  nothing is destroyed. */}
              <p className="text-xs text-bambu-gray">{t('libraryDedupe.whichSurvives')}</p>
              <p className="text-xs text-bambu-gray">{t('libraryDedupe.reversible')}</p>
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark">
          <Button variant="ghost" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => run.mutate()} disabled={trashed === 0 || run.isPending || preview.isLoading}>
            {run.isPending ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
            {t('libraryDedupe.confirm')}
          </Button>
        </div>
      </div>
    </div>
  );
}
