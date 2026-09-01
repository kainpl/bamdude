import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

import { api } from '../../api/client';
import type { SensorThreshold, SensorThresholdInput, ZigbeeSensor } from '../../api/client';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  sensor: ZigbeeSensor;
}

interface Row {
  kind: string;
  unit: string;
  min: string;
  max: string;
  deadband: string;
  enabled: boolean;
}

function toRows(sensor: ZigbeeSensor, configured: SensorThreshold[]): Row[] {
  // The UNION of what the sensor measures and what is already configured.
  // Without it a sensor off the mesh shows an empty dialog and its own limits
  // become invisible and unremovable — its `measurements` are empty precisely
  // because it is absent.
  const byKind = new Map<string, Row>();
  for (const [kind, reading] of Object.entries(sensor.measurements)) {
    byKind.set(kind, { kind, unit: reading.unit, min: '', max: '', deadband: '', enabled: true });
  }
  for (const row of configured) {
    byKind.set(row.kind, {
      kind: row.kind,
      unit: row.unit,
      min: row.min_value == null ? '' : String(row.min_value),
      max: row.max_value == null ? '' : String(row.max_value),
      deadband: row.deadband ? String(row.deadband) : '',
      enabled: row.enabled,
    });
  }
  return [...byKind.values()];
}

function toPayload(rows: Row[]): SensorThresholdInput[] {
  // Only rows carrying a limit. The backend refuses one with neither, so
  // sending an untouched row would turn it into an error message.
  return rows
    .filter((row) => row.min.trim() !== '' || row.max.trim() !== '')
    .map((row) => ({
      kind: row.kind,
      min_value: row.min.trim() === '' ? null : Number(row.min),
      max_value: row.max.trim() === '' ? null : Number(row.max),
      deadband: row.deadband.trim() === '' ? 0 : Number(row.deadband),
      enabled: row.enabled,
    }));
}

/** What counts as wrong for this sensor, one row per quantity. */
export function SensorThresholdsModal({ isOpen, onClose, sensor }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ['sensor-thresholds', sensor.id],
    queryFn: () => api.getSensorThresholds(sensor.id),
    enabled: isOpen,
  });

  useEffect(() => {
    setRows(toRows(sensor, data?.thresholds ?? []));
  }, [sensor, data]);

  const save = useMutation({
    mutationFn: () => api.putSensorThresholds(sensor.id, toPayload(rows)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sensor-thresholds', sensor.id] });
      setError(null);
      onClose();
    },
    // The backend's own sentence: it names which row and why.
    onError: (e: Error) => setError(e.message),
  });

  if (!isOpen) return null;

  const set = (kind: string, field: keyof Row, value: string | boolean) =>
    setRows((current) => current.map((row) => (row.kind === kind ? { ...row, [field]: value } : row)));

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary rounded-xl w-full max-w-2xl max-h-[90vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-4 border-b border-bambu-dark-tertiary">
          <div>
            <h2 className="text-lg font-semibold text-white">{t('settings.zigbee.thresholds.title')}</h2>
            <p className="text-sm text-bambu-gray">{sensor.name}</p>
          </div>
          <button type="button" onClick={onClose} aria-label={t('common.close')} className="p-2 text-bambu-gray">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-6 space-y-4 overflow-y-auto max-h-[calc(90vh-140px)]">
          <p className="text-sm text-bambu-gray">{t('settings.zigbee.thresholds.hint')}</p>

          {rows.map((row) => {
            const name = t(`settings.zigbee.measurement.${row.kind}`, { defaultValue: row.kind });
            return (
              <div key={row.kind} className="flex items-center gap-3 flex-wrap">
                <span className="text-white w-32">{name}</span>
                <Field
                  label={`${name} ${t('settings.zigbee.thresholds.min')}`}
                  value={row.min}
                  unit={row.unit}
                  onChange={(v) => set(row.kind, 'min', v)}
                />
                <Field
                  label={`${name} ${t('settings.zigbee.thresholds.max')}`}
                  value={row.max}
                  unit={row.unit}
                  onChange={(v) => set(row.kind, 'max', v)}
                />
                <Field
                  label={`${name} ${t('settings.zigbee.thresholds.deadband')}`}
                  value={row.deadband}
                  unit={row.unit}
                  onChange={(v) => set(row.kind, 'deadband', v)}
                />
              </div>
            );
          })}

          {error && <p className="text-sm text-status-error">{error}</p>}
        </div>

        <div className="flex justify-end gap-2 px-4 py-4 border-t border-bambu-dark-tertiary">
          <button type="button" onClick={onClose} className="px-3 py-1.5 text-bambu-gray">
            {t('common.cancel')}
          </button>
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={save.isPending}
            className="px-3 py-1.5 bg-bambu-green rounded-lg text-white disabled:opacity-50"
          >
            {t('common.save')}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  unit,
  onChange,
}: {
  label: string;
  value: string;
  unit: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-1 text-sm text-bambu-gray">
      <span className="sr-only">{label}</span>
      <input
        aria-label={label}
        type="number"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-20 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
      />
      {unit}
    </label>
  );
}
