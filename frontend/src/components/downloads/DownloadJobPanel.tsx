import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2, Download, Loader2, XCircle } from 'lucide-react';
import type { DownloadJob, DownloadJobFailure } from '../../api/client';
import { formatFileSize } from '../../utils/file';
import { Button } from '../Button';

interface DownloadJobPanelProps {
  job: DownloadJob;
  /** The server no longer knows the job. */
  lost: boolean;
  saving: boolean;
  saveError: string | null;
  onCancel: () => void;
  onSave: (acceptPartial: boolean) => void;
  /** Drop a finished job from the dialog (and, for a ready one, from the server). */
  onDismiss: () => void;
}

const KNOWN_FILE_STATUSES = [
  'not_readable',
  'unavailable',
  'stalled',
  'short_read',
  'protocol_failed',
  'not_attempted',
  'busy',
  'cancelled',
];

function FailureList({ failures }: { failures: DownloadJobFailure[] }) {
  const { t } = useTranslation();
  if (failures.length === 0) return null;
  return (
    <ul className="mt-2 max-h-28 overflow-y-auto text-xs text-bambu-gray space-y-0.5" data-testid="download-failures">
      {failures.map((failure) => (
        <li key={failure.name} className="flex justify-between gap-3">
          <span className="truncate text-white">{failure.name}</span>
          <span className="flex-shrink-0">
            {t(
              KNOWN_FILE_STATUSES.includes(failure.status)
                ? `downloads.fileStatus.${failure.status}`
                : 'downloads.fileStatus.other',
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * What one download job is doing, in words, with the one action that fits its
 * phase. The phase is the server's: "cancelling" stays until the server says
 * the transfer has stopped.
 */
export function DownloadJobPanel({ job, lost, saving, saveError, onCancel, onSave, onDismiss }: DownloadJobPanelProps) {
  const { t } = useTranslation();
  const phase = lost ? 'expired' : job.phase;
  const done = job.succeeded + job.failed;
  const share =
    job.bytes_expected && job.bytes_expected > 0
      ? Math.min(100, Math.round((job.bytes_received / job.bytes_expected) * 100))
      : null;

  return (
    <div
      className="mx-4 mb-3 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3 text-sm"
      role="status"
      aria-live="polite"
      data-testid="download-job"
      data-phase={phase}
    >
      {(phase === 'queued' || phase === 'preparing' || phase === 'cancelling') && (
        <div>
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 min-w-0 text-white">
              <Loader2 className="w-4 h-4 animate-spin flex-shrink-0" />
              <span className="truncate">
                {phase === 'queued' && t('downloads.queued')}
                {phase === 'cancelling' && t('downloads.cancelling')}
                {phase === 'preparing' &&
                  (job.requested > 1
                    ? t('downloads.preparingMany', { done: Math.min(done + 1, job.requested), total: job.requested })
                    : t('downloads.preparing'))}
              </span>
            </div>
            <Button variant="secondary" size="sm" onClick={onCancel} disabled={phase === 'cancelling'}>
              {t('downloads.cancel')}
            </Button>
          </div>
          {phase === 'preparing' && (
            <>
              <div className="mt-2 h-1.5 rounded bg-bambu-dark-tertiary overflow-hidden">
                <div
                  className={`h-full bg-bambu-green ${share === null ? 'w-1/3 animate-pulse' : ''}`}
                  style={share === null ? undefined : { width: `${share}%` }}
                />
              </div>
              <div className="mt-1 flex justify-between gap-3 text-xs text-bambu-gray">
                <span className="truncate">{job.current ?? ''}</span>
                <span className="flex-shrink-0">
                  {formatFileSize(job.bytes_received)}
                  {job.bytes_expected ? ` / ${formatFileSize(job.bytes_expected)}` : ''}
                </span>
              </div>
            </>
          )}
        </div>
      )}

      {phase === 'ready' && (
        <div>
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 min-w-0">
              {job.partial ? (
                <AlertTriangle className="w-4 h-4 flex-shrink-0 text-yellow-600 dark:text-yellow-400" />
              ) : (
                <CheckCircle2 className="w-4 h-4 flex-shrink-0 text-bambu-green" />
              )}
              <span className="truncate text-white">
                {job.partial
                  ? t('downloads.partial', { failed: job.requested - job.succeeded, total: job.requested })
                  : t('downloads.ready', { name: job.filename })}
                {job.size != null ? ` · ${formatFileSize(job.size)}` : ''}
              </span>
            </div>
            <div className="flex gap-2 flex-shrink-0">
              <Button variant="secondary" size="sm" onClick={onDismiss}>
                {t('downloads.discard')}
              </Button>
              <Button size="sm" onClick={() => onSave(job.partial)} disabled={saving}>
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                {job.partial ? t('downloads.savePartial') : t('downloads.save')}
              </Button>
            </div>
          </div>
          {job.partial && <FailureList failures={job.failures} />}
          {saveError && <div className="mt-2 text-xs text-red-700 dark:text-red-400">{saveError}</div>}
        </div>
      )}

      {(phase === 'failed' || phase === 'cancelled' || phase === 'expired') && (
        <div>
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 min-w-0 text-white">
              <XCircle
                className={`w-4 h-4 flex-shrink-0 ${
                  phase === 'failed' ? 'text-red-600 dark:text-red-400' : 'text-bambu-gray'
                }`}
              />
              <span>
                {phase === 'cancelled' && t('downloads.cancelled')}
                {phase === 'expired' && t('downloads.expired')}
                {phase === 'failed' && t(`downloads.errors.${job.error ?? 'internal'}`)}
              </span>
            </div>
            <Button variant="secondary" size="sm" onClick={onDismiss}>
              {t('downloads.dismiss')}
            </Button>
          </div>
          {phase === 'failed' && <FailureList failures={job.failures} />}
        </div>
      )}
    </div>
  );
}
