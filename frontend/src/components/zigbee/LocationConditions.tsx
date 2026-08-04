import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { LineChart, WifiOff } from 'lucide-react';

import { api } from '../../api/client';
import type { ZigbeeSensor } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { formatRelativeTime } from '../../utils/date';
import { buildLocationIndex } from '../../utils/locationTree';
import { roomReadings, sensorsForGroup } from '../../utils/sensorReadings';
import { iconFor } from './measurementIcons';
import { SensorHistoryModal } from './SensorHistoryModal';

interface Props {
  /** The group's location. Null for the "no location" group, which shows nothing. */
  locationId: number | null;
}

/**
 * What the sensors say about the place this group stands in.
 *
 * Fetches for itself rather than taking props: TanStack collapses every header
 * on the page into one request per key, and the three pages that render this
 * already ask for the locations. It also means a page takes one line to adopt.
 *
 * A group header must never break. Radio down, no sensors, failed query, no
 * permission — every one of them renders nothing, and the place sensors are
 * diagnosed is Settings, not eight red lines on the farm's main screen.
 */
export function LocationConditions({ locationId }: Props) {
  // No `useTranslation` here: every string in this component belongs to a Chip.
  const { hasPermission } = useAuth();
  const [charting, setCharting] = useState<ZigbeeSensor | null>(null);

  const mayRead = hasPermission('smart_sensors:read');
  const enabled = mayRead && locationId != null;

  const { data: sensorData } = useQuery({
    queryKey: ['zigbee-sensors'],
    queryFn: api.getZigbeeSensors,
    // The same cadence as the settings section and as plug status, and no
    // faster than a sensor speaks. Readings are polled, not pushed: the socket
    // carries pairing and radio events only. It does invalidate this very key
    // on those, so a downed radio corrects the header at once.
    refetchInterval: 30000,
    enabled,
  });
  const { data: locationData } = useQuery({
    queryKey: ['printer-locations'],
    queryFn: api.getPrinterLocations,
    enabled,
  });

  const index = useMemo(() => buildLocationIndex(locationData?.locations ?? []), [locationData]);
  const sensors = useMemo(
    () => sensorsForGroup(sensorData?.sensors ?? [], locationId, index),
    [sensorData, locationId, index],
  );

  if (!enabled || sensors.length === 0) return null;

  return (
    <>
      <span className="inline-flex items-center gap-2 flex-wrap">
        {sensors.map((sensor) => (
          <Chip key={sensor.id} sensor={sensor} onOpen={() => setCharting(sensor)} />
        ))}
      </span>

      {charting && <SensorHistoryModal isOpen onClose={() => setCharting(null)} sensor={charting} />}
    </>
  );
}

function Chip({ sensor, onOpen }: { sensor: ZigbeeSensor; onOpen: () => void }) {
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
            {reading.value == null ? '—' : `${reading.value} ${reading.unit}`}
          </span>
        );
      })}
      <LineChart className="w-3.5 h-3.5 text-bambu-gray" />
    </button>
  );
}
