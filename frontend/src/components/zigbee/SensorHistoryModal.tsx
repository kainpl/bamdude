import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { LineChart as LineChartIcon, X } from 'lucide-react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { api } from '../../api/client';
import type { ZigbeeSensor } from '../../api/client';
import { applyTimeFormat, type TimeFormat } from '../../utils/date';
import { formatReading, roomReadings } from '../../utils/sensorReadings';
import { sensorBucketLabelKey, sensorChartPoints } from '../../utils/sensorChart';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  sensor: ZigbeeSensor;
}

type TimeRange = '6h' | '24h' | '48h' | '7d';

const TIME_RANGES: { value: TimeRange; label: string; hours: number }[] = [
  { value: '6h', label: '6h', hours: 6 },
  { value: '24h', label: '24h', hours: 24 },
  { value: '48h', label: '48h', hours: 48 },
  { value: '7d', label: '7d', hours: 168 },
];

/**
 * One sensor's recorded history, one quantity at a time.
 *
 * `PlugPowerHistoryModal`'s structure, with two axis decisions taken from
 * neither it nor `HeaterHistoryModal` — see the comments on the axes.
 */
export function SensorHistoryModal({ isOpen, onClose, sensor }: Props) {
  const { t } = useTranslation();
  const [range, setRange] = useState<TimeRange>('24h');

  // Only what this sensor measures about the room. A battery tab would put a
  // different subject on the same axis.
  const quantities = roomReadings(sensor);
  const [kind, setKind] = useState<string>(quantities[0]?.[0] ?? 'temperature');
  const unit = sensor.measurements[kind]?.unit ?? '';

  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const timeFormat: TimeFormat = settings?.time_format || 'system';

  const hours = TIME_RANGES.find((r) => r.value === range)?.hours ?? 24;

  const { data, isLoading, error } = useQuery({
    queryKey: ['sensor-history', sensor.id, kind, hours],
    queryFn: () => api.getSensorHistory(sensor.id, kind, hours),
    enabled: isOpen,
    // A minute. Faster cannot show anything new — at a 24-hour window the
    // bucket is five minutes wide.
    refetchInterval: 60000,
  });

  if (!isOpen) return null;

  const points = sensorChartPoints(data?.points ?? []);
  const minutes = Math.round((data?.bucket_seconds ?? 300) / 60);

  const modalBg = 'var(--bg-secondary)';
  const cardBg = 'var(--bg-primary)';
  const borderColor = 'var(--border-color)';
  const textPrimary = 'var(--text-primary)';
  const textSecondary = 'var(--text-secondary)';
  const axisColor = 'var(--text-muted)';

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="rounded-xl w-full max-w-4xl max-h-[90vh] overflow-hidden shadow-xl"
        style={{ backgroundColor: modalBg }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b" style={{ borderColor }}>
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2" style={{ color: textPrimary }}>
              <LineChartIcon className="w-4 h-4" />
              {t('sensorHistory.title')}
            </h2>
            <p className="text-sm" style={{ color: textSecondary }}>
              {[sensor.name, sensor.location?.path].filter(Boolean).join(' · ')}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded-lg"
            style={{ color: textSecondary }}
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-6 space-y-6 overflow-y-auto max-h-[calc(90vh-80px)]">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="inline-flex gap-1 rounded-lg p-1" style={{ backgroundColor: cardBg }}>
              {TIME_RANGES.map((r) => (
                <button
                  key={r.value}
                  onClick={() => setRange(r.value)}
                  className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                    range === r.value ? 'bg-bambu-green text-white' : ''
                  }`}
                  style={range === r.value ? undefined : { color: textSecondary }}
                >
                  {r.label}
                </button>
              ))}
            </div>

            <div className="flex items-center gap-4 text-sm">
              <Stat label={t('sensorHistory.min')} value={data?.min_value} unit={unit} color={textSecondary} />
              <Stat label={t('sensorHistory.avg')} value={data?.avg_value} unit={unit} color={textSecondary} />
              <Stat label={t('sensorHistory.max')} value={data?.max_value} unit={unit} color={textSecondary} />
            </div>
          </div>

          <div className="inline-flex gap-1 rounded-lg p-1" style={{ backgroundColor: cardBg }}>
            {quantities.map(([key]) => (
              <button
                key={key}
                onClick={() => setKind(key)}
                className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                  kind === key ? 'bg-bambu-green text-white' : ''
                }`}
                style={kind === key ? undefined : { color: textSecondary }}
              >
                {t(`settings.zigbee.measurement.${key}`, { defaultValue: key })}
              </button>
            ))}
          </div>

          {isLoading ? (
            <div className="h-64" />
          ) : error ? (
            <div className="h-64 flex items-center justify-center" style={{ color: textSecondary }}>
              {t('sensorHistory.error')}
            </div>
          ) : points.length === 0 ? (
            <div className="h-64 flex items-center justify-center" style={{ color: textSecondary }}>
              {t('sensorHistory.empty')}
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={points} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={borderColor} />
                <XAxis
                  dataKey="time"
                  type="number"
                  // Not padded to the edges of the window: HeaterHistoryModal
                  // duplicates its first and last point to reach them, which here
                  // would draw a week of flat line for a sensor adopted
                  // yesterday.
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={(ts) =>
                    new Date(ts).toLocaleTimeString(
                      [],
                      applyTimeFormat({ hour: '2-digit', minute: '2-digit' }, timeFormat),
                    )
                  }
                  stroke={axisColor}
                  fontSize={11}
                />
                <YAxis
                  stroke={axisColor}
                  fontSize={11}
                  // NOT [0, 'auto']. Zero means "off" for a nozzle, which is why
                  // the heater chart starts there; a room sits between 21 and
                  // 26 °C and on that scale is a flat line.
                  domain={['auto', 'auto']}
                  tickFormatter={(v) => `${formatReading(v)}${unit}`}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: modalBg,
                    border: `1px solid ${borderColor}`,
                    borderRadius: 6,
                    color: textPrimary,
                  }}
                  labelFormatter={(ts) =>
                    new Date(ts as number).toLocaleString(
                      undefined,
                      applyTimeFormat(
                        { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' },
                        timeFormat,
                      ),
                    )
                  }
                  formatter={(value) =>
                    t(sensorBucketLabelKey(data?.bucket_seconds ?? 300), {
                      value: formatReading(Number(value)),
                      unit,
                      minutes,
                    })
                  }
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke="#22c55e"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  unit,
  color,
}: {
  label: string;
  value: number | null | undefined;
  unit: string;
  color: string;
}) {
  return (
    <span style={{ color }}>
      {label} <span className="text-white">{value == null ? '—' : `${formatReading(value)} ${unit}`}</span>
    </span>
  );
}
