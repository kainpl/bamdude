import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Trash2, Clock, X } from 'lucide-react';
import { api } from '../api/client';
import type { SpoolUsageRecord } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime, type DateFormat, type TimeFormat } from '../utils/date';

interface SpoolUsageHistoryProps {
  spoolId: number;
}

const STATUS_COLORS: Record<string, string> = {
  completed: 'text-bambu-green',
  failed: 'text-red-400',
  aborted: 'text-yellow-400',
};

export function SpoolUsageHistory({ spoolId }: SpoolUsageHistoryProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // Pull system date/time format so usage timestamps follow the user's
  // preference instead of the previous hard-coded en-GB.
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 60_000,
  });
  const timeFormat = (settings?.time_format ?? 'system') as TimeFormat;
  const dateFormat = (settings?.date_format ?? 'system') as DateFormat;

  const { data: history, isLoading } = useQuery({
    queryKey: ['spool-usage', spoolId],
    queryFn: () => api.getSpoolUsageHistory(spoolId),
  });

  const clearMutation = useMutation({
    mutationFn: () => api.clearSpoolUsageHistory(spoolId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spool-usage', spoolId] });
      // Clear-all returns each row's weight to the spool, so refresh the list too.
      queryClient.invalidateQueries({ queryKey: ['spools'] });
      showToast(t('inventory.historyCleared'), 'success');
    },
  });

  // Per-row delete returns the row's weight to the spool (counts as unused
  // again), so refresh the spool list too — not just the usage list.
  const deleteRowMutation = useMutation({
    mutationFn: (usageId: number) => api.deleteSpoolUsageRecord(spoolId, usageId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spool-usage', spoolId] });
      queryClient.invalidateQueries({ queryKey: ['spools'] });
      showToast(t('inventory.usageRecordDeleted'), 'success');
    },
  });

  if (isLoading) {
    return (
      <div className="flex justify-center py-4">
        <Loader2 className="w-5 h-5 animate-spin text-bambu-green" />
      </div>
    );
  }

  if (!history || history.length === 0) {
    return (
      <div className="text-center py-4 text-bambu-gray text-sm">
        <Clock className="w-5 h-5 mx-auto mb-2 opacity-50" />
        {t('inventory.noUsageHistory')}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-white">{t('inventory.usageHistory')}</h4>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => clearMutation.mutate()}
          disabled={clearMutation.isPending}
          className="text-xs text-bambu-gray hover:text-red-400"
        >
          <Trash2 className="w-3 h-3 mr-1" />
          {t('inventory.clearHistory')}
        </Button>
      </div>
      <div className="max-h-48 overflow-y-auto space-y-1">
        {history.map((record: SpoolUsageRecord) => (
          <div
            key={record.id}
            className="group flex items-center justify-between p-2 rounded bg-bambu-dark/50 text-xs"
          >
            <div className="flex-1 min-w-0">
              <span className="text-bambu-gray">{formatDateTime(record.created_at, timeFormat, dateFormat)}</span>
              {record.print_name && (
                <span className="text-white ml-2 truncate" title={record.print_name}>
                  {record.print_name}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 flex-shrink-0 ml-2">
              <span className="text-white font-medium">{record.weight_used.toFixed(1)}g</span>
              <span className="text-bambu-gray">({record.percent_used}%)</span>
              <span className={STATUS_COLORS[record.status] || 'text-bambu-gray'}>
                {record.status}
              </span>
              <button
                type="button"
                onClick={() => deleteRowMutation.mutate(record.id)}
                disabled={deleteRowMutation.isPending}
                title={t('inventory.deleteUsageRecord')}
                aria-label={t('inventory.deleteUsageRecord')}
                className="text-bambu-gray/40 hover:text-red-400 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity disabled:opacity-30"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
