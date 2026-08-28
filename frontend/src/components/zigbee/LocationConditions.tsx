import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import type { ZigbeeSensor } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { buildLocationIndex } from '../../utils/locationTree';
import { sensorsForGroup } from '../../utils/sensorReadings';
import { SensorChip } from './SensorChip';
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
          <SensorChip key={sensor.id} sensor={sensor} onOpen={() => setCharting(sensor)} />
        ))}
      </span>

      {charting && <SensorHistoryModal isOpen onClose={() => setCharting(null)} sensor={charting} />}
    </>
  );
}
