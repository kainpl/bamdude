import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
} from 'recharts';
import type { ArchiveAggregate } from '../api/client';
import { MetricToggle, type Metric } from './MetricToggle';
import { localDateOfBucket } from '../utils/date';
import { formatWeight } from '../utils/weight';

interface FilamentTrendsProps {
  /**
   * Aggregated on the server.
   *
   * ⚠️ This used to take every archive row of the range and fold it eight ways
   * here. The multi-value split for materials and colours now happens on the
   * server, with the same asymmetric rule: grams and seconds are divided evenly
   * among the parts of "PLA, PETG", while print counts are credited whole to
   * each part.
   */
  aggregate: ArchiveAggregate | undefined;
  currency?: string;
  dateFrom?: string;
  dateTo?: string;
}

const COLORS = ['#00ae42', '#3b82f6', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316'];

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const HOUR_SUFFIXES = ['12am', '1am', '2am', '3am', '4am', '5am', '6am', '7am', '8am', '9am', '10am', '11am', '12pm', '1pm', '2pm', '3pm', '4pm', '5pm', '6pm', '7pm', '8pm', '9pm', '10pm', '11pm'];

export function FilamentTrends({ aggregate, currency = '$', dateFrom, dateTo }: FilamentTrendsProps) {
  const { t } = useTranslation();
  const [filamentTypeMetric, setFilamentTypeMetric] = useState<Metric>('weight');
  const [colorMetric, setColorMetric] = useState<Metric>('weight');

  const buckets = useMemo(() => aggregate?.buckets ?? [], [aggregate]);

  // Daily usage, on the "ended" axis — the day a print finished is the day its
  // filament was spent, which is the axis this chart has always used.
  const dailyData = useMemo(() => {
    const dataMap = new Map<string, { date: string; filament: number; cost: number; prints: number }>();

    buckets.forEach(bucket => {
      const key = bucket.at.split('T')[0];
      const existing = dataMap.get(key) || { date: key, filament: 0, cost: 0, prints: 0 };
      existing.filament += bucket.ended.grams;
      existing.cost += bucket.ended.cost;
      existing.prints += bucket.ended.quantity;
      dataMap.set(key, existing);
    });

    return Array.from(dataMap.values())
      .sort((a, b) => a.date.localeCompare(b.date))
      .map(d => ({
        ...d,
        dateLabel: localDateOfBucket(d.date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      }));
  }, [buckets]);

  // Compute effective span in days from props or archive spread
  const spanDays = useMemo(() => {
    if (dateFrom && dateTo) {
      return Math.max((new Date(dateTo).getTime() - new Date(dateFrom).getTime()) / 86400000, 0) + 1;
    }
    if (dateFrom) {
      return Math.max((Date.now() - new Date(dateFrom).getTime()) / 86400000, 0) + 1;
    }
    if (buckets.length < 2) return 0;
    const times = buckets.map(b => localDateOfBucket(b.at).getTime());
    return (Math.max(...times) - Math.min(...times)) / 86400000;
  }, [buckets, dateFrom, dateTo]);

  // Calculate hourly data for short timeframes (≤ 7 days)
  const hourlyData = useMemo(() => {
    if (spanDays > 7) return [];

    const dataMap = new Map<string, { date: string; filament: number; cost: number; prints: number }>();
    const multiDay = spanDays > 1;

    // The server sends hourly buckets exactly when the range is a week or less,
    // so there is nothing to re-bucket — a day-grained response simply yields
    // no hourly points and the daily chart is used instead.
    buckets.forEach(bucket => {
      if (!bucket.at.includes('T')) return;
      const existing = dataMap.get(bucket.at) || { date: bucket.at, filament: 0, cost: 0, prints: 0 };
      existing.filament += bucket.ended.grams;
      existing.cost += bucket.ended.cost;
      existing.prints += bucket.ended.quantity;
      dataMap.set(bucket.at, existing);
    });

    return Array.from(dataMap.values())
      .sort((a, b) => a.date.localeCompare(b.date))
      .map(d => {
        const dt = localDateOfBucket(d.date);
        const h = dt.getHours();
        const label = multiDay
          ? `${DAY_NAMES[dt.getDay()]} ${HOUR_SUFFIXES[h]}`
          : HOUR_SUFFIXES[h];
        return { ...d, dateLabel: label };
      });
  }, [buckets, spanDays]);

  // Calculate weekly aggregated data when there are many daily points
  const weeklyData = useMemo(() => {
    if (dailyData.length <= 60) return dailyData;

    const dataMap = new Map<string, { week: string; filament: number; cost: number; prints: number }>();

    dailyData.forEach(day => {
      const date = localDateOfBucket(day.date);
      const weekStart = new Date(date);
      weekStart.setDate(date.getDate() - date.getDay());
      const key = `${weekStart.getFullYear()}-${String(weekStart.getMonth() + 1).padStart(2, '0')}-${String(weekStart.getDate()).padStart(2, '0')}`;

      const existing = dataMap.get(key) || { week: key, filament: 0, cost: 0, prints: 0 };
      existing.filament += day.filament;
      existing.cost += day.cost;
      existing.prints += day.prints;
      dataMap.set(key, existing);
    });

    return Array.from(dataMap.values())
      .sort((a, b) => a.week.localeCompare(b.week))
      .map(d => ({
        date: d.week,
        dateLabel: `Week of ${localDateOfBucket(d.week).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`,
        ...d,
      }));
  }, [dailyData]);

  // Usage by filament type. The "PLA, PETG" split and the even division of
  // grams and seconds among its parts happen on the server now, under the same
  // asymmetric rule this component used: measures divide, counts do not.
  const materials = useMemo(() => aggregate?.by_material ?? [], [aggregate]);

  const filamentTypeData = useMemo(
    () =>
      materials
        .map(row => ({ name: row.material, value: Math.round(row.grams) }))
        .sort((a, b) => b.value - a.value),
    [materials],
  );

  // Usage by filament type (print count)
  const filamentTypePrintData = useMemo(
    () =>
      materials
        .map(row => ({ name: row.material, value: row.prints }))
        .sort((a, b) => b.value - a.value),
    [materials],
  );

  // Usage by filament type (print time in hours)
  const filamentTypeTimeData = useMemo(
    () =>
      materials
        .map(row => ({ name: row.material, value: Math.round((row.seconds / 3600) * 10) / 10 }))
        .sort((a, b) => b.value - a.value),
    [materials],
  );

  // Success rate by filament type — only materials with at least two terminal
  // prints, so one lucky spool cannot show a perfect record.
  const filamentSuccessData = useMemo(
    () =>
      materials
        .filter(row => row.completed + row.failed >= 2)
        .map(row => {
          const total = row.completed + row.failed;
          return { name: row.material, rate: Math.round((row.completed / total) * 100), total };
        })
        .sort((a, b) => b.rate - a.rate),
    [materials],
  );

  // Color distribution
  const colorData = useMemo(
    () =>
      (aggregate?.by_color ?? [])
        .map(row => ({
          hex: row.color,
          value: colorMetric === 'prints' ? row.prints : Math.round(row.grams),
        }))
        .sort((a, b) => b.value - a.value),
    [aggregate, colorMetric],
  );

  const activeFilamentTypeData =
    filamentTypeMetric === 'weight' ? filamentTypeData :
    filamentTypeMetric === 'prints' ? filamentTypePrintData :
    filamentTypeTimeData;

  const chartData = spanDays <= 7 && hourlyData.length > 0 ? hourlyData : weeklyData;
  const totals = aggregate?.totals;
  const totalFilament = totals?.grams ?? 0;
  const totalCost = totals?.cost ?? 0;
  // Sum of per-print item quantities = total printed objects (NOT print jobs).
  const totalObjects = totals?.quantity ?? 0;
  // Number of print jobs (one archive = one print).
  const totalPrints = totals?.prints ?? 0;
  const printerCount = totals?.printers ?? 0;

  return (
    <div className="space-y-4">
      {/* Summary Cards */}
      <div className="grid grid-cols-4 gap-2 max-[640px]:grid-cols-2">
        <div className="bg-bambu-dark rounded-lg p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-bambu-gray leading-none">{t('stats.periodFilament')}</p>
            <p className="text-2xl font-bold text-white leading-none">{formatWeight(totalFilament)}</p>
          </div>
          <p className="text-xs text-bambu-gray">{printerCount} {t('nav.printers').toLowerCase()}</p>
        </div>
        <div className="bg-bambu-dark rounded-lg p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-bambu-gray leading-none">{t('stats.periodCost')}</p>
            <p className="text-2xl font-bold text-white leading-none">{currency}{totalCost.toFixed(2)}</p>
          </div>
          <p className="text-xs text-bambu-gray">{totalObjects} {t('common.objects')}</p>
        </div>
        <div className="bg-bambu-dark rounded-lg p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-bambu-gray leading-none">{t('stats.avgPerObject')}</p>
            <p className="text-2xl font-bold text-white leading-none">
              {totalObjects > 0
                ? (totalFilament / totalObjects).toFixed(0)
                : 0}g
            </p>
          </div>
          <p className="text-xs text-bambu-gray">
            {currency}{totalObjects > 0 ? (totalCost / totalObjects).toFixed(2) : '0.00'} avg
          </p>
        </div>
        <div className="bg-bambu-dark rounded-lg p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-bambu-gray leading-none">{t('stats.avgPerPrint')}</p>
            <p className="text-2xl font-bold text-white leading-none">
              {totalPrints > 0
                ? (totalFilament / totalPrints).toFixed(0)
                : 0}g
            </p>
          </div>
          <p className="text-xs text-bambu-gray">
            {currency}{totalPrints > 0 ? (totalCost / totalPrints).toFixed(2) : '0.00'} avg
          </p>
        </div>
      </div>

      {/* Usage Over Time Chart */}
      {chartData.length > 0 ? (
        <div className="bg-bambu-dark rounded-lg p-4">
          <h4 className="text-sm font-medium text-bambu-gray mb-4">{t('stats.usageOverTime')}</h4>
          <ResponsiveContainer width="100%" height={250}>
            <AreaChart data={chartData}>
              <defs>
                <linearGradient id="colorFilament" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#00ae42" stopOpacity={0.3}/>
                  <stop offset="95%" stopColor="#00ae42" stopOpacity={0}/>
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#3d3d3d" />
              <XAxis
                dataKey="dateLabel"
                stroke="#9ca3af"
                tick={{ fontSize: 12 }}
                interval="preserveStartEnd"
              />
              <YAxis
                stroke="#9ca3af"
                tick={{ fontSize: 12 }}
                tickFormatter={(value) => `${value}g`}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#2d2d2d',
                  border: '1px solid #3d3d3d',
                  borderRadius: '8px',
                }}
                labelStyle={{ color: '#fff' }}
                formatter={(value) => [`${Number(value ?? 0).toFixed(0)}g`, 'Filament']}
              />
              <Area
                type="monotone"
                dataKey="filament"
                stroke="#00ae42"
                strokeWidth={2}
                fillOpacity={1}
                fill="url(#colorFilament)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="bg-bambu-dark rounded-lg p-8 text-center text-bambu-gray">
          {t('stats.noPrintDataInRange')}
        </div>
      )}

      {/* Bottom Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Filament Type Distribution */}
        <div className="bg-bambu-dark rounded-lg p-4">
          <div className="flex items-center justify-between mb-4">
            <h4 className="text-sm font-medium text-bambu-gray">{t('stats.byMaterial')}</h4>
            <MetricToggle value={filamentTypeMetric} onChange={setFilamentTypeMetric} />
          </div>
          {activeFilamentTypeData.length > 0 ? (
            <div className="flex items-center gap-4">
              <ResponsiveContainer width={160} height={160}>
                <PieChart>
                  <Pie
                    data={activeFilamentTypeData}
                    cx="50%"
                    cy="50%"
                    innerRadius={40}
                    outerRadius={70}
                    paddingAngle={2}
                    dataKey="value"
                  >
                    {activeFilamentTypeData.map((_, index) => (
                      <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#2d2d2d',
                      border: '1px solid #3d3d3d',
                      borderRadius: '8px',
                    }}
                    formatter={(value) => [
                      filamentTypeMetric === 'weight' ? formatWeight(Number(value ?? 0)) :
                      filamentTypeMetric === 'time' ? `${Number(value ?? 0)}h` :
                      `${value ?? 0}`,
                      filamentTypeMetric === 'weight' ? 'Usage' : filamentTypeMetric === 'time' ? 'Time' : 'Prints',
                    ]}
                  />
                </PieChart>
              </ResponsiveContainer>
              <div className="flex-1 space-y-2 overflow-hidden">
                {activeFilamentTypeData.map((entry, index) => {
                  const total = activeFilamentTypeData.reduce((sum, e) => sum + e.value, 0);
                  const percent = total > 0 ? ((entry.value / total) * 100).toFixed(0) : 0;
                  return (
                    <div key={entry.name} className="flex items-center gap-2 text-sm">
                      <div
                        className="w-3 h-3 rounded-sm flex-shrink-0"
                        style={{ backgroundColor: COLORS[index % COLORS.length] }}
                      />
                      <span className="text-white truncate flex-1">{entry.name}</span>
                      <span className="text-bambu-gray flex-shrink-0">
                        {filamentTypeMetric === 'weight' ? formatWeight(entry.value) :
                         filamentTypeMetric === 'time' ? `${entry.value}h` :
                         entry.value} · {percent}%
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          ) : (
            <div className="h-[160px] flex items-center justify-center text-bambu-gray">
              {t('stats.noFilamentData')}
            </div>
          )}
        </div>

        {/* Success by Material */}
        <div className="bg-bambu-dark rounded-lg p-4">
          <h4 className="text-sm font-medium text-bambu-gray mb-4">{t('stats.filamentSuccess')}</h4>
          {filamentSuccessData.length > 0 ? (
            <div className="space-y-1.5">
              {filamentSuccessData.map(d => (
                <div key={d.name} className="flex items-center gap-2 text-sm">
                  <span className="text-white truncate w-20 flex-shrink-0">{d.name}</span>
                  <div className="flex-1 h-1.5 bg-bambu-dark-secondary rounded-full">
                    <div
                      className={`h-full rounded-full transition-all ${
                        d.rate >= 90 ? 'bg-status-ok' : d.rate >= 70 ? 'bg-status-warning' : 'bg-status-error'
                      }`}
                      style={{ width: `${d.rate}%` }}
                    />
                  </div>
                  <span className={`font-medium flex-shrink-0 tabular-nums ${
                    d.rate >= 90 ? 'text-status-ok' : d.rate >= 70 ? 'text-status-warning' : 'text-status-error'
                  }`}>
                    {d.rate}%
                  </span>
                  <span className="text-bambu-gray flex-shrink-0 text-xs">({d.total})</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="h-[160px] flex items-center justify-center text-bambu-gray">
              {t('stats.noArchiveData')}
            </div>
          )}
        </div>

        {/* Color Distribution */}
        <div className="bg-bambu-dark rounded-lg p-4">
          <div className="flex items-center justify-between mb-4">
            <h4 className="text-sm font-medium text-bambu-gray">{t('stats.colorDistribution')}</h4>
            <MetricToggle value={colorMetric} onChange={setColorMetric} exclude={['time']} />
          </div>
          {colorData.length > 0 ? (() => {
            const colorTotal = colorData.reduce((sum, e) => sum + e.value, 0);
            return (
              <div>
                <div className="relative mx-auto" style={{ width: 160, height: 160 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={colorData}
                        cx="50%"
                        cy="50%"
                        innerRadius={45}
                        outerRadius={70}
                        paddingAngle={2}
                        dataKey="value"
                      >
                        {colorData.map((entry, index) => (
                          <Cell key={`color-${index}`} fill={entry.hex} stroke="#1a1a1a" strokeWidth={1} />
                        ))}
                      </Pie>
                      <Tooltip
                        contentStyle={{
                          backgroundColor: '#2d2d2d',
                          border: '1px solid #3d3d3d',
                          borderRadius: '8px',
                        }}
                        formatter={(value) => [
                          colorMetric === 'weight' ? formatWeight(Number(value ?? 0)) : `${value ?? 0}`,
                          colorMetric === 'weight' ? t('stats.filamentByWeight') : t('stats.filamentByPrints'),
                        ]}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="absolute inset-0 flex flex-col items-center justify-center">
                    <span className="text-lg font-bold text-white">
                      {colorMetric === 'weight' ? formatWeight(colorTotal) : colorTotal}
                    </span>
                    <span className="text-[10px] text-bambu-gray">
                      {colorData.length} {colorData.length === 1 ? 'color' : 'colors'}
                    </span>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-x-3 gap-y-1 mt-2">
                  {colorData.slice(0, 8).map((entry) => {
                    const percent = colorTotal > 0 ? ((entry.value / colorTotal) * 100).toFixed(0) : 0;
                    return (
                      <div key={entry.hex} className="flex items-center gap-1.5 text-xs min-w-0">
                        <div className="w-2.5 h-2.5 rounded-full flex-shrink-0 border border-black/20"
                          style={{ backgroundColor: entry.hex }} />
                        <span className="text-bambu-gray truncate">
                          {percent}%
                        </span>
                      </div>
                    );
                  })}
                </div>
                {colorData.length > 8 && (
                  <p className="text-[10px] text-bambu-gray mt-1 text-center">+{colorData.length - 8} more</p>
                )}
              </div>
            );
          })() : (
            <div className="h-[160px] flex items-center justify-center text-bambu-gray">
              {t('stats.noColorData')}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
