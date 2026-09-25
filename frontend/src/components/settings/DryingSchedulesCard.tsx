import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Pause, Pencil, Play, Trash2 } from 'lucide-react';
import { api, type DryingSchedule } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { DRYING_SCHEDULES_KEY, SCHEDULED_DRYINGS_KEY, amsLabel, weekdaysLabel } from '../../utils/scheduledDrying';
import { Card, CardContent, CardHeader } from '../Card';
import { ConfirmModal } from '../ConfirmModal';
import { DryingScheduleModal } from '../DryingScheduleModal';

/** Every drying rule of the farm in one table (Settings → Filament). Rules are created from a printer card. */
export function DryingSchedulesCard() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const canControl = hasPermission('printers:control');
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<DryingSchedule | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DryingSchedule | null>(null);
  const { data: printers = [] } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  const { data } = useQuery({ queryKey: DRYING_SCHEDULES_KEY, queryFn: () => api.listDryingSchedules() });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: DRYING_SCHEDULES_KEY });
    queryClient.invalidateQueries({ queryKey: SCHEDULED_DRYINGS_KEY });
  };
  const onError = (error: Error) => showToast(error.message || t('printers.drying.scheduleFailed'), 'error');
  const toggle = useMutation({
    mutationFn: (rule: DryingSchedule) => api.updateDryingSchedule(rule.id, { enabled: !rule.enabled }),
    onSuccess: refresh,
    onError,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteDryingSchedule(id),
    onSuccess: () => {
      setConfirmDelete(null);
      refresh();
    },
    onError,
  });

  const printerName = (id: number) => printers.find((p) => p.id === id)?.name ?? `#${id}`;
  const rules = data?.schedules ?? [];

  return (
    <Card id="card-drying-schedules">
      <CardHeader>
        <h2 className="text-lg font-semibold text-white">{t('settings.dryingSchedules.title')}</h2>
      </CardHeader>
      <CardContent className="space-y-3">
        {rules.length === 0 ? (
          <p className="text-sm text-bambu-gray">{t('settings.dryingSchedules.empty')}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-bambu-gray border-b border-bambu-dark-tertiary text-left">
                  <th className="py-1.5 pr-2 font-medium">{t('settings.dryingSchedules.columns.printer')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('settings.dryingSchedules.columns.when')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('settings.dryingSchedules.columns.parameters')}</th>
                  <th className="py-1.5" />
                </tr>
              </thead>
              <tbody>
                {rules.map((rule) => {
                  const toggleLabel = rule.enabled
                    ? t('printers.drying.pauseSchedule')
                    : t('printers.drying.resumeSchedule');
                  return (
                    <tr
                      key={rule.id}
                      className={`border-b border-bambu-dark-tertiary/50 ${rule.enabled ? 'text-white' : 'text-bambu-gray'}`}
                    >
                      <td className="py-1.5 pr-2">
                        <div>{printerName(rule.printer_id)}</div>
                        <div className="text-bambu-gray">{amsLabel(rule.ams_id)}</div>
                      </td>
                      <td className="py-1.5 pr-2">
                        <div>
                          {rule.start_time}
                          {rule.latest_start ? `–${rule.latest_start}` : ''}
                        </div>
                        <div className="text-bambu-gray">{weekdaysLabel(rule.weekdays, t)}</div>
                      </td>
                      <td className="py-1.5 pr-2">
                        <div>
                          {rule.temp}°C × {rule.duration_hours} {t('printers.drying.hours')}
                        </div>
                        <div className="text-bambu-gray">
                          {rule.filament || '—'}
                          {rule.enabled ? '' : ` · ${t('settings.dryingSchedules.paused')}`}
                        </div>
                      </td>
                      <td className="py-1.5">
                        {canControl && (
                        <div className="flex items-center justify-end gap-1.5 text-bambu-gray">
                          <button
                            type="button"
                            onClick={() => setEditing(rule)}
                            aria-label={t('printers.drying.editSchedule')}
                            title={t('printers.drying.editSchedule')}
                            className="p-1 hover:text-white"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => toggle.mutate(rule)}
                            disabled={toggle.isPending}
                            aria-label={toggleLabel}
                            title={toggleLabel}
                            className="p-1 hover:text-white disabled:opacity-50"
                          >
                            {rule.enabled ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirmDelete(rule)}
                            aria-label={t('printers.drying.deleteSchedule')}
                            title={t('printers.drying.deleteSchedule')}
                            className="p-1 hover:text-red-400"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-bambu-gray">
          {t('settings.dryingSchedules.hint', { tz: data?.server_timezone ?? 'UTC' })}
        </p>
      </CardContent>
      {editing && <DryingScheduleModal rule={editing} onClose={() => setEditing(null)} />}
      {confirmDelete && (
        <ConfirmModal
          title={t('printers.drying.deleteSchedule')}
          message={t('printers.drying.deleteScheduleConfirm')}
          confirmText={t('printers.drying.deleteSchedule')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(confirmDelete.id)}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </Card>
  );
}
