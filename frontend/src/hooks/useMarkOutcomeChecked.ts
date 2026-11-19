import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';

/**
 * «No defects» — marks one print's result as checked.
 *
 * It records no defects and changes no count: it only takes the print out of
 * «not checked yet». Refreshes what a defects write refreshes for the archive
 * lists, so the badge and the «Unchecked» filter follow at once.
 */
export function useMarkOutcomeChecked(archiveId: number, onDone?: () => void) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { t } = useTranslation();
  return useMutation({
    mutationFn: () => api.markArchiveOutcomeChecked(archiveId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['archive-detail', archiveId] });
      showToast(t('archives.outcome.marked'));
      onDone?.();
    },
    onError: (error: Error) => {
      showToast(error.message || t('archives.outcome.markFailed'), 'error');
    },
  });
}
