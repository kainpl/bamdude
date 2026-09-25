import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Clock, Pause, Pencil, Play, Repeat, X } from 'lucide-react';
import { api, type DryingSchedule, type ScheduledDryingRun } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { DRYING_SCHEDULES_KEY, SCHEDULED_DRYINGS_KEY, amsLabel, weekdaysLabel } from '../utils/scheduledDrying';
import { DryingScheduleModal } from './DryingScheduleModal';

function didNotHappen(run: ScheduledDryingRun) {
  return run.status === 'failed' || run.status === 'skipped';
}

/** What is planned for a printer's AMS units: waiting / running / missed runs and the recurring rules. */
export function ScheduledDryingStrip({ printerId }: { printerId: number }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<DryingSchedule | null>(null);
  const { data: runs = [] } = useQuery({
    queryKey: SCHEDULED_DRYINGS_KEY,
    queryFn: () => api.listScheduledDryings(),
    refetchInterval: 30_000,
  });
  const { data: rules } = useQuery({ queryKey: DRYING_SCHEDULES_KEY, queryFn: () => api.listDryingSchedules() });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: SCHEDULED_DRYINGS_KEY });
    queryClient.invalidateQueries({ queryKey: DRYING_SCHEDULES_KEY });
  };
  const onError = (error: Error) => showToast(error.message || t('printers.drying.scheduleFailed'), 'error');
  const cancel = useMutation({ mutationFn: (id: number) => api.cancelScheduledDrying(id), onSuccess: refresh, onError });
  const toggle = useMutation({
    mutationFn: (rule: DryingSchedule) => api.updateDryingSchedule(rule.id, { enabled: !rule.enabled }),
    onSuccess: refresh,
    onError,
  });
  const remove = useMutation({ mutationFn: (id: number) => api.deleteDryingSchedule(id), onSuccess: refresh, onError });

  const mine = runs.filter((r) => r.printer_id === printerId);
  const myRules = (rules?.schedules ?? []).filter((r) => r.printer_id === printerId);
  if (mine.length === 0 && myRules.length === 0) return null;

  const reasonText = (code: string | null) => t(`printers.drying.reason.${code ?? 'blocked_other'}`);
  const runText = (run: ScheduledDryingRun) => {
    if (didNotHappen(run)) return t('printers.drying.runDidNotHappen', { reason: reasonText(run.reason) });
    if (run.status === 'running') return t('printers.drying.runRunning');
    const when = run.start_after
      ? t('printers.drying.runAt', {
          time: new Date(run.start_after).toLocaleString([], {
            hour: '2-digit',
            minute: '2-digit',
            day: '2-digit',
            month: '2-digit',
          }),
        })
      : t('printers.drying.runWhenFree');
    return run.reason ? `${when} · ${t('printers.drying.waiting', { reason: reasonText(run.reason) })}` : when;
  };

  return (
    <div data-testid="scheduled-drying-strip" className="mt-2 space-y-1 text-[length:var(--pc-t10,10px)]">
      {mine.map((run) => {
        const label = didNotHappen(run) ? t('printers.drying.dismiss') : t('printers.drying.cancelScheduled');
        return (
          <div
            key={`run-${run.id}`}
            className={`flex items-center gap-1.5 rounded px-2 py-1 ${
              didNotHappen(run)
                ? 'bg-red-500/10 text-red-600 dark:text-red-400'
                : 'bg-amber-500/10 text-amber-700 dark:text-amber-400'
            }`}
          >
            <Clock className="w-3 h-3 shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              {amsLabel(run.ams_id)} · {runText(run)}
            </span>
            <button
              type="button"
              onClick={() => cancel.mutate(run.id)}
              disabled={cancel.isPending}
              aria-label={label}
              title={label}
              className="shrink-0 hover:opacity-70 disabled:opacity-50"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        );
      })}
      {myRules.map((rule) => {
        const toggleLabel = rule.enabled ? t('printers.drying.pauseSchedule') : t('printers.drying.resumeSchedule');
        return (
          <div
            key={`rule-${rule.id}`}
            className={`flex items-center gap-1.5 rounded px-2 py-1 bg-bambu-dark text-bambu-gray ${
              rule.enabled ? '' : 'opacity-60'
            }`}
          >
            <Repeat className="w-3 h-3 shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              {weekdaysLabel(rule.weekdays, t)} {rule.start_time}
              {rule.latest_start ? `–${rule.latest_start}` : ''} · {amsLabel(rule.ams_id)} · {rule.temp}°C ×{' '}
              {rule.duration_hours} {t('printers.drying.hours')}
            </span>
            <button
              type="button"
              onClick={() => setEditing(rule)}
              aria-label={t('printers.drying.editSchedule')}
              title={t('printers.drying.editSchedule')}
              className="shrink-0 hover:text-white"
            >
              <Pencil className="w-3 h-3" />
            </button>
            <button
              type="button"
              onClick={() => toggle.mutate(rule)}
              disabled={toggle.isPending}
              aria-label={toggleLabel}
              title={toggleLabel}
              className="shrink-0 hover:text-white disabled:opacity-50"
            >
              {rule.enabled ? <Pause className="w-3 h-3" /> : <Play className="w-3 h-3" />}
            </button>
            <button
              type="button"
              onClick={() => remove.mutate(rule.id)}
              disabled={remove.isPending}
              aria-label={t('printers.drying.deleteSchedule')}
              title={t('printers.drying.deleteSchedule')}
              className="shrink-0 hover:text-white disabled:opacity-50"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        );
      })}
      {editing && <DryingScheduleModal rule={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
