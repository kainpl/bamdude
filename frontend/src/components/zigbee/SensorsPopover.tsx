import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { LineChart, Loader2, Thermometer, WifiOff } from 'lucide-react';

import { api } from '../../api/client';
import type { ZigbeeSensor } from '../../api/client';
import { formatRelativeTime } from '../../utils/date';
import { formatReading, roomReadings } from '../../utils/sensorReadings';
import { iconFor } from './measurementIcons';
import { SensorHistoryModal } from './SensorHistoryModal';
import { ZigbeeStatusBadge } from './ZigbeeStatusBadge';

interface Props {
  onClose: () => void;
  /** Held true while a chart is open — the wrapper's delayed close would
   *  otherwise unmount this popover and take the chart down with it. */
  onPinnedChange?: (pinned: boolean) => void;
}

/**
 * Every sensor's current readings, from the sidebar.
 *
 * Mirrors `SwitchbarPopover`: same shape, same place, same 10-second refresh
 * while it is open. Conditions belong somewhere reachable from any page, and
 * the group headers only exist on the printers page — which is why the queue
 * and maintenance pages no longer carry them.
 *
 * No `show_in_switchbar` equivalent: a sensor is listed because it was adopted,
 * and adoption is already the deliberate act. A flag would be a second answer
 * to a question that has one.
 */
export function SensorsPopover({ onClose, onPinnedChange }: Props) {
  const { t } = useTranslation();
  const [charting, setCharting] = useState<ZigbeeSensor | null>(null);

  // The chart lives outside this box, so moving the pointer to it reads as
  // leaving — both here and on the wrapper above.
  useEffect(() => {
    onPinnedChange?.(charting !== null);
  }, [charting, onPinnedChange]);

  const { data, isLoading } = useQuery({
    queryKey: ['zigbee-sensors'],
    queryFn: api.getZigbeeSensors,
    // Faster than the 30 s elsewhere because this is only mounted while open,
    // and matching the plug popover beside it.
    refetchInterval: 10000,
  });

  const sensors = data?.sensors ?? [];

  return (
    <>
      <div
        className="absolute bottom-full left-0 mb-2 w-72 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-xl z-50 origin-bottom-left animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-1"
        // Kept open while a chart is up: the modal renders outside this box, so
        // moving the pointer to it would otherwise close the thing that owns it.
        onMouseLeave={() => charting === null && onClose()}
      >
        <div className="px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white flex items-center gap-2">
            <Thermometer className="w-4 h-4 text-bambu-green" />
            {t('settings.zigbee.sensors.title')}
            {/* A dead radio makes every sensor here stop reporting at once, and
                nothing else in this panel would say why. */}
            <ZigbeeStatusBadge variant="dot" />
          </h3>
        </div>

        <div className="p-2 max-h-80 overflow-y-auto">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 text-bambu-gray animate-spin" />
            </div>
          ) : sensors.length === 0 ? (
            <div className="text-center py-6 px-4">
              <Thermometer className="w-8 h-8 text-bambu-gray mx-auto mb-2" />
              <p className="text-sm text-bambu-gray">{t('settings.zigbee.sensors.empty')}</p>
            </div>
          ) : (
            <div className="space-y-1">
              {sensors.map((sensor) => (
                <SensorRow key={sensor.id} sensor={sensor} onChart={() => setCharting(sensor)} />
              ))}
            </div>
          )}
        </div>
      </div>

      {charting && <SensorHistoryModal isOpen onClose={() => setCharting(null)} sensor={charting} />}
    </>
  );
}

function SensorRow({ sensor, onChart }: { sensor: ZigbeeSensor; onChart: () => void }) {
  const { t } = useTranslation();
  const readings = roomReadings(sensor);

  return (
    <div className="flex items-center justify-between gap-2 py-2 px-3 hover:bg-bambu-dark-tertiary rounded-lg transition-colors">
      <div className="min-w-0">
        <p className="text-sm text-white font-medium truncate">{sensor.name}</p>
        {/* Where it belongs — a place or the machine it is taped to. The two
            are exclusive, so at most one of them is ever a string. */}
        <p className="text-xs text-bambu-gray truncate">{sensor.printer_name ?? sensor.location?.path ?? ''}</p>

        {!sensor.present ? (
          // Its measurements are empty BECAUSE it is absent — the quantity list
          // comes from a live device's clusters. Numbers here would be invented.
          <p className="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1 mt-0.5">
            <WifiOff className="w-3 h-3" />
            {t('settings.zigbee.sensors.notOnNetwork')}
          </p>
        ) : (
          <div className={`flex items-center gap-2 mt-0.5 ${sensor.unreachable ? 'opacity-50' : ''}`}>
            {sensor.unreachable && <WifiOff className="w-3 h-3 text-bambu-gray" />}
            {readings.map(([key, reading]) => {
              const Icon = iconFor(key);
              return (
                <span
                  key={key}
                  className={`inline-flex items-center gap-1 text-xs ${
                    reading.stale ? 'text-bambu-gray' : 'text-white'
                  }`}
                  title={`${t(`settings.zigbee.measurement.${key}`, { defaultValue: key })}: ${formatRelativeTime(
                    reading.last_report_at,
                    'system',
                    t,
                  )}`}
                >
                  <Icon className="w-3 h-3 text-bambu-gray" />
                  {reading.value == null ? '—' : `${formatReading(reading.value)} ${reading.unit}`}
                </span>
              );
            })}
          </div>
        )}
      </div>

      {sensor.present && readings.length > 0 && (
        <button
          type="button"
          onClick={onChart}
          aria-label={t('sensorHistory.title')}
          className="p-1.5 rounded text-bambu-gray hover:text-white shrink-0"
        >
          <LineChart className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}
