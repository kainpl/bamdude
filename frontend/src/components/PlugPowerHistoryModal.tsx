import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Zap } from 'lucide-react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { api } from '../api/client';
import { applyTimeFormat, type TimeFormat } from '../utils/date';
import { bucketLabelKey, chartPoints } from '../utils/plugPowerChart';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  plugId: number;
  plugName: string;
}

type TimeRange = '6h' | '24h' | '48h' | '7d';

const TIME_RANGES: { value: TimeRange; label: string; hours: number }[] = [
  { value: '6h', label: '6h', hours: 6 },
  { value: '24h', label: '24h', hours: 24 },
  { value: '48h', label: '48h', hours: 48 },
  { value: '7d', label: '7d', hours: 168 },
];

export function PlugPowerHistoryModal({ isOpen, onClose, plugId, plugName }: Props) {
  const { t } = useTranslation();
  const [range, setRange] = useState<TimeRange>('24h');

  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const timeFormat: TimeFormat = settings?.time_format || 'system';

  const hours = TIME_RANGES.find((r) => r.value === range)?.hours ?? 24;

  const { data, isLoading, error } = useQuery({
    queryKey: ['plug-power-history', plugId, hours],
    queryFn: () => api.getPlugPowerHistory(plugId, hours),
    enabled: isOpen,
    // A minute. Faster cannot show anything new -- at a 24-hour window the
    // bucket is five minutes wide.
    refetchInterval: 60000,
  });

  if (!isOpen) return null;

  const points = chartPoints(data?.points ?? []);
  const minutes = Math.round((data?.bucket_seconds ?? 300) / 60);

  const modalBg = 'var(--bg-secondary)';
  const cardBg = 'var(--bg-primary)';
  const borderColor = 'var(--border-color)';
  const textPrimary = 'var(--text-primary)';
  const textSecondary = 'var(--text-secondary)';
  const axisColor = 'var(--text-muted)';

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="rounded-xl w-full max-w-4xl max-h-[90vh] overflow-hidden shadow-xl"
        style={{ backgroundColor: modalBg }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-4 border-b" style={{ borderColor }}>
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2" style={{ color: textPrimary }}>
              <Zap className="w-4 h-4" />
              {t('smartPlugs.powerHistory.title')}
            </h2>
            <p className="text-sm" style={{ color: textSecondary }}>
              {plugName}
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
              <Stat label={t('smartPlugs.powerHistory.min')} value={data?.min_power} color={textSecondary} />
              <Stat label={t('smartPlugs.powerHistory.avg')} value={data?.avg_power} color={textSecondary} />
              <Stat label={t('smartPlugs.powerHistory.max')} value={data?.max_power} color={textSecondary} />
            </div>
          </div>

          {isLoading ? (
            <div className="h-64" />
          ) : error ? (
            <div className="h-64 flex items-center justify-center" style={{ color: textSecondary }}>
              {t('smartPlugs.powerHistory.error')}
            </div>
          ) : points.length === 0 ? (
            <div className="h-64 flex items-center justify-center" style={{ color: textSecondary }}>
              {t('smartPlugs.powerHistory.empty')}
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={points} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={borderColor} />
                <XAxis
                  dataKey="time"
                  type="number"
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
                  domain={[0, 'auto']}
                  tickFormatter={(v) => `${Math.round(v)}W`}
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
                    value == null
                      ? '—'
                      : t(bucketLabelKey(data?.bucket_seconds ?? 300), {
                          value: Math.round(Number(value)),
                          minutes,
                        })
                  }
                />
                <Line
                  type="monotone"
                  dataKey="power"
                  stroke="#f59e0b"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                  // The reason the empty buckets are sent at all: without this
                  // recharts joins the readings on either side of a gap and
                  // draws consumption that never happened.
                  connectNulls={false}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: number | null | undefined; color: string }) {
  return (
    <span style={{ color }}>
      {label} <span className="text-white">{value == null ? '—' : `${Math.round(value)} W`}</span>
    </span>
  );
}
