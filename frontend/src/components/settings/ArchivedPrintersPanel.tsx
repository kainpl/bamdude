/**
 * Settings → Printing → Archived printers.
 *
 * Lists printers that have been archived (soft-retired) and lets an admin
 * restore them (unarchive) or delete them permanently. Archived printers are
 * hidden everywhere else in the app; this is the one place they resurface.
 *
 * Lives inside an existing Card so it doesn't draw its own page chrome.
 */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RotateCcw, Trash2 } from 'lucide-react';
import { api, type Printer } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';

export function ArchivedPrintersPanel() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const qc = useQueryClient();

  const { data: all, isLoading } = useQuery({
    queryKey: ['printers', 'withArchived'],
    queryFn: api.getPrintersWithArchived,
  });
  const archived = useMemo(() => (all ?? []).filter((p: Printer) => p.archived), [all]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['printers'] });
  };

  const unarchive = useMutation({
    mutationFn: (id: number) => api.unarchivePrinter(id),
    onSuccess: () => {
      invalidate();
      showToast(t('printers.archive.toastRestored'), 'success');
    },
    onError: (e: Error) => showToast(e.message || t('printers.archive.toastFailed'), 'error'),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deletePrinter(id),
    onSuccess: () => {
      invalidate();
      showToast(t('printers.archive.toastDeleted'), 'success');
    },
    onError: (e: Error) => showToast(e.message || t('printers.archive.toastFailed'), 'error'),
  });

  if (isLoading) {
    return <p className="text-sm text-bambu-gray">{t('common.loading')}</p>;
  }
  if (archived.length === 0) {
    return <p className="text-sm text-bambu-gray italic">{t('printers.archive.empty')}</p>;
  }

  return (
    <div className="space-y-2">
      {archived.map((p) => (
        <div key={p.id} className="flex items-center justify-between p-3 bg-bambu-dark rounded-lg">
          <div className="min-w-0">
            <p className="text-sm text-white truncate">{p.name}</p>
            <p className="text-xs text-bambu-gray">
              {p.model || '—'}
              {p.archived_at ? ` · ${new Date(p.archived_at).toLocaleString()}` : ''}
            </p>
          </div>
          <div className="flex gap-2 flex-shrink-0">
            <button
              type="button"
              disabled={unarchive.isPending}
              onClick={() => unarchive.mutate(p.id)}
              className="px-2 py-1 text-xs rounded bg-bambu-dark-tertiary text-white hover:bg-bambu-green disabled:opacity-50 flex items-center gap-1"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              {t('printers.archive.unarchive')}
            </button>
            <button
              type="button"
              disabled={remove.isPending}
              onClick={() => {
                if (window.confirm(t('printers.archive.confirmDelete', { name: p.name }))) {
                  remove.mutate(p.id);
                }
              }}
              className="px-2 py-1 text-xs rounded text-bambu-gray hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50 flex items-center gap-1"
            >
              <Trash2 className="w-3.5 h-3.5" />
              {t('printers.archive.deleteForever')}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
