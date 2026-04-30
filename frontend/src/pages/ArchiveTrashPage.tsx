import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, RotateCcw, Save, Trash2, Loader2 } from 'lucide-react';

import { api } from '../api/client';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { formatFileSize } from '../utils/file';
import { parseUTCDate } from '../utils/date';

function formatRelativeDays(iso: string, t: (key: string, opts?: Record<string, unknown>) => string): string {
  const target = parseUTCDate(iso);
  if (!target) return '';
  const days = Math.ceil((target.getTime() - Date.now()) / (1000 * 60 * 60 * 24));
  if (days <= 0) return t('libraryTrash.anyMoment', { defaultValue: 'any moment' });
  if (days === 1) return t('libraryTrash.oneDay', { defaultValue: '1 day' });
  return t('libraryTrash.nDays', { count: days, defaultValue: `${days} days` });
}

function formatDeletedAt(iso: string): string {
  const date = parseUTCDate(iso);
  return date ? date.toLocaleString() : iso;
}

type PendingAction =
  | { type: 'delete'; id: number; filename: string }
  | { type: 'empty' }
  | { type: 'bulkDelete'; count: number }
  | null;

export function ArchiveTrashPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const [pending, setPending] = useState<PendingAction>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const isAdmin = hasPermission('archives:purge');

  const trashQuery = useQuery({
    queryKey: ['archive-trash'],
    queryFn: () => api.listArchiveTrash(200, 0),
  });

  const settingsQuery = useQuery({
    queryKey: ['archive-trash-settings'],
    queryFn: () => api.getArchiveTrashSettings(),
    enabled: isAdmin,
  });

  const [retentionDraft, setRetentionDraft] = useState<number | null>(null);
  useEffect(() => {
    if (settingsQuery.data && retentionDraft === null) {
      setRetentionDraft(settingsQuery.data.retention_days);
    }
  }, [settingsQuery.data, retentionDraft]);

  const updateRetentionMutation = useMutation({
    mutationFn: (days: number) => api.updateArchiveTrashSettings({ retention_days: days }),
    onSuccess: (res) => {
      showToast(t('archiveTrash.toast.retentionSaved', { days: res.retention_days }), 'success');
      queryClient.invalidateQueries({ queryKey: ['archive-trash-settings'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
    },
    onError: (e: Error) => showToast(e.message || t('archiveTrash.toast.retentionFailed'), 'error'),
  });

  const restoreMutation = useMutation({
    mutationFn: (id: number) => api.restoreArchiveTrash(id),
    onSuccess: () => {
      showToast(t('archiveTrash.toast.restored'), 'success');
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
      queryClient.invalidateQueries({ queryKey: ['archives'] });
    },
    onError: (e: Error) => showToast(e.message || t('archiveTrash.toast.restoreFailed'), 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.hardDeleteArchiveTrash(id),
    onSuccess: () => {
      showToast(t('archiveTrash.toast.purged'), 'success');
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
    },
    onError: (e: Error) => showToast(e.message || t('archiveTrash.toast.purgeFailed'), 'error'),
  });

  const emptyMutation = useMutation({
    mutationFn: () => api.emptyArchiveTrash(),
    onSuccess: (result) => {
      showToast(t('archiveTrash.toast.emptied', { count: result.deleted }), 'success');
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
    },
    onError: (e: Error) => showToast(e.message || t('archiveTrash.toast.emptyFailed'), 'error'),
  });

  const bulkRestoreMutation = useMutation({
    mutationFn: (ids: number[]) => Promise.all(ids.map((id) => api.restoreArchiveTrash(id))),
    onSuccess: (_, ids) => {
      showToast(t('archiveTrash.toast.bulkRestored', { count: ids.length }), 'success');
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
      queryClient.invalidateQueries({ queryKey: ['archives'] });
    },
    onError: (e: Error) => showToast(e.message || t('archiveTrash.toast.restoreFailed'), 'error'),
  });

  const bulkDeleteMutation = useMutation({
    mutationFn: (ids: number[]) => Promise.all(ids.map((id) => api.hardDeleteArchiveTrash(id))),
    onSuccess: (_, ids) => {
      showToast(t('archiveTrash.toast.bulkPurged', { count: ids.length }), 'success');
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ['archive-trash'] });
      queryClient.invalidateQueries({ queryKey: ['archive-trash-count'] });
    },
    onError: (e: Error) => showToast(e.message || t('archiveTrash.toast.purgeFailed'), 'error'),
  });

  const items = useMemo(() => trashQuery.data?.items ?? [], [trashQuery.data?.items]);
  const retentionDays = trashQuery.data?.retention_days ?? 30;
  const totalBytes = useMemo(
    () => items.reduce((sum, i) => sum + (i.file_size ?? 0), 0),
    [items],
  );
  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id));
  const someSelected = selected.size > 0 && !allSelected;

  const toggleOne = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected((prev) => (prev.size === items.length ? new Set() : new Set(items.map((i) => i.id))));
  };

  const handleConfirm = () => {
    if (!pending) return;
    if (pending.type === 'delete') {
      deleteMutation.mutate(pending.id);
    } else if (pending.type === 'bulkDelete') {
      bulkDeleteMutation.mutate(Array.from(selected));
    } else {
      emptyMutation.mutate();
    }
    setPending(null);
  };

  return (
    <div className="p-6 max-w-screen-2xl mx-auto">
      <div className="flex items-center gap-3 mb-4">
        <Link
          to="/archives"
          className="inline-flex items-center gap-1 text-sm text-bambu-gray hover:text-white"
        >
          <ArrowLeft className="w-4 h-4" /> {t('archiveTrash.backToArchives')}
        </Link>
      </div>

      <div className="flex items-start justify-between mb-6 gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-white">{t('archiveTrash.title')}</h1>
          <p className="text-sm text-bambu-gray mt-1">
            {isAdmin
              ? t('archiveTrash.subtitleAdmin', { days: retentionDays })
              : t('archiveTrash.subtitleUser', { days: retentionDays })}
          </p>
        </div>
        {items.length > 0 && (
          <Button
            variant="secondary"
            onClick={() => setPending({ type: 'empty' })}
            className="text-red-400"
          >
            <Trash2 className="w-4 h-4 mr-1" />
            {t('archiveTrash.emptyTrash')}
          </Button>
        )}
      </div>

      {isAdmin && settingsQuery.data && (
        <div className="mb-4 border border-bambu-dark-tertiary rounded-lg p-3 flex items-center gap-3 bg-bambu-dark-secondary/40">
          <label htmlFor="archive-retention-days" className="text-sm font-medium text-white">
            {t('archiveTrash.retentionLabel')}
          </label>
          <input
            id="archive-retention-days"
            type="number"
            min={1}
            max={365}
            value={retentionDraft ?? settingsQuery.data.retention_days}
            onChange={(e) =>
              setRetentionDraft(Math.max(1, Math.min(365, parseInt(e.target.value || '0', 10) || 0)))
            }
            className="w-20 rounded border border-bambu-dark-tertiary bg-bambu-dark text-sm px-2 py-1 text-white"
          />
          <span className="text-sm text-bambu-gray">{t('archiveTrash.days')}</span>
          <Button
            variant="secondary"
            onClick={() => retentionDraft != null && updateRetentionMutation.mutate(retentionDraft)}
            disabled={
              updateRetentionMutation.isPending ||
              retentionDraft == null ||
              retentionDraft === settingsQuery.data.retention_days
            }
            className="ml-auto"
          >
            <Save className="w-4 h-4 mr-1" />
            {t('common.save')}
          </Button>
        </div>
      )}

      {trashQuery.isLoading ? (
        <div className="flex items-center gap-2 text-bambu-gray">
          <Loader2 className="w-4 h-4 animate-spin" /> {t('archiveTrash.loading')}
        </div>
      ) : items.length === 0 ? (
        <div className="border border-dashed border-bambu-dark-tertiary rounded-lg p-12 text-center">
          <p className="text-bambu-gray">{t('archiveTrash.empty')}</p>
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs text-bambu-gray">
              {t('archiveTrash.summary', { count: items.length, size: formatFileSize(totalBytes) })}
            </div>
            {selected.size > 0 && (
              <div className="flex items-center gap-2 text-sm">
                <span className="text-bambu-gray">
                  {t('archiveTrash.selectionCount', { count: selected.size })}
                </span>
                <Button
                  variant="secondary"
                  onClick={() => bulkRestoreMutation.mutate(Array.from(selected))}
                  disabled={bulkRestoreMutation.isPending}
                >
                  <RotateCcw className="w-4 h-4 mr-1" />
                  {t('archiveTrash.bulkRestore')}
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => setPending({ type: 'bulkDelete', count: selected.size })}
                  disabled={bulkDeleteMutation.isPending}
                  className="text-red-400"
                >
                  <Trash2 className="w-4 h-4 mr-1" />
                  {t('archiveTrash.bulkPurge')}
                </Button>
              </div>
            )}
          </div>
          <div className="border border-bambu-dark-tertiary rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-bambu-dark-secondary text-left text-bambu-gray">
                <tr>
                  <th className="px-3 py-2 w-10">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      ref={(el) => {
                        if (el) el.indeterminate = someSelected;
                      }}
                      onChange={toggleAll}
                      aria-label={t('archiveTrash.selectAll')}
                      className="rounded border-bambu-dark-tertiary cursor-pointer"
                    />
                  </th>
                  <th className="px-3 py-2 font-medium">{t('archiveTrash.col.filename')}</th>
                  <th className="px-3 py-2 font-medium">{t('archiveTrash.col.printName')}</th>
                  <th className="px-3 py-2 font-medium text-right">{t('archiveTrash.col.size')}</th>
                  <th className="px-3 py-2 font-medium whitespace-nowrap">{t('archiveTrash.col.deleted')}</th>
                  <th className="px-3 py-2 font-medium whitespace-nowrap">{t('archiveTrash.col.autoPurge')}</th>
                  {isAdmin && <th className="px-3 py-2 font-medium">{t('archiveTrash.col.owner')}</th>}
                  <th className="px-3 py-2 font-medium text-right">{t('archiveTrash.col.actions')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bambu-dark-tertiary">
                {items.map((item) => (
                  <tr key={item.id} className="hover:bg-bambu-dark-secondary/50">
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        checked={selected.has(item.id)}
                        onChange={() => toggleOne(item.id)}
                        aria-label={t('archiveTrash.selectOne', { filename: item.filename })}
                        className="rounded border-bambu-dark-tertiary cursor-pointer"
                      />
                    </td>
                    <td
                      className="px-3 py-2 text-white truncate max-w-md"
                      title={item.filename}
                    >
                      {item.filename}
                    </td>
                    <td className="px-3 py-2 text-bambu-gray truncate max-w-md" title={item.print_name ?? ''}>
                      {item.print_name ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-right text-bambu-gray tabular-nums whitespace-nowrap">
                      {formatFileSize(item.file_size ?? 0)}
                    </td>
                    <td className="px-3 py-2 text-bambu-gray whitespace-nowrap">
                      {formatDeletedAt(item.deleted_at)}
                    </td>
                    <td className="px-3 py-2 text-bambu-gray whitespace-nowrap">
                      <span title={formatDeletedAt(item.auto_purge_at)}>
                        {t('archiveTrash.autoPurgeIn', { when: formatRelativeDays(item.auto_purge_at, t) })}
                      </span>
                    </td>
                    {isAdmin && (
                      <td className="px-3 py-2 text-bambu-gray">{item.created_by_username ?? '—'}</td>
                    )}
                    <td className="px-3 py-2 text-right whitespace-nowrap">
                      <button
                        onClick={() => restoreMutation.mutate(item.id)}
                        disabled={restoreMutation.isPending}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs text-blue-400 hover:text-blue-300"
                      >
                        <RotateCcw className="w-3.5 h-3.5" />
                        {t('archiveTrash.restore')}
                      </button>
                      <button
                        onClick={() => setPending({ type: 'delete', id: item.id, filename: item.filename })}
                        disabled={deleteMutation.isPending}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs text-red-400 hover:text-red-300 ml-2"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                        {t('archiveTrash.purgeNow')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {pending && (
        <ConfirmModal
          onCancel={() => setPending(null)}
          onConfirm={handleConfirm}
          title={
            pending.type === 'delete'
              ? t('archiveTrash.confirm.purgeTitle')
              : pending.type === 'bulkDelete'
                ? t('archiveTrash.confirm.bulkPurgeTitle')
                : t('archiveTrash.confirm.emptyTitle')
          }
          message={
            pending.type === 'delete'
              ? t('archiveTrash.confirm.purgeBody', { filename: pending.filename })
              : pending.type === 'bulkDelete'
                ? t('archiveTrash.confirm.bulkPurgeBody', { count: pending.count })
                : t('archiveTrash.confirm.emptyBody', { count: items.length })
          }
          confirmText={t('archiveTrash.confirm.cta')}
          variant="danger"
        />
      )}

      {trashQuery.isError && (
        <div className="mt-4 text-sm text-red-400">
          {(trashQuery.error as Error | null)?.message ?? t('archiveTrash.loadError')}
          <Button variant="secondary" onClick={() => navigate('/archives')} className="ml-3">
            {t('archiveTrash.backToArchives')}
          </Button>
        </div>
      )}
    </div>
  );
}
