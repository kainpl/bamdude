import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Loader2, Trash2, X } from 'lucide-react';

import { api } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { formatFileSize } from '../utils/file';

interface PurgeArchivesModalProps {
  onClose: () => void;
}

export function PurgeArchivesModal({ onClose }: PurgeArchivesModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // Pull the configured threshold to seed the input. The status query is
  // also used to surface the auto-mode hint when ``enabled=false``.
  const statusQuery = useQuery({
    queryKey: ['archive-cleanup-status'],
    queryFn: api.getArchiveCleanupStatus,
  });

  const settingsDays = statusQuery.data?.days;
  const [days, setDays] = useState<number | null>(null);

  // Lazy-init from settings as soon as we have it.
  useEffect(() => {
    if (days === null && typeof settingsDays === 'number') {
      setDays(settingsDays);
    }
  }, [days, settingsDays]);

  // Debounce the days input so dragging the spinner isn't a DoS on the preview.
  const [debouncedDays, setDebouncedDays] = useState<number | null>(null);
  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedDays(days), 300);
    return () => window.clearTimeout(handle);
  }, [days]);

  const previewQuery = useQuery({
    queryKey: ['archive-cleanup-preview', debouncedDays],
    queryFn: () => api.getArchiveCleanupPreview(debouncedDays ?? undefined),
    enabled: debouncedDays !== null && debouncedDays >= 1,
  });

  const runMutation = useMutation({
    mutationFn: () => api.runArchiveCleanup(days ?? undefined),
    onSuccess: (res) => {
      showToast(
        t('archivePurge.toast.success', {
          count: res.archives_cleared,
          size: formatFileSize(res.bytes_freed),
        }),
        'success',
      );
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['archive-stats'] });
      queryClient.invalidateQueries({ queryKey: ['archive-cleanup-status'] });
      queryClient.invalidateQueries({ queryKey: ['archive-cleanup-preview'] });
      onClose();
    },
    onError: (e: Error) => showToast(e.message || t('archivePurge.toast.failed'), 'error'),
  });

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !runMutation.isPending) onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose, runMutation.isPending]);

  const status = statusQuery.data;
  const preview = previewQuery.data;
  const autoEnabled = status?.enabled ?? preview?.enabled ?? false;
  const archives = preview?.archives ?? 0;
  const groups = preview?.groups ?? 0;
  const bytes = preview?.bytes ?? 0;
  const canConfirm = days !== null && days >= 1 && archives > 0 && !runMutation.isPending;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg shadow-xl max-w-lg w-full border border-bambu-dark-tertiary">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <Trash2 className="w-5 h-5" />
            {t('archivePurge.title')}
          </h2>
          <button
            onClick={onClose}
            className="text-bambu-gray hover:text-white"
            aria-label={t('common.close')}
            disabled={runMutation.isPending}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-bambu-gray">{t('archivePurge.description')}</p>

          <div>
            <label htmlFor="archive-cleanup-days" className="block text-sm font-medium text-white mb-1">
              {t('archivePurge.ageLabel')}
            </label>
            <div className="flex items-center gap-3">
              <input
                id="archive-cleanup-days"
                type="number"
                min={1}
                max={3650}
                value={days ?? ''}
                onChange={(e) => {
                  const v = parseInt(e.target.value || '0', 10);
                  setDays(Number.isFinite(v) && v >= 1 ? Math.min(3650, v) : null);
                }}
                className="w-24 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-sm text-white focus:border-bambu-green focus:outline-none"
              />
              <span className="text-sm text-bambu-gray">{t('archivePurge.days')}</span>
              {typeof settingsDays === 'number' && days !== settingsDays && (
                <button
                  type="button"
                  onClick={() => setDays(settingsDays)}
                  className="text-xs text-bambu-gray hover:text-white underline"
                >
                  {t('archivePurge.resetToSettings', { days: settingsDays })}
                </button>
              )}
            </div>
            <p className="text-xs text-bambu-gray mt-1">
              {t('archivePurge.ageHint')}
            </p>
            {!autoEnabled && (
              <p className="text-xs text-amber-300 mt-1">{t('archivePurge.autoDisabledHint')}</p>
            )}
          </div>

          <div className="rounded border border-bambu-dark-tertiary bg-bambu-dark/40 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-bambu-gray mb-2">
              {t('archivePurge.effectsTitle')}
            </div>
            <ul className="text-xs text-bambu-gray space-y-1 list-disc pl-4">
              <li>{t('archivePurge.effect1')}</li>
              <li>{t('archivePurge.effect2')}</li>
              <li>{t('archivePurge.effect3')}</li>
              <li>{t('archivePurge.effect4')}</li>
            </ul>
          </div>

          <div className="rounded border border-bambu-dark-tertiary bg-bambu-dark/60 p-3">
            {previewQuery.isLoading || previewQuery.isFetching ? (
              <div className="flex items-center gap-2 text-sm text-bambu-gray">
                <Loader2 className="w-4 h-4 animate-spin" /> {t('archivePurge.previewLoading')}
              </div>
            ) : previewQuery.isError ? (
              <div className="text-sm text-red-400">
                {(previewQuery.error as Error | null)?.message ?? t('archivePurge.previewFailed')}
              </div>
            ) : (
              <div className="text-sm text-white">
                <div className="font-medium">
                  {t('archivePurge.previewSummary', {
                    archives,
                    groups,
                    size: formatFileSize(bytes),
                  })}
                </div>
                {archives === 0 && (
                  <div className="text-xs text-bambu-gray mt-1">{t('archivePurge.previewEmpty')}</div>
                )}
              </div>
            )}
          </div>

          <div className="flex gap-2 items-start text-xs text-amber-200 bg-amber-900/20 border border-amber-700/40 rounded px-3 py-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{t('archivePurge.warning')}</span>
          </div>
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
          <Button variant="secondary" onClick={onClose} disabled={runMutation.isPending}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={!canConfirm}
            onClick={() => runMutation.mutate()}
            className="bg-red-500 hover:bg-red-600"
          >
            {runMutation.isPending ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin mr-1" />
                {t('archivePurge.purging')}
              </>
            ) : (
              t('archivePurge.confirmCta', { count: archives })
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
