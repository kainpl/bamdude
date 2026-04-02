import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { ArchiveSlim } from '../api/client';
import { api } from '../api/client';
import { parseUTCDate } from '../utils/date';

interface CalendarViewProps {
  archives: ArchiveSlim[];
  printerMap?: Map<number, string>;
}

// Day names resolved via Intl at render time for proper localization

export function CalendarView({ archives, printerMap }: CalendarViewProps) {
  const { t } = useTranslation();
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  // Build list of last 30 days (today first, 29 days back last)
  const days = useMemo(() => {
    const result: Date[] = [];
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    for (let i = 0; i <= 29; i++) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      result.push(d);
    }
    return result;
  }, []);

  // Group archives by date key
  const archivesByDate = useMemo(() => {
    const map = new Map<string, ArchiveSlim[]>();
    archives.forEach(archive => {
      const date = parseUTCDate(archive.completed_at || archive.created_at) || new Date();
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
      const existing = map.get(key) || [];
      existing.push(archive);
      map.set(key, existing);
    });
    return map;
  }, [archives]);

  const today = new Date();
  today.setHours(0, 0, 0, 0);

  // Stats for the 30-day period
  const totalPrints = archives.length;
  const successCount = archives.filter(a => a.status === 'completed').length;
  const failedCount = archives.filter(a => a.status === 'failed' || a.status === 'aborted').length;

  // Find max prints per day for scaling
  const maxPerDay = useMemo(() => {
    let max = 0;
    for (const dayArchives of archivesByDate.values()) {
      if (dayArchives.length > max) max = dayArchives.length;
    }
    return max || 1;
  }, [archivesByDate]);

  const selectedArchives = selectedDate ? archivesByDate.get(selectedDate) || [] : [];

  const handleDateSelect = (dateKey: string) => {
    setSelectedDate(dateKey === selectedDate ? null : dateKey);
  };

  return (
    <div className="flex flex-col lg:flex-row gap-6">
      {/* Calendar */}
      <div className="flex-1">
        {/* Stats row */}
        <div className="grid grid-cols-3 gap-4 text-center mb-6">
          <div>
            <div className="text-2xl font-bold text-white">{totalPrints}</div>
            <div className="text-xs text-bambu-gray">{t('archives.calendar.totalPrints', 'Prints (30 days)')}</div>
          </div>
          <div>
            <div className="text-2xl font-bold text-green-400">{successCount}</div>
            <div className="text-xs text-bambu-gray">{t('archives.calendar.successful', 'Successful')}</div>
          </div>
          <div>
            <div className="text-2xl font-bold text-red-400">{failedCount}</div>
            <div className="text-xs text-bambu-gray">{t('archives.calendar.failed', 'Failed')}</div>
          </div>
        </div>

        {/* Day headers */}
        <div className="grid grid-cols-7 gap-1 mb-1">
          {Array.from({ length: 7 }, (_, i) => (
            <div key={i} className="text-center text-xs text-bambu-gray py-1">
              {new Date(2024, 0, i).toLocaleDateString(undefined, { weekday: 'short' })}
            </div>
          ))}
        </div>

        {/* Leading empty cells to align first day to correct weekday */}
        <div className="grid grid-cols-7 gap-1">
          {Array.from({ length: days[0].getDay() }, (_, i) => (
            <div key={`pad-${i}`} className="aspect-square" />
          ))}

          {days.map(day => {
            const dateKey = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
            const dayArchives = archivesByDate.get(dateKey) || [];
            const count = dayArchives.length;
            const isToday = day.getTime() === today.getTime();
            const isSelected = dateKey === selectedDate;
            const daySuccess = dayArchives.filter(a => a.status === 'completed').length;
            const dayFailed = dayArchives.filter(a => a.status === 'failed' || a.status === 'aborted').length;

            // Intensity based on count relative to max
            const intensity = count > 0 ? Math.max(0.15, count / maxPerDay) : 0;

            // Color: green if all ok, yellow if mixed, red if all failed
            let bgColor = 'transparent';
            if (count > 0) {
              if (dayFailed > 0 && daySuccess === 0) {
                bgColor = `rgba(248, 113, 113, ${intensity})`; // red
              } else if (dayFailed > 0) {
                bgColor = `rgba(250, 204, 21, ${intensity})`; // yellow
              } else {
                bgColor = `rgba(0, 174, 66, ${intensity})`; // bambu green
              }
            }

            return (
              <button
                key={dateKey}
                onClick={() => handleDateSelect(dateKey)}
                className={`aspect-square rounded-lg flex flex-col items-center justify-center relative transition-colors ${
                  isSelected
                    ? 'ring-2 ring-bambu-green bg-bambu-green/20'
                    : isToday
                    ? 'ring-2 ring-bambu-green'
                    : ''
                }`}
                style={!isSelected ? { backgroundColor: bgColor } : undefined}
                title={count > 0 ? `${dateKey}: ${count} (✓${daySuccess} ✗${dayFailed})` : dateKey}
              >
                <span className={`text-sm font-medium ${
                  isToday ? 'text-bambu-green' : count > 0 ? 'text-white' : 'text-bambu-gray'
                }`}>
                  {day.getDate()}
                </span>
                {count > 0 && (
                  <span className="text-[10px] font-medium text-white/80">{count}</span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* Selected day details */}
      <div className="lg:w-80 bg-bambu-dark rounded-xl p-4">
        {selectedDate ? (
          <>
            <h3 className="text-sm font-medium text-bambu-gray mb-3">
              {new Date(selectedDate + 'T12:00:00').toLocaleDateString(undefined, {
                weekday: 'long',
                month: 'long',
                day: 'numeric',
                year: 'numeric'
              })}
            </h3>
            {selectedArchives.length > 0 ? (
              <div className="space-y-2 max-h-96 overflow-y-auto">
                {selectedArchives.map(archive => (
                  <div
                    key={archive.id}
                    className="flex items-center gap-3 p-2 rounded-lg hover:bg-bambu-dark-tertiary transition-colors"
                  >
                    {archive.thumbnail_path ? (
                      <img
                        src={api.getArchiveThumbnail(archive.id)}
                        alt=""
                        className="w-12 h-12 rounded object-cover flex-shrink-0"
                      />
                    ) : (
                      <div className="w-12 h-12 rounded bg-bambu-dark-tertiary flex items-center justify-center flex-shrink-0">
                        <span className="text-xs text-bambu-gray">3MF</span>
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-white truncate">
                        {archive.print_name || archive.filename}
                      </p>
                      <div className="flex items-center gap-2 text-xs text-bambu-gray">
                        <span className={archive.status === 'failed' || archive.status === 'aborted' ? 'text-red-400' : 'text-green-400'}>
                          {archive.status === 'failed' || archive.status === 'aborted' ? t('archives.calendar.failed', 'Failed') : t('archives.calendar.successful', 'Successful')}
                        </span>
                        <span>
                          {(() => {
                            const d = parseUTCDate(archive.created_at);
                            return d ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) : '';
                          })()}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 text-xs text-bambu-gray mt-0.5">
                        {archive.printer_id && printerMap?.get(archive.printer_id) && (
                          <span className="truncate">{printerMap.get(archive.printer_id)}</span>
                        )}
                        {archive.filament_type && (
                          <span className="truncate">{archive.filament_type}</span>
                        )}
                        {archive.filament_color && (
                          <div className="flex gap-0.5 flex-shrink-0">
                            {archive.filament_color.split(',').map((color, i) => (
                              <div
                                key={i}
                                className="w-3 h-3 rounded-full border border-black/20"
                                style={{ backgroundColor: color }}
                              />
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-bambu-gray">{t('archives.calendar.noPrints', 'No prints on this day')}</p>
            )}
          </>
        ) : (
          <div className="text-center py-8">
            <p className="text-sm text-bambu-gray">{t('archives.calendar.selectDay', 'Select a day to see prints')}</p>
          </div>
        )}
      </div>
    </div>
  );
}
