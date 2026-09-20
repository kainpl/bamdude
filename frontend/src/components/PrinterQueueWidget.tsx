import { useEffect } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { Clock, Calendar, ChevronRight, Loader2, CircleCheck, RotateCcw } from 'lucide-react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { formatRelativeTime } from '../utils/date';
import { usePlateDefects } from '../hooks/usePlateDefects';
import { PlateDefectsRow } from './PlateDefectsRow';

interface PrinterQueueWidgetProps {
  printerId: number;
  printerModel?: string | null;
  printerState?: string | null;
  awaitingPlateClear?: boolean;
  // Whether Repeat has a finished row to re-arm — the backend's
  // `repeat_available`. Absent (older backend) means "don't hide it".
  repeatAvailable?: boolean;
  requirePlateClear?: boolean;
}

export function PrinterQueueWidget({ printerId, printerState, awaitingPlateClear, repeatAvailable, requirePlateClear = true }: PrinterQueueWidgetProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const { data: queue } = useQuery({
    queryKey: ['queue', printerId, 'pending'],
    queryFn: () => api.getQueue(printerId, 'pending'),
    refetchInterval: 30000,
  });

  const gateArmed = requirePlateClear && (printerState === 'FINISH' || printerState === 'FAILED') && !!awaitingPlateClear;
  // Split into auto-dispatchable vs staged (manual_start) items. Read up here
  // because the waiting-print query is gated on it: the counters live inside the
  // `needsClearPlate` block, which also needs a non-empty auto queue, so a gate
  // armed over an empty queue used to issue one GET per card that nobody read.
  const autoDispatchQueue = queue?.filter(item => !item.manual_start) ?? [];
  const totalPending = queue?.length || 0;
  // The print the gate is about, with its part rows — fetched only while the
  // block is on screen, and the counters live in the hook so both answers can
  // carry them (the printer card's yellow pair and the Queue page share it).
  const plateDefects = usePlateDefects(printerId, gateArmed && autoDispatchQueue.length > 0);

  // The other answer to a full plate — see services/plate_hold on the backend.
  const repeatPrintMutation = useMutation({
    mutationFn: () => api.repeatPrint(printerId, plateDefects.body()),
    onSuccess: (result) => {
      showToast(t('queue.repeatPrintSuccess'), 'success');
      plateDefects.afterAnswer(result.ledger_refused_parts);
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
      plateDefects.afterFailedAnswer();
    },
  });

  const clearPlateMutation = useMutation({
    mutationFn: () => api.clearPlate(printerId, plateDefects.body()),
    onSuccess: (result) => {
      showToast(t('queue.clearPlateSuccess'), 'success');
      plateDefects.afterAnswer(result.ledger_refused_parts);
    },
    onError: (err: Error) => {
      showToast(err.message, 'error');
      plateDefects.afterFailedAnswer();
    },
  });

  // Reset mutation state when printer starts a new print cycle so the button
  // is clickable again when the next print finishes (fixes upstream #912)
  useEffect(() => {
    if (printerState !== 'FINISH' && printerState !== 'FAILED') {
      clearPlateMutation.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [printerState]);

  if (totalPending === 0) {
    return null;
  }

  const nextAutoItem = autoDispatchQueue[0];
  const nextItem = queue?.[0];
  // Only prompt "Clear Plate & Start Next" when there are auto-dispatchable items
  const needsClearPlate = requirePlateClear && (printerState === 'FINISH' || printerState === 'FAILED') && !!awaitingPlateClear && autoDispatchQueue.length > 0;

  if (needsClearPlate) {
    const displayItem = nextAutoItem || nextItem;
    return (
      <div className="mb-3 p-3 bg-bambu-dark rounded-lg border border-yellow-400/30">
        <div className="flex items-center gap-3 mb-2">
          <Calendar className="w-5 h-5 text-yellow-600 dark:text-yellow-400 flex-shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="text-xs text-bambu-gray">{t('queue.nextInQueue')}</p>
            <p className="text-sm text-white truncate">
              {displayItem?.archive_name || displayItem?.library_file_name || `File #${displayItem?.archive_id || displayItem?.library_file_id}`}
            </p>
          </div>
          {totalPending > 1 && (
            <span className="text-xs px-1.5 py-0.5 bg-yellow-100 dark:bg-yellow-400/20 text-yellow-700 dark:text-yellow-400 rounded flex-shrink-0">
              +{totalPending - 1}
            </span>
          )}
        </div>
        <PlateDefectsRow defects={plateDefects} className="mb-2" />
        {clearPlateMutation.isSuccess ? (
          <div className="w-full py-2 px-3 rounded-lg bg-bambu-green/10 border border-bambu-green/20 text-bambu-green text-sm flex items-center justify-center gap-2">
            <CircleCheck className="w-4 h-4" />
            {t('queue.plateReady')}
          </div>
        ) : (
          <div className="flex gap-2">
            {repeatAvailable !== false && (
              <button
                onClick={() => repeatPrintMutation.mutate()}
                disabled={repeatPrintMutation.isPending || !hasPermission('printers:clear_plate')}
                className="flex-1 py-2 px-3 rounded-lg bg-bambu-green/20 border border-bambu-green/40 text-bambu-green hover:bg-bambu-green/30 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-50"
              >
                {repeatPrintMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RotateCcw className="w-4 h-4" />
                )}
                {t('queue.repeatPrint')}
              </button>
            )}
            <button
              onClick={() => clearPlateMutation.mutate()}
              disabled={clearPlateMutation.isPending || !hasPermission('printers:clear_plate')}
              className="flex-1 py-2 px-3 rounded-lg bg-bambu-green/20 border border-bambu-green/40 text-bambu-green hover:bg-bambu-green/30 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-50"
            >
              {clearPlateMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <CircleCheck className="w-4 h-4" />
              )}
              {t('queue.clearPlateShort')}
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <Link
      to="/queue"
      className="block mb-3 p-3 bg-bambu-dark rounded-lg hover:bg-bambu-dark-tertiary transition-colors"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <Calendar className="w-5 h-5 text-yellow-600 dark:text-yellow-400 flex-shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="text-xs text-bambu-gray">{t('queue.nextInQueue')}</p>
            <p className="text-sm text-white truncate">
              {nextItem?.archive_name || nextItem?.library_file_name || `File #${nextItem?.archive_id || nextItem?.library_file_id}`}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-xs text-bambu-gray flex items-center gap-1">
            <Clock className="w-3 h-3" />
            {nextItem?.scheduled_time ? formatRelativeTime(nextItem.scheduled_time, 'system', t) : t('time.waiting')}
          </span>
          {totalPending > 1 && (
            <span className="text-xs px-1.5 py-0.5 bg-yellow-100 dark:bg-yellow-400/20 text-yellow-700 dark:text-yellow-400 rounded">
              +{totalPending - 1}
            </span>
          )}
          <ChevronRight className="w-4 h-4 text-bambu-gray" />
        </div>
      </div>
    </Link>
  );
}
