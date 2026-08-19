import { useTranslation } from 'react-i18next';
import { LineChart, WifiOff } from 'lucide-react';

import type { ZigbeeSensor } from '../../api/client';
import { formatRelativeTime } from '../../utils/date';
import { formatReading, roomReadings } from '../../utils/sensorReadings';
import { iconFor } from './measurementIcons';

/**
 * One sensor as a single line of readings.
 *
 * Shared by the location group header and the printer card, because a sensor
 * bound to a place and one bound to a machine are the same sensor read the same
 * way -- only the question of WHERE it belongs differs, and that is answered
 * once, in the sensor's own settings.
 */
export function SensorChip({ sensor, onOpen }: { sensor: ZigbeeSensor; onOpen: () => void }) {
  const { t } = useTranslation();
  const readings = roomReadings(sensor);

  // The name and when each quantity last spoke. A header has no room for the
  // relative times inline, and with a single chip the sensor's own name would
  // otherwise be invisible.
  const title = [
    sensor.name,
    ...readings.map(
      ([key, reading]) =>
        `${t(`settings.zigbee.measurement.${key}`, { defaultValue: key })}: ${formatRelativeTime(
          reading.last_report_at,
          'system',
          t,
        )}`,
    ),
  ].join('\n');

  if (!sensor.present) {
    // No numbers to show: the backend derives the quantity list from a live
    // device's clusters, so there is nothing honest to put here. Not a button
    // either — there is no quantity list to open a chart on.
    return (
      <span
        className="inline-flex items-center gap-1 text-sm font-normal text-bambu-gray"
        title={t('settings.zigbee.sensors.notOnNetwork')}
      >
        <WifiOff className="w-3.5 h-3.5" />
        {sensor.name}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={onOpen}
      title={title}
      aria-label={t('locationConditions.openChart', { name: sensor.name })}
      className={`inline-flex items-center gap-2 px-2 py-0.5 rounded-lg text-sm font-normal bg-bambu-dark-secondary hover:bg-bambu-dark-tertiary ${
        sensor.unreachable ? 'opacity-50' : ''
      }`}
    >
      {sensor.unreachable && <WifiOff className="w-3.5 h-3.5 text-bambu-gray" />}
      {readings.map(([key, reading]) => {
        const Icon = iconFor(key);
        return (
          <span
            key={key}
            className={`inline-flex items-center gap-1 ${reading.stale ? 'text-bambu-gray' : 'text-white'}`}
          >
            <Icon className="w-3.5 h-3.5 text-bambu-gray" />
            {reading.value == null ? '—' : `${formatReading(reading.value)} ${reading.unit}`}
          </span>
        );
      })}
      <LineChart className="w-3.5 h-3.5 text-bambu-gray" />
    </button>
  );
}
