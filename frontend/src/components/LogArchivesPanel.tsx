import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Download, Trash2, FileText, Loader2, RefreshCw } from 'lucide-react';
import { supportApi } from '../api/client';
import { useToast } from '../contexts/ToastContext';

/**
 * Lists rotated daily log archives (``bamdude-YYYY-MM-DD.log`` files
 * produced by ``TimedRotatingFileHandler``) with download + delete
 * actions. Live ``bamdude.log`` is shown by the existing ``<LogViewer>``
 * component above this panel — that's the streaming-tail UI; this is
 * the on-disk historical-archive manager.
 *
 * Operator-facing rationale: deleting via the UI saves a shell session
 * into the container; downloading streams the raw text file (no zip
 * wrap so ``less`` / ``grep`` work directly).
 */
export function LogArchivesPanel() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [downloadingFile, setDownloadingFile] = useState<string | null>(null);

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['log-archives'],
    queryFn: () => supportApi.listLogArchives(),
    staleTime: 30 * 1000,
  });

  const deleteMutation = useMutation({
    mutationFn: (filename: string) => supportApi.deleteLogArchive(filename),
    onSuccess: (_, filename) => {
      showToast(t('logArchives.deleted', { filename, defaultValue: `Deleted ${filename}` }), 'success');
      queryClient.invalidateQueries({ queryKey: ['log-archives'] });
    },
    onError: (err: Error) => showToast(err.message, 'error'),
    onSettled: () => setPendingDelete(null),
  });

  const handleDownload = async (filename: string) => {
    setDownloadingFile(filename);
    try {
      await supportApi.downloadLogArchive(filename);
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err), 'error');
    } finally {
      setDownloadingFile(null);
    }
  };

  const formatBytes = (b: number): string => {
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / 1024 / 1024).toFixed(1)} MB`;
  };

  const archives = data?.archives ?? [];

  return (
    <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <FileText className="w-5 h-5 text-bambu-green" />
          <h3 className="text-base font-semibold text-white">
            {t('logArchives.title', { defaultValue: 'Historical Logs' })}
          </h3>
          <span className="text-xs text-bambu-gray">
            {t('logArchives.subtitle', {
              defaultValue: 'Daily-rotated archives. Live bamdude.log is shown above.',
            })}
          </span>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          disabled={isFetching}
          className="inline-flex items-center gap-1.5 px-2 py-1 rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray text-xs transition-colors disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${isFetching ? 'animate-spin' : ''}`} />
          {t('common.refresh', { defaultValue: 'Refresh' })}
        </button>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="w-5 h-5 animate-spin text-bambu-green" />
        </div>
      ) : archives.length === 0 ? (
        <p className="text-sm text-bambu-gray py-4 text-center">
          {t('logArchives.empty', {
            defaultValue: 'No rotated log archives yet — daily rotation runs at midnight.',
          })}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-bambu-gray uppercase border-b border-bambu-dark-tertiary">
              <tr>
                <th className="text-left py-2 px-2 font-medium">
                  {t('logArchives.filename', { defaultValue: 'Filename' })}
                </th>
                <th className="text-right py-2 px-2 font-medium">
                  {t('logArchives.size', { defaultValue: 'Size' })}
                </th>
                <th className="text-left py-2 px-2 font-medium">
                  {t('logArchives.modified', { defaultValue: 'Modified' })}
                </th>
                <th className="text-right py-2 px-2 font-medium">
                  {t('common.actions', { defaultValue: 'Actions' })}
                </th>
              </tr>
            </thead>
            <tbody>
              {archives.map((a) => {
                const isDownloading = downloadingFile === a.filename;
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === a.filename;
                const confirming = pendingDelete === a.filename;
                return (
                  <tr
                    key={a.filename}
                    className="border-b border-bambu-dark-tertiary/50 last:border-b-0 hover:bg-bambu-dark/30"
                  >
                    <td className="py-2 px-2 font-mono text-xs text-white">{a.filename}</td>
                    <td className="py-2 px-2 text-right text-bambu-gray tabular-nums">
                      {formatBytes(a.size_bytes)}
                    </td>
                    <td className="py-2 px-2 text-bambu-gray text-xs">
                      {new Date(a.mtime).toLocaleString()}
                    </td>
                    <td className="py-2 px-2 text-right">
                      <div className="inline-flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => handleDownload(a.filename)}
                          disabled={isDownloading}
                          className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
                          title={t('common.download', { defaultValue: 'Download' })}
                        >
                          {isDownloading ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : (
                            <Download className="w-3.5 h-3.5" />
                          )}
                        </button>
                        {confirming ? (
                          <>
                            <button
                              type="button"
                              onClick={() => deleteMutation.mutate(a.filename)}
                              disabled={isDeleting}
                              className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-red-400 hover:text-red-300 hover:bg-red-500/10 transition-colors disabled:opacity-50"
                            >
                              {isDeleting ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              ) : (
                                t('common.confirm', { defaultValue: 'Confirm' })
                              )}
                            </button>
                            <button
                              type="button"
                              onClick={() => setPendingDelete(null)}
                              className="px-2 py-1 rounded text-xs text-bambu-gray hover:text-white"
                            >
                              {t('common.cancel', { defaultValue: 'Cancel' })}
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setPendingDelete(a.filename)}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-bambu-gray hover:text-red-400 hover:bg-red-500/10 transition-colors"
                            title={t('common.delete', { defaultValue: 'Delete' })}
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
