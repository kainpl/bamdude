import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Trash2, Clock, X } from 'lucide-react';
import { api } from '../api/client';
import type { SpoolUsageRecord } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime, type DateFormat, type TimeFormat } from '../utils/date';
import { invalidateSpoolViews } from '../utils/queryInvalidation';

interface SpoolUsageHistoryProps {
  spoolId: number;
  /** A Spoolman spool: its own rows, from its own feed. Clearing or deleting
   *  them is bookkeeping only — the spool's weight in Spoolman is not changed. */
  spoolmanMode?: boolean;
}

const STATUS_COLORS: Record<string, string> = {
  completed: 'text-bambu-green',
  failed: 'text-red-700 dark:text-red-400',
  aborted: 'text-yellow-700 dark:text-yellow-400',
  ams_sync: 'text-blue-700 dark:text-blue-400',
  runout: 'text-orange-700 dark:text-orange-400',
};

// Only `ams_sync` is translated. The print outcomes are the printer's own
// vocabulary and read the same in both locales; this one is a BamDude concept
// ("the AMS says filament is gone and no print of ours explains it") that means
// nothing as a raw enum value. Anything unmapped still falls back to the raw
// string, so a status added later shows up rather than disappearing.
const STATUS_LABEL_KEYS: Record<string, string> = {
  ams_sync: 'inventory.usageStatusAmsSync',
  runout: 'inventory.usageStatusRunout',
};

export function SpoolUsageHistory({ spoolId, spoolmanMode = false }: SpoolUsageHistoryProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // Pull system date/time format so usage timestamps follow the user's
  // preference instead of the previous hard-coded en-GB.
  const { data: settings } = useQuery({
    queryKey: ['ui-preferences'],
    queryFn: api.getUiPreferences,
    staleTime: 60_000,
  });
  const timeFormat = (settings?.time_format ?? 'system') as TimeFormat;
  const dateFormat = (settings?.date_format ?? 'system') as DateFormat;

  // The inventory is part of the key: the two number their spools
  // independently, and one's cached list must never be shown for the other.
  const usageKey = ['spool-usage', spoolmanMode ? 'spoolman' : 'built-in', spoolId];
  const { data: history, isLoading } = useQuery({
    queryKey: usageKey,
    queryFn: () => (spoolmanMode ? api.getSpoolmanSpoolUsageHistory(spoolId) : api.getSpoolUsageHistory(spoolId)),
  });

  const afterChange = (builtInToast: string, spoolmanToast: string) => {
    queryClient.invalidateQueries({ queryKey: usageKey });
    queryClient.invalidateQueries({ queryKey: ['usage-history'] });
    if (spoolmanMode) {
      // Spoolman owns the spool's weight; only the record is gone.
      showToast(spoolmanToast, 'success');
      return;
    }
    // The built-in inventory returns the rows' weight to the spool, so the
    // spool list moves too.
    invalidateSpoolViews(queryClient);
    queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
    showToast(builtInToast, 'success');
  };

  const clearMutation = useMutation({
    mutationFn: (): Promise<unknown> =>
      spoolmanMode ? api.clearSpoolmanSpoolUsageHistory(spoolId) : api.clearSpoolUsageHistory(spoolId),
    onSuccess: () => afterChange(t('inventory.historyCleared'), t('inventory.historyClearedSpoolman')),
  });

  const deleteRowMutation = useMutation({
    mutationFn: (usageId: number): Promise<unknown> =>
      spoolmanMode
        ? api.deleteSpoolmanSpoolUsageRecord(spoolId, usageId)
        : api.deleteSpoolUsageRecord(spoolId, usageId),
    onSuccess: () => afterChange(t('inventory.usageRecordDeleted'), t('inventory.usageRecordDeletedSpoolman')),
  });
  const deleteHint = spoolmanMode ? t('inventory.deleteUsageRecordSpoolman') : t('inventory.deleteUsageRecord');

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
        {spoolmanMode ? t('inventory.usageView.emptySpoolman') : t('inventory.noUsageHistory')}
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
          title={spoolmanMode ? t('inventory.clearHistorySpoolmanHint') : undefined}
          className="text-xs text-bambu-gray hover:text-red-700 dark:hover:text-red-400"
        >
          <Trash2 className="w-3 h-3 mr-1" />
          {t('inventory.clearHistory')}
        </Button>
      </div>
      {/* No height cap and no scroller of its own — this fills its own tab in
          the spool dialog, whose body already scrolls. A second scroll box
          inside it would strand the list in 48 units of the space it has. */}
      <div className="space-y-1">
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
              <span
                className={STATUS_COLORS[record.status] || 'text-bambu-gray'}
                title={record.status === 'ams_sync' ? t('inventory.usageStatusAmsSyncHint') : record.status === 'runout' ? t('inventory.usageStatusRunoutHint') : undefined}
              >
                {STATUS_LABEL_KEYS[record.status] ? t(STATUS_LABEL_KEYS[record.status]) : record.status}
              </span>
              <button
                type="button"
                onClick={() => deleteRowMutation.mutate(record.id)}
                disabled={deleteRowMutation.isPending}
                title={deleteHint}
                aria-label={deleteHint}
                className="text-bambu-gray/40 hover:text-red-600 dark:hover:text-red-400 can-hover:opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity disabled:opacity-30"
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
