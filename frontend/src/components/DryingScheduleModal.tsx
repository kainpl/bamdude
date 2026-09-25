import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Repeat } from 'lucide-react';
import { api, type DryingSchedule, type DryingScheduleInput } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import {
  DRYING_SCHEDULES_KEY,
  SCHEDULED_DRYINGS_KEY,
  WEEKDAY_BITS,
  WEEKDAY_NAMES,
  amsLabel,
} from '../utils/scheduledDrying';
import { Button } from './Button';
import { Modal } from './Modal';

const INPUT =
  'px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm focus:outline-none focus:border-amber-500/50';

/**
 * Edit a drying rule. Sends only what changed; the backend re-creates a waiting
 * run from the edited rule, and a running one finishes as it started.
 */
export function DryingScheduleModal({
  rule,
  maxTemp,
  onClose,
}: {
  rule: DryingSchedule;
  /** The unit's ceiling — AMS-HT (id ≥ 128) 85 °C, AMS 2 Pro 65 °C; the backend checks the real unit. */
  maxTemp?: number;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const ceiling = maxTemp ?? (rule.ams_id >= 128 ? 85 : 65);
  const [startTime, setStartTime] = useState(rule.start_time);
  const [weekdays, setWeekdays] = useState(rule.weekdays);
  const [latest, setLatest] = useState(rule.latest_start ?? '');
  const [temp, setTemp] = useState(rule.temp);
  const [duration, setDuration] = useState(rule.duration_hours);
  const [enabled, setEnabled] = useState(rule.enabled);

  const changes: Partial<DryingScheduleInput> = {};
  if (startTime !== rule.start_time) changes.start_time = startTime;
  if (weekdays !== rule.weekdays) changes.weekdays = weekdays;
  if ((latest || null) !== rule.latest_start) changes.latest_start = latest || null;
  if (temp !== rule.temp) changes.temp = temp;
  if (duration !== rule.duration_hours) changes.duration_hours = duration;
  if (enabled !== rule.enabled) changes.enabled = enabled;

  const save = useMutation({
    mutationFn: () => api.updateDryingSchedule(rule.id, changes),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: DRYING_SCHEDULES_KEY });
      queryClient.invalidateQueries({ queryKey: SCHEDULED_DRYINGS_KEY });
      onClose();
    },
    onError: (error: Error) => showToast(error.message || t('printers.drying.scheduleFailed'), 'error'),
  });

  const invalid = !startTime || weekdays === 0;
  const onSave = () => {
    if (Object.keys(changes).length === 0) {
      onClose();
      return;
    }
    save.mutate();
  };

  return (
    <Modal
      onClose={onClose}
      closeDisabled={save.isPending}
      size="sm"
      title={`${t('printers.drying.editSchedule')} · ${amsLabel(rule.ams_id)}`}
      icon={<Repeat className="w-5 h-5 text-amber-500" />}
      footer={
        <div className="flex gap-3">
          <Button variant="secondary" onClick={onClose} className="flex-1" disabled={save.isPending}>
            {t('common.cancel')}
          </Button>
          <Button onClick={onSave} className="flex-1" disabled={save.isPending || invalid}>
            {t('common.save')}
          </Button>
        </div>
      }
    >
      <div className="p-4 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <label htmlFor="drying-rule-time" className="text-sm text-bambu-gray">
            {t('printers.drying.repeatAt')}
          </label>
          <input
            id="drying-rule-time"
            type="time"
            value={startTime}
            onChange={(e) => setStartTime(e.target.value)}
            className={INPUT}
          />
        </div>
        <div className="flex gap-1">
          {WEEKDAY_BITS.map((bit, index) => (
            <button
              key={bit}
              type="button"
              aria-pressed={(weekdays & bit) !== 0}
              onClick={() => setWeekdays((mask) => mask ^ bit)}
              className={`flex-1 py-1 rounded text-xs border transition-colors ${
                weekdays & bit
                  ? 'border-amber-500/60 bg-amber-500/15 text-amber-600 dark:text-amber-400'
                  : 'border-bambu-dark-tertiary text-bambu-gray/60 hover:text-white'
              }`}
            >
              {t(`printers.drying.weekdayShort.${WEEKDAY_NAMES[index]}`)}
            </button>
          ))}
        </div>
        <div className="flex items-center justify-between gap-3">
          <label htmlFor="drying-rule-latest" className="text-sm text-bambu-gray">
            {t('printers.drying.notLaterThan')}
          </label>
          <input
            id="drying-rule-latest"
            type="time"
            value={latest}
            onChange={(e) => setLatest(e.target.value)}
            className={INPUT}
          />
        </div>
        <div className="flex items-center justify-between gap-3">
          <label htmlFor="drying-rule-temp" className="text-sm text-bambu-gray">
            {t('printers.drying.temperature')} (°C)
          </label>
          <input
            id="drying-rule-temp"
            type="number"
            min={45}
            max={ceiling}
            value={temp}
            onChange={(e) => setTemp(Math.min(ceiling, Math.max(45, Number(e.target.value) || 45)))}
            className={`${INPUT} w-20 text-center`}
          />
        </div>
        <div className="flex items-center justify-between gap-3">
          <label htmlFor="drying-rule-duration" className="text-sm text-bambu-gray">
            {t('printers.drying.duration')} ({t('printers.drying.hours')})
          </label>
          <input
            id="drying-rule-duration"
            type="number"
            min={1}
            max={24}
            value={duration}
            onChange={(e) => setDuration(Math.min(24, Math.max(1, Number(e.target.value) || 1)))}
            className={`${INPUT} w-20 text-center`}
          />
        </div>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 accent-amber-500"
          />
          <span className="text-sm text-bambu-gray">{t('settings.dryingSchedules.enabled')}</span>
        </label>
      </div>
    </Modal>
  );
}
