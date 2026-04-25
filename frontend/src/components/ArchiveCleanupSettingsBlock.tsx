import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { ConfirmModal } from './ConfirmModal';

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatRelativeFromNow(iso: string | null, t: (k: string) => string): string {
  if (!iso) return t('settings.archiveCleanup.never');
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return t('settings.archiveCleanup.never');
  const diffMin = Math.round((Date.now() - ts) / 60000);
  if (diffMin < 1) return t('settings.archiveCleanup.justNow');
  if (diffMin < 60) return `${diffMin} ${t('settings.archiveCleanup.minutesAgo')}`;
  const diffH = Math.round(diffMin / 60);
  if (diffH < 48) return `${diffH} ${t('settings.archiveCleanup.hoursAgo')}`;
  const diffD = Math.round(diffH / 24);
  return `${diffD} ${t('settings.archiveCleanup.daysAgo')}`;
}

function formatRelativeUntil(iso: string | null, t: (k: string) => string): string {
  if (!iso) return '—';
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return '—';
  const diffMin = Math.max(0, Math.round((ts - Date.now()) / 60000));
  if (diffMin < 60) return `${t('settings.archiveCleanup.in')} ${diffMin} ${t('settings.archiveCleanup.minutes')}`;
  const diffH = Math.round(diffMin / 60);
  return `${t('settings.archiveCleanup.in')} ${diffH} ${t('settings.archiveCleanup.hours')}`;
}

interface Props {
  enabled: boolean;
  days: number;
  onChangeEnabled: (value: boolean) => void;
  onChangeDays: (value: number) => void;
}

export function ArchiveCleanupSettingsBlock({ enabled, days, onChangeEnabled, onChangeDays }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const qc = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const status = useQuery({
    queryKey: ['archive-cleanup-status'],
    queryFn: api.getArchiveCleanupStatus,
    refetchInterval: 60_000,
  });

  const preview = useQuery({
    queryKey: ['archive-cleanup-preview', enabled, days],
    queryFn: api.getArchiveCleanupPreview,
    enabled,
    refetchInterval: 120_000,
  });

  const runMutation = useMutation({
    mutationFn: api.runArchiveCleanup,
    onSuccess: (data) => {
      showToast(
        t('settings.archiveCleanup.runDone', {
          archives: data.archives_cleared,
          bytes: formatBytes(data.bytes_freed),
        }) || `Cleared ${data.archives_cleared} archive(s), freed ${formatBytes(data.bytes_freed)}`,
        'success',
      );
      qc.invalidateQueries({ queryKey: ['archive-cleanup-status'] });
      qc.invalidateQueries({ queryKey: ['archive-cleanup-preview'] });
    },
    onError: (err: Error) => {
      showToast(err.message, 'error');
    },
  });

  return (
    <div className="border-t border-bambu-dark-tertiary pt-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <p className="text-white">{t('settings.archiveCleanup.title')}</p>
          <p className="text-sm text-bambu-gray">{t('settings.archiveCleanup.description')}</p>
        </div>
        <label className="relative inline-flex items-center cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => onChangeEnabled(e.target.checked)}
            className="sr-only peer"
          />
          <div className="w-11 h-6 bg-bambu-dark-tertiary peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-bambu-green"></div>
        </label>
      </div>

      {enabled && (
        <div className="space-y-3 pl-1">
          <div>
            <label className="block text-sm text-bambu-gray mb-1">
              {t('settings.archiveCleanup.daysLabel')}
            </label>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                max={3650}
                step={1}
                value={days}
                onChange={(e) => {
                  const v = parseInt(e.target.value, 10);
                  if (Number.isFinite(v) && v >= 1) onChangeDays(v);
                }}
                className="w-24 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              />
              <span className="text-bambu-gray">{t('settings.archiveCleanup.daysUnit')}</span>
            </div>
            <p className="text-xs text-bambu-gray mt-1">
              {t('settings.archiveCleanup.daysHint')}
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm">
            <div className="p-3 bg-bambu-dark rounded-lg">
              <div className="text-bambu-gray text-xs mb-1">
                {t('settings.archiveCleanup.lastRun')}
              </div>
              <div className="text-white">
                {formatRelativeFromNow(status.data?.last_run?.finished_at ?? null, t)}
              </div>
              {status.data?.last_run && (
                <div className="text-xs text-bambu-gray mt-1">
                  {t('settings.archiveCleanup.lastRunSummary', {
                    archives: status.data.last_run.archives_cleared,
                    bytes: formatBytes(status.data.last_run.bytes_freed),
                  }) || `cleared ${status.data.last_run.archives_cleared} archive(s), freed ${formatBytes(status.data.last_run.bytes_freed)}`}
                </div>
              )}
            </div>
            <div className="p-3 bg-bambu-dark rounded-lg">
              <div className="text-bambu-gray text-xs mb-1">
                {t('settings.archiveCleanup.nextRun')}
              </div>
              <div className="text-white">{formatRelativeUntil(status.data?.next_run_at ?? null, t)}</div>
              <div className="text-xs text-bambu-gray mt-1">
                {t('settings.archiveCleanup.nextRunHint')}
              </div>
            </div>
          </div>

          {preview.data && preview.data.enabled && (
            <div className="p-3 bg-bambu-dark/60 border border-bambu-dark-tertiary rounded-lg text-sm">
              <div className="text-bambu-gray text-xs mb-1">{t('settings.archiveCleanup.previewLabel')}</div>
              {preview.data.archives === 0 ? (
                <div className="text-white">{t('settings.archiveCleanup.previewEmpty')}</div>
              ) : (
                <div className="text-white">
                  {t('settings.archiveCleanup.previewBody', {
                    archives: preview.data.archives,
                    groups: preview.data.groups,
                    bytes: formatBytes(preview.data.bytes),
                  }) ||
                    `${preview.data.archives} archive(s) in ${preview.data.groups} group(s) — ${formatBytes(preview.data.bytes)} ready to free`}
                </div>
              )}
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setConfirmOpen(true)}
              disabled={runMutation.isPending}
              className="inline-flex items-center gap-2 px-3 py-2 bg-bambu-dark hover:bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded-lg text-sm text-white disabled:opacity-50"
            >
              {runMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )}
              {t('settings.archiveCleanup.runNow')}
            </button>
          </div>
        </div>
      )}

      {confirmOpen && (
        <ConfirmModal
          title={t('settings.archiveCleanup.confirmTitle')}
          message={
            preview.data && preview.data.archives > 0
              ? (t('settings.archiveCleanup.confirmBody', {
                  archives: preview.data.archives,
                  bytes: formatBytes(preview.data.bytes),
                }) || `Delete 3MF for ${preview.data.archives} archive(s) and free ${formatBytes(preview.data.bytes)}?`)
              : t('settings.archiveCleanup.confirmEmpty')
          }
          confirmText={t('settings.archiveCleanup.confirmAction')}
          onConfirm={() => {
            setConfirmOpen(false);
            runMutation.mutate();
          }}
          onCancel={() => setConfirmOpen(false)}
          variant="danger"
        />
      )}
    </div>
  );
}
